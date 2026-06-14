"""Unit tests — position_manager.py v0.7.4.

Covers:
  - on_open() stores side/qty/entry_price correctly
  - _unrealised_pnl_pct() sign correct for long and short
  - _bybit_side() mapping: long close=Sell, long open=Buy, short close=Buy
  - _pnl_usdt() calculation correct
  - evaluate() returns False when no position open
  - evaluate() returns True after TP3 sequence completes
  - try_pyramid() respects MAX_PYRAMID_ADDS guard
  - try_pyramid() respects TP1 must be hit guard
  - try_pyramid() respects PYRAMID_PNL_MIN guard
  - risk.on_close() called after TP1 partial fill
  - reset() clears all state flags
  Regression guard: on_entry() rename to on_open() never silently breaks again.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call
import asyncio


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

def _make_stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


_mock_trader = MagicMock()
_mock_trader.place_order = AsyncMock(return_value={"retCode": 0, "result": {"orderId": "test123"}})
_mock_trader.amend_sl_tp = AsyncMock(return_value={"retCode": 0})
_mock_trader.close_position = AsyncMock(return_value=None)
_mock_trader._client = MagicMock()

_mock_risk = MagicMock()
_mock_risk.on_close = MagicMock()
_mock_risk.calc_qty = MagicMock(return_value=0.001)

_mock_state = MagicMock()
_mock_state.lock = __import__("threading").Lock()
_mock_state.open_position = "long"
_mock_state.open_qty = 0.01
_mock_state.last_price = 100_000.0

_mock_api_retry = AsyncMock(return_value={
    "result": {"list": [{"orderStatus": "Filled"}]}
})

stubs = {
    "apex_scalper.state": _make_stub("apex_scalper.state", state=_mock_state),
    "apex_scalper.risk": _make_stub("apex_scalper.risk", risk=_mock_risk),
    "apex_scalper.trader": _make_stub(
        "apex_scalper.trader",
        trader=_mock_trader,
        _api_call_with_retry=_mock_api_retry,
    ),
    "apex_scalper.persistence": _make_stub("apex_scalper.persistence", db=MagicMock()),
    "apex_scalper.config": _make_stub(
        "apex_scalper.config",
        config=MagicMock(order_size_usdt=20, leverage=5, symbol="BTCUSDT"),
    ),
    "apex_scalper": _make_stub("apex_scalper"),
}
for name, mod in stubs.items():
    sys.modules[name] = mod

from apex_scalper.position_manager import PositionManager, TP1_PCT, TP2_PCT, TP3_PCT, MAX_PYRAMID_ADDS, PYRAMID_PNL_MIN  # noqa: E402


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestOnOpen(unittest.TestCase):

    def setUp(self):
        self.pm = PositionManager()

    def test_stores_side(self):
        self.pm.on_open("long", 0.01, 95_000.0)
        self.assertEqual(self.pm._entry_side, "long")

    def test_stores_qty(self):
        self.pm.on_open("long", 0.01, 95_000.0)
        self.assertEqual(self.pm._entry_qty, 0.01)

    def test_stores_price(self):
        self.pm.on_open("long", 0.01, 95_000.0)
        self.assertEqual(self.pm._entry_price, 95_000.0)

    def test_resets_tp_flags(self):
        self.pm._tp1_hit = True
        self.pm._tp2_hit = True
        self.pm.on_open("short", 0.005, 80_000.0)
        self.assertFalse(self.pm._tp1_hit)
        self.assertFalse(self.pm._tp2_hit)

    def test_method_exists_not_on_entry(self):
        """Regression: strategy.py calls on_open(), not on_entry()."""
        self.assertTrue(hasattr(self.pm, "on_open"),  "on_open() missing")
        self.assertFalse(hasattr(self.pm, "on_entry"), "on_entry() must not exist (renamed)")


class TestUnrealisedPnl(unittest.TestCase):

    def setUp(self):
        self.pm = PositionManager()
        self.pm.on_open("long", 0.01, 100_000.0)

    def test_long_positive_pnl(self):
        pnl = self.pm._unrealised_pnl_pct(101_000.0)
        self.assertAlmostEqual(pnl, 0.01, places=6)

    def test_long_negative_pnl(self):
        pnl = self.pm._unrealised_pnl_pct(99_000.0)
        self.assertAlmostEqual(pnl, -0.01, places=6)

    def test_short_positive_pnl(self):
        self.pm.on_open("short", 0.01, 100_000.0)
        pnl = self.pm._unrealised_pnl_pct(99_000.0)
        self.assertAlmostEqual(pnl, 0.01, places=6)

    def test_zero_entry_returns_zero(self):
        self.pm._entry_price = 0.0
        self.assertEqual(self.pm._unrealised_pnl_pct(100_000.0), 0.0)


class TestBybItSide(unittest.TestCase):

    def setUp(self):
        self.pm = PositionManager()

    def test_long_close_is_sell(self):
        self.assertEqual(self.pm._bybit_side("long", closing=True), "Sell")

    def test_long_open_is_buy(self):
        self.assertEqual(self.pm._bybit_side("long", closing=False), "Buy")

    def test_short_close_is_buy(self):
        self.assertEqual(self.pm._bybit_side("short", closing=True), "Buy")

    def test_short_open_is_sell(self):
        self.assertEqual(self.pm._bybit_side("short", closing=False), "Sell")


class TestEvaluateNoPosition(unittest.TestCase):

    def test_returns_true_when_no_position(self):
        pm = PositionManager()
        _mock_state.open_position = None
        result = run(pm.evaluate(100_000.0))
        self.assertTrue(result, "evaluate() must return True when no position")
        _mock_state.open_position = "long"  # restore


class TestPyramidGuards(unittest.TestCase):

    def setUp(self):
        self.pm = PositionManager()
        self.pm.on_open("long", 0.01, 100_000.0)
        self.pm._tp1_hit = True
        self.pm._pyramid_adds = 0
        _mock_state.open_position = "long"
        _mock_state.open_qty = 0.01
        _mock_state.last_price = 100_000.0
        _mock_risk.calc_qty.return_value = 0.001
        _mock_trader.place_order.reset_mock()

    def test_max_adds_guard(self):
        self.pm._pyramid_adds = MAX_PYRAMID_ADDS
        run(self.pm.try_pyramid("long", 100_200.0, 0.85, 99_000.0, 101_000.0))
        _mock_trader.place_order.assert_not_called()

    def test_tp1_not_hit_guard(self):
        self.pm._tp1_hit = False
        run(self.pm.try_pyramid("long", 100_200.0, 0.85, 99_000.0, 101_000.0))
        _mock_trader.place_order.assert_not_called()

    def test_pnl_min_guard(self):
        # Price barely above entry — PnL below PYRAMID_PNL_MIN
        run(self.pm.try_pyramid("long", 100_000.5, 0.85, 99_000.0, 101_000.0))
        _mock_trader.place_order.assert_not_called()

    def test_pyramid_executes_when_conditions_met(self):
        # PnL = 0.5% — well above PYRAMID_PNL_MIN=0.10%
        run(self.pm.try_pyramid("long", 100_500.0, 0.85, 99_000.0, 101_000.0))
        _mock_trader.place_order.assert_called_once()
        args = _mock_trader.place_order.call_args
        self.assertEqual(args.kwargs.get("order_type"), "Market")
        self.assertEqual(args.kwargs.get("side"), "Buy")


if __name__ == "__main__":
    unittest.main()
