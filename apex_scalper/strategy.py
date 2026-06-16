"""Strategy v1.1.6 — regime.update() apelat in update_indicators.

Changelog:
  v1.1.6 —
    FIX: regime.update() nu era apelat in update_indicators() v1.1.5.
      Rezultat: ADX=0.0 permanent, label=UNKNOWN, GATE2 blocat mereu.
    Fix: regime.update(close, atr_value, high, low) adaugat in update_indicators()
      dupa apply_ind_from_state().
  v1.1.5 — update_indicators async, elimina run_coroutine_threadsafe deadlock.
  v1.1.4 — entry fill confirm, SL unic, state dupa fill, circuit_breaker.
"""
from __future__ import annotations

import asyncio
from collections import deque
from loguru import logger
from .state import state
from .position_manager import ROUND_TRIP_FEE

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

TAKER_FEE_PCT = 0.00055

_BLOCKED_SESSIONS   = [(0, 2), (12, 13)]
_NEWS_BLOCK_MINUTES = 4
_DIV_WINDOW         = 5
_SR_WINDOW          = 20

_ind_lock: asyncio.Lock | None = None

_price_highs: deque = deque(maxlen=_SR_WINDOW)
_price_lows:  deque = deque(maxlen=_SR_WINDOW)
_rsi_buf:     deque = deque(maxlen=_DIV_WINDOW)
_close_buf:   deque = deque(maxlen=_DIV_WINDOW)


def set_main_loop(loop) -> None:
    """Pastrat pentru compatibilitate backwards — no-op."""
    pass


def _get_ind_lock() -> asyncio.Lock:
    global _ind_lock
    if _ind_lock is None:
        _ind_lock = asyncio.Lock()
    return _ind_lock


# --------------------------------------------------------------------------- #
#  Session + News
# --------------------------------------------------------------------------- #

def _session_allowed() -> bool:
    import datetime
    h = datetime.datetime.utcnow().hour
    for (s, e) in _BLOCKED_SESSIONS:
        if s <= h < e:
            return False
    return True


def _news_window_clear() -> bool:
    import datetime
    m = datetime.datetime.utcnow().minute
    if m >= 60 - _NEWS_BLOCK_MINUTES or m < _NEWS_BLOCK_MINUTES:
        return False
    return True


# --------------------------------------------------------------------------- #
#  Divergence
# --------------------------------------------------------------------------- #

def _has_bullish_divergence() -> bool:
    if len(_close_buf) < _DIV_WINDOW or len(_rsi_buf) < _DIV_WINDOW:
        return False
    closes = list(_close_buf)
    rsis   = list(_rsi_buf)
    return closes[-1] < closes[-3] and rsis[-1] > rsis[-3]


def _has_bearish_divergence() -> bool:
    if len(_close_buf) < _DIV_WINDOW or len(_rsi_buf) < _DIV_WINDOW:
        return False
    closes = list(_close_buf)
    rsis   = list(_rsi_buf)
    return closes[-1] > closes[-3] and rsis[-1] < rsis[-3]


# --------------------------------------------------------------------------- #
#  S/R Breakout
# --------------------------------------------------------------------------- #

def _breakout_long(price: float) -> bool:
    if len(_price_highs) < 10:
        return False
    pivot = max(list(_price_highs)[:-3])
    return price > pivot * 1.0005


def _breakout_short(price: float) -> bool:
    if len(_price_lows) < 10:
        return False
    pivot = min(list(_price_lows)[:-3])
    return price < pivot * 0.9995


# --------------------------------------------------------------------------- #
#  IndicatorState
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
#  Scoring
# --------------------------------------------------------------------------- #

def _score_long(snapshot: IndicatorState, ob, price: float) -> float:
    score = 0.0
    from .book_pressure import bp
    if bp.pressure_long():                                            score += 0.24
    if snapshot.rsi_ready:
        if RSI_LONG_MIN <= snapshot.rsi_value <= RSI_OB_PENALTY:     score += 0.16
        elif snapshot.rsi_value < RSI_LONG_MIN:                       score += 0.08
    if ob.imbalance >= IMBALANCE_LONG:                                score += 0.14
    if price > snapshot.ema_trend > 0:                                score += 0.12
    if snapshot.ema_fast > snapshot.ema_slow > 0:                     score += 0.10
    if snapshot.vol_ready and snapshot.vol_zscore >= VOL_ZSCORE_MIN:  score += 0.08
    if snapshot.macd_ready and snapshot.macd_histogram > 0:           score += 0.04
    if snapshot.stoch_ready and snapshot.stoch_k > snapshot.stoch_d and snapshot.stoch_k < 80:
        score += 0.04
    if snapshot.bb_ready and price > snapshot.bb_mid:                 score += 0.04
    if snapshot.vwap > 0 and price > snapshot.vwap:                   score += 0.04
    if _has_bullish_divergence():                                      score += 0.08
    if _breakout_long(price):                                          score += 0.06
    return min(score, 1.0)


def _score_short(snapshot: IndicatorState, ob, price: float) -> float:
    score = 0.0
    from .book_pressure import bp
    if bp.pressure_short():                                           score += 0.24
    if snapshot.rsi_ready:
        if RSI_OS_PENALTY <= snapshot.rsi_value <= RSI_SHORT_MAX:    score += 0.16
        elif snapshot.rsi_value > RSI_SHORT_MAX:                      score += 0.08
    if ob.imbalance <= IMBALANCE_SHORT:                               score += 0.14
    if 0 < snapshot.ema_trend and price < snapshot.ema_trend:        score += 0.12
    if snapshot.ema_fast < snapshot.ema_slow and snapshot.ema_slow > 0:
        score += 0.10
    if snapshot.vol_ready and snapshot.vol_zscore >= VOL_ZSCORE_MIN:  score += 0.08
    if snapshot.macd_ready and snapshot.macd_histogram < 0:           score += 0.04
    if snapshot.stoch_ready and snapshot.stoch_k < snapshot.stoch_d and snapshot.stoch_k > 20:
        score += 0.04
    if snapshot.bb_ready and price < snapshot.bb_mid:                 score += 0.04
    if snapshot.vwap > 0 and price < snapshot.vwap:                   score += 0.04
    if _has_bearish_divergence():                                      score += 0.08
    if _breakout_short(price):                                         score += 0.06
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


async def update_indicators(price: float, kline_data: dict) -> None:
    """FIX v1.1.6: apeleaza regime.update() dupa actualizarea indicatorilor."""
    from .indicators import update_all
    from .regime_filter import regime

    s = _get_ind_state()
    high   = float(kline_data.get("high",   price))
    low    = float(kline_data.get("low",    price))
    volume = float(kline_data.get("volume", 0.0))
    update_all(s, price, high, low, volume)

    _price_highs.append(high)
    _price_lows.append(low)
    _close_buf.append(price)
    if s.rsi_ready:
        _rsi_buf.append(s.rsi_value)

    async with _get_ind_lock():
        _apply_ind_from_state(s, price)

    # FIX v1.1.6: actualizeaza regime dupa ce ind e gata
    regime.update(price, s.atr_value, high, low)


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


# --------------------------------------------------------------------------- #
#  Evaluate
# --------------------------------------------------------------------------- #

async def evaluate(price: float) -> None:
    from .regime_filter import regime
    from .risk import risk
    from .mtf_filter import mtf
    from .funding_rate import funding
    from .orderbook_analytics import compute as compute_ob
    from .position_manager import position_manager as pm
    from .book_pressure import bp
    from .anti_manipulation import anti_manipulation
    from .config import config

    with state.lock:
        pos = state.open_position

    if pos:
        closed = await pm.evaluate(price)
        if not closed:
            prof    = config.profile(config.symbol)
            sl_pct  = prof.get("sl_pct",  0.0020)
            tp3_pct = prof.get("tp3_pct", 0.0100)
            score, _ = await score_snapshot(price, compute_ob())
            if score >= 0.85:
                is_long = (pos == "long")
                await pm.try_pyramid(
                    side=pos, price=price, score=score,
                    stop_loss=price   * (1 - sl_pct  if is_long else 1 + sl_pct),
                    take_profit=price * (1 + tp3_pct if is_long else 1 - tp3_pct),
                )
        return

    if not state.running or state.paused:
        return

    prof       = config.profile(config.symbol)
    tp1_pct    = prof.get("tp1_pct",        0.0030)
    order_usdt = prof.get("order_size_usdt", 5.0)
    lev        = prof.get("leverage",        10)
    notional   = order_usdt * lev
    net_tp1    = notional * (tp1_pct - ROUND_TRIP_FEE)

    if net_tp1 <= 0:
        logger.debug(
            f"[evaluate] GATE0 MIN_PROFIT: net_tp1={net_tp1:.5f} USDT "
            f"(notional={notional:.1f} tp1={tp1_pct:.4%} fee={ROUND_TRIP_FEE:.4%})"
        )
        return

    if not risk.can_open():
        logger.debug("[evaluate] GATE1 RISK blocat")
        return

    from .circuit_breaker import circuit_breaker, CircuitOpenError
    if circuit_breaker.is_open:
        logger.debug(f"[evaluate] GATE_CB CIRCUIT OPEN — skip entry")
        return

    if not regime.allow_entry():
        logger.debug(f"[evaluate] GATE2 REGIME blocat: {regime.label}")
        return

    if not _session_allowed():
        import datetime
        logger.debug(f"[evaluate] GATE9 SESSION blocat: ora UTC={datetime.datetime.utcnow().hour}")
        return

    if not _news_window_clear():
        import datetime
        logger.debug(f"[evaluate] GATE10 NEWS blocat: min={datetime.datetime.utcnow().minute}")
        return

    ob = __import__('apex_scalper.orderbook_analytics', fromlist=['compute']).compute()
    score_l, score_s = await score_snapshot(price, ob)
    best_score = max(score_l, score_s)

    spread_bps = state.orderbook.spread / price * 10_000 if price > 0 else 999
    atr_ratio  = ind.atr_value / (ATR_BASELINE * price) if price > 0 else 1.0
    max_spread = BASE_SPREAD_BPS * (1 + ATR_SPREAD_MULT * atr_ratio)
    if spread_bps > max_spread:
        logger.debug(f"[evaluate] GATE3 SPREAD: {spread_bps:.2f} > {max_spread:.2f}bps")
        return

    if ind.atr_ready:
        atr_pct = ind.atr_value / price if price > 0 else 0
        if not (ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT):
            logger.debug(f"[evaluate] GATE4 ATR: {atr_pct:.6f}")
            return

    from .anti_manipulation import anti_manipulation
    if anti_manipulation.is_suspicious():
        logger.debug("[evaluate] GATE5 ANTI-MANIP blocat")
        return

    if not mtf.ready:
        logger.debug("[evaluate] GATE6 MTF not ready")
        return

    await funding.maybe_refresh(config.symbol)

    if score_l >= ENTRY_THRESHOLD:
        if not funding.can_enter_long():
            return
        if price <= mtf.ema50:
            logger.debug("[evaluate] GATE8 MTF blocat LONG")
            return
        await _enter("long", "Buy", price, score_l, config, prof)

    elif score_s >= ENTRY_THRESHOLD:
        if not funding.can_enter_short():
            return
        if price >= mtf.ema50:
            logger.debug("[evaluate] GATE8 MTF blocat SHORT")
            return
        await _enter("short", "Sell", price, score_s, config, prof)

    else:
        if best_score >= ENTRY_THRESHOLD * 0.85:
            logger.debug(
                f"[evaluate] SCORE sub prag: L={score_l:.4f} S={score_s:.4f} "
                f"lipsa={ENTRY_THRESHOLD - best_score:.4f}"
            )


async def _enter(
    side: str,
    bybit_side: str,
    price: float,
    score: float,
    config,
    prof: dict,
) -> None:
    from .risk import risk
    from .regime_filter import regime
    from .position_manager import position_manager as pm
    from .limit_order_manager import lom
    from .telegram_ui import notify_open
    from .trader import trader
    from .circuit_breaker import circuit_breaker, CircuitOpenError
    from .persistence import db

    is_long  = side == "long"
    sl_pct   = prof.get("sl_pct",          0.0020)
    tp3_pct  = prof.get("tp3_pct",         0.0100)
    tp1_pct  = prof.get("tp1_pct",         0.0030)
    lev      = prof.get("leverage",         10)
    order_sz = prof.get("order_size_usdt",  5.0)

    notional = order_sz * lev
    net_tp1  = notional * (tp1_pct - ROUND_TRIP_FEE)
    if net_tp1 <= 0:
        logger.warning(
            f"[{side.upper()}] Skip: net_tp1={net_tp1:.5f} <= 0 "
            f"(tp1={tp1_pct:.4%} fee={ROUND_TRIP_FEE:.4%})"
        )
        return

    sl = price * (1.0 - sl_pct  if is_long else 1.0 + sl_pct)
    tp = price * (1.0 + tp3_pct if is_long else 1.0 - tp3_pct)

    qty = risk.calc_qty(
        price,
        order_size_usdt=order_sz,
        leverage=lev,
        qty_step=trader._qty_step,
        regime_factor=regime.size_factor(),
    )
    if qty <= 0:
        logger.warning(f"[{side.upper()}] calc_qty=0 — skip entry")
        return

    logger.info(
        f"[{side.upper()}] ENTRY score={score:.4f} price={price} qty={qty} "
        f"sl={sl:.5f} ({sl_pct:.3%}) tp={tp:.5f} ({tp3_pct:.3%}) "
        f"net_tp1={net_tp1:.4f} USDT notional={notional:.1f}"
    )

    try:
        success, filled_qty, avg_price, order_id = await circuit_breaker.call(
            lom.place_entry,
            side=bybit_side,
            qty=qty,
            stop_loss=sl,
            take_profit=tp,
        )
    except CircuitOpenError as e:
        logger.warning(f"[{side.upper()}] Circuit OPEN la place_entry: {e}")
        return
    except Exception as e:
        logger.error(f"[{side.upper()}] place_entry exceptie: {e}")
        return

    if not success or filled_qty <= 0:
        logger.error(f"[{side.upper()}] place_entry_order failed sau qty=0 — nu setam stare")
        return

    trade_id = db.record_open_trade(
        symbol=config.symbol,
        side=side,
        entry=avg_price,
        qty=filled_qty,
    )

    await pm.on_open(side, filled_qty, avg_price, trade_id, sl_price=sl)
    risk.on_open()

    with state.lock:
        state.open_position = side
        state.open_qty      = filled_qty
        state.open_entry    = avg_price

    logger.info(
        f"[{side.upper()}] OPENED trade_id={trade_id} "
        f"filled_qty={filled_qty} avg_price={avg_price:.6f}"
    )
    await notify_open(side, filled_qty, avg_price, sl, tp)
