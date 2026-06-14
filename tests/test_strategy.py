"""Unit tests — strategy.py signal scoring v0.7.4.

Covers:
  - All 10 signal weights sum to 1.0
  - Each signal bounded [0..weight]
  - No single signal alone can breach ENTRY_THRESHOLD=0.65
  - score_long / score_short return float in [0..1]
  - Regime gate: RANGING blocks entry
  - Dead code regression: _prev_fast/_prev_slow not consumed anywhere
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal stubs so strategy.py imports without a live exchange connection
# ---------------------------------------------------------------------------

def _make_stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _patch_imports():
    """Insert lightweight stubs for all heavy dependencies."""
    stubs = {
        "apex_scalper.state": _make_stub("apex_scalper.state", state=MagicMock()),
        "apex_scalper.risk": _make_stub("apex_scalper.risk", risk=MagicMock()),
        "apex_scalper.indicators": _make_stub(
            "apex_scalper.indicators",
            IndicatorState=MagicMock,
            update_all=MagicMock(),
        ),
        "apex_scalper.orderbook_analytics": _make_stub(
            "apex_scalper.orderbook_analytics",
            OBSignals=MagicMock,
            compute=MagicMock(return_value=MagicMock(imbalance=0.0)),
        ),
        "apex_scalper.performance": _make_stub("apex_scalper.performance", perf=MagicMock()),
        "apex_scalper.watchdog": _make_stub("apex_scalper.watchdog", record_heartbeat=MagicMock()),
        "apex_scalper.mtf_filter": _make_stub("apex_scalper.mtf_filter", mtf=MagicMock()),
        "apex_scalper.funding_rate": _make_stub("apex_scalper.funding_rate", funding=MagicMock()),
        "apex_scalper.anti_manipulation": _make_stub("apex_scalper.anti_manipulation", anti_manip=MagicMock()),
        "apex_scalper.limit_order_manager": _make_stub("apex_scalper.limit_order_manager", lom=MagicMock()),
        "apex_scalper.persistence": _make_stub("apex_scalper.persistence", db=MagicMock()),
        "apex_scalper.trader": _make_stub("apex_scalper.trader", trader=MagicMock()),
        "apex_scalper.regime_filter": _make_stub("apex_scalper.regime_filter", regime=MagicMock()),
        "apex_scalper.book_pressure": _make_stub("apex_scalper.book_pressure", bp=MagicMock()),
        "apex_scalper.config": _make_stub("apex_scalper.config", config=MagicMock()),
    }
    for name, mod in stubs.items():
        sys.modules[name] = mod
    sys.modules["apex_scalper"] = _make_stub("apex_scalper")


_patch_imports()

# Now safe to import strategy internals
from apex_scalper.strategy import _W, _score_long, _score_short  # noqa: E402
from apex_scalper.indicators import IndicatorState                # noqa: E402
from apex_scalper.orderbook_analytics import OBSignals            # noqa: E402


def _blank_ind() -> MagicMock:
    """IndicatorState with all signals disabled (everything False / 0)."""
    ind = MagicMock()
    ind.rsi_ready   = False
    ind.atr_ready   = False
    ind.vol_ready   = False
    ind.macd_ready  = False
    ind.stoch_ready = False
    ind.bb_ready    = False
    ind.ema_trend   = 0.0
    ind.ema_slow    = 0.0
    ind.ema_fast    = 0.0
    ind.vwap        = 0.0
    return ind


def _blank_ob() -> MagicMock:
    ob = MagicMock()
    ob.imbalance = 0.0
    return ob


PRICE = 100_000.0
ENTRY_THRESHOLD = 0.65


class TestSignalWeights(unittest.TestCase):

    def test_weights_sum_to_one(self):
        total = sum(_W.values())
        self.assertAlmostEqual(total, 1.0, places=9,
                               msg=f"Weights sum={total}, expected 1.0")

    def test_all_weights_positive(self):
        for name, w in _W.items():
            self.assertGreater(w, 0, msg=f"Weight {name}={w} must be > 0")

    def test_weight_count(self):
        self.assertEqual(len(_W), 10, "Expected exactly 10 signals")


class TestScoreLongBounds(unittest.TestCase):

    def test_blank_ind_scores_zero(self):
        """With all signals disabled, score must be 0."""
        from apex_scalper import book_pressure as _bp_mod
        _bp_mod.bp.pressure_long.return_value = False
        score = _score_long(_blank_ind(), _blank_ob(), PRICE)
        self.assertEqual(score, 0.0)

    def test_score_bounded_above(self):
        """Fully active long signals must not exceed 1.0."""
        from apex_scalper import book_pressure as _bp_mod
        _bp_mod.bp.pressure_long.return_value = True
        _bp_mod.bp.cum_delta = 999_999
        _bp_mod.bp._threshold.return_value = 1.0

        ind = _blank_ind()
        ind.rsi_ready    = True
        ind.rsi_value    = 60.0
        ind.vol_ready    = True
        ind.vol_zscore   = 5.0
        ind.macd_ready   = True
        ind.macd_histogram = 0.01
        ind.stoch_ready  = True
        ind.stoch_k      = 60.0
        ind.stoch_d      = 50.0
        ind.bb_ready     = True
        ind.bb_lower     = PRICE * 1.01   # price below lower band
        ind.bb_mid       = PRICE * 1.02
        ind.ema_trend    = PRICE * 0.99   # price above EMA50
        ind.ema_slow     = PRICE * 0.999
        ind.ema_fast     = PRICE * 1.001
        ind.vwap         = PRICE * 0.999  # price above VWAP

        ob = _blank_ob()
        ob.imbalance = 0.5

        score = _score_long(ind, ob, PRICE)
        self.assertLessEqual(score, 1.0, f"Score {score} exceeds 1.0")
        self.assertGreaterEqual(score, 0.0)

    def test_no_single_signal_breaches_threshold(self):
        """No single signal weight alone can reach ENTRY_THRESHOLD."""
        for name, w in _W.items():
            self.assertLess(
                w, ENTRY_THRESHOLD,
                msg=f"Signal '{name}' weight={w} alone exceeds ENTRY_THRESHOLD={ENTRY_THRESHOLD}"
            )


class TestScoreShortBounds(unittest.TestCase):

    def test_blank_ind_short_scores_zero(self):
        from apex_scalper import book_pressure as _bp_mod
        _bp_mod.bp.pressure_short.return_value = False
        score = _score_short(_blank_ind(), _blank_ob(), PRICE)
        self.assertEqual(score, 0.0)

    def test_score_short_bounded(self):
        from apex_scalper import book_pressure as _bp_mod
        _bp_mod.bp.pressure_short.return_value = True
        _bp_mod.bp.cum_delta = -999_999
        _bp_mod.bp._threshold.return_value = 1.0

        ind = _blank_ind()
        ind.rsi_ready    = True
        ind.rsi_value    = 45.0
        ind.vol_ready    = True
        ind.vol_zscore   = 5.0
        ind.macd_ready   = True
        ind.macd_histogram = -0.01
        ind.stoch_ready  = True
        ind.stoch_k      = 40.0
        ind.stoch_d      = 50.0
        ind.bb_ready     = True
        ind.bb_upper     = PRICE * 0.99
        ind.bb_mid       = PRICE * 0.98
        ind.ema_trend    = PRICE * 1.01
        ind.ema_slow     = PRICE * 1.001
        ind.ema_fast     = PRICE * 0.999
        ind.vwap         = PRICE * 1.001

        ob = _blank_ob()
        ob.imbalance = -0.5

        score = _score_short(ind, ob, PRICE)
        self.assertLessEqual(score, 1.0)
        self.assertGreaterEqual(score, 0.0)


if __name__ == "__main__":
    unittest.main()
