"""Unit tests for OrderBook delta logic."""
import pytest
from apex_scalper.state import OrderBook


def test_snapshot():
    ob = OrderBook()
    ob.apply_snapshot([["50000", "1.0"], ["49999", "0.5"]], [["50001", "1.5"]])
    assert ob.best_bid == 50000.0
    assert ob.best_ask == 50001.0
    assert abs(ob.mid_price - 50000.5) < 0.001


def test_delta_remove():
    ob = OrderBook()
    ob.apply_snapshot([["50000", "1.0"]], [["50001", "1.5"]])
    ob.apply_delta("b", "50000", "0")  # remove
    assert ob.best_bid is None


def test_spread_bps():
    ob = OrderBook()
    ob.apply_snapshot([["50000", "1.0"]], [["50005", "1.5"]])
    mid = ob.mid_price
    spread_bps = (ob.spread / mid) * 10_000
    assert 0 < spread_bps < 2.0
