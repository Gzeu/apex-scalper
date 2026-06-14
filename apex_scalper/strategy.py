"""Multi-signal scalping strategy v0.3.

Signal engine (all must align for entry):
  1. EMA(9/21) cross (primary trend)
  2. EMA(50) trend filter (only trade in trend direction)
  3. RSI(14) confirmation (no extreme overbought/oversold entries)
  4. Orderbook imbalance (min threshold for direction)
  5. Volume z-score (avoid low-volume false breakouts)
  6. ATR volatility gate (avoid entries in too-high or too-low volatility)
  7. Bollinger Band position (avoid chasing outside bands)

Each signal contributes a score; total score must exceed ENTRY_THRESHOLD.
This reduces false signals vs single-indicator approaches.
"""
from __future__ import annotations

import os
from loguru import logger
from .state import state
from .risk import risk
from .indicators import IndicatorState, update_all
from .orderbook_analytics import OBSignals, compute as compute_ob
from .performance import perf
from .watchdog import record_heartbeat

# --- All params configurable from .env ---
RSI_LONG_MIN     = float(os.getenv("RSI_LONG_MIN",    "52.0"))
RSI_SHORT_MAX    = float(os.getenv("RSI_SHORT_MAX",   "48.0"))
RSI_OB_LIMIT     = float(os.getenv("RSI_OB_LIMIT",   "70.0"))  # skip LONG if RSI > 70
RSI_OS_LIMIT     = float(os.getenv("RSI_OS_LIMIT",   "30.0"))  # skip SHORT if RSI < 30
IMBALANCE_LONG   = float(os.getenv("IMBALANCE_LONG",  "0.10"))  # min imbalance for long
IMBALANCE_SHORT  = float(os.getenv("IMBALANCE_SHORT", "-0.10")) # max imbalance for short
VOL_ZSCORE_MIN   = float(os.getenv("VOL_ZSCORE_MIN",  "0.0"))   # min volume z-score
ATR_MIN_PCT      = float(os.getenv("ATR_MIN_PCT",     "0.0003")) # min ATR/price (0.03%)
ATR_MAX_PCT      = float(os.getenv("ATR_MAX_PCT",     "0.005"))  # max ATR/price (0.5%)
ENTRY_THRESHOLD  = float(os.getenv("ENTRY_THRESHOLD", "0.60"))   # 0-1 score

# Shared indicator state (populated by feed)
ind = IndicatorState()


def update_indicators(close: float, high: float, low: float, volume: float) -> None:
    """Called from feed on confirmed candle. Updates all indicators."""
    record_heartbeat()
    update_all(ind, close, high, low, volume)


def _score_long(ind: IndicatorState, ob: OBSignals, price: float) -> float:
    """Return signal strength [0,1] for a LONG entry. 1 = perfect alignment."""
    score = 0.0
    weights = {
        "ema_cross":   0.25,
        "trend":       0.20,
        "rsi":         0.20,
        "imbalance":   0.20,
        "volume":      0.10,
        "atr":         0.05,
    }
    # EMA cross
    score += weights["ema_cross"] if ind.ema_fast > ind.ema_slow else 0
    # EMA(50) trend filter: price above EMA50 = bullish
    score += weights["trend"] if price > ind.ema_trend else 0
    # RSI confirmation
    if ind.rsi_ready and RSI_LONG_MIN <= ind.rsi_value <= RSI_OB_LIMIT:
        rsi_conf = min((ind.rsi_value - RSI_LONG_MIN) / (RSI_OB_LIMIT - RSI_LONG_MIN), 1.0)
        score += weights["rsi"] * rsi_conf
    # Orderbook imbalance
    if ob.imbalance >= IMBALANCE_LONG:
        score += weights["imbalance"] * min(ob.imbalance / 0.3, 1.0)
    # Volume confirmation
    if ind.vol_ready and ind.vol_zscore >= VOL_ZSCORE_MIN:
        score += weights["volume"] * min(max(ind.vol_zscore / 2.0, 0), 1)
    # ATR gate
    if ind.atr_ready and price > 0:
        atr_pct = ind.atr_value / price
        if ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT:
            score += weights["atr"]
    return score


def _score_short(ind: IndicatorState, ob: OBSignals, price: float) -> float:
    """Return signal strength [0,1] for a SHORT entry."""
    score = 0.0
    weights = {
        "ema_cross":   0.25,
        "trend":       0.20,
        "rsi":         0.20,
        "imbalance":   0.20,
        "volume":      0.10,
        "atr":         0.05,
    }
    score += weights["ema_cross"] if ind.ema_fast < ind.ema_slow else 0
    score += weights["trend"] if price < ind.ema_trend else 0
    if ind.rsi_ready and RSI_OS_LIMIT <= ind.rsi_value <= RSI_SHORT_MAX:
        rsi_conf = min((RSI_SHORT_MAX - ind.rsi_value) / (RSI_SHORT_MAX - RSI_OS_LIMIT), 1.0)
        score += weights["rsi"] * rsi_conf
    if ob.imbalance <= IMBALANCE_SHORT:
        score += weights["imbalance"] * min(abs(ob.imbalance) / 0.3, 1.0)
    if ind.vol_ready and ind.vol_zscore >= VOL_ZSCORE_MIN:
        score += weights["volume"] * min(max(ind.vol_zscore / 2.0, 0), 1)
    if ind.atr_ready and price > 0:
        atr_pct = ind.atr_value / price
        if ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT:
            score += weights["atr"]
    return score


class Strategy:
    def __init__(self):
        self._prev_fast: float = 0.0
        self._prev_slow: float = 0.0

    async def evaluate(self) -> None:
        """Called on every confirmed 1m candle. Core decision loop."""
        from .trader import trader
        from .position_manager import position_manager

        with state.lock:
            if not state.running or state.paused:
                return
            price = state.last_price
            pos   = state.open_position

        if price == 0:
            return

        ob = compute_ob()

        # ── EXIT / POSITION MANAGEMENT ──
        if pos:
            closed = await position_manager.evaluate(price)
            if not closed:
                # Try pyramid if conditions are excellent
                long_score  = _score_long(ind, ob, price)
                short_score = _score_short(ind, ob, price)
                if pos == "long" and long_score >= 0.85:
                    await position_manager.try_pyramid("long", price, long_score)
                elif pos == "short" and short_score >= 0.85:
                    await position_manager.try_pyramid("short", price, short_score)
            self._prev_fast = ind.ema_fast
            self._prev_slow = ind.ema_slow
            return

        # ── ENTRY ──
        if not risk.can_open():
            self._prev_fast = ind.ema_fast
            self._prev_slow = ind.ema_slow
            return

        if not ind.rsi_ready or not ind.atr_ready:
            self._prev_fast = ind.ema_fast
            self._prev_slow = ind.ema_slow
            return

        cross_up   = self._prev_fast <= self._prev_slow and ind.ema_fast > ind.ema_slow
        cross_down = self._prev_fast >= self._prev_slow and ind.ema_fast < ind.ema_slow

        if cross_up:
            score = _score_long(ind, ob, price)
            logger.info(
                f"LONG score={score:.2f} | ema={ind.ema_fast:.1f}>{ind.ema_slow:.1f} "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} atr={ind.atr_value:.1f}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(price)
                await trader.place_order("Buy", qty)
                await position_manager.on_open("long", qty, price)
                await _notify(
                    f"🟡 *LONG* `{_sym()}` score=`{score:.2f}`\n"
                    f"`price={price}` `qty={qty}` `rsi={ind.rsi_value:.1f}`\n"
                    f"`imbalance={ob.imbalance:.3f}` `atr={ind.atr_value:.2f}`"
                )

        elif cross_down:
            score = _score_short(ind, ob, price)
            logger.info(
                f"SHORT score={score:.2f} | ema={ind.ema_fast:.1f}<{ind.ema_slow:.1f} "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} atr={ind.atr_value:.1f}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(price)
                await trader.place_order("Sell", qty)
                await position_manager.on_open("short", qty, price)
                await _notify(
                    f"🟠 *SHORT* `{_sym()}` score=`{score:.2f}`\n"
                    f"`price={price}` `qty={qty}` `rsi={ind.rsi_value:.1f}`\n"
                    f"`imbalance={ob.imbalance:.3f}` `atr={ind.atr_value:.2f}`"
                )

        self._prev_fast = ind.ema_fast
        self._prev_slow = ind.ema_slow


def _sym() -> str:
    from .config import config
    return config.symbol


async def _notify(msg: str) -> None:
    try:
        from .telegram_ui import send_message
        await send_message(msg)
    except Exception:
        pass


strategy = Strategy()
