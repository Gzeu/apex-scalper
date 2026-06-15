"""Strategy v1.1.0 — 5 features noi pentru DOGEUSDT scalping 2026.

Changelog:
  v1.1.0 — Features noi:
    GATE 0 — Min Net Profit: blocheaza entry daca TP1 nu acopera comisionul
      taker Bybit (0.055% x2 = 0.11% dus-intors). Previne trade-uri care
      castiga directia dar pierd pe comision.
    GATE 9 — Session Filter: evita sesiunile cu lichiditate scazuta
      (00:00-02:00 UTC = noapte Asia, 12:00-13:00 UTC = pauza pranz Europa).
      DOGE e mai volatil si mai manipulat in aceste ferestre.
    GATE 10 — News/Event Blocker: blocheaza 5 minute inainte si dupa
      ora rotunda (xx:00 UTC) cand vin frecvent date macro (CPI, FOMC etc.).
    SCORE BONUS — RSI Divergence: +0.08 la scor daca RSI face divergenta
      cu pretul (bullish div pentru long, bearish div pentru short).
      Detectie simpla pe ultimele 3 candle-uri.
    SCORE BONUS — S/R Breakout: +0.06 la scor daca pretul sparge un nivel
      pivot calculat dinamic din ultimele 20 candle-uri (high/low pivot).
  v1.0.0 — debug logging la fiecare gate blocat.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
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

# Comision Bybit taker: 0.055% per leg, dus-intors = 0.11%
TAKER_FEE_PCT    = 0.00055
ROUND_TRIP_FEE   = TAKER_FEE_PCT * 2   # 0.0011

# Session filter — ore UTC blocate (tuples de (start_h, end_h))
_BLOCKED_SESSIONS = [
    (0, 2),    # 00:00-02:00 UTC — noapte Asia, volum mic, manipulare
    (12, 13),  # 12:00-13:00 UTC — pauza pranz Europa
]

# News blocker — minute inainte/dupa ora rotunda (xx:00)
_NEWS_BLOCK_MINUTES = 4   # blocheaza xx:57-xx:04

# Divergence detection — cate candle-uri istorice pastram
_DIV_WINDOW = 5

# S/R Breakout — cate candle-uri pentru pivot
_SR_WINDOW = 20

_ind_lock: asyncio.Lock | None = None
_main_loop: asyncio.AbstractEventLoop | None = None

# Buffer pentru divergence + S/R
_price_highs: deque = deque(maxlen=_SR_WINDOW)
_price_lows:  deque = deque(maxlen=_SR_WINDOW)
_rsi_buf:     deque = deque(maxlen=_DIV_WINDOW)
_close_buf:   deque = deque(maxlen=_DIV_WINDOW)


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def _get_ind_lock() -> asyncio.Lock:
    global _ind_lock
    if _ind_lock is None:
        _ind_lock = asyncio.Lock()
    return _ind_lock


# --------------------------------------------------------------------------- #
#  Session + News filters
# --------------------------------------------------------------------------- #

def _session_allowed() -> bool:
    """Returneaza False in sesiunile cu lichiditate scazuta."""
    import datetime
    now_utc = datetime.datetime.utcnow()
    h = now_utc.hour
    for (start, end) in _BLOCKED_SESSIONS:
        if start <= h < end:
            return False
    return True


def _news_window_clear() -> bool:
    """Returneaza False in fereastra de 4 minute in jurul orei rotunde."""
    import datetime
    now_utc = datetime.datetime.utcnow()
    m = now_utc.minute
    # Blocheaza minutele 57,58,59,0,1,2,3
    if m >= 60 - _NEWS_BLOCK_MINUTES or m < _NEWS_BLOCK_MINUTES:
        return False
    return True


# --------------------------------------------------------------------------- #
#  Divergence detection
# --------------------------------------------------------------------------- #

def _has_bullish_divergence() -> bool:
    """Price face lower low dar RSI face higher low -> bullish divergence."""
    if len(_close_buf) < _DIV_WINDOW or len(_rsi_buf) < _DIV_WINDOW:
        return False
    closes = list(_close_buf)
    rsis   = list(_rsi_buf)
    # Compara ultimele 2 vs precedentele 2
    price_ll = closes[-1] < closes[-3]   # price lower low
    rsi_hl   = rsis[-1]   > rsis[-3]     # RSI higher low
    return price_ll and rsi_hl


def _has_bearish_divergence() -> bool:
    """Price face higher high dar RSI face lower high -> bearish divergence."""
    if len(_close_buf) < _DIV_WINDOW or len(_rsi_buf) < _DIV_WINDOW:
        return False
    closes = list(_close_buf)
    rsis   = list(_rsi_buf)
    price_hh = closes[-1] > closes[-3]   # price higher high
    rsi_lh   = rsis[-1]   < rsis[-3]     # RSI lower high
    return price_hh and rsi_lh


# --------------------------------------------------------------------------- #
#  S/R Breakout detection
# --------------------------------------------------------------------------- #

def _breakout_long(price: float) -> bool:
    """Price sparge recent pivot high -> breakout bullish."""
    if len(_price_highs) < 10:
        return False
    highs = list(_price_highs)
    pivot_high = max(highs[:-3])   # max din toate mai putin ultimele 3
    return price > pivot_high * 1.0005   # 0.05% buffer anti-fakeout


def _breakout_short(price: float) -> bool:
    """Price sparge recent pivot low -> breakout bearish."""
    if len(_price_lows) < 10:
        return False
    lows = list(_price_lows)
    pivot_low = min(lows[:-3])   # min din toate mai putin ultimele 3
    return price < pivot_low * 0.9995   # 0.05% buffer anti-fakeout


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
#  Scoring — cu divergence + breakout bonus
# --------------------------------------------------------------------------- #

def _score_long(snapshot: IndicatorState, ob, price: float) -> float:
    score = 0.0
    from .book_pressure import bp
    if bp.pressure_long():                                           score += 0.24
    if snapshot.rsi_ready:
        if RSI_LONG_MIN <= snapshot.rsi_value <= RSI_OB_PENALTY:    score += 0.16
        elif snapshot.rsi_value < RSI_LONG_MIN:                      score += 0.08
    if ob.imbalance >= IMBALANCE_LONG:                               score += 0.14
    if price > snapshot.ema_trend > 0:                               score += 0.12
    if snapshot.ema_fast > snapshot.ema_slow > 0:                    score += 0.10
    if snapshot.vol_ready and snapshot.vol_zscore >= VOL_ZSCORE_MIN: score += 0.08
    if snapshot.macd_ready and snapshot.macd_histogram > 0:          score += 0.04
    if snapshot.stoch_ready and snapshot.stoch_k > snapshot.stoch_d and snapshot.stoch_k < 80:
        score += 0.04
    if snapshot.bb_ready and price > snapshot.bb_mid:                score += 0.04
    if snapshot.vwap > 0 and price > snapshot.vwap:                  score += 0.04
    # BONUS: divergence bullish
    if _has_bullish_divergence():                                     score += 0.08
    # BONUS: breakout bullish
    if _breakout_long(price):                                         score += 0.06
    return min(score, 1.0)


def _score_short(snapshot: IndicatorState, ob, price: float) -> float:
    score = 0.0
    from .book_pressure import bp
    if bp.pressure_short():                                          score += 0.24
    if snapshot.rsi_ready:
        if RSI_OS_PENALTY <= snapshot.rsi_value <= RSI_SHORT_MAX:   score += 0.16
        elif snapshot.rsi_value > RSI_SHORT_MAX:                     score += 0.08
    if ob.imbalance <= IMBALANCE_SHORT:                              score += 0.14
    if 0 < snapshot.ema_trend and price < snapshot.ema_trend:       score += 0.12
    if snapshot.ema_fast < snapshot.ema_slow and snapshot.ema_slow > 0:
        score += 0.10
    if snapshot.vol_ready and snapshot.vol_zscore >= VOL_ZSCORE_MIN: score += 0.08
    if snapshot.macd_ready and snapshot.macd_histogram < 0:          score += 0.04
    if snapshot.stoch_ready and snapshot.stoch_k < snapshot.stoch_d and snapshot.stoch_k > 20:
        score += 0.04
    if snapshot.bb_ready and price < snapshot.bb_mid:                score += 0.04
    if snapshot.vwap > 0 and price < snapshot.vwap:                  score += 0.04
    # BONUS: divergence bearish
    if _has_bearish_divergence():                                     score += 0.08
    # BONUS: breakout bearish
    if _breakout_short(price):                                        score += 0.06
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

    # Actualizeaza bufferele pentru divergence + S/R
    _price_highs.append(high)
    _price_lows.append(low)
    _close_buf.append(price)
    if s.rsi_ready:
        _rsi_buf.append(s.rsi_value)

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


# --------------------------------------------------------------------------- #
#  Evaluate — toate gate-urile + features noi
# --------------------------------------------------------------------------- #

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

    # --- pozitie deschisa: gestioneaza TP/SL si eventual pyramid
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

    # --- fara pozitie deschisa: verifica toate gate-urile
    if not state.running or state.paused:
        return

    # GATE 0: Min Net Profit — TP1 trebuie sa acopere comisionul
    prof = config.profile(config.symbol)
    tp1_pct      = prof.get("tp1_pct", 0.003)
    order_usdt   = prof.get("order_size_usdt", 5.0)
    leverage     = prof.get("leverage", 10)
    notional     = order_usdt * leverage
    gross_profit = notional * tp1_pct
    commission   = notional * ROUND_TRIP_FEE
    if gross_profit <= commission:
        logger.debug(
            f"[evaluate] GATE0 MIN_PROFIT blocat: "
            f"tp1={gross_profit:.4f} USDT <= comision={commission:.4f} USDT "
            f"(notional={notional:.1f} tp1_pct={tp1_pct:.4%})"
        )
        return

    # GATE 1: Risk
    if not risk.can_open():
        logger.debug(
            f"[evaluate] GATE1 RISK blocat: daily_loss={risk._daily_loss:.2f} "
            f"consec={risk.consecutive_losses} open={risk._open_count}"
        )
        return

    # GATE 2: Regime
    if not regime.allow_entry():
        logger.debug(
            f"[evaluate] GATE2 REGIME blocat: label={regime.label} ADX={regime.adx}"
        )
        return

    # GATE 9: Session filter
    if not _session_allowed():
        import datetime
        h = datetime.datetime.utcnow().hour
        logger.debug(f"[evaluate] GATE9 SESSION blocat: ora UTC={h}:xx")
        return

    # GATE 10: News/Event blocker
    if not _news_window_clear():
        import datetime
        m = datetime.datetime.utcnow().minute
        logger.debug(f"[evaluate] GATE10 NEWS blocat: minut={m} (fereastra ora rotunda)")
        return

    # Calculeaza score
    ob = compute_ob()
    score_l, score_s = await score_snapshot(price, ob)
    best_score = max(score_l, score_s)
    near_threshold = best_score >= ENTRY_THRESHOLD * 0.85

    # GATE 3: Spread
    spread_bps = state.orderbook.spread / price * 10_000 if price > 0 else 999
    atr_ratio  = ind.atr_value / (ATR_BASELINE * price) if price > 0 else 1.0
    max_spread = BASE_SPREAD_BPS * (1 + ATR_SPREAD_MULT * atr_ratio)
    if spread_bps > max_spread:
        logger.debug(
            f"[evaluate] GATE3 SPREAD blocat: "
            f"spread={spread_bps:.2f}bps > max={max_spread:.2f}bps"
        )
        return

    # GATE 4: ATR volatility window
    if ind.atr_ready:
        atr_pct = ind.atr_value / price if price > 0 else 0
        if not (ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT):
            logger.debug(
                f"[evaluate] GATE4 ATR blocat: atr_pct={atr_pct:.6f} "
                f"window=[{ATR_MIN_PCT},{ATR_MAX_PCT}]"
            )
            return

    # GATE 5: Anti-manipulation
    if anti_manipulation.is_suspicious():
        logger.debug("[evaluate] GATE5 ANTI-MANIP blocat")
        return

    # GATE 6: MTF ready
    if not mtf.ready:
        logger.debug(f"[evaluate] GATE6 MTF not ready: ema50={mtf.ema50:.4f}")
        return

    # Refresh funding
    await funding.maybe_refresh(config.symbol)

    # --- LONG path ---
    if score_l >= ENTRY_THRESHOLD:
        if not funding.can_enter_long():
            logger.warning(f"[evaluate] GATE7 FUNDING blocheaza LONG: rate={funding.rate_pct}")
            return
        if price <= mtf.ema50:
            logger.debug(f"[evaluate] GATE8 MTF dir blocheaza LONG: price={price} <= ema50={mtf.ema50:.4f}")
            return
        await _enter("long", "Buy", price, score_l, config)

    # --- SHORT path ---
    elif score_s >= ENTRY_THRESHOLD:
        if not funding.can_enter_short():
            logger.warning(f"[evaluate] GATE7 FUNDING blocheaza SHORT: rate={funding.rate_pct}")
            return
        if price >= mtf.ema50:
            logger.debug(f"[evaluate] GATE8 MTF dir blocheaza SHORT: price={price} >= ema50={mtf.ema50:.4f}")
            return
        await _enter("short", "Sell", price, score_s, config)

    else:
        if near_threshold:
            logger.debug(
                f"[evaluate] SCORE sub prag: L={score_l:.4f} S={score_s:.4f} "
                f"prag={ENTRY_THRESHOLD} lipsa={ENTRY_THRESHOLD - best_score:.4f}"
            )


async def _enter(
    side: str,
    bybit_side: str,
    price: float,
    score: float,
    config,
) -> None:
    from .risk import risk
    from .regime_filter import regime
    from .position_manager import position_manager as pm
    from .limit_order_manager import place_entry_order
    from .telegram_ui import notify_open

    is_long = side == "long"
    sl = price * (1 - 0.0008 if is_long else 1 + 0.0008)
    tp = price * (1 + 0.004  if is_long else 1 - 0.004)

    qty = risk.calc_qty(
        price,
        order_size_usdt=config.order_size_usdt,
        leverage=config.leverage,
        regime_factor=regime.size_factor(),
    )
    if qty <= 0:
        logger.warning(f"[{side.upper()}] calc_qty=0 — skip entry (price={price})")
        return

    logger.info(
        f"[{side.upper()}] ENTRY score={score:.4f} price={price} "
        f"qty={qty} sl={sl:.4f} tp={tp:.4f}"
    )

    trade_id = await place_entry_order(
        side=bybit_side, qty=qty, stop_loss=sl, take_profit=tp
    )
    if not trade_id:
        logger.error(f"[{side.upper()}] place_entry_order failed — no trade_id")
        return

    await pm.on_open(side, qty, price, trade_id)
    risk.on_open()
    with state.lock:
        state.open_position = side
        state.open_qty      = qty
        state.open_entry    = price

    logger.info(f"[{side.upper()}] OPENED trade_id={trade_id} qty={qty} @ {price}")
    await notify_open(side, qty, price, sl, tp)
