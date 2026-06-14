"""Live performance metrics: Sharpe ratio, max drawdown, avg win/loss.

All metrics computed incrementally (no full history scan on each trade).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List


@dataclass
class PerfMetrics:
    trades: List[float] = field(default_factory=list)  # PnL per trade in USDT
    # Running Sharpe components (Welford online algorithm)
    _n: int = 0
    _mean: float = 0.0
    _M2: float = 0.0
    # Drawdown tracking
    _peak_equity: float = 0.0
    _equity: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    # Streak
    win_streak: int = 0
    lose_streak: int = 0
    _cur_win_streak: int = 0
    _cur_lose_streak: int = 0

    def record(self, pnl: float, balance: float = 0.0) -> None:
        self.trades.append(pnl)
        # Welford online mean + variance
        self._n += 1
        delta = pnl - self._mean
        self._mean += delta / self._n
        self._M2 += delta * (pnl - self._mean)
        # Equity drawdown
        self._equity += pnl
        if self._equity > self._peak_equity:
            self._peak_equity = self._equity
        dd = self._peak_equity - self._equity
        if dd > self.max_drawdown:
            self.max_drawdown = dd
        if self._peak_equity > 0:
            self.max_drawdown_pct = self.max_drawdown / self._peak_equity * 100
        # Streaks
        if pnl > 0:
            self._cur_win_streak  += 1
            self._cur_lose_streak  = 0
            self.win_streak = max(self.win_streak, self._cur_win_streak)
        else:
            self._cur_lose_streak += 1
            self._cur_win_streak   = 0
            self.lose_streak = max(self.lose_streak, self._cur_lose_streak)

    @property
    def sharpe(self) -> float:
        """Annualized Sharpe (risk-free=0, 1m candles, 525600 candles/year)."""
        if self._n < 2:
            return 0.0
        variance = self._M2 / (self._n - 1)
        std = math.sqrt(variance) if variance > 0 else 0
        if std == 0:
            return 0.0
        return (self._mean / std) * math.sqrt(525_600)

    @property
    def avg_win(self) -> float:
        wins = [t for t in self.trades if t > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t for t in self.trades if t < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t for t in self.trades if t > 0)
        gross_loss   = abs(sum(t for t in self.trades if t < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return len([t for t in self.trades if t > 0]) / len(self.trades) * 100

    @property
    def expectancy(self) -> float:
        """Expected PnL per trade."""
        wr = self.win_rate / 100
        return wr * self.avg_win + (1 - wr) * self.avg_loss

    def summary(self) -> str:
        return (
            f"Trades: {len(self.trades)} | WR: {self.win_rate:.1f}% | "
            f"Sharpe: {self.sharpe:.2f} | PF: {self.profit_factor:.2f} | "
            f"Expectancy: {self.expectancy:+.4f} USDT | "
            f"MaxDD: {self.max_drawdown:.4f} USDT ({self.max_drawdown_pct:.2f}%) | "
            f"AvgW: {self.avg_win:+.4f} AvgL: {self.avg_loss:+.4f} | "
            f"WStreak: {self.win_streak} LStreak: {self.lose_streak}"
        )


perf = PerfMetrics()
