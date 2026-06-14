"""Risk manager v0.3.1.

Fixes vs v0.3.0:
- _spread_ok() and _depth_ok() had no lock -> race condition on OB reads fixed.
- daily loss check now properly sets state.paused inside lock.
- NEW: MAX_CONSECUTIVE_LOSSES circuit breaker.
- update_pnl() now acquires lock before mutating state fields.
"""
from __future__ import annotations

import os
import datetime
from loguru import logger
from .config import config
from .state import state

# All overridable from .env or injected by main.inject_profile()
MAX_SPREAD_BPS         = float(os.getenv("MAX_SPREAD_BPS",          "5.0"))
MIN_BID_DEPTH          = float(os.getenv("MIN_BID_DEPTH",           "0.5"))
MIN_ASK_DEPTH          = float(os.getenv("MIN_ASK_DEPTH",           "0.5"))
TRADE_HOUR_START       = int(os.getenv("TRADE_HOUR_START",          "2"))
TRADE_HOUR_END         = int(os.getenv("TRADE_HOUR_END",            "22"))
SKIP_SESSION_FILTER    = os.getenv("SKIP_SESSION_FILTER", "false").lower() == "true"
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES",    "5"))


class RiskManager:
    def __init__(self):
        self._consecutive_losses: int = 0

    def can_open(self) -> bool:
        with state.lock:
            if state.open_position is not None:
                return False
            if state.paused:
                return False
            daily_pnl = state.daily_pnl

        if daily_pnl <= -config.daily_loss_limit_usdt:
            logger.warning("Daily loss limit reached — pausing bot")
            state.paused = True
            return False

        if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            logger.warning(
                f"Max consecutive losses ({MAX_CONSECUTIVE_LOSSES}) reached — pausing"
            )
            state.paused = True
            return False

        if not self._spread_ok():
            return False
        if not self._depth_ok():
            return False
        if not self._session_ok():
            return False
        return True

    def _spread_ok(self) -> bool:
        # FIX: acquire lock before reading OB (race condition with WS thread)
        with state.lock:
            mid    = state.orderbook.mid_price
            spread = state.orderbook.spread
        if mid is None or spread is None or mid == 0:
            return False
        return (spread / mid * 10_000) <= MAX_SPREAD_BPS

    def _depth_ok(self) -> bool:
        # FIX: acquire lock
        with state.lock:
            bid_d = state.orderbook.bid_depth(5)
            ask_d = state.orderbook.ask_depth(5)
        return bid_d >= MIN_BID_DEPTH and ask_d >= MIN_ASK_DEPTH

    def _session_ok(self) -> bool:
        if SKIP_SESSION_FILTER:
            return True
        hour = datetime.datetime.utcnow().hour
        return TRADE_HOUR_START <= hour < TRADE_HOUR_END

    def calc_qty(self, price: float) -> float:
        raw = (config.order_size_usdt * config.leverage) / price
        return max(round(raw, 3), 0.001)

    def update_pnl(self, closed_pnl: float) -> None:
        # FIX: acquire lock before mutating shared state
        with state.lock:
            state.realized_pnl += closed_pnl
            state.daily_pnl    += closed_pnl
            state.total_trades += 1
            if closed_pnl > 0:
                state.win_trades += 1
                self._consecutive_losses = 0
            else:
                self._consecutive_losses += 1
        from .performance import perf
        perf.record(closed_pnl)

    def reset_consecutive_losses(self) -> None:
        self._consecutive_losses = 0


risk = RiskManager()
