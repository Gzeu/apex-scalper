"""Unit tests for OrderBook."""
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
    ob.apply_delta("b", "50000", "0")
    assert ob.best_bid is None


def test_depth():
    ob = OrderBook()
    ob.apply_snapshot(
        [["50000", "1.0"], ["49999", "2.0"], ["49998", "0.5"]],
        [["50001", "1.5"], ["50002", "0.5"]],
    )
    assert abs(ob.bid_depth(3) - 3.5) < 0.001
    assert abs(ob.ask_depth(2) - 2.0) < 0.001


def test_spread_bps():
    ob = OrderBook()
    ob.apply_snapshot([["50000", "1.0"]], [["50005", "1.5"]])
    bps = ob.spread / ob.mid_price * 10_000
    assert 0 < bps < 2.0
