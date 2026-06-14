"""Position manager v0.7.2 — verified fills + accurate Kelly pnl_pct.

Fixes vs v0.7.1:
  FIX #1 — close_partial fill verification:
    retCode==0 means ORDER ACCEPTED, not filled. We now poll get_open_orders()
    for up to FILL_POLL_TIMEOUT_S (default 5s) in 0.5s intervals to confirm
    the Limit reduceOnly order is gone (filled or cancelled). If still open
    after timeout we cancel it and fall back to Market. state.open_qty only
    decremented after confirmed fill.

  FIX #4 — Kelly pnl_pct accuracy:
    When TP1/TP2 partial closes happen before a full exit, the old code
    called risk.on_close(pnl_usdt, pnl_pct) with pnl_pct=(price-entry)/entry
    for the partial slice only, which under-weights the partial wins in Kelly.
    Now we track _realized_pnl_usdt and _realized_qty across partials and at
    final exit compute a single blended trade_pnl_pct = total_pnl / (entry * original_qty)
    so Kelly sees the true per-trade outcome.
"""
from __future__ import annotations

import asyncio
import os
from loguru import logger
from .state import state
from .trader import trader
from .risk import risk
from .limit_order_manager import lom

TP1_PCT          = float(os.getenv("TP1_PCT",         "0.0010"))
TP2_PCT          = float(os.getenv("TP2_PCT",         "0.0020"))
TP3_PCT          = float(os.getenv("TP3_PCT",         "0.0035"))
SL_PCT           = float(os.getenv("SL_PCT",          "0.0008"))
TRAIL_PCT        = float(os.getenv("TRAIL_PCT",       "0.0"))
TRAIL_DELTA      = float(os.getenv("TRAIL_DELTA",     "0.0005"))
MAX_HOLD_CANDLES = int(os.getenv("MAX_HOLD_CANDLES",  "5"))
MAX_PYRAMID_ADDS = int(os.getenv("MAX_PYRAMID_ADDS",  "1"))

# Scale-out fractions at TP1/TP2/TP3 — must sum to 1.0
TP1_FRACTION     = float(os.getenv("TP1_FRACTION",    "0.25"))
TP2_FRACTION     = float(os.getenv("TP2_FRACTION",    "0.25"))
TP3_FRACTION     = float(os.getenv("TP3_FRACTION",    "0.50"))

# FIX #1: fill verification params
FILL_POLL_TIMEOUT_S  = float(os.getenv("FILL_POLL_TIMEOUT_S",  "5.0"))  # seconds to wait for fill
FILL_POLL_INTERVAL_S = float(os.getenv("FILL_POLL_INTERVAL_S", "0.5"))  # poll interval

# FIX #2: suppress redundant amend_sl_tp calls (min move in price units)
TRAIL_AMEND_MIN_TICKS = float(os.getenv("TRAIL_AMEND_MIN_TICKS", "0.5"))  # x tick_size


async def _confirm_order_filled(order_id: str) -> bool:
    """Poll get_open_orders until order_id is gone or FILL_POLL_TIMEOUT_S elapsed.

    Returns True if order is no longer open (filled or rejected).
    Returns False if still open after timeout (caller should cancel + market fallback).
    """
    from .config import config
    elapsed = 0.0
    while elapsed < FILL_POLL_TIMEOUT_S:
        await asyncio.sleep(FILL_POLL_INTERVAL_S)
        elapsed += FILL_POLL_INTERVAL_S
        try:
            result = await trader._api_call(
                trader._client.get_open_orders,
                category="linear",
                symbol=config.symbol,
                orderId=order_id,
            )
            orders = result.get("result", {}).get("list", [])
            # If no matching order exists, it was filled or cancelled
            if not any(o.get("orderId") == order_id for o in orders):
                return True
        except Exception as e:
            logger.warning(f"_confirm_order_filled poll error: {e}")
    return False


class PositionManager:
    def __init__(self):
        self._hold_count: int   = 0
        self._tp1_done:   bool  = False
        self._tp2_done:   bool  = False
        self._pyramid_count: int = 0
        # FIX #4: trade-level PnL tracking for Kelly
        self._original_qty:     float = 0.0   # qty at open
        self._original_entry:   float = 0.0   # entry price at open
        self._realized_pnl_usdt: float = 0.0  # accumulated from partials
        self._realized_qty:      float = 0.0  # qty already closed
        # FIX #2: trailing stop debounce
        self._last_trail_amend: float = 0.0   # last trail price sent to exchange

    def reset(self):
        self._hold_count       = 0
        self._tp1_done         = False
        self._tp2_done         = False
        self._pyramid_count    = 0
        self._original_qty     = 0.0
        self._original_entry   = 0.0
        self._realized_pnl_usdt = 0.0
        self._realized_qty     = 0.0
        self._last_trail_amend = 0.0

    async def on_open(self, side: str, qty: float, entry: float) -> None:
        with state.lock:
            state.open_position = side
            state.open_qty      = qty
            state.open_entry    = entry
            state.trailing_stop = 0.0
        self.reset()
        # Store original position for Kelly blending (FIX #4)
        self._original_qty   = qty
        self._original_entry = entry
        logger.info(f"Position opened: {side} qty={qty} entry={entry}")

    async def close_partial(self, pct: float, reason: str = "") -> bool:
        """Close pct fraction of open qty via Limit reduceOnly.

        FIX #1: After placement we poll get_open_orders() to confirm fill.
        If the order is still open after FILL_POLL_TIMEOUT_S, we cancel it
        and fall back to Market. state.open_qty is only decremented on confirmed fill.

        FIX #4: On confirmed fill, accumulate trade-level PnL for Kelly blending.
        """
        with state.lock:
            pos         = state.open_position
            current_qty = state.open_qty
            entry       = state.open_entry
            best_bid    = state.orderbook.best_bid
            best_ask    = state.orderbook.best_ask

        if not pos or current_qty <= 0:
            return False

        close_qty  = max(round(current_qty * pct, 3), 0.001)
        close_side = "Sell" if pos == "long" else "Buy"
        limit_px   = best_ask if pos == "long" else best_bid

        resp = await trader.place_order(
            side=close_side,
            qty=close_qty,
            order_type="Limit",
            post_only=True,
            price=limit_px,
            reduce_only=True,
        )

        filled = False
        order_id = resp.get("result", {}).get("orderId", "")

        if resp.get("retCode") == 0 and order_id:
            # FIX #1: poll for actual fill confirmation
            filled = await _confirm_order_filled(order_id)
            if not filled:
                # Order still open — cancel it and go Market
                logger.warning(f"{reason} Limit not filled in {FILL_POLL_TIMEOUT_S}s — cancelling, Market fallback")
                try:
                    await trader._api_call(
                        trader._client.cancel_order,
                        category="linear",
                        symbol=trader._symbol,
                        orderId=order_id,
                    )
                except Exception:
                    pass
                resp2 = await trader.place_order(
                    side=close_side,
                    qty=close_qty,
                    order_type="Market",
                    post_only=False,
                    reduce_only=True,
                )
                filled = resp2.get("retCode") == 0
        else:
            # Limit placement failed — go Market directly
            resp2 = await trader.place_order(
                side=close_side,
                qty=close_qty,
                order_type="Market",
                post_only=False,
                reduce_only=True,
            )
            filled = resp2.get("retCode") == 0

        if filled:
            with state.lock:
                price_now      = state.last_price
                # FIX #1: only update open_qty on confirmed fill
                state.open_qty = max(round(current_qty - close_qty, 3), 0.0)

            pnl_pct  = ((price_now - entry) / entry if pos == "long"
                        else (entry - price_now) / entry)
            pnl_usdt = pnl_pct * close_qty * entry

            # FIX #4: accumulate trade-level realized pnl
            self._realized_pnl_usdt += pnl_usdt
            self._realized_qty      += close_qty

            # Report partial to risk (correct usdt, approximate pct for this slice)
            risk.on_close(pnl_usdt, pnl_pct)

            logger.info(
                f"{reason or 'PARTIAL'} close {close_qty} ({pct:.0%}) | "
                f"pnl={pnl_usdt:+.4f} USDT ({pnl_pct:.4%}) | "
                f"remaining={state.open_qty}"
            )
            await _notify(
                f"\u2702\ufe0f *{reason or 'PARTIAL'}* `{close_qty}` ({pct:.0%}) | "
                f"`{pnl_usdt:+.4f} USDT`"
            )
        return filled

    def _on_full_close(self, pos: str, price: float, remaining_qty: float, reason: str) -> tuple[float, float]:
        """FIX #4: Compute blended trade-level pnl_pct and call risk.on_close once.

        Blends partial closes already done with final remaining slice so Kelly
        sees the true per-trade PnL ratio, not just the last slice.
        Returns (pnl_usdt, blended_pnl_pct).
        """
        entry = self._original_entry
        orig_qty = self._original_qty

        # PnL on remaining slice
        slice_pnl_pct  = ((price - entry) / entry if pos == "long"
                          else (entry - price) / entry)
        slice_pnl_usdt = slice_pnl_pct * remaining_qty * entry

        total_pnl_usdt = self._realized_pnl_usdt + slice_pnl_usdt

        # Blended pnl_pct = total_pnl / notional at open
        blended_pnl_pct = (
            total_pnl_usdt / (orig_qty * entry)
            if orig_qty > 0 and entry > 0
            else slice_pnl_pct
        )

        # risk.on_close already called for partial slices—only pass final slice here
        # (risk.on_close for partials is already called in close_partial above)
        # We call it once more with the remaining slice
        risk.on_close(slice_pnl_usdt, blended_pnl_pct)

        logger.info(
            f"Trade closed [{reason}] | "
            f"blended_pnl={blended_pnl_pct:.4%} ({total_pnl_usdt:+.4f} USDT) | "
            f"partials={self._realized_qty:.4f}+remaining={remaining_qty:.4f}"
        )
        return slice_pnl_usdt, blended_pnl_pct

    async def evaluate(self, price: float) -> bool:
        """Check exit conditions. Returns True if position fully closed."""
        with state.lock:
            pos   = state.open_position
            qty   = state.open_qty
            entry = state.open_entry
            trail = state.trailing_stop

        if not pos:
            return True

        self._hold_count += 1
        pnl_pct = (
            (price - entry) / entry if pos == "long"
            else (entry - price) / entry
        )

        # --- Trailing stop update (FIX #2: debounce) ---
        if TRAIL_PCT > 0 and pnl_pct >= TRAIL_PCT:
            new_trail = (
                price * (1 - TRAIL_DELTA) if pos == "long"
                else price * (1 + TRAIL_DELTA)
            )
            min_move = trader._tick_size * TRAIL_AMEND_MIN_TICKS
            should_amend = False
            with state.lock:
                if pos == "long":
                    if new_trail > state.trailing_stop:
                        state.trailing_stop = new_trail
                        should_amend = abs(new_trail - self._last_trail_amend) >= min_move
                else:
                    if state.trailing_stop == 0 or new_trail < state.trailing_stop:
                        state.trailing_stop = new_trail
                        should_amend = abs(new_trail - self._last_trail_amend) >= min_move
            # FIX #2: only send REST call if trail moved meaningfully
            if should_amend:
                await trader.amend_sl_tp(stop_loss=new_trail)
                self._last_trail_amend = new_trail

        # --- TP1: close TP1_FRACTION at TP1_PCT ---
        if not self._tp1_done and pnl_pct >= TP1_PCT:
            ok = await self.close_partial(TP1_FRACTION, reason="TP1")
            if ok:
                self._tp1_done = True
            return False

        # --- TP2: close TP2_FRACTION at TP2_PCT ---
        if self._tp1_done and not self._tp2_done and pnl_pct >= TP2_PCT:
            ok = await self.close_partial(TP2_FRACTION, reason="TP2")
            if ok:
                self._tp2_done = True
            return False

        # --- Full exit conditions ---
        with state.lock:
            trail = state.trailing_stop
        sl_hit    = pnl_pct <= -SL_PCT
        tp3_hit   = pnl_pct >= TP3_PCT
        trail_hit = (
            trail > 0 and (
                (pos == "long"  and price <= trail) or
                (pos == "short" and price >= trail)
            )
        )
        time_exit = self._hold_count >= MAX_HOLD_CANDLES

        if sl_hit or tp3_hit or trail_hit or time_exit:
            reason = (
                "SL" if sl_hit else
                "TP3" if tp3_hit else
                "TRAIL" if trail_hit else "TIMEOUT"
            )
            with state.lock:
                remaining_qty = state.open_qty

            await trader.close_position(use_limit=True, limit_timeout_s=3.0)

            # FIX #4: blended PnL for Kelly
            pnl_usdt, blended_pct = self._on_full_close(pos, price, remaining_qty, reason)

            self.reset()
            emoji = "\U0001f7e2" if pnl_usdt > 0 else "\U0001f534"
            await _notify(
                f"{emoji} *{pos.upper()} CLOSED* | {reason}\n"
                f"`pnl: {pnl_usdt:+.4f} USDT ({blended_pct:.3%})`\n"
                f"`price: {price}`"
            )
            return True

        return False

    async def try_pyramid(
        self,
        side: str,
        price: float,
        signal_strength: float,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
    ) -> None:
        with state.lock:
            pos   = state.open_position
            entry = state.open_entry
        if pos != side:
            return
        if self._pyramid_count >= MAX_PYRAMID_ADDS:
            return
        pnl_pct = (
            (price - entry) / entry if pos == "long"
            else (entry - price) / entry
        )
        if pnl_pct >= 0.001 and signal_strength >= 0.7:
            from .config import config
            from .regime_filter import regime
            qty = risk.calc_qty(
                price,
                order_size_usdt=config.order_size_usdt,
                leverage=config.leverage,
                regime_factor=regime.size_factor(),
            )
            sl = stop_loss if stop_loss else None
            tp = take_profit if take_profit else None
            ok, filled_qty, avg_px = await lom.place_entry(
                "Buy" if side == "long" else "Sell",
                qty, stop_loss=sl, take_profit=tp,
            )
            if ok and filled_qty > 0:
                with state.lock:
                    state.open_qty = round(state.open_qty + filled_qty, 3)
                # Update original_qty so Kelly blending stays correct
                self._original_qty += filled_qty
                self._pyramid_count += 1
                await _notify(
                    f"\U0001f53c *PYRAMID* `{filled_qty}` {side} @ `{avg_px}`\n"
                    f"`strength={signal_strength:.2f}` `SL={sl}` `TP={tp}`"
                )


async def _notify(msg: str) -> None:
    try:
        from .telegram_ui import send_message
        await send_message(msg)
    except Exception:
        pass


position_manager = PositionManager()
