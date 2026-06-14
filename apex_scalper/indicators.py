"""Streaming technical indicators — all O(1) or O(period) updates.

All functions operate on state fields directly (no pandas overhead in hot path).
Includes: EMA, RSI(14 Wilder), ATR(14), Bollinger Bands(20,2), Volume Z-Score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class IndicatorState:
    # EMA
    ema_fast: float = 0.0    # EMA(9)
    ema_slow: float = 0.0    # EMA(21)
    ema_trend: float = 0.0   # EMA(50) - trend filter

    # RSI(14) Wilder smoothing
    rsi_value: float = 50.0
    rsi_ready: bool = False
    rsi_avg_gain: float = 0.0
    rsi_avg_loss: float = 0.0
    rsi_prev_price: float = 0.0
    rsi_count: int = 0
    rsi_gains: list = field(default_factory=list)
    rsi_losses: list = field(default_factory=list)

    # ATR(14) using Wilder smoothing
    atr_value: float = 0.0
    atr_ready: bool = False
    atr_prev_close: float = 0.0
    atr_smooth: float = 0.0
    atr_count: int = 0
    atr_buf: list = field(default_factory=list)

    # Bollinger Bands(20, 2)
    bb_period: int = 20
    bb_prices: list = field(default_factory=list)
    bb_upper: float = 0.0
    bb_mid: float = 0.0
    bb_lower: float = 0.0
    bb_ready: bool = False

    # Volume Z-Score (20-period)
    vol_period: int = 20
    vol_buf: list = field(default_factory=list)
    vol_zscore: float = 0.0
    vol_ready: bool = False

    # VWAP (session, resets on new day)
    vwap: float = 0.0
    vwap_cum_vol: float = 0.0
    vwap_cum_tpv: float = 0.0   # sum(typical_price * volume)


def update_all(
    s: IndicatorState,
    close: float,
    high: float,
    low: float,
    volume: float,
) -> None:
    """Call once per confirmed candle with OHLCV data."""
    _update_ema(s, close)
    _update_rsi(s, close)
    _update_atr(s, high, low, close)
    _update_bb(s, close)
    _update_volume_zscore(s, volume)
    _update_vwap(s, close, high, low, volume)


def _update_ema(s: IndicatorState, close: float) -> None:
    k9  = 2 / (9  + 1)
    k21 = 2 / (21 + 1)
    k50 = 2 / (50 + 1)
    if s.ema_fast == 0:
        s.ema_fast = s.ema_slow = s.ema_trend = close
    else:
        s.ema_fast  = close * k9  + s.ema_fast  * (1 - k9)
        s.ema_slow  = close * k21 + s.ema_slow  * (1 - k21)
        s.ema_trend = close * k50 + s.ema_trend * (1 - k50)


def _update_rsi(s: IndicatorState, close: float) -> None:
    RSI_PERIOD = 14
    if s.rsi_prev_price == 0:
        s.rsi_prev_price = close
        return
    change = close - s.rsi_prev_price
    s.rsi_prev_price = close
    gain = max(change, 0.0)
    loss = max(-change, 0.0)
    s.rsi_count += 1
    if s.rsi_count <= RSI_PERIOD:
        s.rsi_gains.append(gain)
        s.rsi_losses.append(loss)
        if s.rsi_count == RSI_PERIOD:
            s.rsi_avg_gain = sum(s.rsi_gains) / RSI_PERIOD
            s.rsi_avg_loss = sum(s.rsi_losses) / RSI_PERIOD
            s.rsi_ready = True
    else:
        s.rsi_avg_gain = (s.rsi_avg_gain * (RSI_PERIOD - 1) + gain) / RSI_PERIOD
        s.rsi_avg_loss = (s.rsi_avg_loss * (RSI_PERIOD - 1) + loss) / RSI_PERIOD
    if s.rsi_ready:
        s.rsi_value = (
            100.0 if s.rsi_avg_loss == 0
            else 100.0 - (100.0 / (1 + s.rsi_avg_gain / s.rsi_avg_loss))
        )


def _update_atr(s: IndicatorState, high: float, low: float, close: float) -> None:
    ATR_PERIOD = 14
    if s.atr_prev_close == 0:
        s.atr_prev_close = close
        return
    tr = max(high - low, abs(high - s.atr_prev_close), abs(low - s.atr_prev_close))
    s.atr_prev_close = close
    s.atr_count += 1
    if s.atr_count <= ATR_PERIOD:
        s.atr_buf.append(tr)
        if s.atr_count == ATR_PERIOD:
            s.atr_smooth = sum(s.atr_buf) / ATR_PERIOD
            s.atr_ready = True
    else:
        s.atr_smooth = (s.atr_smooth * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
    if s.atr_ready:
        s.atr_value = s.atr_smooth


def _update_bb(s: IndicatorState, close: float) -> None:
    s.bb_prices.append(close)
    if len(s.bb_prices) > s.bb_period:
        s.bb_prices.pop(0)
    if len(s.bb_prices) == s.bb_period:
        mean = sum(s.bb_prices) / s.bb_period
        variance = sum((p - mean) ** 2 for p in s.bb_prices) / s.bb_period
        std = math.sqrt(variance)
        s.bb_mid   = mean
        s.bb_upper = mean + 2 * std
        s.bb_lower = mean - 2 * std
        s.bb_ready = True


def _update_volume_zscore(s: IndicatorState, volume: float) -> None:
    s.vol_buf.append(volume)
    if len(s.vol_buf) > s.vol_period:
        s.vol_buf.pop(0)
    if len(s.vol_buf) == s.vol_period:
        mean = sum(s.vol_buf) / s.vol_period
        std  = math.sqrt(sum((v - mean) ** 2 for v in s.vol_buf) / s.vol_period)
        s.vol_zscore = (volume - mean) / std if std > 0 else 0.0
        s.vol_ready  = True


def _update_vwap(s: IndicatorState, close: float, high: float, low: float, volume: float) -> None:
    typical = (high + low + close) / 3
    s.vwap_cum_vol += volume
    s.vwap_cum_tpv += typical * volume
    if s.vwap_cum_vol > 0:
        s.vwap = s.vwap_cum_tpv / s.vwap_cum_vol
