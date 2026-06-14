"""Limit order manager v0.4.1 — PostOnly entry with cancel-replace + Market fallback.

Changes vs v0.4.0:
- FILL_TIMEOUT_S reduced from 10s to 2s (scalping: 10s = too slow)
- Cancel-replace logic: if price moves > 1 tick, cancel + repost at new price
- Max MAX_REPLACE_ATTEMPTS cancel-replace cycles before Market fallback
- Poll interval tightened to 0.25s for faster fill detection

Strategy:
1. Place PostOnly Limit at best_bid (LONG) or best_ask (SHORT)
2. Poll every 0.25s for fill
3. If price moves > 1 tick away from our limit price: cancel + replace
4. After MAX_REPLACE_ATTEMPTS or FILL_TIMEOUT_S total elapsed: Market fallback
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Literal, Optional
from loguru import logger

from .config import config
from .state import state
from .trader import trader

FILL_TIMEOUT_S      = float(2)    # total wall-clock time before Market fallback
POLL_INTERVAL_S     = 0.25        # how often to check fill status
MAX_REPLACE_ATTEMPTS = 3          # cancel-replace cycles before giving up
TICK_MOVE_THRESHOLD = 1           # price must move >= N ticks to trigger replace


class LimitOrderManager:
    """PostOnly limit entry with cancel-replace and Market fallback."""

    async def place_entry(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        symbol: Optional[str] = None,
    ) -> tuple[bool, float, float]:
        """Place PostOnly Limit with cancel-replace. Returns (success, filled_qty, avg_price)."""
        sym = symbol or config.symbol
        deadline = time.monotonic() + FILL_TIMEOUT_S
        attempt  = 0

        while attempt <= MAX_REPLACE_ATTEMPTS and time.monotonic() < deadline:
            attempt += 1

            with state.lock:
                best_bid = state.orderbook.best_bid
                best_ask = state.orderbook.best_ask
                tick_size = getattr(state.orderbook, "tick_size", None) or self._estimate_tick(best_bid, best_ask)

            if best_bid is None or best_ask is None:
                logger.warning("OB not ready — Market fallback")
                return await self._market_fallback(side, qty, sym)

            limit_price = best_bid if side == "Buy" else best_ask

            logger.info(
                f"[LOM] attempt={attempt}/{MAX_REPLACE_ATTEMPTS+1} "
                f"{side} {qty} {sym} @ {limit_price} (PostOnly)"
            )

            resp = await trader.place_order(
                side=side, qty=qty,
                order_type="Limit", post_only=True,
                price=limit_price, symbol=sym,
            )

            if not resp or resp.get("retCode") != 0:
                logger.warning(f"Limit order rejected (attempt {attempt}) — Market fallback")
                return await self._market_fallback(side, qty, sym)

            order_id = resp.get("result", {}).get("orderId", "")
            remaining_timeout = deadline - time.monotonic()

            # Poll with cancel-replace awareness
            filled_qty, avg_price, cancelled = await self._poll_with_replace(
                order_id=order_id,
                sym=sym,
                qty=qty,
                limit_price=limit_price,
                tick_size=tick_size,
                side=side,
                timeout=min(remaining_timeout, FILL_TIMEOUT_S / (MAX_REPLACE_ATTEMPTS + 1)),
            )

            if filled_qty > 0:
                logger.info(
                    f"[LOM] ✅ Filled: {side} {filled_qty}/{qty} @ {avg_price:.4f} "
                    f"attempt={attempt} (maker)"
                )
                return True, filled_qty, avg_price

            if not cancelled:
                # Timed out, not cancelled by us — cancel manually
                await self._cancel_order(order_id, sym)

            if time.monotonic() >= deadline:
                break

        logger.warning(
            f"[LOM] No fill after {attempt} attempts / {FILL_TIMEOUT_S}s — Market fallback"
        )
        return await self._market_fallback(side, qty, sym)

    async def _poll_with_replace(
        self,
        order_id: str,
        sym: str,
        qty: float,
        limit_price: float,
        tick_size: float,
        side: str,
        timeout: float,
    ) -> tuple[float, float, bool]:
        """Poll for fill. If price moves > TICK_MOVE_THRESHOLD ticks, cancel.
        Returns (filled_qty, avg_price, was_cancelled_for_replace).
        """
        loop = asyncio.get_running_loop()
        start = time.monotonic()
        cancelled_for_replace = False

        while time.monotonic() - start < timeout:
            await asyncio.sleep(POLL_INTERVAL_S)

            # Check if price has moved away (cancel-replace trigger)
            with state.lock:
                current_bid = state.orderbook.best_bid
                current_ask = state.orderbook.best_ask

            new_price = current_bid if side == "Buy" else current_ask
            if new_price and tick_size > 0:
                ticks_moved = abs(new_price - limit_price) / tick_size
                if ticks_moved >= TICK_MOVE_THRESHOLD:
                    logger.debug(
                        f"[LOM] Price moved {ticks_moved:.1f} ticks — cancel-replace "
                        f"(was @ {limit_price}, now @ {new_price})"
                    )
                    await self._cancel_order(order_id, sym)
                    cancelled_for_replace = True
                    return 0.0, 0.0, True

            # Poll fill status
            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda: trader._session.get_order_history(
                        category="linear", symbol=sym, orderId=order_id,
                    ),
                )
                items = resp.get("result", {}).get("list", [])
                if not items:
                    continue
                order     = items[0]
                status    = order.get("orderStatus", "")
                filled    = float(order.get("cumExecQty", 0))
                avg_price = float(order.get("avgPrice", 0))

                if status in ("Filled", "PartiallyFilled") and filled > 0:
                    return filled, avg_price, False
                if status in ("Cancelled", "Rejected", "Deactivated"):
                    return 0.0, 0.0, False
            except Exception as e:
                logger.warning(f"[LOM] Poll error: {e}")

        return 0.0, 0.0, False

    @staticmethod
    def _estimate_tick(best_bid: Optional[float], best_ask: Optional[float]) -> float:
        """Estimate tick size from spread if not available in OB state."""
        if best_bid and best_ask and best_ask > best_bid:
            spread = best_ask - best_bid
            # Tick is typically 1/10 of the spread on most perp contracts
            return max(spread / 10, 0.0001)
        return 0.1  # safe fallback for BTC

    async def _cancel_order(self, order_id: str, symbol: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: trader._session.cancel_order(
                    category="linear", symbol=symbol, orderId=order_id,
                ),
            )
        except Exception as e:
            logger.warning(f"[LOM] Cancel error: {e}")

    async def _market_fallback(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        symbol: str,
    ) -> tuple[bool, float, float]:
        with state.lock:
            price = state.last_price
        resp = await trader.place_order(side=side, qty=qty, order_type="Market", symbol=symbol)
        if resp.get("retCode") == 0:
            logger.info(f"[LOM] Market fallback OK: {side} {qty} {symbol} ~ {price}")
            return True, qty, price
        return False, 0.0, 0.0


lom = LimitOrderManager()
