"""Multi-signal scalping strategy v0.7.0.

Major upgrades vs v0.6.0:
  - Book pressure replaces EMA cross as PRIMARY entry trigger
    EMA cross demoted to confirmation filter (weight 0.10, was 0.23)
    imbalance weight raised to 0.28 (was 0.18) to reflect new primary role
    of order flow signals
  - Regime filter integrated: RANGING blocks entries, VOLATILE halves size
  - Dynamic spread gate: threshold scales with current ATR vs baseline ATR
  - Kelly sizing: risk.calc_qty() now receives order_size_usdt + regime_factor

Signal weights v0.7.0 (sum=1.0):
  book_pressure=0.28, rsi=0.18, imbalance=0.16, trend=0.12,
  ema_cross=0.10, volume=0.08, bb=0.04, vwap=0.04

Entry trigger (changed):
  v0.6.0: EMA fast/slow cross   (3-5 candle lag)
  v0.7.0: bp.pressure_long/short() (1-5 tick lag) + EMA cross as filter
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
from .trader import trader as _trader
from .regime_filter import regime
from .book_pressure import bp

# --- Params injected by inject_profile() ---
RSI_LONG_MIN     = float(os.getenv("RSI_LONG_MIN",    "52.0"))
RSI_SHORT_MAX    = float(os.getenv("RSI_SHORT_MAX",   "48.0"))
RSI_OB_LIMIT     = float(os.getenv("RSI_OB_LIMIT",   "70.0"))
RSI_OS_LIMIT     = float(os.getenv("RSI_OS_LIMIT",   "30.0"))
RSI_OB_PENALTY   = float(os.getenv("RSI_OB_PENALTY",  "65.0"))
RSI_OS_PENALTY   = float(os.getenv("RSI_OS_PENALTY",  "35.0"))
IMBALANCE_LONG   = float(os.getenv("IMBALANCE_LONG",  "0.10"))
IMBALANCE_SHORT  = float(os.getenv("IMBALANCE_SHORT", "-0.10"))
VOL_ZSCORE_MIN   = float(os.getenv("VOL_ZSCORE_MIN",  "0.0"))
ATR_MIN_PCT      = float(os.getenv("ATR_MIN_PCT",     "0.0003"))
ATR_MAX_PCT      = float(os.getenv("ATR_MAX_PCT",     "0.005"))
ENTRY_THRESHOLD  = float(os.getenv("ENTRY_THRESHOLD", "0.65"))
USE_LIMIT_ORDERS = os.getenv("USE_LIMIT_ORDERS", "true").lower() == "true"
# Dynamic spread: base * (1 + ATR_SPREAD_MULT * atr_vs_baseline)
BASE_SPREAD_BPS  = float(os.getenv("BASE_SPREAD_BPS",   "3.0"))
ATR_SPREAD_MULT  = float(os.getenv("ATR_SPREAD_MULT",   "2.0"))
ATR_BASELINE     = float(os.getenv("ATR_BASELINE",      "0.001"))  # 0.10% baseline ATR

# Signal weights v0.7.0
_W = {
    "book_pressure": 0.28,   # NEW primary trigger
    "rsi":           0.18,
    "imbalance":     0.16,
    "trend":         0.12,
    "ema_cross":     0.10,   # demoted to filter
    "volume":        0.08,
    "bb":            0.04,
    "vwap":          0.04,
}
assert abs(sum(_W.values()) - 1.0) < 1e-9, f"Weights must sum to 1.0, got {sum(_W.values())}"

ind = IndicatorState()


def update_indicators(close: float, high: float, low: float, volume: float) -> None:
    record_heartbeat()
    update_all(ind, close, high, low, volume)
    anti_manip.analyze(vol_zscore=ind.vol_zscore if ind.vol_ready else 0.0, current_close=close)
    # Update regime every candle
    if ind.atr_ready:
        regime.update(close, ind.atr_value, high, low)
    # Update book pressure vol_zscore for adaptive threshold
    if ind.vol_ready:
        bp.set_vol_zscore(ind.vol_zscore)


def _calc_sl_tp(side: str, price: float) -> tuple[float, float]:
    sl_pct  = float(os.getenv("SL_PCT",  "0.0008"))
    tp2_pct = float(os.getenv("TP2_PCT", "0.0020"))
    if side in ("long", "Buy"):
        return round(price * (1 - sl_pct), 8), round(price * (1 + tp2_pct), 8)
    return round(price * (1 + sl_pct), 8), round(price * (1 - tp2_pct), 8)


def _dynamic_spread_ok(spread_bps: float) -> bool:
    """Dynamic spread gate: allow wider spread when ATR is high."""
    if not ind.atr_ready:
        return True
    atr_ratio = (ind.atr_value / max(1.0, ind.ema_trend)) / ATR_BASELINE
    limit = BASE_SPREAD_BPS * (1 + ATR_SPREAD_MULT * max(atr_ratio - 1.0, 0))
    return spread_bps <= limit


def _score_long(ind: IndicatorState, ob: OBSignals, price: float) -> float:
    score = 0.0

    # 1. Book pressure — primary trigger (replaces EMA cross as dominant signal)
    if bp.pressure_long():
        score += _W["book_pressure"]

    # 2. RSI confirmation with overbought penalty
    if ind.rsi_ready and RSI_LONG_MIN <= ind.rsi_value <= RSI_OB_LIMIT:
        rsi_conf = min((ind.rsi_value - RSI_LONG_MIN) / (RSI_OB_LIMIT - RSI_LONG_MIN), 1.0)
        rsi_score = _W["rsi"] * rsi_conf
        if ind.rsi_value >= RSI_OB_PENALTY:
            pf = 1.0 - (ind.rsi_value - RSI_OB_PENALTY) / (RSI_OB_LIMIT - RSI_OB_PENALTY)
            rsi_score *= max(pf, 0.0)
        score += rsi_score

    # 3. Orderbook imbalance (level 2 confirmation)
    if ob.imbalance >= IMBALANCE_LONG:
        score += _W["imbalance"] * min(ob.imbalance / 0.3, 1.0)

    # 4. EMA50 1m trend: price above EMA50
    if price > ind.ema_trend:
        score += _W["trend"]

    # 5. EMA cross confirmation (filter role, not trigger)
    if ind.ema_fast > ind.ema_slow:
        score += _W["ema_cross"]

    # 6. Volume z-score
    if ind.vol_ready and ind.vol_zscore >= VOL_ZSCORE_MIN:
        score += _W["volume"] * min(max(ind.vol_zscore / 2.0, 0), 1)

    # 7. BB: price near lower band
    if ind.bb_ready and ind.bb_mid > ind.bb_lower:
        if price <= ind.bb_lower:
            score += _W["bb"]
        elif price < ind.bb_mid:
            bb_c = (ind.bb_mid - price) / (ind.bb_mid - ind.bb_lower)
            score += _W["bb"] * min(bb_c, 1.0)

    # 8. VWAP
    if ind.vwap > 0:
        if price > ind.vwap:
            score += _W["vwap"]
        else:
            gap = (ind.vwap - price) / ind.vwap
            if gap < 0.001:
                score += _W["vwap"] * (1 - gap / 0.001)

    return score


def _score_short(ind: IndicatorState, ob: OBSignals, price: float) -> float:
    score = 0.0

    # 1. Book pressure — primary trigger
    if bp.pressure_short():
        score += _W["book_pressure"]

    # 2. RSI with oversold penalty
    if ind.rsi_ready and RSI_OS_LIMIT <= ind.rsi_value <= RSI_SHORT_MAX:
        rsi_conf = min((RSI_SHORT_MAX - ind.rsi_value) / (RSI_SHORT_MAX - RSI_OS_LIMIT), 1.0)
        rsi_score = _W["rsi"] * rsi_conf
        if ind.rsi_value <= RSI_OS_PENALTY:
            pf = 1.0 - (RSI_OS_PENALTY - ind.rsi_value) / (RSI_OS_PENALTY - RSI_OS_LIMIT)
            rsi_score *= max(pf, 0.0)
        score += rsi_score

    # 3. OB imbalance
    if ob.imbalance <= IMBALANCE_SHORT:
        score += _W["imbalance"] * min(abs(ob.imbalance) / 0.3, 1.0)

    # 4. EMA50 trend
    if price < ind.ema_trend:
        score += _W["trend"]

    # 5. EMA cross confirmation
    if ind.ema_fast < ind.ema_slow:
        score += _W["ema_cross"]

    # 6. Volume
    if ind.vol_ready and ind.vol_zscore >= VOL_ZSCORE_MIN:
        score += _W["volume"] * min(max(ind.vol_zscore / 2.0, 0), 1)

    # 7. BB: price near upper band
    if ind.bb_ready and ind.bb_upper > ind.bb_mid:
        if price >= ind.bb_upper:
            score += _W["bb"]
        elif price > ind.bb_mid:
            bb_c = (price - ind.bb_mid) / (ind.bb_upper - ind.bb_mid)
            score += _W["bb"] * min(bb_c, 1.0)

    # 8. VWAP
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
        from .config import config

        with state.lock:
            if not state.running or state.paused:
                return
            price = state.last_price
            pos   = state.open_position

        if price == 0:
            return

        ob = compute_ob()

        # ── REGIME GATE ──
        if not regime.allow_entry() and not pos:
            logger.debug(f"Regime={regime.label} — entries blocked")
            self._prev_fast = ind.ema_fast
            self._prev_slow = ind.ema_slow
            return

        # ── EXIT / PYRAMID ──
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
            self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
        if not ind.rsi_ready or not ind.atr_ready:
            self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
        if not bp.ready():
            self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return

        # ── DYNAMIC SPREAD GATE ──
        with state.lock:
            best_bid = state.orderbook.best_bid
            best_ask = state.orderbook.best_ask
        if best_bid > 0 and best_ask > 0:
            spread_bps = (best_ask - best_bid) / best_bid * 10000
            if not _dynamic_spread_ok(spread_bps):
                logger.debug(f"Spread {spread_bps:.2f}bps > dynamic limit")
                self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return

        # ── ENTRY TRIGGER: book pressure (primary) ──
        long_signal  = bp.pressure_long()
        short_signal = bp.pressure_short()

        if long_signal:
            if not mtf.allow_long(price): self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not funding.can_enter_long(): self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not anti_manip.clear_for_entry("long"): self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return

            score = _score_long(ind, ob, price)
            logger.info(
                f"LONG bp score={score:.3f}/{ENTRY_THRESHOLD} | "
                f"regime={regime.label}({regime.adx:.1f}) "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} "
                f"cum_delta={bp.cum_delta:+.0f} accel={bp.acceleration:+.0f}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(
                    price,
                    order_size_usdt=config.order_size_usdt,
                    leverage=config.leverage,
                    regime_factor=regime.size_factor(),
                )
                sl, tp = _calc_sl_tp("long", price)
                if USE_LIMIT_ORDERS:
                    ok, filled_qty, avg_price = await lom.place_entry("Buy", qty, stop_loss=sl, take_profit=tp)
                else:
                    resp = await _trader.place_order("Buy", qty, order_type="Market", stop_loss=sl, take_profit=tp)
                    ok = resp.get("retCode") == 0
                    filled_qty, avg_price = (qty, price) if ok else (0, 0)
                if ok and filled_qty > 0:
                    await position_manager.on_open("long", filled_qty, avg_price)
                    risk.on_open()
                    db.record_trade(symbol=_sym(), side="long", entry=avg_price, exit_price=0,
                                    qty=filled_qty, pnl_usdt=0, pnl_pct=0, reason="OPEN",
                                    signal_score=score, funding_rate=funding.rate)
                    await _notify(
                        f"🟡 *LONG* `{_sym()}` score=`{score:.3f}`\n"
                        f"`price={avg_price}` `qty={filled_qty}`\n"
                        f"`rsi={ind.rsi_value:.1f}` `regime={regime.label}`\n"
                        f"`delta={bp.cum_delta:+.0f}` `SL={sl}` `TP={tp}`"
                    )

        elif short_signal:
            if not mtf.allow_short(price): self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not funding.can_enter_short(): self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return
            if not anti_manip.clear_for_entry("short"): self._prev_fast = ind.ema_fast; self._prev_slow = ind.ema_slow; return

            score = _score_short(ind, ob, price)
            logger.info(
                f"SHORT bp score={score:.3f}/{ENTRY_THRESHOLD} | "
                f"regime={regime.label}({regime.adx:.1f}) "
                f"rsi={ind.rsi_value:.1f} imb={ob.imbalance:.3f} "
                f"cum_delta={bp.cum_delta:+.0f} accel={bp.acceleration:+.0f}"
            )
            if score >= ENTRY_THRESHOLD:
                qty = risk.calc_qty(
                    price,
                    order_size_usdt=config.order_size_usdt,
                    leverage=config.leverage,
                    regime_factor=regime.size_factor(),
                )
                sl, tp = _calc_sl_tp("short", price)
                if USE_LIMIT_ORDERS:
                    ok, filled_qty, avg_price = await lom.place_entry("Sell", qty, stop_loss=sl, take_profit=tp)
                else:
                    resp = await _trader.place_order("Sell", qty, order_type="Market", stop_loss=sl, take_profit=tp)
                    ok = resp.get("retCode") == 0
                    filled_qty, avg_price = (qty, price) if ok else (0, 0)
                if ok and filled_qty > 0:
                    await position_manager.on_open("short", filled_qty, avg_price)
                    risk.on_open()
                    db.record_trade(symbol=_sym(), side="short", entry=avg_price, exit_price=0,
                                    qty=filled_qty, pnl_usdt=0, pnl_pct=0, reason="OPEN",
                                    signal_score=score, funding_rate=funding.rate)
                    await _notify(
                        f"🟠 *SHORT* `{_sym()}` score=`{score:.3f}`\n"
                        f"`price={avg_price}` `qty={filled_qty}`\n"
                        f"`rsi={ind.rsi_value:.1f}` `regime={regime.label}`\n"
                        f"`delta={bp.cum_delta:+.0f}` `SL={sl}` `TP={tp}`"
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
