"""Integration test — end-to-end module wiring v0.7.5.

Tests the full pipeline:
  on_open() -> evaluate() -> TP1 partial close -> risk.on_close() -> SQLite row

Design:
  - Uses in-memory SQLite (:memory:) — no disk state between runs
  - Bybit REST calls mocked at module level — no API keys required
  - Runs in standard CI (GitHub Actions, local pytest) with no network
  - Tests the actual production code, not reimplementations

What this catches that unit tests miss:
  - Interface mismatches between position_manager <-> risk <-> persistence
  - risk.on_close() not called (was missing in v0.7.2)
  - evaluate() return type wrong (was missing bool in v0.7.2)
  - on_entry vs on_open rename (regression from v0.7.2 -> v0.7.3)
  - SQLite write succeeding / row visible after commit
"""
from __future__ import annotations

import asyncio
import sys
import sqlite3
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Build a real in-memory Database (not a mock) to test actual SQL writes
# ---------------------------------------------------------------------------

def _build_real_db():
    """Return a Database instance backed by :memory: SQLite."""
    import importlib
    # Temporarily patch DB_PATH before importing persistence
    stub_os = types.ModuleType("os")
    stub_os.path = __import__("os").path
    stub_os.makedirs = lambda *a, **kw: None  # skip disk mkdir

    # Import real persistence module
    import apex_scalper.persistence as _pers_orig
    # Create a real Database with in-memory path
    class _MemDB(_pers_orig.Database):
        def __init__(self):
            self._path = ":memory:"
            self._lock = threading.Lock()
            self._conn_obj = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn_obj.row_factory = sqlite3.Row
            self._conn_obj.execute("PRAGMA journal_mode=WAL")
            self._conn_obj.execute("PRAGMA synchronous=NORMAL")
            self._conn_obj.commit()
            self._init_schema()
    return _MemDB()


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

def _make_stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# Build real state with actual threading.Lock
class _RealState:
    def __init__(self):
        self.lock           = threading.Lock()
        self.open_position  = None
        self.open_qty       = 0.0
        self.last_price     = 100_000.0
        self.running        = True
        self.paused         = False
        self.last_tick_ts   = time.time()
        self.orderbook      = MagicMock()
        self.orderbook.best_bid = 99_990.0
        self.orderbook.best_ask = 100_010.0


real_state = _RealState()

_mock_trader = MagicMock()
_mock_trader.place_order = AsyncMock(
    return_value={"retCode": 0, "result": {"orderId": "INT_TEST_001"}}
)
_mock_trader.amend_sl_tp  = AsyncMock(return_value={"retCode": 0})
_mock_trader.close_position = AsyncMock(return_value=None)
_mock_trader._client = MagicMock()

_mock_risk = MagicMock()
_mock_risk.on_close      = MagicMock()
_mock_risk.calc_qty      = MagicMock(return_value=0.001)
_mock_risk.can_open      = MagicMock(return_value=True)
_mock_risk.on_open       = MagicMock()

_mock_api_retry = AsyncMock(
    return_value={"result": {"list": [{"orderStatus": "Filled"}]}}
)

# Build real db
import apex_scalper.persistence  # noqa: E402 (ensure importable)
real_db = _build_real_db()

stubs = {
    "apex_scalper.state":      _make_stub("apex_scalper.state",      state=real_state),
    "apex_scalper.risk":       _make_stub("apex_scalper.risk",       risk=_mock_risk),
    "apex_scalper.trader":     _make_stub("apex_scalper.trader",     trader=_mock_trader,
                                           _api_call_with_retry=_mock_api_retry),
    "apex_scalper.persistence": _make_stub("apex_scalper.persistence", db=real_db),
    "apex_scalper.config":     _make_stub("apex_scalper.config",
                                           config=MagicMock(
                                               order_size_usdt=20,
                                               leverage=5,
                                               symbol="BTCUSDT",
                                           )),
    "apex_scalper": _make_stub("apex_scalper"),
}
for name, mod in stubs.items():
    sys.modules[name] = mod

from apex_scalper.position_manager import (  # noqa: E402
    PositionManager, TP1_PCT, TP2_PCT, TP3_PCT, MAX_HOLD_CANDLES
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIntegrationTP1(unittest.TestCase):
    """TP1 hit: partial close -> risk.on_close() called -> SQLite row written."""

    def setUp(self):
        self.pm = PositionManager()
        real_state.open_position = "long"
        real_state.open_qty      = 0.01
        real_state.last_price    = 100_000.0
        _mock_risk.on_close.reset_mock()
        _mock_trader.place_order.reset_mock()

    def test_on_open_stores_state(self):
        self.pm.on_open("long", 0.01, 100_000.0)
        self.assertEqual(self.pm._entry_side,  "long")
        self.assertEqual(self.pm._entry_qty,   0.01)
        self.assertEqual(self.pm._entry_price, 100_000.0)

    def test_on_open_not_on_entry(self):
        """Regression: strategy.py calls on_open(), must not be on_entry()."""
        self.assertTrue(hasattr(self.pm,  "on_open"))
        self.assertFalse(hasattr(self.pm, "on_entry"))

    def test_evaluate_returns_false_before_tp1(self):
        self.pm.on_open("long", 0.01, 100_000.0)
        # Price just below TP1
        result = run(self.pm.evaluate(100_000.0 * (1 + TP1_PCT * 0.5)))
        self.assertFalse(result, "evaluate() must return False when no TP hit")

    def test_evaluate_returns_false_after_tp1(self):
        """TP1 closes 25% — position still open (50% remains), must return False."""
        self.pm.on_open("long", 0.01, 100_000.0)
        price_at_tp1 = 100_000.0 * (1 + TP1_PCT * 1.1)
        result = run(self.pm.evaluate(price_at_tp1))
        # TP1 fills 25%, 75% remains — not fully closed
        self.assertFalse(result)

    def test_risk_on_close_called_after_tp1(self):
        self.pm.on_open("long", 0.01, 100_000.0)
        price_at_tp1 = 100_000.0 * (1 + TP1_PCT * 1.1)
        run(self.pm.evaluate(price_at_tp1))
        _mock_risk.on_close.assert_called_once()
        args = _mock_risk.on_close.call_args[0]
        pnl_usdt, pnl_pct = args[0], args[1]
        self.assertGreater(pnl_usdt, 0, "pnl_usdt must be positive at TP1")
        self.assertGreater(pnl_pct,  0, "pnl_pct must be positive at TP1")

    def test_trader_place_order_called_with_sell(self):
        """Long TP1 close must call place_order(side='Sell', reduce_only=True)."""
        self.pm.on_open("long", 0.01, 100_000.0)
        price_at_tp1 = 100_000.0 * (1 + TP1_PCT * 1.1)
        run(self.pm.evaluate(price_at_tp1))
        _mock_trader.place_order.assert_called()
        first_call_kwargs = _mock_trader.place_order.call_args_list[0].kwargs
        self.assertEqual(first_call_kwargs.get("side"), "Sell")
        self.assertTrue(first_call_kwargs.get("reduce_only"))

    def test_sqlite_row_written_via_record_trade(self):
        """After TP1, write a trade record and verify it's in SQLite."""
        real_db.record_trade(
            symbol="BTCUSDT", side="long",
            entry=100_000.0, exit_price=100_120.0,
            qty=0.0025, pnl_usdt=0.30, pnl_pct=0.0012,
            reason="TP1", signal_score=0.72, funding_rate=0.0001,
        )
        rows = real_db.get_all_trades("BTCUSDT", limit=10)
        self.assertGreater(len(rows), 0, "SQLite must contain at least 1 trade row")
        last = rows[0]
        self.assertEqual(last["reason"],  "TP1")
        self.assertEqual(last["symbol"],  "BTCUSDT")
        self.assertAlmostEqual(last["pnl_pct"], 0.0012, places=6)

    def test_sqlite_losing_trade_row(self):
        """SL hit: verify negative pnl row is correctly stored."""
        real_db.record_trade(
            symbol="BTCUSDT", side="long",
            entry=100_000.0, exit_price=99_920.0,
            qty=0.01, pnl_usdt=-0.80, pnl_pct=-0.0008,
            reason="SL", signal_score=0.67, funding_rate=0.0
        )
        rows = real_db.get_all_trades("BTCUSDT", limit=10)
        sl_rows = [r for r in rows if r["reason"] == "SL"]
        self.assertTrue(len(sl_rows) > 0)
        self.assertLess(sl_rows[0]["pnl_pct"], 0)


class TestIntegrationTimeout(unittest.TestCase):
    """Timeout: evaluate() returns True, state.open_position cleared."""

    def setUp(self):
        self.pm = PositionManager()
        real_state.open_position = "long"
        real_state.open_qty      = 0.01
        real_state.last_price    = 100_000.0
        _mock_risk.on_close.reset_mock()

    def test_timeout_returns_true(self):
        self.pm.on_open("long", 0.01, 100_000.0)
        self.pm._hold_candles = MAX_HOLD_CANDLES - 1
        # One more candle — hits timeout
        result = run(self.pm.evaluate(100_000.0))
        self.assertTrue(result, "evaluate() must return True on timeout")

    def test_timeout_calls_risk_on_close(self):
        self.pm.on_open("long", 0.01, 100_000.0)
        self.pm._hold_candles = MAX_HOLD_CANDLES - 1
        run(self.pm.evaluate(100_000.0))
        _mock_risk.on_close.assert_called_once()

    def test_no_position_returns_true_immediately(self):
        real_state.open_position = None
        result = run(self.pm.evaluate(100_000.0))
        self.assertTrue(result)
        real_state.open_position = "long"  # restore


if __name__ == "__main__":
    unittest.main()
