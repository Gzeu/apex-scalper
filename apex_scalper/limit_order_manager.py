"""Limit order manager v0.5.0 — amend instead of cancel+replace.

Changes vs v0.4.1:
- Uses trader.amend_order() (/v5/order/amend) instead of cancel+repost.
  Amend: single REST call, preserves queue position, no re-queuing delay.
  Cancel+repost: 2 REST calls, lose queue position, risk of gap fill.
- FILL_TIMEOUT_S = 2s (unchanged, correct for 1m scalping)
- POLL_INTERVAL_S = 0.25s (unchanged)
- MAX_AMEND_ATTEMPTS = 3 before Market fallback
- Logs exact fee saved vs Market on every successful Limit fill
- Native SL/TP attached on entry order (stopLoss/takeProfit params)
  so exchange enforces stops even if WS disconnects

Fee impact on mainnet:
  Limit (PostOnly) fill: 0.020% maker fee
  Market fallback:       0.055% taker fee
  -> Savings per avoided market order: 0.035% of notional
  On $200 notional per trade x 10 trades/day = $0.14/day = $51/year
"""
from __future__ import annotations

import asyncio
import time
from typing import Literal, Optional
from loguru import logger

from .config import config
from .state import state
from .trader import trader

FILL_TIMEOUT_S    = float(2)     # total wall-clock timeout before Market fallback
POLL_INTERVAL_S   = 0.25         # fill poll interval
MAX_AMEND_ATTEMPTS = 3           # amend-price cycles before Market fallback
TICK_MOVE_THRESHOLD = 1          # ticks price must move to trigger amend


class LimitOrderManager:
    """PostOnly Limit entry with amend-on-move and Market fallback."""

    async def place_entry(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        symbol: Optional[str] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> tuple[bool, float, float]:
        """Place PostOnly Limit with native SL/TP and amend-on-move.

        Returns (success, filled_qty, avg_price).
        """
        sym      = symbol or config.symbol
        deadline = time.monotonic() + FILL_TIMEOUT_S
        amend_count = 0
        order_id    = None

        with state.lock:
            best_bid   = state.orderbook.best_bid
            best_ask   = state.orderbook.best_ask
        info      = await trader.get_instrument_info(sym)
        tick_size = info.get("tickSize", 0.01)

        if best_bid is None or best_ask is None:
            logger.warning("[LOM] OB not ready — Market fallback")
            return await self._market_fallback(side, qty, sym)

        limit_price = best_bid if side == "Buy" else best_ask
        limit_price = trader.round_price(limit_price, sym)

        # Place initial PostOnly Limit with native SL/TP
        resp = await trader.place_order(
            side=side, qty=qty,
            order_type="Limit", post_only=True,
            price=limit_price, symbol=sym,
            stop_loss=stop_loss, take_profit=take_profit,
        )
        if not resp or resp.get("retCode") != 0:
            logger.warning("[LOM] Initial Limit rejected — Market fallback")
            return await self._market_fallback(side, qty, sym)

        order_id = resp.get("result", {}).get("orderId", "")
        logger.info(
            f"[LOM] Limit placed: {side} {qty} {sym} @ {limit_price} "
            f"order_id={order_id[:8]}... "
            + (f"SL={stop_loss} " if stop_loss else "")
            + (f"TP={take_profit}" if take_profit else "")
        )

        # Poll loop: fill check + amend on price move
        loop = asyncio.get_running_loop()
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL_S)

            # Check for fill
            try:
                r = await loop.run_in_executor(
                    None,
                    lambda: trader._session.get_order_history(
                        category="linear", symbol=sym, orderId=order_id,
                    ),
                )
                items = r.get("result", {}).get("list", [])
                if items:
                    order     = items[0]
                    status    = order.get("orderStatus", "")
                    filled    = float(order.get("cumExecQty", 0))
                    avg_price = float(order.get("avgPrice", 0))

                    if status in ("Filled", "PartiallyFilled") and filled > 0:
                        notional  = filled * avg_price
                        fee_saved = notional * (0.00055 - 0.00020)
                        logger.info(
                            f"[LOM] ✅ Limit fill: {side} {filled}/{qty} @ {avg_price:.4f} "
                            f"fee=Maker(0.020%) saved={fee_saved:.4f} USDT vs Market"
                        )
                        return True, filled, avg_price

                    if status in ("Cancelled", "Rejected", "Deactivated"):
                        break
            except Exception as e:
                logger.warning(f"[LOM] Poll error: {e}")

            # Check if price moved — amend instead of cancel+repost
            with state.lock:
                current_bid = state.orderbook.best_bid
                current_ask = state.orderbook.best_ask

            new_price = current_bid if side == "Buy" else current_ask
            if new_price and tick_size > 0:
                ticks_moved = abs(new_price - limit_price) / tick_size
                if ticks_moved >= TICK_MOVE_THRESHOLD and amend_count < MAX_AMEND_ATTEMPTS:
                    new_limit = trader.round_price(new_price, sym)
                    amend_resp = await trader.amend_order(
                        order_id=order_id, symbol=sym, price=new_limit
                    )
                    if amend_resp.get("retCode") == 0:
                        logger.debug(
                            f"[LOM] Amended: {limit_price} → {new_limit} "
                            f"(moved {ticks_moved:.1f} ticks) "
                            f"[amend {amend_count+1}/{MAX_AMEND_ATTEMPTS}]"
                        )
                        limit_price = new_limit
                        amend_count += 1
                    else:
                        logger.debug("[LOM] Amend failed — order may have filled")

        # Timeout — cancel unfilled order + Market fallback
        if order_id:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: trader._session.cancel_order(
                        category="linear", symbol=sym, orderId=order_id,
                    ),
                )
            except Exception:
                pass

        logger.warning(
            f"[LOM] Not filled in {FILL_TIMEOUT_S}s ({amend_count} amends) — Market fallback"
        )
        return await self._market_fallback(side, qty, sym)

    async def _market_fallback(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        symbol: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> tuple[bool, float, float]:
        """Market fallback. Logs fee cost."""
        with state.lock:
            price = state.last_price
        notional = qty * price if price > 0 else 0
        fee      = trader.fee_estimate(notional, "Market")
        logger.warning(
            f"[LOM] Market fallback: {side} {qty} {symbol} "
            f"taker_fee={fee['fee_usdt']:.4f} USDT (0.055%)"
        )
        resp = await trader.place_order(
            side=side, qty=qty,
            order_type="Market", post_only=False,
            symbol=symbol,
            stop_loss=stop_loss, take_profit=take_profit,
        )
        if resp.get("retCode") == 0:
            return True, qty, price
        return False, 0.0, 0.0


lom = LimitOrderManager()
