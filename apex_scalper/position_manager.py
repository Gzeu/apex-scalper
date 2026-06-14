"""Position manager v0.3.1: partial TP, pyramid, trailing stop.

Fixes vs v0.3.0:
- Module-level TP/SL vars now injected by main.inject_profile() per symbol.
  Previously they were frozen at import time from os.getenv() and ignored profiles.
- PnL double-counting fixed: update_pnl() called only once per exit event,
  routed through risk.update_pnl() (single source of truth for perf tracking).
- state clearing after full exit now done by trader.close_position() (v0.3.1),
  removed duplicate clearing here.
"""
from __future__ import annotations

import os
from loguru import logger
from .state import state
from .trader import trader
from .risk import risk

# All injected by main.inject_profile() on startup per symbol.
# ENV fallback for single-symbol / manual override mode.
TP1_PCT          = float(os.getenv("TP1_PCT",         "0.0010"))
TP2_PCT          = float(os.getenv("TP2_PCT",         "0.0020"))
SL_PCT           = float(os.getenv("SL_PCT",          "0.0008"))
TRAIL_PCT        = float(os.getenv("TRAIL_PCT",       "0.0"))
TRAIL_DELTA      = float(os.getenv("TRAIL_DELTA",     "0.0005"))
MAX_HOLD_CANDLES = int(os.getenv("MAX_HOLD_CANDLES",  "5"))
MAX_PYRAMID_ADDS = int(os.getenv("MAX_PYRAMID_ADDS",  "1"))


class PositionManager:
    def __init__(self):
        self._hold_count: int = 0
        self._tp1_done: bool = False
        self._pyramid_count: int = 0

    def reset(self):
        self._hold_count    = 0
        self._tp1_done      = False
        self._pyramid_count = 0

    async def on_open(self, side: str, qty: float, entry: float) -> None:
        with state.lock:
            state.open_position = side
            state.open_qty      = qty
            state.open_entry    = entry
            state.trailing_stop = 0.0
        self.reset()
        logger.info(f"Position opened: {side} qty={qty} entry={entry}")

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

        # --- Trailing stop update ---
        if TRAIL_PCT > 0 and pnl_pct >= TRAIL_PCT:
            new_trail = (
                price * (1 - TRAIL_DELTA) if pos == "long"
                else price * (1 + TRAIL_DELTA)
            )
            with state.lock:
                if pos == "long":
                    state.trailing_stop = max(state.trailing_stop, new_trail)
                else:
                    state.trailing_stop = (
                        min(state.trailing_stop, new_trail)
                        if state.trailing_stop > 0 else new_trail
                    )
            trail = state.trailing_stop

        # --- TP1: scale out 50% ---
        if not self._tp1_done and pnl_pct >= TP1_PCT:
            with state.lock:
                current_qty = state.open_qty
            half_qty = max(round(current_qty / 2, 3), 0.001)
            logger.info(f"TP1 hit ({pnl_pct:.4%}) — scaling out {half_qty}")
            await trader.place_order(
                "Sell" if pos == "long" else "Buy",
                half_qty,
            )
            with state.lock:
                state.open_qty = round(current_qty - half_qty, 3)
            self._tp1_done = True
            pnl_usdt = pnl_pct * half_qty * entry
            # FIX: single call to risk.update_pnl (was double-counting in v0.3.0)
            risk.update_pnl(pnl_usdt)
            await _notify(
                f"🟡 *TP1* scaled out `{half_qty}` | `pnl: +{pnl_usdt:.4f} USDT`"
            )
            return False

        # --- Full exit conditions ---
        sl_hit    = pnl_pct <= -SL_PCT
        tp2_hit   = pnl_pct >= TP2_PCT
        trail_hit = (
            trail > 0 and (
                (pos == "long"  and price <= trail) or
                (pos == "short" and price >= trail)
            )
        )
        time_exit = self._hold_count >= MAX_HOLD_CANDLES

        if sl_hit or tp2_hit or trail_hit or time_exit:
            reason = (
                "SL" if sl_hit else
                "TP2" if tp2_hit else
                "TRAIL" if trail_hit else "TIMEOUT"
            )
            with state.lock:
                remaining_qty = state.open_qty

            logger.info(f"EXIT {pos.upper()} | reason={reason} | pnl={pnl_pct:.4%}")
            # trader.close_position() clears state fields (v0.3.1 fix)
            await trader.close_position()

            pnl_usdt = pnl_pct * remaining_qty * entry
            risk.update_pnl(pnl_usdt)
            self.reset()

            emoji = "🟢" if pnl_usdt > 0 else "🔴"
            await _notify(
                f"{emoji} *{pos.upper()} CLOSED* | {reason}\n"
                f"`pnl: {pnl_usdt:+.4f} USDT ({pnl_pct:.3%})`\n"
                f"`price: {price}`"
            )
            return True

        return False

    async def try_pyramid(
        self,
        side: str,
        price: float,
        signal_strength: float,
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
        if pnl_pct >= 0.0005 and signal_strength >= 0.7:
            qty = risk.calc_qty(price)
            logger.info(f"PYRAMID add {side} qty={qty} (#{self._pyramid_count + 1})")
            await trader.place_order(
                "Buy" if side == "long" else "Sell", qty
            )
            with state.lock:
                state.open_qty = round(state.open_qty + qty, 3)
            self._pyramid_count += 1
            await _notify(
                f"🔼 *PYRAMID* add `{qty}` {side} | `strength={signal_strength:.2f}`"
            )


async def _notify(msg: str) -> None:
    try:
        from .telegram_ui import send_message
        await send_message(msg)
    except Exception:
        pass


position_manager = PositionManager()
