"""Risk manager v0.7.0 — Kelly fractional position sizing.

Upgrade vs v0.4.0 (fixed sizing per profile):
  - Half-Kelly sizing: f* = (edge / odds) * KELLY_FRACTION
    edge = rolling_win_rate - rolling_loss_rate (last KELLY_LOOKBACK trades)
    odds = avg_win_pct / avg_loss_pct
    qty  = base_qty * clamp(f*, MIN_KELLY_F, MAX_KELLY_F)
  - Falls back to base_qty when < MIN_KELLY_TRADES in history
  - KELLY_FRACTION = 0.5 (half-Kelly, standard for live trading)
  - Hard caps: MIN_KELLY_F=0.3, MAX_KELLY_F=1.8 (never < 30% or > 180%)
  - regime size_factor from regime_filter applied on top

Result:
  - Sizes up automatically in winning streaks (edge high, odds high)
  - Sizes down automatically in drawdowns (edge drops, odds drop)
  - Never needs manual tuning of order_size_usdt during live trading
"""
from __future__ import annotations

import os
import threading
from collections import deque
from loguru import logger

MAX_DAILY_LOSS      = float(os.getenv("MAX_DAILY_LOSS_USDT",  "50.0"))
MAX_OPEN_POSITIONS  = int(os.getenv("MAX_OPEN_POSITIONS",     "1"))
MAX_SPREAD_BPS      = float(os.getenv("MAX_SPREAD_BPS",       "5.0"))
MIN_BID_DEPTH       = float(os.getenv("MIN_BID_DEPTH",        "10000"))
MIN_ASK_DEPTH       = float(os.getenv("MIN_ASK_DEPTH",        "10000"))

# Kelly params
KELLY_FRACTION      = float(os.getenv("KELLY_FRACTION",       "0.5"))
KELLY_LOOKBACK      = int(os.getenv("KELLY_LOOKBACK",         "50"))
MIN_KELLY_TRADES    = int(os.getenv("MIN_KELLY_TRADES",       "20"))
MIN_KELLY_F         = float(os.getenv("MIN_KELLY_F",          "0.30"))
MAX_KELLY_F         = float(os.getenv("MAX_KELLY_F",          "1.80"))


class RiskManager:
    def __init__(self):
        self._lock         = threading.Lock()
        self._daily_loss   = 0.0
        self._daily_limit  = MAX_DAILY_LOSS
        self._open_count   = 0
        # Kelly tracking
        self._trade_results: deque = deque(maxlen=KELLY_LOOKBACK)
        # Each entry: {"pnl_pct": float, "win": bool}

    def can_open(self) -> bool:
        with self._lock:
            if self._daily_loss >= self._daily_limit:
                logger.warning(f"Daily loss limit hit: {self._daily_loss:.2f}/{self._daily_limit:.2f}")
                return False
            if self._open_count >= MAX_OPEN_POSITIONS:
                return False
        return True

    def on_open(self) -> None:
        with self._lock:
            self._open_count += 1

    def on_close(self, pnl_usdt: float, pnl_pct: float) -> None:
        with self._lock:
            if pnl_usdt < 0:
                self._daily_loss += abs(pnl_usdt)
            self._open_count = max(0, self._open_count - 1)
            self._trade_results.append({
                "pnl_pct": pnl_pct,
                "win":     pnl_usdt > 0,
            })

    def update_pnl(self, pnl_usdt: float, pnl_pct: float = 0.0) -> None:
        self.on_close(pnl_usdt, pnl_pct)

    def _kelly_factor(self) -> float:
        """Compute half-Kelly sizing factor from recent trade history."""
        trades = list(self._trade_results)
        if len(trades) < MIN_KELLY_TRADES:
            return 1.0  # Not enough data, use base size

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
        f    = (edge / (1.0 / odds)) * KELLY_FRACTION   # half-Kelly formula
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
        """Compute order quantity using Kelly-scaled sizing.

        Args:
            price:           current market price
            order_size_usdt: base notional from profile
            leverage:        account leverage
            tick_size:       min qty step for the symbol
            regime_factor:   from regime_filter.size_factor() (0.5 in VOLATILE, 0 in RANGING)
        """
        if price <= 0 or order_size_usdt <= 0:
            return 0.0

        with self._lock:
            kelly_f = self._kelly_factor()

        effective_usdt = order_size_usdt * kelly_f * regime_factor
        notional       = effective_usdt * leverage
        qty            = notional / price

        # Round to tick_size
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


risk = RiskManager()
