"""Tests for performance metrics."""
from apex_scalper.performance import PerfMetrics


def test_basic():
    p = PerfMetrics()
    for pnl in [1.0, -0.5, 2.0, -0.3, 1.5]:
        p.record(pnl)
    assert p.win_rate == 60.0
    assert p.avg_win > 0
    assert p.avg_loss < 0
    assert p.profit_factor > 1
    assert p.max_drawdown >= 0


def test_sharpe_positive_trend():
    p = PerfMetrics()
    for _ in range(50):
        p.record(0.1)
    assert p.sharpe > 0


def test_expectancy():
    p = PerfMetrics()
    p.record(2.0)
    p.record(-1.0)
    assert p.expectancy == 0.5
