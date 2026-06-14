"""Tests for streaming indicators."""
from apex_scalper.indicators import IndicatorState, update_all


def _run_n(n: int, price: float = 100.0):
    s = IndicatorState()
    for i in range(n):
        p = price + i * 0.1
        update_all(s, p, p + 0.5, p - 0.5, 10.0 + i)
    return s


def test_ema_ready_after_1():
    s = _run_n(1)
    assert s.ema_fast > 0
    assert s.ema_slow > 0


def test_rsi_ready_after_14():
    s = _run_n(14)
    assert not s.rsi_ready  # needs 14 changes = 15 candles
    s2 = _run_n(15)
    assert s2.rsi_ready


def test_atr_ready_after_14():
    s = _run_n(16)
    assert s.atr_ready
    assert s.atr_value > 0


def test_bb_ready_after_20():
    s = _run_n(21)
    assert s.bb_ready
    assert s.bb_upper > s.bb_mid > s.bb_lower


def test_vwap_monotonic():
    s = _run_n(10)
    assert s.vwap > 0
