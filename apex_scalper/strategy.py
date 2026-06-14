"""Scalping strategy v0.2: EMA(9/21) cross + RSI(14) confirmation + trailing stop.

Entry:
  LONG  — ema_fast crosses above ema_slow AND rsi > RSI_LONG_MIN (default 52)
  SHORT — ema_fast crosses below ema_slow AND rsi < RSI_SHORT_MAX (default 48)

Exit:
  - Take profit: TP_PCT from entry (default 0.15%)
  - Stop loss:   SL_PCT from entry (default 0.10%)
  - Trailing stop: TRAIL_PCT activation + TRAIL_DELTA trail (default off)
  - Timeout:     MAX_HOLD_CANDLES (default 3)

All parameters readable from .env for live tuning.
"""
from __future__ import annotations

import os
from loguru import logger
from .state import state
from .risk import risk

# --- Configurable params (env override) ---
TP_PCT           = float(os.getenv("TP_PCT",           "0.0015"))
SL_PCT           = float(os.getenv("SL_PCT",           "0.0010"))
TRAIL_PCT        = float(os.getenv("TRAIL_PCT",        "0.0"))   # 0 = disabled
TRAIL_DELTA      = float(os.getenv("TRAIL_DELTA",      "0.0005"))
MAX_HOLD_CANDLES = int(os.getenv("MAX_HOLD_CANDLES",   "3"))
RSI_LONG_MIN     = float(os.getenv("RSI_LONG_MIN",     "52.0"))  # RSI must be above for LONG
RSI_SHORT_MAX    = float(os.getenv("RSI_SHORT_MAX",    "48.0"))  # RSI must be below for SHORT


async def _notify(msg: str) -> None:
    """Send Telegram message if configured (fire-and-forget)."""
    try:
        from .telegram_ui import send_message
        await send_message(msg)
    except Exception:
        pass


class Strategy:
    def __init__(self):
        self._prev_fast: float = 0.0
        self._prev_slow: float = 0.0
        self._hold_count: int = 0

    async def evaluate(self) -> None:
        """Called on every confirmed 1m candle close."""
        from .trader import trader

        # Read state atomically
        with state.lock:
            if not state.running or state.paused:
                return
            fast  = state.ema_fast
            slow  = state.ema_slow
            price = state.last_price
            rsi   = state.rsi_value
            rsi_ok = state.rsi_ready
            pos   = state.open_position
            entry = state.open_entry
            trail = state.trailing_stop

        # ── EXIT LOGIC ────────────────────────────────────────────────────────
        if pos:
            self._hold_count += 1
            pnl_pct = (
                (price - entry) / entry if pos == "long"
                else (entry - price) / entry
            )

            # Update trailing stop
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

            tp_hit    = pnl_pct >= TP_PCT
            sl_hit    = pnl_pct <= -SL_PCT
            trail_hit = (
                trail > 0 and (
                    (pos == "long"  and price <= trail) or
                    (pos == "short" and price >= trail)
                )
            )
            time_exit = self._hold_count >= MAX_HOLD_CANDLES

            if tp_hit or sl_hit or trail_hit or time_exit:
                reason = (
                    "TP" if tp_hit else
                    "SL" if sl_hit else
                    "TRAIL" if trail_hit else "TIMEOUT"
                )
                logger.info(f"EXIT {pos.upper()} | reason={reason} | pnl={pnl_pct:.4%}")
                await trader.close_position()
                pnl_usdt = pnl_pct * _notional()
                risk.update_pnl(pnl_usdt)
                with state.lock:
                    state.open_position = None
                    state.open_qty      = 0.0
                    state.open_entry    = 0.0
                    state.trailing_stop = 0.0
                self._hold_count = 0
                emoji = "🟢" if pnl_usdt > 0 else "🔴"
                await _notify(
                    f"{emoji} *{pos.upper()} CLOSED* | {reason}\n"
                    f"`pnl: {pnl_usdt:+.4f} USDT ({pnl_pct:.3%})`\n"
                    f"`price: {price}` | `rsi: {rsi:.1f}`"
                )
            self._prev_fast = fast
            self._prev_slow = slow
            return

        # ── ENTRY LOGIC ───────────────────────────────────────────────────────
        if not risk.can_open():
            self._prev_fast = fast
            self._prev_slow = slow
            return

        if not rsi_ok:
            logger.debug("RSI not ready yet (warming up)")
            self._prev_fast = fast
            self._prev_slow = slow
            return

        cross_up   = self._prev_fast <= self._prev_slow and fast > slow
        cross_down = self._prev_fast >= self._prev_slow and fast < slow

        if cross_up and rsi > RSI_LONG_MIN:
            qty = risk.calc_qty(price)
            logger.info(f"LONG | ema={fast:.2f}>{slow:.2f} rsi={rsi:.1f} qty={qty}")
            await trader.place_order("Buy", qty)
            with state.lock:
                state.open_position = "long"
                state.open_qty      = qty
                state.open_entry    = price
                state.trailing_stop = 0.0
            self._hold_count = 0
            await _notify(
                f"🟡 *LONG ENTRY* `{state.symbol_str()}`\n"
                f"`price: {price}` | `qty: {qty}`\n"
                f"`ema_fast: {fast:.2f}` | `rsi: {rsi:.1f}`"
            )

        elif cross_down and rsi < RSI_SHORT_MAX:
            qty = risk.calc_qty(price)
            logger.info(f"SHORT | ema={fast:.2f}<{slow:.2f} rsi={rsi:.1f} qty={qty}")
            await trader.place_order("Sell", qty)
            with state.lock:
                state.open_position = "short"
                state.open_qty      = qty
                state.open_entry    = price
                state.trailing_stop = 0.0
            self._hold_count = 0
            await _notify(
                f"🟠 *SHORT ENTRY* `{state.symbol_str()}`\n"
                f"`price: {price}` | `qty: {qty}`\n"
                f"`ema_fast: {fast:.2f}` | `rsi: {rsi:.1f}`"
            )

        self._prev_fast = fast
        self._prev_slow = slow


def _notional() -> float:
    from .config import config
    return config.order_size_usdt * config.leverage


strategy = Strategy()
