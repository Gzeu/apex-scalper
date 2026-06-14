"""Risk manager v0.7.1 — Kelly fractional position sizing + consecutive loss guard.

Fixes vs v0.7.0:
  - reset_daily now also clears _open_count (prevents phantom open_count
    after a crash that left _open_count > 0 without a matching on_close).
  - MAX_CONSECUTIVE_LOSSES env guard: can_open() returns False after N
    consecutive losses, forcing a pause until manual /resume.
  - on_close now updates consecutive loss counter correctly for partial
    closes (pnl_usdt < 0 increments, pnl_usdt > 0 resets to 0).
  - No change to Kelly formula or trade history deque.
"""
from __future__ import annotations

import os
import threading
from collections import deque
from loguru import logger

MAX_DAILY_LOSS         = float(os.getenv("MAX_DAILY_LOSS_USDT",      "50.0"))
MAX_OPEN_POSITIONS     = int(os.getenv("MAX_OPEN_POSITIONS",         "1"))
MAX_SPREAD_BPS         = float(os.getenv("MAX_SPREAD_BPS",           "5.0"))
MIN_BID_DEPTH          = float(os.getenv("MIN_BID_DEPTH",            "10000"))
MIN_ASK_DEPTH          = float(os.getenv("MIN_ASK_DEPTH",            "10000"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES",     "5"))

# Kelly params
KELLY_FRACTION      = float(os.getenv("KELLY_FRACTION",       "0.5"))
KELLY_LOOKBACK      = int(os.getenv("KELLY_LOOKBACK",         "50"))
MIN_KELLY_TRADES    = int(os.getenv("MIN_KELLY_TRADES",       "20"))
MIN_KELLY_F         = float(os.getenv("MIN_KELLY_F",          "0.30"))
MAX_KELLY_F         = float(os.getenv("MAX_KELLY_F",          "1.80"))


class RiskManager:
    def __init__(self):
        self._lock               = threading.Lock()
        self._daily_loss         = 0.0
        self._daily_limit        = MAX_DAILY_LOSS
        self._open_count         = 0
        self._consecutive_losses = 0
        # Kelly tracking: {"pnl_pct": float, "win": bool}
        self._trade_results: deque = deque(maxlen=KELLY_LOOKBACK)

    def can_open(self) -> bool:
        with self._lock:
            if self._daily_loss >= self._daily_limit:
                logger.warning(f"Daily loss limit hit: {self._daily_loss:.2f}/{self._daily_limit:.2f}")
                return False
            if self._open_count >= MAX_OPEN_POSITIONS:
                return False
            if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                logger.warning(
                    f"Consecutive losses={self._consecutive_losses} >= "
                    f"MAX_CONSECUTIVE_LOSSES={MAX_CONSECUTIVE_LOSSES} — paused"
                )
                return False
        return True

    def on_open(self) -> None:
        with self._lock:
            self._open_count += 1

    def on_close(self, pnl_usdt: float, pnl_pct: float) -> None:
        with self._lock:
            if pnl_usdt < 0:
                self._daily_loss         += abs(pnl_usdt)
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0   # reset on any win
            self._open_count = max(0, self._open_count - 1)
            self._trade_results.append({
                "pnl_pct": pnl_pct,
                "win":     pnl_usdt > 0,
            })

    def update_pnl(self, pnl_usdt: float, pnl_pct: float = 0.0) -> None:
        self.on_close(pnl_usdt, pnl_pct)

    def _kelly_factor(self) -> float:
        """Half-Kelly sizing factor from recent trade history."""
        trades = list(self._trade_results)
        if len(trades) < MIN_KELLY_TRADES:
            return 1.0

        wins   = [t for t in trades if t["win"]]
        losses = [t for t in trades if not t["win"]]

        if not wins or not losses:
            return MIN_KELLY_F if not wins else MAX_KELLY_F

        win_rate  = len(wins)  / len(trades)
        loss_rate = len(losses) / len(trades)

        avg_win  = sum(abs(t["pnl_pct"]) for t in wins)  / len(wins)
        avg_loss = sum(abs(t["pnl_pct"]) for t in losses) / len(losses)

        if avg_loss == 0:
            return MAX_KELLY_F

        edge = win_rate - loss_rate
        odds = avg_win / avg_loss
        f    = (edge / (1.0 / odds)) * KELLY_FRACTION
        f    = max(MIN_KELLY_F, min(MAX_KELLY_F, f))
        return f

    def calc_qty(
        self,
        price: float,
        order_size_usdt: float = 0.0,
        leverage: float = 1.0,
        tick_size: float = 0.001,
        regime_factor: float = 1.0,
    ) -> float:
        if price <= 0 or order_size_usdt <= 0:
            return 0.0

        with self._lock:
            kelly_f = self._kelly_factor()

        effective_usdt = order_size_usdt * kelly_f * regime_factor
        notional       = effective_usdt * leverage
        qty            = notional / price

        if tick_size > 0:
            qty = round(qty / tick_size) * tick_size

        qty = max(qty, tick_size)
        logger.debug(
            f"Kelly sizing: base={order_size_usdt} f={kelly_f:.3f} "
            f"regime={regime_factor:.2f} qty={qty:.4f}"
        )
        return round(qty, 6)

    def reset_daily(self) -> None:
        with self._lock:
            self._daily_loss  = 0.0
            # FIX: also reset open_count to 0 to avoid phantom lock after crash
            self._open_count  = 0
            # Note: consecutive losses intentionally NOT reset on midnight
            # (carry-over is the conservative choice)

    @property
    def consecutive_losses(self) -> int:
        with self._lock:
            return self._consecutive_losses

    def reset_consecutive_losses(self) -> None:
        """Called by /resume Telegram command to unblock after manual review."""
        with self._lock:
            self._consecutive_losses = 0
        logger.info("Consecutive losses counter reset manually")


risk = RiskManager()
