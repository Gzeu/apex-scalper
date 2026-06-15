"""Limit order manager v0.8.7 — Bug 32 fix.

Changelog:
  v0.8.7 — BUG 32 FIX: place_entry_order() returna un ID sintetic str
    (ex: 'Buy_65432.1_0.001') in loc de int -> close_trade_record(trade_id: int)
    facea trade_id > 0 -> TypeError sau fallback mereu -> duplicate insert in DB.
    Fix: place_entry_order() apeleaza db.record_open_trade() si returneaza
    int row_id (sau 0 la esec). close_trade_record() primeste int valid -> UPDATE corect.
  v0.8.1 — BUG 8+9 fix: place_entry_order() wrapper, round_price inline,
    fee_estimate, amend_order fara symbol, trader._client.
  v0.5.1 — _market_fallback SL/TP fix, tick_size cached.
"""
from __future__ import annotations

import asyncio
import time
from typing import Literal, Optional
from loguru import logger

from .config import config
from .state import state
from .trader import trader

FILL_TIMEOUT_S      = float(2)
POLL_INTERVAL_S     = 0.25
MAX_AMEND_ATTEMPTS  = 3
TICK_MOVE_THRESHOLD = 1


class LimitOrderManager:
    """PostOnly Limit entry with amend-on-move and Market fallback."""

    def __init__(self):
        pass

    def _get_tick_size(self) -> float:
        return getattr(trader, "_tick_size", 0.01) or 0.01

    def _round_price(self, price: float) -> float:
        tick = self._get_tick_size()
        if tick <= 0:
            return price
        return round(round(price / tick) * tick, 8)

    async def place_entry(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        symbol: Optional[str] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> tuple[bool, float, float, str]:
        """Place PostOnly Limit with native SL/TP and amend-on-move.

        Returns (success, filled_qty, avg_price, order_id).
        """
        sym      = symbol or config.symbol
        deadline = time.monotonic() + FILL_TIMEOUT_S
        amend_count = 0
        order_id    = ""

        with state.lock:
            best_bid = state.orderbook.best_bid
            best_ask = state.orderbook.best_ask

        tick_size = self._get_tick_size()

        if best_bid is None or best_ask is None:
            logger.warning("[LOM] OB not ready — Market fallback")
            return await self._market_fallback(side, qty, sym, stop_loss, take_profit)

        limit_price = best_bid if side == "Buy" else best_ask
        limit_price = self._round_price(limit_price)

        resp = await trader.place_order(
            side=side, qty=qty,
            order_type="Limit", post_only=True,
            price=limit_price,
            stop_loss=stop_loss, take_profit=take_profit,
        )
        if not resp or resp.get("retCode") != 0:
            logger.warning("[LOM] Initial Limit rejected — Market fallback")
            return await self._market_fallback(side, qty, sym, stop_loss, take_profit)

        order_id = resp.get("result", {}).get("orderId", "")
        logger.info(
            f"[LOM] Limit placed: {side} {qty} {sym} @ {limit_price} "
            f"order_id={order_id[:8] if order_id else '?'}... "
            + (f"SL={stop_loss} " if stop_loss else "")
            + (f"TP={take_profit}" if take_profit else "")
        )

        loop = asyncio.get_running_loop()
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL_S)

            try:
                r = await loop.run_in_executor(
                    None,
                    lambda: trader._client.get_order_history(
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
                        fee_data  = trader.fee_estimate(filled, avg_price, "Limit")
                        fee_saved = filled * avg_price * (0.00055 - 0.00020)
                        logger.info(
                            f"[LOM] \u2705 Limit fill: {side} {filled}/{qty} @ {avg_price:.4f} "
                            f"fee={fee_data:.6f} USDT saved={fee_saved:.4f} USDT vs Market"
                        )
                        return True, filled, avg_price, order_id

                    if status in ("Cancelled", "Rejected", "Deactivated"):
                        break
            except Exception as e:
                logger.warning(f"[LOM] Poll error: {e}")

            with state.lock:
                current_bid = state.orderbook.best_bid
                current_ask = state.orderbook.best_ask

            new_price = current_bid if side == "Buy" else current_ask
            if new_price and tick_size > 0:
                ticks_moved = abs(new_price - limit_price) / tick_size
                if ticks_moved >= TICK_MOVE_THRESHOLD and amend_count < MAX_AMEND_ATTEMPTS:
                    new_limit = self._round_price(new_price)
                    amend_resp = await trader.amend_order(
                        order_id=order_id, price=new_limit
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

        if order_id:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: trader._client.cancel_order(
                        category="linear", symbol=sym, orderId=order_id,
                    ),
                )
            except Exception:
                pass

        logger.warning(
            f"[LOM] Not filled in {FILL_TIMEOUT_S}s ({amend_count} amends) — Market fallback"
        )
        return await self._market_fallback(side, qty, sym, stop_loss, take_profit)

    async def _market_fallback(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        symbol: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> tuple[bool, float, float, str]:
        """Market fallback cu SL/TP intotdeauna atasat."""
        with state.lock:
            price = state.last_price
        fee = trader.fee_estimate(qty, price, "Market")
        logger.warning(
            f"[LOM] Market fallback: {side} {qty} {symbol} "
            f"taker_fee={fee:.6f} USDT (0.055%)"
            + (f" SL={stop_loss}" if stop_loss else "")
            + (f" TP={take_profit}" if take_profit else "")
        )
        resp = await trader.place_order(
            side=side, qty=qty,
            order_type="Market", post_only=False,
            stop_loss=stop_loss, take_profit=take_profit,
        )
        order_id = resp.get("result", {}).get("orderId", "") if resp else ""
        if resp and resp.get("retCode") == 0:
            return True, qty, price, order_id
        return False, 0.0, 0.0, ""


lom = LimitOrderManager()


async def place_entry_order(
    side: Literal["Buy", "Sell"],
    qty: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
) -> int:
    """Wrapper pentru lom.place_entry() — returneaza int row_id pentru DB.

    v0.8.7 BUG 32 FIX: inainte returna str sintetic (ex: 'Buy_65432_0.001')
      -> close_trade_record(trade_id: int) facea trade_id > 0 -> TypeError
      -> fallback record_trade() apelat mereu -> duplicate insert in DB.
    Acum: apeleaza db.record_open_trade() dupa fill confirmat si returneaza
      int row_id. close_trade_record() primeste int valid -> UPDATE corect.
    Returneaza 0 daca fill a esuat (strategy verifica if trade_id).
    """
    from .persistence import db

    success, filled_qty, avg_price, order_id = await lom.place_entry(
        side=side,
        qty=qty,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    if success and filled_qty > 0:
        row_id = db.record_open_trade(
            symbol=config.symbol,
            side="long" if side == "Buy" else "short",
            entry=avg_price,
            qty=filled_qty,
        )
        return row_id  # int > 0
    return 0
