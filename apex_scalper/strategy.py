"""Scalping strategy: EMA(9/21) cross + momentum + spread filter.

Entry conditions:
  LONG  — ema_fast crosses above ema_slow AND best_ask liquidity ok AND spread ok
  SHORT — ema_fast crosses below ema_slow AND best_bid liquidity ok AND spread ok

Exit conditions:
  - Take profit: +0.15% from entry (configurable)
  - Stop loss:   -0.10% from entry (configurable)
  - Time-based: max 3 candles held
"""
from __future__ import annotations

import asyncio
from loguru import logger
from .state import state
from .risk import risk

# Strategy parameters (can be overridden via env if desired)
TP_PCT = 0.0015   # 0.15%
SL_PCT = 0.0010   # 0.10%
MAX_HOLD_CANDLES = 3


class Strategy:
    def __init__(self):
        self._prev_fast: float = 0.0
        self._prev_slow: float = 0.0
        self._hold_count: int = 0

    async def evaluate(self) -> None:
        """Called on every new 1m close. Decides open / close."""
        from .trader import trader  # lazy import avoids circular

        async with state.lock:
            if not state.running or state.paused:
                return

            fast = state.ema_fast
            slow = state.ema_slow
            price = state.last_price
            pos = state.open_position
            entry = state.open_entry

        # --- EXIT logic first ---
        if pos:
            self._hold_count += 1
            pnl_pct = (price - entry) / entry if pos == "long" else (entry - price) / entry
            tp_hit = pnl_pct >= TP_PCT
            sl_hit = pnl_pct <= -SL_PCT
            time_exit = self._hold_count >= MAX_HOLD_CANDLES

            if tp_hit or sl_hit or time_exit:
                reason = "TP" if tp_hit else ("SL" if sl_hit else "timeout")
                logger.info(f"Closing {pos} — reason={reason} pnl_pct={pnl_pct:.4%}")
                await trader.close_position()
                pnl_usdt = pnl_pct * config_val()
                risk.update_pnl(pnl_usdt)
                async with state.lock:
                    state.open_position = None
                    state.open_qty = 0.0
                    state.open_entry = 0.0
                self._hold_count = 0
            return

        # --- ENTRY logic ---
        if not risk.can_open():
            self._prev_fast = fast
            self._prev_slow = slow
            return

        cross_up = self._prev_fast <= self._prev_slow and fast > slow
        cross_down = self._prev_fast >= self._prev_slow and fast < slow

        if cross_up:
            qty = risk.calc_qty(price)
            logger.info(f"LONG signal: ema_fast={fast:.2f} > ema_slow={slow:.2f} price={price}")
            await trader.place_order("Buy", qty)
            async with state.lock:
                state.open_position = "long"
                state.open_qty = qty
                state.open_entry = price
            self._hold_count = 0

        elif cross_down:
            qty = risk.calc_qty(price)
            logger.info(f"SHORT signal: ema_fast={fast:.2f} < ema_slow={slow:.2f} price={price}")
            await trader.place_order("Sell", qty)
            async with state.lock:
                state.open_position = "short"
                state.open_qty = qty
                state.open_entry = price
            self._hold_count = 0

        self._prev_fast = fast
        self._prev_slow = slow


def config_val():
    from .config import config
    return config.order_size_usdt * config.leverage


strategy = Strategy()
