"""Regime Filter v0.8.9 — Wilder ADX + O(log n) ATR percentile.

Changelog:
  v0.8.9 — BUG 37 FIX: ADX folosea SMA(DX) in loc de Wilder smoothing.
    Standard Wilder ADX: dupa seed (primele N bare), ADX se calculeaza ca
    Wilder EMA a DX cu alpha=1/N (adica: adx = adx*(N-1)/N + dx).
    Codul vechi: self._adx_buf.append(dx); self._adx = mean(buf) → SMA pe
    un buffer rolling de 14 DX → ADX mai reactiv si mai mare decat Wilder
    → filtrarea regimului (RANGING/TRENDING) era calibrata gresit.
    Fix: seed = media simpla a primelor N valori DX; dupa seed: Wilder EMA.
  v0.8.9 — BUG 38 FIX: DX era calculat si acumulat in seed inainte ca
    sumele Wilder (atr_s, pdm_s, ndm_s) sa fie complet seeduite.
    Fix: DX calculat si ADX actualizat NUMAI dupa _wilder_ready > N (seed complet).

Fixes anterioare:
  FIX #4 (v0.7.1) — Wilder smoothing pentru ATR/DM (era SMA).
  FIX #10 (v0.7.1) — O(log n) ATR percentile via bisect.
"""
from __future__ import annotations

import bisect
import math
from collections import deque
from loguru import logger

# Tunable via inject_profile()
ADX_TRENDING_MIN   = 25.0
ADX_RANGING_MAX    = 20.0
ATR_VOLATILE_PCT   = 80.0
ATR_RANGING_PCT    = 20.0
HURST_TREND_MIN    = 0.55
HURST_RANGE_MAX    = 0.45
ATR_WINDOW         = 20 * 24 * 60   # 28800 entries
HURST_WINDOW       = 50
ADX_PERIOD         = 14


def _hurst(closes: list[float]) -> float:
    """R/S Hurst exponent on last N closes. Returns 0.5 if insufficient data."""
    n = len(closes)
    if n < 20:
        return 0.5
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
        self._closes:      deque = deque(maxlen=HURST_WINDOW)
        # FIX #10: sorted list for O(log n) percentile
        self._atrs_raw:    deque = deque(maxlen=ATR_WINDOW)
        self._atrs_sorted: list  = []
        # ADX internals — Wilder smoothing state
        self._prev_high:   float = 0.0
        self._prev_low:    float = 0.0
        self._prev_close:  float = 0.0
        self._atr_s:       float = 0.0   # Wilder-smoothed ATR sum
        self._pdm_s:       float = 0.0   # Wilder-smoothed +DM sum
        self._ndm_s:       float = 0.0   # Wilder-smoothed -DM sum
        # BUG 37/38 FIX: buffer seed DX pentru primele N bare (SMA seed)
        self._dx_seed_buf: list  = []    # acumuleaza DX in faza seed
        self._adx:         float = 0.0   # Wilder EMA a DX dupa seed
        self._adx_seeded:  bool  = False # True dupa ce seed SMA e calculat
        self._wilder_ready: int  = 0
        self.label:        str   = "UNKNOWN"
        self._size_f:      float = 1.0
        self._allow:       bool  = True
        self._candles:     int   = 0

    def update(self, close: float, atr_value: float, high: float, low: float) -> None:
        self._closes.append(close)
        self._candles += 1

        # FIX #10: maintain sorted list alongside raw deque
        if len(self._atrs_raw) == ATR_WINDOW:
            evicted = self._atrs_raw[0]
            idx = bisect.bisect_left(self._atrs_sorted, evicted)
            if idx < len(self._atrs_sorted) and self._atrs_sorted[idx] == evicted:
                del self._atrs_sorted[idx]
        self._atrs_raw.append(atr_value)
        bisect.insort(self._atrs_sorted, atr_value)

        # Wilder-smoothed ADX
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

            self._wilder_ready += 1
            N = ADX_PERIOD

            if self._wilder_ready <= N:
                # Seed phase: simple sum pentru primele N valori TR/DM
                self._atr_s += tr
                self._pdm_s += pdm
                self._ndm_s += ndm

                # BUG 38 FIX: NU calculam DX pana seed-ul TR/DM e complet
                # Doar la bara N exacta calculam primul DX si il adaugam in seed_buf
                if self._wilder_ready == N and self._atr_s > 0:
                    pdi = self._pdm_s / self._atr_s * 100
                    ndi = self._ndm_s / self._atr_s * 100
                    dxd = pdi + ndi
                    dx  = abs(pdi - ndi) / dxd * 100 if dxd > 0 else 0.0
                    self._dx_seed_buf.append(dx)

            else:
                # Wilder smoothing: smoothed = prev * (N-1)/N + new
                self._atr_s = self._atr_s * (N - 1) / N + tr
                self._pdm_s = self._pdm_s * (N - 1) / N + pdm
                self._ndm_s = self._ndm_s * (N - 1) / N + ndm

                if self._atr_s > 0:
                    pdi = self._pdm_s / self._atr_s * 100
                    ndi = self._ndm_s / self._atr_s * 100
                    dxd = pdi + ndi
                    dx  = abs(pdi - ndi) / dxd * 100 if dxd > 0 else 0.0

                    # BUG 37 FIX: Wilder ADX seed cu SMA(primele N DX),
                    # apoi Wilder EMA pentru toate DX urmatoare
                    if not self._adx_seeded:
                        self._dx_seed_buf.append(dx)
                        if len(self._dx_seed_buf) >= N:
                            # Seed ADX = SMA a primelor N valori DX
                            self._adx = sum(self._dx_seed_buf) / len(self._dx_seed_buf)
                            self._adx_seeded = True
                    else:
                        # Wilder EMA: adx = adx*(N-1)/N + dx
                        self._adx = self._adx * (N - 1) / N + dx

        self._prev_high  = high
        self._prev_low   = low
        self._prev_close = close

        if self._candles < ADX_PERIOD * 2:
            self.label   = "UNKNOWN"
            self._allow  = True
            self._size_f = 1.0
            return

        # FIX #10: O(log n) ATR percentile via bisect_right
        n_atrs = len(self._atrs_sorted)
        if n_atrs >= 10:
            rank    = bisect.bisect_right(self._atrs_sorted, atr_value)
            atr_pct = rank / n_atrs * 100
        else:
            atr_pct = 50.0

        hurst = _hurst(list(self._closes))

        is_volatile = atr_pct >= ATR_VOLATILE_PCT
        is_ranging  = (
            self._adx < ADX_RANGING_MAX
            or atr_pct < ATR_RANGING_PCT
            or hurst < HURST_RANGE_MAX
        )
        is_trending = (
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
