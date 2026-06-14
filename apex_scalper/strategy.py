"""Multi-signal scalping strategy v0.4.0.

New vs v0.3.1:
  + MTF confirmation: 15m EMA50 trend filter (mtf_filter.py)
  + Funding rate check: skip entry if rate unfavorable (funding_rate.py)
  + Limit order entry: PostOnly Limit with Market fallback (limit_order_manager.py)
  + Anti-manipulation filter: spoof/ignition detection (anti_manipulation.py)
  + Persistence: signal_score logged to DB on each trade

Signal engine (all must align for entry):
  1. EMA(9/21) cross (primary trend)
  2. EMA(50) trend filter on 1m (only trade in trend direction)
  3. RSI(14) confirmation
  4. Orderbook imbalance
  5. Volume z-score
  6. ATR volatility gate
  7. MTF: 15m EMA50 confirmation (NEW)
  8. Funding rate awareness (NEW)
  9. Anti-manipulation clear (NEW)
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
from .mtf_filter import mtf
from .funding_rate import funding
from .anti_manipulation import anti_manip
from .limit_order_manager import lom
from .persistence import db

# --- All params configurable from .env / injected by inject_profile() ---
RSI_LONG_MIN     = float(os.getenv("RSI_LONG_MIN",    "52.0"))
RSI_SHORT_MAX    = float(os.getenv("RSI_SHORT_MAX",   "48.0"))
RSI_OB_LIMIT     = float(os.getenv("RSI_OB_LIMIT",   "70.0"))
RSI_OS_LIMIT     = float(os.getenv("RSI_OS_LIMIT",   "30.0"))
IMBALANCE_LONG   = float(os.getenv("IMBALANCE_LONG",  "0.10"))
IMBALANCE_SHORT  = float(os.getenv("IMBALANCE_SHORT", "-0.10"))
VOL_ZSCORE_MIN   = float(os.getenv("VOL_ZSCORE_MIN",  "0.0"))
ATR_MIN_PCT      = float(os.getenv("ATR_MIN_PCT",     "0.0003"))
ATR_MAX_PCT      = float(os.getenv("ATR_MAX_PCT",     "0.005"))
ENTRY_THRESHOLD  = float(os.getenv("ENTRY_THRESHOLD", "0.60"))
USE_LIMIT_ORDERS = os.getenv("USE_LIMIT_ORDERS", "true").lower() == "true"

ind = IndicatorState()


def update_indicators(close: float, high: float, low: float, volume: float) -> None:
    """Called from feed on confirmed candle."""
    record_heartbeat()
    update_all(ind, close, high, low, volume)
    # Run anti-manipulation analysis on each candle
    anti_manip.analyze(vol_zscore=ind.vol_zscore if ind.vol_ready else 0.0, current_close=close)


def _score_long(ind: IndicatorState, ob: OBSignals, price: float) -> float:
    score = 0.0
    weights = {"ema_cross": 0.25, "trend": 0.20, "rsi": 0.20,
               "imbalance": 0.20, "volume": 0.10, "atr": 0.05}
    score += weights["ema_cross"] if ind.ema_fast > ind.ema_slow else 0
    score += weights["trend"] if price > ind.ema_trend else 0
    if ind.rsi_ready and RSI_LONG_MIN <= ind.rsi_value <= RSI_OB_LIMIT:
        rsi_conf = min((ind.rsi_value - RSI_LONG_MIN) / (RSI_OB_LIMIT - RSI_LONG_MIN), 1.0)
        score += weights["rsi"] * rsi_conf
    if ob.imbalance >= IMBALANCE_LONG:
        score += weights["imbalance"] * min(ob.imbalance / 0.3, 1.0)
    if ind.vol_ready and ind.vol_zscore >= VOL_ZSCORE_MIN:
        score += weights["volume"] * min(max(ind.vol_zscore / 2.0, 0), 1)
    if ind.atr_ready and price > 0:
        atr_pct = ind.atr_value / price
        if ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT:
            score += weights["atr"]
    return score


def _score_short(ind: IndicatorState, ob: OBSignals, price: float) -> float:
    score = 0.0
    weights = {"ema_cross": 0.25, "trend": 0.20, "rsi": 0.20,
               "imbalance": 0.20, "volume": 0.10, "atr": 0.05}
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
                long_score  = _score_long(ind, ob, price)
                short_score = _score_short(ind, ob, price)
                if pos == "long" and long_score >= 0.85:
                    await position_manager.try_pyramid("long", price, long_score)
                elif pos == "short" and short_score >= 0.85:
                    await position_manager.try_pyramid("short", price, short_score)
            self._prev_fast = ind.ema_fast
            self._prev_slow = ind.ema_slow
            return

        # ── ENTRY GUARDS ──
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
            # NEW: MTF + funding + anti-manipulation checks for LONG
            if not mtf.allow_long(price):
                logger.debug(f"MTF blocks LONG: price={price:.2f} < EMA50(15m)={mtf.ema50:.2f}")
                self._prev_fast = ind.ema_fast
                self._prev_slow = ind.ema_slow
                return
            if not funding.can_enter_long():
                logger.debug(f"Funding blocks LONG: rate={funding.rate_pct}")
                self._prev_fast = ind.ema_fast
                self._prev_slow = ind.ema_slow
                return
            if not anti_manip.clear_for_entry("long"):
                logger.debug("Anti-manipulation blocks LONG")
                self._prev_fast = ind.ema_fast
                self._prev_slow = ind.ema_slow
                return

            score = _score_long(ind, ob, price)
            logger.info(
                f"LONG score={score:.2f} | ema={ind.ema_fast:.1f}>{ind.ema_slow:.1f} "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} "
                f"mtf={'✓' if mtf.ready else '?'} fund={funding.rate_pct}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(price)
                # NEW: Limit order entry (PostOnly) with Market fallback
                if USE_LIMIT_ORDERS:
                    ok, filled_qty, avg_price = await lom.place_entry("Buy", qty)
                else:
                    resp = await _market_entry("Buy", qty)
                    ok, filled_qty, avg_price = resp.get("retCode") == 0, qty, price

                if ok and filled_qty > 0:
                    from .position_manager import position_manager
                    await position_manager.on_open("long", filled_qty, avg_price)
                    db.record_trade(
                        symbol=_sym(), side="long",
                        entry=avg_price, exit_price=0,
                        qty=filled_qty, pnl_usdt=0, pnl_pct=0,
                        reason="OPEN", signal_score=score,
                        funding_rate=funding.rate,
                    )
                    await _notify(
                        f"🟡 *LONG* `{_sym()}` score=`{score:.2f}`\n"
                        f"`price={avg_price}` `qty={filled_qty}` "
                        f"`rsi={ind.rsi_value:.1f}`\n"
                        f"`imb={ob.imbalance:.3f}` `fund={funding.rate_pct}` "
                        f"`mtf={'✓' if mtf.ready else '?'}`"
                    )

        elif cross_down:
            # NEW: MTF + funding + anti-manipulation checks for SHORT
            if not mtf.allow_short(price):
                logger.debug(f"MTF blocks SHORT: price={price:.2f} > EMA50(15m)={mtf.ema50:.2f}")
                self._prev_fast = ind.ema_fast
                self._prev_slow = ind.ema_slow
                return
            if not funding.can_enter_short():
                logger.debug(f"Funding blocks SHORT: rate={funding.rate_pct}")
                self._prev_fast = ind.ema_fast
                self._prev_slow = ind.ema_slow
                return
            if not anti_manip.clear_for_entry("short"):
                logger.debug("Anti-manipulation blocks SHORT")
                self._prev_fast = ind.ema_fast
                self._prev_slow = ind.ema_slow
                return

            score = _score_short(ind, ob, price)
            logger.info(
                f"SHORT score={score:.2f} | ema={ind.ema_fast:.1f}<{ind.ema_slow:.1f} "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} "
                f"mtf={'✓' if mtf.ready else '?'} fund={funding.rate_pct}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(price)
                if USE_LIMIT_ORDERS:
                    ok, filled_qty, avg_price = await lom.place_entry("Sell", qty)
                else:
                    resp = await _market_entry("Sell", qty)
                    ok, filled_qty, avg_price = resp.get("retCode") == 0, qty, price

                if ok and filled_qty > 0:
                    from .position_manager import position_manager
                    await position_manager.on_open("short", filled_qty, avg_price)
                    db.record_trade(
                        symbol=_sym(), side="short",
                        entry=avg_price, exit_price=0,
                        qty=filled_qty, pnl_usdt=0, pnl_pct=0,
                        reason="OPEN", signal_score=score,
                        funding_rate=funding.rate,
                    )
                    await _notify(
                        f"🟠 *SHORT* `{_sym()}` score=`{score:.2f}`\n"
                        f"`price={avg_price}` `qty={filled_qty}` "
                        f"`rsi={ind.rsi_value:.1f}`\n"
                        f"`imb={ob.imbalance:.3f}` `fund={funding.rate_pct}` "
                        f"`mtf={'✓' if mtf.ready else '?'}`"
                    )

        self._prev_fast = ind.ema_fast
        self._prev_slow = ind.ema_slow


async def _market_entry(side: str, qty: float) -> dict:
    from .trader import trader
    return await trader.place_order(side, qty, order_type="Market")


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
