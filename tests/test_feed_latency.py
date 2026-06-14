"""Unit tests — feed.py latency guard v0.7.4.

Covers:
  - _handle_kline() skips strategy.evaluate() when feed is stale
  - _handle_kline() calls strategy.evaluate() when feed is fresh
  - _handle_orderbook() updates state.last_tick_ts on every message
"""
from __future__ import annotations

import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# Minimal state mock with last_tick_ts
_mock_state = MagicMock()
_mock_state.lock = __import__("threading").Lock()
_mock_state.last_price = 100_000.0
_mock_state.last_tick_ts = time.time()  # fresh by default

stubs = {
    "apex_scalper.config": _make_stub("apex_scalper.config", config=MagicMock(symbol="BTCUSDT", testnet=True)),
    "apex_scalper.state": _make_stub("apex_scalper.state", state=_mock_state),
    "apex_scalper.book_pressure": _make_stub("apex_scalper.book_pressure", bp=MagicMock()),
    "apex_scalper.strategy": _make_stub(
        "apex_scalper.strategy",
        update_indicators=MagicMock(),
        strategy=MagicMock(),
    ),
    "apex_scalper.watchdog": _make_stub("apex_scalper.watchdog", feed_restart_needed=MagicMock(return_value=False)),
    "pybit.unified_trading": _make_stub("pybit.unified_trading", WebSocket=MagicMock()),
    "apex_scalper": _make_stub("apex_scalper"),
}
for name, mod in stubs.items():
    sys.modules[name] = mod

import apex_scalper.feed as feed_module  # noqa: E402


CANDLE_MSG = {
    "data": [{
        "confirm": True,
        "close": "100000",
        "high": "100500",
        "low": "99500",
        "volume": "123.45",
    }]
}


class TestFeedLatencyGuard(unittest.TestCase):

    def test_fresh_feed_dispatches_strategy(self):
        """Fresh tick (< FEED_STALE_S ago) — strategy.evaluate() must be scheduled."""
        _mock_state.last_tick_ts = time.time()  # fresh
        from apex_scalper import strategy as strat_mod
        strat_mod.update_indicators.reset_mock()

        feed_module._handle_kline(CANDLE_MSG)
        strat_mod.update_indicators.assert_called_once()

    def test_stale_feed_blocks_strategy(self):
        """Stale tick (> FEED_STALE_S ago) — strategy.evaluate() must NOT be scheduled."""
        _mock_state.last_tick_ts = time.time() - (feed_module.FEED_STALE_S + 5.0)
        from apex_scalper import strategy as strat_mod
        strat_mod.update_indicators.reset_mock()

        feed_module._handle_kline(CANDLE_MSG)
        strat_mod.update_indicators.assert_not_called()

    def test_ob_handler_updates_timestamp(self):
        """Every OB message must update state.last_tick_ts."""
        old_ts = time.time() - 10.0
        _mock_state.last_tick_ts = old_ts

        ob_msg = {
            "type": "delta",
            "data": {"b": [["100000", "1.5"]], "a": []}
        }
        # Patch orderbook methods
        _mock_state.orderbook = MagicMock()
        _mock_state.orderbook.top_bids.return_value = []
        _mock_state.orderbook.top_asks.return_value = []

        feed_module._handle_orderbook(ob_msg)
        self.assertGreater(
            _mock_state.last_tick_ts, old_ts,
            "last_tick_ts must be updated after OB message"
        )


if __name__ == "__main__":
    unittest.main()
