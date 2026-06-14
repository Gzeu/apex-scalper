"""Risk manager: position sizing, daily loss guard, spread filter."""
from __future__ import annotations

from loguru import logger
from .config import config
from .state import state


class RiskManager:
    # Stop trading if spread > this fraction of mid price
    MAX_SPREAD_BPS: float = 5.0  # 5 bps

    def can_open(self) -> bool:
        """Return True if a new position is allowed."""
        if state.open_position is not None:
            return False
        if state.daily_pnl <= -config.daily_loss_limit_usdt:
            logger.warning("Daily loss limit hit — bot paused")
            state.paused = True
            return False
        if not self._spread_ok():
            return False
        return True

    def _spread_ok(self) -> bool:
        mid = state.orderbook.mid_price
        spread = state.orderbook.spread
        if mid is None or spread is None:
            return False
        spread_bps = (spread / mid) * 10_000
        return spread_bps <= self.MAX_SPREAD_BPS

    def calc_qty(self, price: float) -> float:
        """Compute order quantity from fixed USDT size and current price."""
        raw = (config.order_size_usdt * config.leverage) / price
        # Bybit BTCUSDT min qty 0.001 — round down to 3 decimals
        qty = round(raw, 3)
        return max(qty, 0.001)

    def update_pnl(self, closed_pnl: float) -> None:
        state.realized_pnl += closed_pnl
        state.daily_pnl += closed_pnl
        state.total_trades += 1
        if closed_pnl > 0:
            state.win_trades += 1


risk = RiskManager()
