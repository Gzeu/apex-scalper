"""Limit order manager v0.4.0 — PostOnly entry with Market fallback.

Strategy:
- Place PostOnly Limit at best_bid (for LONG) or best_ask (for SHORT).
  This earns maker rebate (0% fee on Bybit) instead of paying taker (0.055%).
- If not filled within FILL_TIMEOUT_S seconds, cancel + fallback to Market.
- On partial fill, cancel remainder and proceed with filled qty.
- Exposes place_entry() which returns (filled: bool, qty: float, avg_price: float).
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Literal, Optional
from loguru import logger

from .config import config
from .state import state
from .trader import trader

FILL_TIMEOUT_S  = float(10)   # seconds to wait for limit fill before fallback
PRICE_OFFSET    = 0           # ticks to offset from best bid/ask (0 = at touch)


class LimitOrderManager:
    """PostOnly limit entry with Market fallback."""

    async def place_entry(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        symbol: Optional[str] = None,
    ) -> tuple[bool, float, float]:
        """Place PostOnly Limit. Returns (success, filled_qty, avg_price)."""
        sym = symbol or config.symbol

        # Get limit price from live orderbook
        with state.lock:
            best_bid = state.orderbook.best_bid
            best_ask = state.orderbook.best_ask

        if best_bid is None or best_ask is None:
            logger.warning("OB not ready for limit entry — falling back to Market")
            return await self._market_fallback(side, qty, sym)

        # Buy at best_bid (join the bid = maker), Sell at best_ask (join ask = maker)
        limit_price = best_bid if side == "Buy" else best_ask
        order_link_id = str(uuid.uuid4())

        logger.info(
            f"Limit entry: {side} {qty} {sym} @ {limit_price} (PostOnly) "
            f"link={order_link_id[:8]}"
        )

        resp = await trader.place_order(
            side=side,
            qty=qty,
            order_type="Limit",
            post_only=True,
            price=limit_price,
            symbol=sym,
        )

        if not resp or resp.get("retCode") != 0:
            logger.warning("Limit order rejected — falling back to Market")
            return await self._market_fallback(side, qty, sym)

        order_id = resp.get("result", {}).get("orderId", "")

        # Poll for fill
        filled_qty, avg_price = await self._wait_for_fill(
            order_id, sym, qty, FILL_TIMEOUT_S
        )

        if filled_qty > 0:
            logger.info(
                f"Limit fill OK: {side} {filled_qty}/{qty} @ {avg_price:.4f} (maker)"
            )
            return True, filled_qty, avg_price

        # Timed out — cancel + fallback
        logger.warning(
            f"Limit not filled in {FILL_TIMEOUT_S}s — cancelling + Market fallback"
        )
        await self._cancel_order(order_id, sym)
        return await self._market_fallback(side, qty, sym)

    async def _wait_for_fill(
        self,
        order_id: str,
        symbol: str,
        expected_qty: float,
        timeout: float,
    ) -> tuple[float, float]:
        """Poll order status until filled or timeout. Returns (filled_qty, avg_price)."""
        poll_interval = 0.5
        elapsed = 0.0
        loop = asyncio.get_running_loop()

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda: trader._session.get_order_history(
                        category="linear",
                        symbol=symbol,
                        orderId=order_id,
                    ),
                )
                items = resp.get("result", {}).get("list", [])
                if not items:
                    continue
                order = items[0]
                status    = order.get("orderStatus", "")
                filled    = float(order.get("cumExecQty", 0))
                avg_price = float(order.get("avgPrice", 0))

                if status in ("Filled", "PartiallyFilled") and filled > 0:
                    return filled, avg_price
                if status in ("Cancelled", "Rejected", "Deactivated"):
                    return 0.0, 0.0
            except Exception as e:
                logger.warning(f"Order poll error: {e}")

        return 0.0, 0.0

    async def _cancel_order(self, order_id: str, symbol: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: trader._session.cancel_order(
                    category="linear",
                    symbol=symbol,
                    orderId=order_id,
                ),
            )
        except Exception as e:
            logger.warning(f"Cancel order error: {e}")

    async def _market_fallback(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        symbol: str,
    ) -> tuple[bool, float, float]:
        """Place market order as fallback. Returns (success, qty, last_price)."""
        with state.lock:
            price = state.last_price
        resp = await trader.place_order(side=side, qty=qty, order_type="Market", symbol=symbol)
        if resp.get("retCode") == 0:
            logger.info(f"Market fallback OK: {side} {qty} {symbol} ~ {price}")
            return True, qty, price
        return False, 0.0, 0.0


lom = LimitOrderManager()
