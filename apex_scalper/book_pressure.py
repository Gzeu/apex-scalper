"""Book Pressure v0.7.0 — cumulative bid/ask delta as primary entry trigger.

Replaces EMA cross as the PRIMARY signal for entries. EMA cross is demoted
to a confirmation filter (weight reduced from 0.23 to 0.10 in strategy.py).

How it works:
  Every orderbook update (from feed.py) calls bp.on_tick(bid_vol, ask_vol).
  We maintain a rolling window of 50 ticks.
  delta[i] = bid_vol[i] - ask_vol[i]   (positive = buy pressure)
  cum_delta = sum(last N deltas)
  acceleration = cum_delta_last_10 - cum_delta_prev_10  (momentum of flow)

  pressure_long():
    cum_delta > +PRESSURE_THRESHOLD
    AND acceleration > 0            (flow is accelerating, not exhausting)
    AND not in absorption zone      (ask wall > bid_vol * ABSORPTION_RATIO)

  pressure_short():
    cum_delta < -PRESSURE_THRESHOLD
    AND acceleration < 0
    AND not in absorption zone

Threshold auto-scales with volatility:
  PRESSURE_THRESHOLD = BASE_THRESHOLD * (1 + vol_zscore * 0.3)
  In high-vol markets, need stronger pressure to confirm (avoids noise entries)

Benefits vs EMA cross:
  - No lag: responds within 1-5 ticks instead of 3-5 candles
  - Detects institutional flow before price moves
  - Natural exit signal when pressure reverses

Integration:
  from .book_pressure import bp
  bp.on_tick(bid_vol, ask_vol)   <- called in feed.py on every OB update
  signal = bp.pressure_long()    <- called in strategy.py
"""
from __future__ import annotations

from collections import deque
from loguru import logger

BASE_THRESHOLD    = float(50_000)   # USDT equivalent bid/ask delta
WINDOW_TICKS      = 50
ACCEL_WINDOW      = 10
ABSORPTION_RATIO  = 3.0             # ask_wall > bid * 3.0 = absorption


class BookPressure:
    def __init__(self):
        self._deltas:   deque = deque(maxlen=WINDOW_TICKS)
        self._bid_vols: deque = deque(maxlen=WINDOW_TICKS)
        self._ask_vols: deque = deque(maxlen=WINDOW_TICKS)
        self._vol_zscore: float = 0.0
        self.cum_delta:   float = 0.0
        self.acceleration: float = 0.0
        self._ready: bool = False

    def on_tick(self, bid_vol: float, ask_vol: float) -> None:
        """Call on every orderbook snapshot update."""
        delta = bid_vol - ask_vol
        self._deltas.append(delta)
        self._bid_vols.append(bid_vol)
        self._ask_vols.append(ask_vol)

        if len(self._deltas) >= ACCEL_WINDOW * 2:
            self._ready   = True
            all_d         = list(self._deltas)
            self.cum_delta = sum(all_d)
            last  = sum(all_d[-ACCEL_WINDOW:])
            prev  = sum(all_d[-ACCEL_WINDOW * 2:-ACCEL_WINDOW])
            self.acceleration = last - prev

    def set_vol_zscore(self, z: float) -> None:
        """Feed current volume z-score for adaptive threshold."""
        self._vol_zscore = z

    def _threshold(self) -> float:
        return BASE_THRESHOLD * (1 + max(self._vol_zscore, 0) * 0.3)

    def pressure_long(self) -> bool:
        if not self._ready:
            return False
        th = self._threshold()
        if self.cum_delta < th:
            return False
        if self.acceleration <= 0:
            return False
        # Absorption check: large ask wall killing buy pressure
        if self._ask_vols and self._bid_vols:
            avg_ask = sum(self._ask_vols) / len(self._ask_vols)
            avg_bid = sum(self._bid_vols) / len(self._bid_vols)
            if avg_bid > 0 and avg_ask / avg_bid > ABSORPTION_RATIO:
                return False  # being absorbed by seller wall
        return True

    def pressure_short(self) -> bool:
        if not self._ready:
            return False
        th = self._threshold()
        if self.cum_delta > -th:
            return False
        if self.acceleration >= 0:
            return False
        # Absorption check: large bid wall killing sell pressure
        if self._ask_vols and self._bid_vols:
            avg_ask = sum(self._ask_vols) / len(self._ask_vols)
            avg_bid = sum(self._bid_vols) / len(self._bid_vols)
            if avg_ask > 0 and avg_bid / avg_ask > ABSORPTION_RATIO:
                return False  # being absorbed by buyer wall
        return True

    def ready(self) -> bool:
        return self._ready

    def debug_str(self) -> str:
        return (
            f"cum_delta={self.cum_delta:+.0f} "
            f"accel={self.acceleration:+.0f} "
            f"thr={self._threshold():.0f} "
            f"long={self.pressure_long()} short={self.pressure_short()}"
        )


bp = BookPressure()
