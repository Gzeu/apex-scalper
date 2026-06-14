"""Risk manager: position sizing, daily loss guard, spread filter, depth check."""
from __future__ import annotations

import os
from loguru import logger
from .config import config
from .state import state

# Configurable via env
MAX_SPREAD_BPS    = float(os.getenv("MAX_SPREAD_BPS",    "5.0"))
MIN_BID_DEPTH     = float(os.getenv("MIN_BID_DEPTH",     "0.5"))   # BTC in top-5 bids
MIN_ASK_DEPTH     = float(os.getenv("MIN_ASK_DEPTH",     "0.5"))   # BTC in top-5 asks


class RiskManager:
    def can_open(self) -> bool:
        if state.open_position is not None:
            return False
        if state.daily_pnl <= -config.daily_loss_limit_usdt:
            logger.warning("Daily loss limit reached — pausing")
            state.paused = True
            return False
        if not self._spread_ok():
            return False
        if not self._depth_ok():
            return False
        return True

    def _spread_ok(self) -> bool:
        mid    = state.orderbook.mid_price
        spread = state.orderbook.spread
        if mid is None or spread is None:
            return False
        return (spread / mid * 10_000) <= MAX_SPREAD_BPS

    def _depth_ok(self) -> bool:
        """Check there is enough liquidity in top-5 levels."""
        return (
            state.orderbook.bid_depth(5) >= MIN_BID_DEPTH and
            state.orderbook.ask_depth(5) >= MIN_ASK_DEPTH
        )

    def calc_qty(self, price: float) -> float:
        raw = (config.order_size_usdt * config.leverage) / price
        return max(round(raw, 3), 0.001)

    def update_pnl(self, closed_pnl: float) -> None:
        state.realized_pnl += closed_pnl
        state.daily_pnl    += closed_pnl
        state.total_trades += 1
        if closed_pnl > 0:
            state.win_trades += 1


risk = RiskManager()
