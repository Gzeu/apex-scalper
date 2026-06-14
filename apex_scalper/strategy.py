"""Multi-signal scalping strategy v0.4.1.

Changes vs v0.4.0:
  + BB score added to _score_long/_score_short (signal was documented but missing)
    LONG:  price near lower BB = oversold zone entry bonus
    SHORT: price near upper BB = overbought zone entry bonus
  + VWAP bias added: price > VWAP = bullish session bias (+weight 0.07)
  + Weights rebalanced (total still = 1.0):
    ema_cross=0.23, trend=0.18, rsi=0.18, imbalance=0.18,
    volume=0.10, atr=0.03, bb=0.05, vwap=0.05
  + MTF, funding, anti-manipulation checks retained from v0.4.0
  + Limit order entry (PostOnly + cancel-replace v0.4.1) retained

Signal engine (9 signals):
  1. EMA(9/21) cross
  2. EMA(50) 1m trend filter
  3. RSI(14) confirmation
  4. Orderbook imbalance
  5. Volume z-score
  6. ATR volatility gate
  7. Bollinger Band position  ← now implemented
  8. VWAP session bias        ← now implemented
  [GUARDS]
  9. MTF 15m EMA50 confirmation
  10. Funding rate awareness
  11. Anti-manipulation filter
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

# --- Params from .env / injected by inject_profile() ---
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

# Signal weights — must sum to 1.0
# ema_cross + trend + rsi + imbalance + volume + atr + bb + vwap = 1.0
_W = {
    "ema_cross":  0.23,
    "trend":      0.18,
    "rsi":        0.18,
    "imbalance":  0.18,
    "volume":     0.10,
    "atr":        0.03,
    "bb":         0.05,  # NEW v0.4.1
    "vwap":       0.05,  # NEW v0.4.1
}
assert abs(sum(_W.values()) - 1.0) < 1e-9, f"Weights must sum to 1.0, got {sum(_W.values())}"

ind = IndicatorState()


def update_indicators(close: float, high: float, low: float, volume: float) -> None:
    record_heartbeat()
    update_all(ind, close, high, low, volume)
    anti_manip.analyze(vol_zscore=ind.vol_zscore if ind.vol_ready else 0.0, current_close=close)


def _score_long(ind: IndicatorState, ob: OBSignals, price: float) -> float:
    """Return signal strength [0,1] for LONG entry."""
    score = 0.0

    # 1. EMA cross: fast > slow
    score += _W["ema_cross"] if ind.ema_fast > ind.ema_slow else 0

    # 2. EMA50 1m trend: price above EMA50
    score += _W["trend"] if price > ind.ema_trend else 0

    # 3. RSI confirmation
    if ind.rsi_ready and RSI_LONG_MIN <= ind.rsi_value <= RSI_OB_LIMIT:
        rsi_conf = min((ind.rsi_value - RSI_LONG_MIN) / (RSI_OB_LIMIT - RSI_LONG_MIN), 1.0)
        score += _W["rsi"] * rsi_conf

    # 4. Orderbook imbalance
    if ob.imbalance >= IMBALANCE_LONG:
        score += _W["imbalance"] * min(ob.imbalance / 0.3, 1.0)

    # 5. Volume z-score
    if ind.vol_ready and ind.vol_zscore >= VOL_ZSCORE_MIN:
        score += _W["volume"] * min(max(ind.vol_zscore / 2.0, 0), 1)

    # 6. ATR gate
    if ind.atr_ready and price > 0:
        atr_pct = ind.atr_value / price
        if ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT:
            score += _W["atr"]

    # 7. Bollinger Band position: LONG bonus if price near/at lower band
    #    Full score if price <= lower band (oversold zone)
    #    Partial score if price between lower and midline
    if ind.bb_ready and ind.bb_mid > ind.bb_lower:
        if price <= ind.bb_lower:
            score += _W["bb"]
        elif price < ind.bb_mid:
            # Linear interpolation: lower=1.0, mid=0.0
            bb_conf = (ind.bb_mid - price) / (ind.bb_mid - ind.bb_lower)
            score += _W["bb"] * min(bb_conf, 1.0)

    # 8. VWAP session bias: price above VWAP = bullish session
    if ind.vwap > 0:
        if price > ind.vwap:
            score += _W["vwap"]
        else:
            # Partial: within 0.1% below VWAP still gets partial credit
            gap = (ind.vwap - price) / ind.vwap
            if gap < 0.001:
                score += _W["vwap"] * (1 - gap / 0.001)

    return score


def _score_short(ind: IndicatorState, ob: OBSignals, price: float) -> float:
    """Return signal strength [0,1] for SHORT entry."""
    score = 0.0

    # 1. EMA cross: fast < slow
    score += _W["ema_cross"] if ind.ema_fast < ind.ema_slow else 0

    # 2. EMA50 1m trend: price below EMA50
    score += _W["trend"] if price < ind.ema_trend else 0

    # 3. RSI confirmation
    if ind.rsi_ready and RSI_OS_LIMIT <= ind.rsi_value <= RSI_SHORT_MAX:
        rsi_conf = min((RSI_SHORT_MAX - ind.rsi_value) / (RSI_SHORT_MAX - RSI_OS_LIMIT), 1.0)
        score += _W["rsi"] * rsi_conf

    # 4. Orderbook imbalance
    if ob.imbalance <= IMBALANCE_SHORT:
        score += _W["imbalance"] * min(abs(ob.imbalance) / 0.3, 1.0)

    # 5. Volume z-score
    if ind.vol_ready and ind.vol_zscore >= VOL_ZSCORE_MIN:
        score += _W["volume"] * min(max(ind.vol_zscore / 2.0, 0), 1)

    # 6. ATR gate
    if ind.atr_ready and price > 0:
        atr_pct = ind.atr_value / price
        if ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT:
            score += _W["atr"]

    # 7. Bollinger Band position: SHORT bonus if price near/at upper band
    if ind.bb_ready and ind.bb_upper > ind.bb_mid:
        if price >= ind.bb_upper:
            score += _W["bb"]
        elif price > ind.bb_mid:
            bb_conf = (price - ind.bb_mid) / (ind.bb_upper - ind.bb_mid)
            score += _W["bb"] * min(bb_conf, 1.0)

    # 8. VWAP session bias: price below VWAP = bearish session
    if ind.vwap > 0:
        if price < ind.vwap:
            score += _W["vwap"]
        else:
            # Partial: within 0.1% above VWAP still gets partial credit
            gap = (price - ind.vwap) / ind.vwap
            if gap < 0.001:
                score += _W["vwap"] * (1 - gap / 0.001)

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
            if not mtf.allow_long(price):
                logger.debug(f"MTF blocks LONG @ {price:.2f} < EMA50(15m)={mtf.ema50:.2f}")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not funding.can_enter_long():
                logger.debug(f"Funding blocks LONG: {funding.rate_pct}")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not anti_manip.clear_for_entry("long"):
                logger.debug("AntiManip blocks LONG")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return

            score = _score_long(ind, ob, price)
            logger.info(
                f"LONG score={score:.3f} | ema={ind.ema_fast:.1f}>{ind.ema_slow:.1f} "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} "
                f"bb_r={ind.bb_ready} vwap={ind.vwap:.1f} "
                f"mtf={'✓' if mtf.ready else '?'} fund={funding.rate_pct}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(price)
                if USE_LIMIT_ORDERS:
                    ok, filled_qty, avg_price = await lom.place_entry("Buy", qty)
                else:
                    resp = await trader.place_order("Buy", qty, order_type="Market")
                    ok = resp.get("retCode") == 0
                    filled_qty, avg_price = (qty, price) if ok else (0, 0)

                if ok and filled_qty > 0:
                    from .position_manager import position_manager
                    await position_manager.on_open("long", filled_qty, avg_price)
                    db.record_trade(
                        symbol=_sym(), side="long",
                        entry=avg_price, exit_price=0, qty=filled_qty,
                        pnl_usdt=0, pnl_pct=0, reason="OPEN",
                        signal_score=score, funding_rate=funding.rate,
                    )
                    await _notify(
                        f"🟡 *LONG* `{_sym()}` score=`{score:.3f}`\n"
                        f"`price={avg_price}` `qty={filled_qty}` `rsi={ind.rsi_value:.1f}`\n"
                        f"`imb={ob.imbalance:.3f}` `fund={funding.rate_pct}` "
                        f"`vwap={ind.vwap:.1f}` `mtf={'✓' if mtf.ready else '?'}`"
                    )

        elif cross_down:
            if not mtf.allow_short(price):
                logger.debug(f"MTF blocks SHORT @ {price:.2f} > EMA50(15m)={mtf.ema50:.2f}")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not funding.can_enter_short():
                logger.debug(f"Funding blocks SHORT: {funding.rate_pct}")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not anti_manip.clear_for_entry("short"):
                logger.debug("AntiManip blocks SHORT")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return

            score = _score_short(ind, ob, price)
            logger.info(
                f"SHORT score={score:.3f} | ema={ind.ema_fast:.1f}<{ind.ema_slow:.1f} "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} "
                f"bb_r={ind.bb_ready} vwap={ind.vwap:.1f} "
                f"mtf={'✓' if mtf.ready else '?'} fund={funding.rate_pct}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(price)
                if USE_LIMIT_ORDERS:
                    ok, filled_qty, avg_price = await lom.place_entry("Sell", qty)
                else:
                    resp = await trader.place_order("Sell", qty, order_type="Market")
                    ok = resp.get("retCode") == 0
                    filled_qty, avg_price = (qty, price) if ok else (0, 0)

                if ok and filled_qty > 0:
                    from .position_manager import position_manager
                    await position_manager.on_open("short", filled_qty, avg_price)
                    db.record_trade(
                        symbol=_sym(), side="short",
                        entry=avg_price, exit_price=0, qty=filled_qty,
                        pnl_usdt=0, pnl_pct=0, reason="OPEN",
                        signal_score=score, funding_rate=funding.rate,
                    )
                    await _notify(
                        f"🟠 *SHORT* `{_sym()}` score=`{score:.3f}`\n"
                        f"`price={avg_price}` `qty={filled_qty}` `rsi={ind.rsi_value:.1f}`\n"
                        f"`imb={ob.imbalance:.3f}` `fund={funding.rate_pct}` "
                        f"`vwap={ind.vwap:.1f}` `mtf={'✓' if mtf.ready else '?'}`"
                    )

        self._prev_fast = ind.ema_fast
        self._prev_slow = ind.ema_slow


async def trader():
    from .trader import trader as _t
    return _t


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
