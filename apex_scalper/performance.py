"""Live performance metrics v0.8.4 — O(1) running counters, bounded trades deque.

Changelog:
  v0.8.4 — BUG 18 FIX: trades List[float] crestea nelimitat.
    avg_win / avg_loss / profit_factor / win_rate rescaneaza toata lista O(n)
    dupa mii de trade-uri -> RAM leak + latenta per calcul.
    Fix:
      - trades: deque(maxlen=MAX_TRADES_HISTORY) capateaza memoria
      - running counters (_n_wins, _gross_profit, _n_losses, _gross_loss)
        actualizati incremental in record() -> O(1) per proprietate
      - Sharpe Welford nemodificat (era deja O(1))
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

MAX_TRADES_HISTORY = 1_000  # cap memorie: ~8KB la 1000 float-uri


@dataclass
class PerfMetrics:
    # BUG 18 FIX: deque cu maxlen in loc de list nelimitata
    trades: Deque[float] = field(default_factory=lambda: deque(maxlen=MAX_TRADES_HISTORY))

    # Welford online Sharpe (nemodificat)
    _n: int = 0
    _mean: float = 0.0
    _M2: float = 0.0

    # BUG 18 FIX: running counters O(1) pentru win/loss stats
    _n_wins: int = 0
    _n_losses: int = 0
    _gross_profit: float = 0.0
    _gross_loss: float = 0.0    # stored as positive value
    _sum_win: float = 0.0
    _sum_loss: float = 0.0      # stored as negative value

    # Drawdown
    _peak_equity: float = 0.0
    _equity: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0

    # Streaks
    win_streak: int = 0
    lose_streak: int = 0
    _cur_win_streak: int = 0
    _cur_lose_streak: int = 0

    def record(self, pnl: float, balance: float = 0.0) -> None:
        self.trades.append(pnl)

        # Welford online mean + variance (O(1))
        self._n += 1
        delta = pnl - self._mean
        self._mean += delta / self._n
        self._M2 += delta * (pnl - self._mean)

        # BUG 18 FIX: running win/loss counters (O(1))
        if pnl > 0:
            self._n_wins += 1
            self._gross_profit += pnl
            self._sum_win += pnl
        else:
            self._n_losses += 1
            self._gross_loss += abs(pnl)
            self._sum_loss += pnl

        # Drawdown
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
        std = math.sqrt(variance) if variance > 0 else 0.0
        if std == 0:
            return 0.0
        return (self._mean / std) * math.sqrt(525_600)

    @property
    def avg_win(self) -> float:
        """BUG 18 FIX: O(1) via running counter."""
        return self._sum_win / self._n_wins if self._n_wins > 0 else 0.0

    @property
    def avg_loss(self) -> float:
        """BUG 18 FIX: O(1) via running counter."""
        return self._sum_loss / self._n_losses if self._n_losses > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        """BUG 18 FIX: O(1) via running counter."""
        return self._gross_profit / self._gross_loss if self._gross_loss > 0 else float("inf")

    @property
    def win_rate(self) -> float:
        """BUG 18 FIX: O(1) via running counter."""
        total = self._n_wins + self._n_losses
        return self._n_wins / total * 100 if total > 0 else 0.0

    @property
    def expectancy(self) -> float:
        """Expected PnL per trade."""
        wr = self.win_rate / 100
        return wr * self.avg_win + (1 - wr) * self.avg_loss

    def summary(self) -> str:
        return (
            f"Trades: {self._n} | WR: {self.win_rate:.1f}% | "
            f"Sharpe: {self.sharpe:.2f} | PF: {self.profit_factor:.2f} | "
            f"Expectancy: {self.expectancy:+.4f} USDT | "
            f"MaxDD: {self.max_drawdown:.4f} USDT ({self.max_drawdown_pct:.2f}%) | "
            f"AvgW: {self.avg_win:+.4f} AvgL: {self.avg_loss:+.4f} | "
            f"WStreak: {self.win_streak} LStreak: {self.lose_streak}"
        )


perf = PerfMetrics()
