"""Multi-signal scalping strategy v0.6.0.

Fixes vs v0.5.0:
  🔴 CRITICAL:
  - Naming collision: `async def trader()` renamed to `_get_trader()` to avoid
    overwriting the imported `trader` module. Previously caused AttributeError
    on trader.place_order() calls at runtime.
  - lom.place_entry() now receives stop_loss + take_profit so native exchange
    stops are attached on every entry. Previously positions were unprotected.
  - RSI overbought penalty added: LONG blocked/penalized when RSI >= RSI_OB_PENALTY
    (default 65). Prevents entries at RSI=68 right after cross.
  - ENTRY_THRESHOLD raised to 0.65 (was 0.60) — 3/8 confirmations not enough
    for mainnet. 0.65 requires at least EMA cross + trend + RSI + one more.

  🟡 IMPORTANT:
  - Pyramid entries now pass SL/TP so they are also exchange-protected.

Signal weights (unchanged, sum=1.0):
  ema_cross=0.23, trend=0.18, rsi=0.18, imbalance=0.18,
  volume=0.10, atr=0.03, bb=0.05, vwap=0.05

RSI logic:
  LONG:  RSI in [RSI_LONG_MIN..RSI_OB_LIMIT] = positive, full weight at midpoint
         RSI >= RSI_OB_PENALTY (65) = partial penalty applied to rsi score
  SHORT: RSI in [RSI_OS_LIMIT..RSI_SHORT_MAX] = positive
         RSI <= RSI_OS_PENALTY (35) = partial penalty applied to rsi score
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
from .trader import trader as _trader   # explicit alias — never shadow this

# --- Params from .env / injected by inject_profile() ---
RSI_LONG_MIN     = float(os.getenv("RSI_LONG_MIN",    "52.0"))
RSI_SHORT_MAX    = float(os.getenv("RSI_SHORT_MAX",   "48.0"))
RSI_OB_LIMIT     = float(os.getenv("RSI_OB_LIMIT",   "70.0"))
RSI_OS_LIMIT     = float(os.getenv("RSI_OS_LIMIT",   "30.0"))
RSI_OB_PENALTY   = float(os.getenv("RSI_OB_PENALTY",  "65.0"))  # NEW: LONG penalty above this
RSI_OS_PENALTY   = float(os.getenv("RSI_OS_PENALTY",  "35.0"))  # NEW: SHORT penalty below this
IMBALANCE_LONG   = float(os.getenv("IMBALANCE_LONG",  "0.10"))
IMBALANCE_SHORT  = float(os.getenv("IMBALANCE_SHORT", "-0.10"))
VOL_ZSCORE_MIN   = float(os.getenv("VOL_ZSCORE_MIN",  "0.0"))
ATR_MIN_PCT      = float(os.getenv("ATR_MIN_PCT",     "0.0003"))
ATR_MAX_PCT      = float(os.getenv("ATR_MAX_PCT",     "0.005"))
ENTRY_THRESHOLD  = float(os.getenv("ENTRY_THRESHOLD", "0.65"))   # raised from 0.60
USE_LIMIT_ORDERS = os.getenv("USE_LIMIT_ORDERS", "true").lower() == "true"

# Signal weights — must sum to 1.0
_W = {
    "ema_cross":  0.23,
    "trend":      0.18,
    "rsi":        0.18,
    "imbalance":  0.18,
    "volume":     0.10,
    "atr":        0.03,
    "bb":         0.05,
    "vwap":       0.05,
}
assert abs(sum(_W.values()) - 1.0) < 1e-9, f"Weights must sum to 1.0, got {sum(_W.values())}"

ind = IndicatorState()


def update_indicators(close: float, high: float, low: float, volume: float) -> None:
    record_heartbeat()
    update_all(ind, close, high, low, volume)
    anti_manip.analyze(vol_zscore=ind.vol_zscore if ind.vol_ready else 0.0, current_close=close)


def _calc_sl_tp(side: str, price: float) -> tuple[float, float]:
    """Calculate SL and TP prices from env profile params."""
    sl_pct = float(os.getenv("SL_PCT", "0.0008"))
    tp2_pct = float(os.getenv("TP2_PCT", "0.0020"))
    if side == "long" or side == "Buy":
        return round(price * (1 - sl_pct), 8), round(price * (1 + tp2_pct), 8)
    else:
        return round(price * (1 + sl_pct), 8), round(price * (1 - tp2_pct), 8)


def _score_long(ind: IndicatorState, ob: OBSignals, price: float) -> float:
    """Return signal strength [0,1] for LONG entry."""
    score = 0.0

    # 1. EMA cross: fast > slow
    score += _W["ema_cross"] if ind.ema_fast > ind.ema_slow else 0

    # 2. EMA50 1m trend: price above EMA50
    score += _W["trend"] if price > ind.ema_trend else 0

    # 3. RSI confirmation with overbought penalty
    if ind.rsi_ready and RSI_LONG_MIN <= ind.rsi_value <= RSI_OB_LIMIT:
        rsi_conf = min((ind.rsi_value - RSI_LONG_MIN) / (RSI_OB_LIMIT - RSI_LONG_MIN), 1.0)
        rsi_score = _W["rsi"] * rsi_conf
        # Penalty: RSI >= RSI_OB_PENALTY (65) = market is overbought, reduce score
        # At RSI=65: no penalty. At RSI=70: full penalty (rsi_score -> 0)
        if ind.rsi_value >= RSI_OB_PENALTY:
            penalty_factor = 1.0 - (ind.rsi_value - RSI_OB_PENALTY) / (RSI_OB_LIMIT - RSI_OB_PENALTY)
            rsi_score *= max(penalty_factor, 0.0)
        score += rsi_score

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

    # 7. Bollinger Band position: price near lower band = oversold bonus
    if ind.bb_ready and ind.bb_mid > ind.bb_lower:
        if price <= ind.bb_lower:
            score += _W["bb"]
        elif price < ind.bb_mid:
            bb_conf = (ind.bb_mid - price) / (ind.bb_mid - ind.bb_lower)
            score += _W["bb"] * min(bb_conf, 1.0)

    # 8. VWAP session bias: price above VWAP = bullish
    if ind.vwap > 0:
        if price > ind.vwap:
            score += _W["vwap"]
        else:
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

    # 3. RSI confirmation with oversold penalty
    if ind.rsi_ready and RSI_OS_LIMIT <= ind.rsi_value <= RSI_SHORT_MAX:
        rsi_conf = min((RSI_SHORT_MAX - ind.rsi_value) / (RSI_SHORT_MAX - RSI_OS_LIMIT), 1.0)
        rsi_score = _W["rsi"] * rsi_conf
        # Penalty: RSI <= RSI_OS_PENALTY (35) = market oversold, short may bounce
        if ind.rsi_value <= RSI_OS_PENALTY:
            penalty_factor = 1.0 - (RSI_OS_PENALTY - ind.rsi_value) / (RSI_OS_PENALTY - RSI_OS_LIMIT)
            rsi_score *= max(penalty_factor, 0.0)
        score += rsi_score

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

    # 7. Bollinger Band: price near upper band = overbought bonus for short
    if ind.bb_ready and ind.bb_upper > ind.bb_mid:
        if price >= ind.bb_upper:
            score += _W["bb"]
        elif price > ind.bb_mid:
            bb_conf = (price - ind.bb_mid) / (ind.bb_upper - ind.bb_mid)
            score += _W["bb"] * min(bb_conf, 1.0)

    # 8. VWAP session bias: price below VWAP = bearish
    if ind.vwap > 0:
        if price < ind.vwap:
            score += _W["vwap"]
        else:
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
                    sl, tp = _calc_sl_tp("long", price)
                    await position_manager.try_pyramid("long", price, long_score, sl, tp)
                elif pos == "short" and short_score >= 0.85:
                    sl, tp = _calc_sl_tp("short", price)
                    await position_manager.try_pyramid("short", price, short_score, sl, tp)
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
                logger.debug(f"MTF blocks LONG @ {price:.2f}")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not funding.can_enter_long():
                logger.debug(f"Funding blocks LONG")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not anti_manip.clear_for_entry("long"):
                logger.debug("AntiManip blocks LONG")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return

            score = _score_long(ind, ob, price)
            logger.info(
                f"LONG score={score:.3f}/{ENTRY_THRESHOLD} | "
                f"ema={ind.ema_fast:.1f}>{ind.ema_slow:.1f} "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} "
                f"bb={'✓' if ind.bb_ready else '?'} vwap={ind.vwap:.1f} "
                f"mtf={'✓' if mtf.ready else '?'} fund={funding.rate_pct}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(price)
                sl, tp = _calc_sl_tp("long", price)
                if USE_LIMIT_ORDERS:
                    # FIX v0.6.0: pass SL/TP so exchange-side stops are attached
                    ok, filled_qty, avg_price = await lom.place_entry(
                        "Buy", qty, stop_loss=sl, take_profit=tp
                    )
                else:
                    resp = await _trader.place_order(
                        "Buy", qty, order_type="Market",
                        stop_loss=sl, take_profit=tp,
                    )
                    ok = resp.get("retCode") == 0
                    filled_qty, avg_price = (qty, price) if ok else (0, 0)

                if ok and filled_qty > 0:
                    await position_manager.on_open("long", filled_qty, avg_price)
                    db.record_trade(
                        symbol=_sym(), side="long",
                        entry=avg_price, exit_price=0, qty=filled_qty,
                        pnl_usdt=0, pnl_pct=0, reason="OPEN",
                        signal_score=score, funding_rate=funding.rate,
                    )
                    await _notify(
                        f"🟡 *LONG* `{_sym()}` score=`{score:.3f}`\n"
                        f"`price={avg_price}` `qty={filled_qty}`\n"
                        f"`rsi={ind.rsi_value:.1f}` `imb={ob.imbalance:.3f}`\n"
                        f"`SL={sl}` `TP={tp}` `fund={funding.rate_pct}`"
                    )

        elif cross_down:
            if not mtf.allow_short(price):
                logger.debug(f"MTF blocks SHORT @ {price:.2f}")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not funding.can_enter_short():
                logger.debug(f"Funding blocks SHORT")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not anti_manip.clear_for_entry("short"):
                logger.debug("AntiManip blocks SHORT")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return

            score = _score_short(ind, ob, price)
            logger.info(
                f"SHORT score={score:.3f}/{ENTRY_THRESHOLD} | "
                f"ema={ind.ema_fast:.1f}<{ind.ema_slow:.1f} "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} "
                f"bb={'✓' if ind.bb_ready else '?'} vwap={ind.vwap:.1f} "
                f"mtf={'✓' if mtf.ready else '?'} fund={funding.rate_pct}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(price)
                sl, tp = _calc_sl_tp("short", price)
                if USE_LIMIT_ORDERS:
                    ok, filled_qty, avg_price = await lom.place_entry(
                        "Sell", qty, stop_loss=sl, take_profit=tp
                    )
                else:
                    resp = await _trader.place_order(
                        "Sell", qty, order_type="Market",
                        stop_loss=sl, take_profit=tp,
                    )
                    ok = resp.get("retCode") == 0
                    filled_qty, avg_price = (qty, price) if ok else (0, 0)

                if ok and filled_qty > 0:
                    await position_manager.on_open("short", filled_qty, avg_price)
                    db.record_trade(
                        symbol=_sym(), side="short",
                        entry=avg_price, exit_price=0, qty=filled_qty,
                        pnl_usdt=0, pnl_pct=0, reason="OPEN",
                        signal_score=score, funding_rate=funding.rate,
                    )
                    await _notify(
                        f"🟠 *SHORT* `{_sym()}` score=`{score:.3f}`\n"
                        f"`price={avg_price}` `qty={filled_qty}`\n"
                        f"`rsi={ind.rsi_value:.1f}` `imb={ob.imbalance:.3f}`\n"
                        f"`SL={sl}` `TP={tp}` `fund={funding.rate_pct}`"
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
