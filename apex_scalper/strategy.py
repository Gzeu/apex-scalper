"""Strategy v0.7.9 — score_snapshot() async helper pentru pulse (Bug 5 fix).

Changelog:
  v0.7.9 — BUG FIX: _score_long/_score_short apelate in pulse din alt task
    asyncio cu ind partial-updated => scoruri eronate in Telegram.
    Adaugat _ind_lock = asyncio.Lock() + score_snapshot(price, ob) async
    function care ia lock-ul si returneaza (score_l, score_s) atomic.
    update_indicators() achizitioneaza acelasi lock la scriere.
"""
from __future__ import annotations

import asyncio
from loguru import logger
from .state import state

# --------------------------------------------------------------------------- #
#  Strategy parameters (overrideable via /setparam or inject_profile)         #
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

# Lock pentru acces concurent la ind (update_indicators vs pulse.score_snapshot)
_ind_lock = asyncio.Lock()


class IndicatorState:
    """Mutable shared state for all computed indicators."""
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


# --------------------------------------------------------------------------- #
#  Scoring functions (private — consume un snapshot consistent al ind)        #
# --------------------------------------------------------------------------- #

def _score_long(
    snapshot: IndicatorState,
    ob,
    price: float,
) -> float:
    """Calculeaza scorul LONG din snapshot consistent al indicatorilor."""
    score = 0.0

    # Book pressure (0.24)
    from .book_pressure import bp
    if bp.pressure_long():
        score += 0.24

    # RSI (0.16)
    if snapshot.rsi_ready:
        if RSI_LONG_MIN <= snapshot.rsi_value <= RSI_OB_PENALTY:
            score += 0.16
        elif snapshot.rsi_value < RSI_LONG_MIN:
            score += 0.08

    # OB Imbalance (0.14)
    if ob.imbalance >= IMBALANCE_LONG:
        score += 0.14

    # EMA Trend (0.12): price > ema_trend
    if price > snapshot.ema_trend > 0:
        score += 0.12

    # EMA Cross (0.10): fast > slow
    if snapshot.ema_fast > snapshot.ema_slow > 0:
        score += 0.10

    # Volume Z-Score (0.08)
    if snapshot.vol_ready and snapshot.vol_zscore >= VOL_ZSCORE_MIN:
        score += 0.08

    # MACD histogram (0.04)
    if snapshot.macd_ready and snapshot.macd_histogram > 0:
        score += 0.04

    # Stochastic RSI (0.04)
    if snapshot.stoch_ready and snapshot.stoch_k > snapshot.stoch_d and snapshot.stoch_k < 80:
        score += 0.04

    # Bollinger Bands (0.04): price above mid
    if snapshot.bb_ready and price > snapshot.bb_mid:
        score += 0.04

    # VWAP (0.04)
    if snapshot.vwap > 0 and price > snapshot.vwap:
        score += 0.04

    return min(score, 1.0)


def _score_short(
    snapshot: IndicatorState,
    ob,
    price: float,
) -> float:
    """Calculeaza scorul SHORT din snapshot consistent al indicatorilor."""
    score = 0.0

    from .book_pressure import bp
    if bp.pressure_short():
        score += 0.24

    if snapshot.rsi_ready:
        if RSI_OS_PENALTY <= snapshot.rsi_value <= RSI_SHORT_MAX:
            score += 0.16
        elif snapshot.rsi_value > RSI_SHORT_MAX:
            score += 0.08

    if ob.imbalance <= IMBALANCE_SHORT:
        score += 0.14

    if 0 < snapshot.ema_trend and price < snapshot.ema_trend:
        score += 0.12

    if snapshot.ema_fast < snapshot.ema_slow and snapshot.ema_slow > 0:
        score += 0.10

    if snapshot.vol_ready and snapshot.vol_zscore >= VOL_ZSCORE_MIN:
        score += 0.08

    if snapshot.macd_ready and snapshot.macd_histogram < 0:
        score += 0.04

    if snapshot.stoch_ready and snapshot.stoch_k < snapshot.stoch_d and snapshot.stoch_k > 20:
        score += 0.04

    if snapshot.bb_ready and price < snapshot.bb_mid:
        score += 0.04

    if snapshot.vwap > 0 and price < snapshot.vwap:
        score += 0.04

    return min(score, 1.0)


async def score_snapshot(price: float, ob) -> tuple[float, float]:
    """Returneaza (score_long, score_short) calculat atomic sub _ind_lock.

    v0.7.9 Bug 5 fix: pulse.py apeleaza asta in loc de _score_long(ind,...)
    direct. Previne citirea ind partial-updated in mid-candle.
    """
    async with _ind_lock:
        # Copiem campurile relevante in variabile locale sub lock
        snap = IndicatorState()
        snap.rsi_value   = ind.rsi_value
        snap.rsi_ready   = ind.rsi_ready
        snap.atr_value   = ind.atr_value
        snap.atr_ready   = ind.atr_ready
        snap.ema_fast    = ind.ema_fast
        snap.ema_slow    = ind.ema_slow
        snap.ema_trend   = ind.ema_trend
        snap.bb_upper    = ind.bb_upper
        snap.bb_mid      = ind.bb_mid
        snap.bb_lower    = ind.bb_lower
        snap.bb_ready    = ind.bb_ready
        snap.vwap        = ind.vwap
        snap.vol_zscore  = ind.vol_zscore
        snap.vol_ready   = ind.vol_ready
        snap.macd_line      = ind.macd_line
        snap.macd_signal    = ind.macd_signal
        snap.macd_histogram = ind.macd_histogram
        snap.macd_ready     = ind.macd_ready
        snap.stoch_k     = ind.stoch_k
        snap.stoch_d     = ind.stoch_d
        snap.stoch_ready = ind.stoch_ready

    # Calculul scorurilor e in afara lock-ului (nu modifica ind)
    score_l = _score_long(snap, ob, price)
    score_s = _score_short(snap, ob, price)
    return score_l, score_s


def update_indicators(price: float, kline_data: dict) -> None:
    """Update all indicators from latest confirmed candle.

    v0.7.9: achizitioneaza _ind_lock la scriere pentru a preveni
    citire partiala din score_snapshot() in pulse.
    Apelata din pybit WebSocket thread via run_coroutine_threadsafe.
    """
    from .indicators import compute_all
    results = compute_all(price, kline_data)

    # Scriem atomic sub lock
    loop = asyncio.get_event_loop()
    future = asyncio.run_coroutine_threadsafe(
        _update_ind_locked(results, price), loop
    )
    try:
        future.result(timeout=1.0)
    except Exception as e:
        logger.warning(f"[strategy] update_indicators lock timeout: {e}")
        # Fallback: scrie fara lock (mai bine date usor inconsistente decat nimic)
        _apply_ind(results, price)


async def _update_ind_locked(results: dict, price: float) -> None:
    """Scrie rezultatele in ind sub _ind_lock."""
    async with _ind_lock:
        _apply_ind(results, price)


def _apply_ind(results: dict, price: float) -> None:
    """Aplica results dict pe ind. Apelata sub lock sau in fallback."""
    ind.last_price   = price
    ind.ema_fast     = results.get("ema_fast",  ind.ema_fast)
    ind.ema_slow     = results.get("ema_slow",  ind.ema_slow)
    ind.ema_trend    = results.get("ema_trend", ind.ema_trend)
    ind.rsi_value    = results.get("rsi",       ind.rsi_value)
    ind.rsi_ready    = results.get("rsi_ready", ind.rsi_ready)
    ind.atr_value    = results.get("atr",       ind.atr_value)
    ind.atr_ready    = results.get("atr_ready", ind.atr_ready)
    ind.bb_upper     = results.get("bb_upper",  ind.bb_upper)
    ind.bb_mid       = results.get("bb_mid",    ind.bb_mid)
    ind.bb_lower     = results.get("bb_lower",  ind.bb_lower)
    ind.bb_ready     = results.get("bb_ready",  ind.bb_ready)
    ind.vwap         = results.get("vwap",      ind.vwap)
    ind.vol_zscore   = results.get("vol_zscore",  ind.vol_zscore)
    ind.vol_ready    = results.get("vol_ready",   ind.vol_ready)
    ind.macd_line      = results.get("macd_line",      ind.macd_line)
    ind.macd_signal    = results.get("macd_signal",    ind.macd_signal)
    ind.macd_histogram = results.get("macd_histogram", ind.macd_histogram)
    ind.macd_ready     = results.get("macd_ready",     ind.macd_ready)
    ind.stoch_k      = results.get("stoch_k",   ind.stoch_k)
    ind.stoch_d      = results.get("stoch_d",   ind.stoch_d)
    ind.stoch_ready  = results.get("stoch_ready", ind.stoch_ready)


async def evaluate(price: float) -> None:
    """Main strategy evaluation — called every confirmed candle."""
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

    # If position open: evaluate exit conditions
    if pos:
        closed = await pm.evaluate(price)
        if not closed:
            score, _ = await score_snapshot(price, compute_ob())
            if score >= 0.85 and pos:
                await pm.try_pyramid(
                    side=pos,
                    price=price,
                    score=score,
                    stop_loss=price * (1 - 0.0008 if pos == "long" else 1 + 0.0008),
                    take_profit=price * (1 + 0.004 if pos == "long" else 1 - 0.004),
                )
        return

    # No position: evaluate entry conditions
    if not state.running or state.paused:
        return
    if not risk.can_open():
        return
    if not regime.allow_entry():
        return

    ob = compute_ob()
    score_l, score_s = await score_snapshot(price, ob)

    # Spread gate
    spread_bps = state.orderbook.spread / price * 10_000 if price > 0 else 999
    atr_ratio  = ind.atr_value / (ATR_BASELINE * price) if price > 0 else 1.0
    max_spread = BASE_SPREAD_BPS * (1 + ATR_SPREAD_MULT * atr_ratio)
    if spread_bps > max_spread:
        return

    # ATR gate
    if ind.atr_ready:
        atr_pct = ind.atr_value / price if price > 0 else 0
        if not (ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT):
            return

    # Anti-manipulation gate
    if anti_manipulation.is_suspicious():
        return

    # MTF gate
    if not mtf.ready:
        return

    # Funding gate
    await funding.maybe_refresh(config.symbol)

    if score_l >= ENTRY_THRESHOLD:
        if not funding.can_enter_long():
            return
        if price <= mtf.ema50:
            return
        # Entry LONG
        from .limit_order_manager import place_entry_order
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
        trade_id = await place_entry_order(
            side="Buy", qty=qty, price=price,
            stop_loss=sl, take_profit=tp,
        )
        if trade_id:
            pm.on_open("long", qty, price, trade_id)
            with state.lock:
                state.open_position = "long"
                state.open_qty = qty
            logger.info(
                f"LONG bp score={score_l:.3f}/{ENTRY_THRESHOLD} | "
                f"price={price} qty={qty} sl={sl:.2f} tp={tp:.2f}"
            )

    elif score_s >= ENTRY_THRESHOLD:
        if not funding.can_enter_short():
            return
        if price >= mtf.ema50:
            return
        from .limit_order_manager import place_entry_order
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
        trade_id = await place_entry_order(
            side="Sell", qty=qty, price=price,
            stop_loss=sl, take_profit=tp,
        )
        if trade_id:
            pm.on_open("short", qty, price, trade_id)
            with state.lock:
                state.open_position = "short"
                state.open_qty = qty
            logger.info(
                f"SHORT bp score={score_s:.3f}/{ENTRY_THRESHOLD} | "
                f"price={price} qty={qty} sl={sl:.2f} tp={tp:.2f}"
            )
