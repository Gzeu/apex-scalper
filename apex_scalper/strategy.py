"""Strategy v0.9.9 — notificari automate open/TP/SL.

Changelog:
  v0.9.9 — notify_open() apelata la deschidere long/short via telegram_ui.
  v0.8.7 — BUG 30+31 fix.
"""
from __future__ import annotations

import asyncio
from loguru import logger
from .state import state

# --------------------------------------------------------------------------- #
#  Strategy parameters
# --------------------------------------------------------------------------- #
RSI_LONG_MIN     = 45.0
RSI_SHORT_MAX    = 55.0
RSI_OB_PENALTY   = 65.0
RSI_OS_PENALTY   = 35.0
IMBALANCE_LONG   = 0.05
IMBALANCE_SHORT  = -0.05
VOL_ZSCORE_MIN   = 0.5
ATR_MIN_PCT      = 0.0003
ATR_MAX_PCT      = 0.010
ENTRY_THRESHOLD  = 0.65
BASE_SPREAD_BPS  = 3.0
ATR_SPREAD_MULT  = 2.0
ATR_BASELINE     = 0.001

_ind_lock: asyncio.Lock | None = None
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def _get_ind_lock() -> asyncio.Lock:
    global _ind_lock
    if _ind_lock is None:
        _ind_lock = asyncio.Lock()
    return _ind_lock


class IndicatorState:
    __slots__ = [
        "ema_fast", "ema_slow", "ema_trend",
        "rsi_value", "rsi_ready",
        "atr_value", "atr_ready",
        "bb_upper", "bb_mid", "bb_lower", "bb_ready",
        "vwap",
        "vol_zscore", "vol_ready",
        "macd_line", "macd_signal", "macd_histogram", "macd_ready",
        "stoch_k", "stoch_d", "stoch_ready",
        "last_price",
    ]

    def __init__(self):
        self.ema_fast = self.ema_slow = self.ema_trend = 0.0
        self.rsi_value = 50.0;  self.rsi_ready  = False
        self.atr_value = 0.0;   self.atr_ready  = False
        self.bb_upper  = 0.0;   self.bb_mid     = 0.0
        self.bb_lower  = 0.0;   self.bb_ready   = False
        self.vwap      = 0.0
        self.vol_zscore = 0.0;  self.vol_ready  = False
        self.macd_line = self.macd_signal = self.macd_histogram = 0.0
        self.macd_ready = False
        self.stoch_k   = 0.0;   self.stoch_d    = 0.0
        self.stoch_ready = False
        self.last_price  = 0.0


ind = IndicatorState()
_ind_state = None


def _get_ind_state():
    global _ind_state
    if _ind_state is None:
        from .indicators import IndicatorState as IndState
        _ind_state = IndState()
    return _ind_state


def _score_long(snapshot: IndicatorState, ob, price: float) -> float:
    score = 0.0
    from .book_pressure import bp
    if bp.pressure_long():                                          score += 0.24
    if snapshot.rsi_ready:
        if RSI_LONG_MIN <= snapshot.rsi_value <= RSI_OB_PENALTY:   score += 0.16
        elif snapshot.rsi_value < RSI_LONG_MIN:                     score += 0.08
    if ob.imbalance >= IMBALANCE_LONG:                              score += 0.14
    if price > snapshot.ema_trend > 0:                              score += 0.12
    if snapshot.ema_fast > snapshot.ema_slow > 0:                   score += 0.10
    if snapshot.vol_ready and snapshot.vol_zscore >= VOL_ZSCORE_MIN: score += 0.08
    if snapshot.macd_ready and snapshot.macd_histogram > 0:         score += 0.04
    if snapshot.stoch_ready and snapshot.stoch_k > snapshot.stoch_d and snapshot.stoch_k < 80: score += 0.04
    if snapshot.bb_ready and price > snapshot.bb_mid:               score += 0.04
    if snapshot.vwap > 0 and price > snapshot.vwap:                 score += 0.04
    return min(score, 1.0)


def _score_short(snapshot: IndicatorState, ob, price: float) -> float:
    score = 0.0
    from .book_pressure import bp
    if bp.pressure_short():                                          score += 0.24
    if snapshot.rsi_ready:
        if RSI_OS_PENALTY <= snapshot.rsi_value <= RSI_SHORT_MAX:   score += 0.16
        elif snapshot.rsi_value > RSI_SHORT_MAX:                     score += 0.08
        elif snapshot.rsi_value > RSI_SHORT_MAX:                     score += 0.08
    if ob.imbalance <= IMBALANCE_SHORT:                              score += 0.14
    if 0 < snapshot.ema_trend and price < snapshot.ema_trend:        score += 0.12
    if snapshot.ema_fast < snapshot.ema_slow and snapshot.ema_slow > 0: score += 0.10
    if snapshot.vol_ready and snapshot.vol_zscore >= VOL_ZSCORE_MIN: score += 0.08
    if snapshot.macd_ready and snapshot.macd_histogram < 0:          score += 0.04
    if snapshot.stoch_ready and snapshot.stoch_k < snapshot.stoch_d and snapshot.stoch_k > 20: score += 0.04
    if snapshot.bb_ready and price < snapshot.bb_mid:                score += 0.04
    if snapshot.vwap > 0 and price < snapshot.vwap:                  score += 0.04
    return min(score, 1.0)


async def score_snapshot(price: float, ob) -> tuple[float, float]:
    async with _get_ind_lock():
        snap = IndicatorState()
        snap.rsi_value = ind.rsi_value;   snap.rsi_ready = ind.rsi_ready
        snap.atr_value = ind.atr_value;   snap.atr_ready = ind.atr_ready
        snap.ema_fast  = ind.ema_fast;    snap.ema_slow  = ind.ema_slow
        snap.ema_trend = ind.ema_trend
        snap.bb_upper  = ind.bb_upper;    snap.bb_mid    = ind.bb_mid
        snap.bb_lower  = ind.bb_lower;    snap.bb_ready  = ind.bb_ready
        snap.vwap      = ind.vwap
        snap.vol_zscore = ind.vol_zscore; snap.vol_ready = ind.vol_ready
        snap.macd_line = ind.macd_line;   snap.macd_signal = ind.macd_signal
        snap.macd_histogram = ind.macd_histogram; snap.macd_ready = ind.macd_ready
        snap.stoch_k   = ind.stoch_k;    snap.stoch_d   = ind.stoch_d
        snap.stoch_ready = ind.stoch_ready
    return _score_long(snap, ob, price), _score_short(snap, ob, price)


def update_indicators(price: float, kline_data: dict) -> None:
    from .indicators import update_all
    s = _get_ind_state()
    high   = float(kline_data.get("high",   price))
    low    = float(kline_data.get("low",    price))
    volume = float(kline_data.get("volume", 0.0))
    update_all(s, price, high, low, volume)
    loop = _main_loop
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            _update_ind_locked(s, price), loop
        )
        try:
            future.result(timeout=1.0)
        except Exception as e:
            logger.warning(f"[strategy] update_indicators lock timeout: {e}")
            _apply_ind_from_state(s, price)
    else:
        _apply_ind_from_state(s, price)


async def _update_ind_locked(s, price: float) -> None:
    async with _get_ind_lock():
        _apply_ind_from_state(s, price)


def _apply_ind_from_state(s, price: float) -> None:
    ind.last_price      = price
    ind.ema_fast        = s.ema_fast;    ind.ema_slow       = s.ema_slow
    ind.ema_trend       = s.ema_trend
    ind.rsi_value       = s.rsi_value;   ind.rsi_ready      = s.rsi_ready
    ind.atr_value       = s.atr_value;   ind.atr_ready      = s.atr_ready
    ind.bb_upper        = s.bb_upper;    ind.bb_mid         = s.bb_mid
    ind.bb_lower        = s.bb_lower;    ind.bb_ready       = s.bb_ready
    ind.vwap            = s.vwap
    ind.vol_zscore      = s.vol_zscore;  ind.vol_ready      = s.vol_ready
    ind.macd_line       = s.macd_line;   ind.macd_signal    = s.macd_signal
    ind.macd_histogram  = s.macd_histogram; ind.macd_ready  = s.macd_ready
    ind.stoch_k         = s.stoch_k;    ind.stoch_d        = s.stoch_d
    ind.stoch_ready     = s.stoch_ready


async def evaluate(price: float) -> None:
    from .regime_filter import regime
    from .risk import risk
    from .mtf_filter import mtf
    from .funding_rate import funding
    from .orderbook_analytics import compute as compute_ob
    from .position_manager import position_manager as pm, MAX_HOLD_CANDLES
    from .book_pressure import bp
    from .anti_manipulation import anti_manipulation
    from .config import config

    with state.lock:
        pos      = state.open_position
        open_qty = state.open_qty

    if pos:
        closed = await pm.evaluate(price)
        if not closed:
            score, _ = await score_snapshot(price, compute_ob())
            if score >= 0.85 and pos:
                await pm.try_pyramid(
                    side=pos, price=price, score=score,
                    stop_loss=price * (1 - 0.0008 if pos == "long" else 1 + 0.0008),
                    take_profit=price * (1 + 0.004 if pos == "long" else 1 - 0.004),
                )
        return

    if not state.running or state.paused:
        return
    if not risk.can_open():
        return
    if not regime.allow_entry():
        return

    ob = compute_ob()
    score_l, score_s = await score_snapshot(price, ob)

    spread_bps = state.orderbook.spread / price * 10_000 if price > 0 else 999
    atr_ratio  = ind.atr_value / (ATR_BASELINE * price) if price > 0 else 1.0
    max_spread = BASE_SPREAD_BPS * (1 + ATR_SPREAD_MULT * atr_ratio)
    if spread_bps > max_spread:
        return

    if ind.atr_ready:
        atr_pct = ind.atr_value / price if price > 0 else 0
        if not (ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT):
            return

    if anti_manipulation.is_suspicious():
        return
    if not mtf.ready:
        return

    await funding.maybe_refresh(config.symbol)

    if score_l >= ENTRY_THRESHOLD:
        if not funding.can_enter_long():
            return
        if price <= mtf.ema50:
            return
        from .limit_order_manager import place_entry_order
        from .telegram_ui import notify_open
        sl = price * (1 - 0.0008)
        tp = price * (1 + 0.004)
        qty = risk.calc_qty(
            price,
            order_size_usdt=config.order_size_usdt,
            leverage=config.leverage,
            regime_factor=regime.size_factor(),
        )
        if qty <= 0:
            return
        trade_id = await place_entry_order(side="Buy", qty=qty, stop_loss=sl, take_profit=tp)
        if trade_id:
            await pm.on_open("long", qty, price, trade_id)
            risk.on_open()
            with state.lock:
                state.open_position = "long"
                state.open_qty      = qty
                state.open_entry    = price
            logger.info(f"LONG score={score_l:.3f} price={price} qty={qty} sl={sl:.2f} tp={tp:.2f}")
            await notify_open("long", qty, price, sl, tp)

    elif score_s >= ENTRY_THRESHOLD:
        if not funding.can_enter_short():
            return
        if price >= mtf.ema50:
            return
        from .limit_order_manager import place_entry_order
        from .telegram_ui import notify_open
        sl = price * (1 + 0.0008)
        tp = price * (1 - 0.004)
        qty = risk.calc_qty(
            price,
            order_size_usdt=config.order_size_usdt,
            leverage=config.leverage,
            regime_factor=regime.size_factor(),
        )
        if qty <= 0:
            return
        trade_id = await place_entry_order(side="Sell", qty=qty, stop_loss=sl, take_profit=tp)
        if trade_id:
            await pm.on_open("short", qty, price, trade_id)
            risk.on_open()
            with state.lock:
                state.open_position = "short"
                state.open_qty      = qty
                state.open_entry    = price
            logger.info(f"SHORT score={score_s:.3f} price={price} qty={qty} sl={sl:.2f} tp={tp:.2f}")
            await notify_open("short", qty, price, sl, tp)
