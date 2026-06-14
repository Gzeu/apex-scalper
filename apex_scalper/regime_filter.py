"""Regime Filter v0.7.0 — ADX + ATR percentile + Hurst exponent.

Classifies market into 3 regimes every candle:
  TRENDING  — ADX > 25 AND ATR percentile > 40th  -> full entries allowed
  RANGING   — ADX < 20 OR ATR percentile < 20th   -> entries BLOCKED
  VOLATILE  — ATR percentile > 80th               -> entries at 50% size

Logic:
  ADX(14):          measures trend strength, not direction
  ATR percentile:   rolling 20-day window, where is current ATR vs history
  Hurst exponent:   H > 0.55 = trending, H < 0.45 = mean-reverting (ranging)
                    Computed on last 50 closes, R/S method (fast, no scipy)

Integration with strategy.py:
  regime.allow_entry()  -> bool  (False in RANGING)
  regime.size_factor()  -> float (0.5 in VOLATILE, 1.0 otherwise)
  regime.label          -> str   (TRENDING / RANGING / VOLATILE / UNKNOWN)

Updated every candle via regime.update(close, atr_value, high, low)
"""
from __future__ import annotations

import math
from collections import deque
from loguru import logger

# Tunable via inject_profile()
ADX_TRENDING_MIN   = 25.0
ADX_RANGING_MAX    = 20.0
ATR_VOLATILE_PCT   = 80.0   # above this percentile = VOLATILE
ATR_RANGING_PCT    = 20.0   # below this percentile = RANGING
HURST_TREND_MIN    = 0.55
HURST_RANGE_MAX    = 0.45
ATR_WINDOW         = 20 * 24 * 60   # 20d * 1m candles
HURST_WINDOW       = 50
ADX_PERIOD         = 14


def _hurst(closes: list[float]) -> float:
    """R/S Hurst exponent on last N closes. Returns 0.5 if insufficient data."""
    n = len(closes)
    if n < 20:
        return 0.5
    # Use log returns
    lrets = [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    mean_r = sum(lrets) / len(lrets)
    cumdev = []
    s = 0.0
    for r in lrets:
        s += r - mean_r
        cumdev.append(s)
    R = max(cumdev) - min(cumdev)
    std = math.sqrt(sum((r - mean_r) ** 2 for r in lrets) / len(lrets))
    if std == 0 or R == 0:
        return 0.5
    rs = R / std
    return math.log(rs) / math.log(n)


class RegimeFilter:
    def __init__(self):
        self._closes:   deque = deque(maxlen=HURST_WINDOW)
        self._atrs:     deque = deque(maxlen=ATR_WINDOW)
        # ADX internals
        self._prev_high:  float = 0.0
        self._prev_low:   float = 0.0
        self._prev_close: float = 0.0
        self._tr_buf:   deque = deque(maxlen=ADX_PERIOD)
        self._pdm_buf:  deque = deque(maxlen=ADX_PERIOD)
        self._ndm_buf:  deque = deque(maxlen=ADX_PERIOD)
        self._adx_buf:  deque = deque(maxlen=ADX_PERIOD)
        self._adx:      float = 0.0
        self.label:     str   = "UNKNOWN"
        self._size_f:   float = 1.0
        self._allow:    bool  = True
        self._candles:  int   = 0

    def update(self, close: float, atr_value: float, high: float, low: float) -> None:
        self._closes.append(close)
        self._atrs.append(atr_value)
        self._candles += 1

        # ADX calculation
        if self._prev_close > 0:
            tr  = max(high - low,
                      abs(high - self._prev_close),
                      abs(low  - self._prev_close))
            pdm = max(high - self._prev_high, 0.0)
            ndm = max(self._prev_low - low,   0.0)
            if pdm <= ndm:
                pdm = 0.0
            else:
                ndm = 0.0
            self._tr_buf.append(tr)
            self._pdm_buf.append(pdm)
            self._ndm_buf.append(ndm)

            if len(self._tr_buf) >= ADX_PERIOD:
                atr14 = sum(self._tr_buf) / ADX_PERIOD
                if atr14 > 0:
                    pdi = (sum(self._pdm_buf) / ADX_PERIOD) / atr14 * 100
                    ndi = (sum(self._ndm_buf) / ADX_PERIOD) / atr14 * 100
                    dxd = pdi + ndi
                    dx  = abs(pdi - ndi) / dxd * 100 if dxd > 0 else 0
                    self._adx_buf.append(dx)
                    self._adx = sum(self._adx_buf) / len(self._adx_buf)

        self._prev_high  = high
        self._prev_low   = low
        self._prev_close = close

        if self._candles < ADX_PERIOD * 2:
            self.label    = "UNKNOWN"
            self._allow   = True
            self._size_f  = 1.0
            return

        # ATR percentile
        if len(self._atrs) >= 10:
            sorted_atrs = sorted(self._atrs)
            rank = sum(1 for a in sorted_atrs if a <= atr_value)
            atr_pct = rank / len(sorted_atrs) * 100
        else:
            atr_pct = 50.0

        # Hurst exponent
        hurst = _hurst(list(self._closes))

        # Regime classification
        is_volatile  = atr_pct >= ATR_VOLATILE_PCT
        is_ranging   = (
            self._adx < ADX_RANGING_MAX
            or atr_pct < ATR_RANGING_PCT
            or hurst < HURST_RANGE_MAX
        )
        is_trending  = (
            self._adx >= ADX_TRENDING_MIN
            and atr_pct >= ATR_RANGING_PCT
            and hurst >= HURST_TREND_MIN
        )

        if is_volatile:
            self.label   = "VOLATILE"
            self._allow  = True
            self._size_f = 0.5
        elif is_ranging:
            self.label   = "RANGING"
            self._allow  = False
            self._size_f = 0.0
        elif is_trending:
            self.label   = "TRENDING"
            self._allow  = True
            self._size_f = 1.0
        else:
            self.label   = "NEUTRAL"
            self._allow  = True
            self._size_f = 0.75

    def allow_entry(self) -> bool:
        return self._allow

    def size_factor(self) -> float:
        return self._size_f

    @property
    def adx(self) -> float:
        return round(self._adx, 2)


regime = RegimeFilter()
