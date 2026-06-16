"""Telegram bot UI v1.0.6 — fix pm constante v1.3.3 + funding attrs.

Changelog:
  v1.0.6 —
    FIX: _send_tp() si _send_config() accesau pm_mod.SL_PCT / TP1_PCT etc.
      care NU mai exista in position_manager v1.3.3.
      Fix: _get_pm_vals() citeste din _DEFAULT_* + profil la runtime.
    FIX: _send_funding() accesa funding._next_funding_ms si funding._near_funding()
      care nu exista in FundingRateMonitor v1.1.1.
      Fix: campurile lipsesc -> afisam doar rate + can_enter_long/short.
  v1.0.5 — /daily, rate limiter, consecutive_losses property fix.
  v1.0.3 — /history [N], /risk, /start feed check.
"""
from __future__ import annotations

import asyncio
import time
from loguru import logger
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

from .config import config
from .state import state
from .trader import trader
from .performance import perf

_bot: Bot | None = None
_last_send_ts: float = 0.0
_SEND_MIN_INTERVAL = 1.0


async def send_message(text: str) -> None:
    if not config.telegram_token or not config.telegram_chat_id:
        return
    global _bot, _last_send_ts
    if _bot is None:
        _bot = Bot(token=config.telegram_token)
    now = time.monotonic()
    wait = _SEND_MIN_INTERVAL - (now - _last_send_ts)
    if wait > 0:
        await asyncio.sleep(wait)
    try:
        await _bot.send_message(
            chat_id=config.telegram_chat_id,
            text=text,
            parse_mode="Markdown",
        )
        _last_send_ts = time.monotonic()
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def _check_owner(u: Update) -> bool:
    if not config.telegram_chat_id:
        return True
    user = u.effective_user
    if user is None:
        return False
    return str(user.id) == str(config.telegram_chat_id)


def _get_pm_vals() -> dict:
    """Citeste TP/SL/trail/hold din pm v1.3.3 _DEFAULT_* + profil la runtime."""
    import apex_scalper.position_manager as pm
    prof = {}
    try:
        from .config import config as cfg
        prof = cfg.profile(cfg.symbol)
    except Exception:
        pass
    return {
        "TP1_PCT":        prof.get("tp1_pct",        getattr(pm, "_DEFAULT_TP1_PCT",       0.0030)),
        "TP2_PCT":        prof.get("tp2_pct",        getattr(pm, "_DEFAULT_TP2_PCT",       0.0060)),
        "TP3_PCT":        prof.get("tp3_pct",        getattr(pm, "_DEFAULT_TP3_PCT",       0.0100)),
        "TP1_FRACTION":   prof.get("tp1_fraction",   getattr(pm, "_DEFAULT_TP1_FRACTION",  0.40)),
        "TP2_FRACTION":   prof.get("tp2_fraction",   getattr(pm, "_DEFAULT_TP2_FRACTION",  0.30)),
        "TP3_FRACTION":   prof.get("tp3_fraction",   getattr(pm, "_DEFAULT_TP3_FRACTION",  0.30)),
        "SL_PCT":         prof.get("sl_pct",         getattr(pm, "_DEFAULT_SL_PCT",        0.0020)),
        "TRAIL_PCT":      prof.get("trail_pct",      getattr(pm, "_DEFAULT_TRAIL_PCT",     0.0030)),
        "TRAIL_DELTA":    prof.get("trail_delta",    getattr(pm, "_DEFAULT_TRAIL_DELTA",   0.0010)),
        "MAX_HOLD":       prof.get("max_hold_candles", getattr(pm, "_DEFAULT_MAX_HOLD",    4)),
        "MAX_PYRAMID":    prof.get("max_pyramid_adds", getattr(pm, "_DEFAULT_MAX_PYRAMID", 0)),
        "ROUND_TRIP_FEE": getattr(pm, "ROUND_TRIP_FEE", 0.0011),
    }


# ---------------------------------------------------------------------------
# NOTIFICARI AUTOMATE
# ---------------------------------------------------------------------------

async def notify_open(side: str, qty: float, price: float, sl: float, tp1: float) -> None:
    icon = "\U0001f7e2" if side == "long" else "\U0001f534"
    await send_message(
        f"{icon} *POZITIE DESCHISA \u2014 {side.upper()}*\n"
        f"Entry: `{price}` | Qty: `{qty}`\n"
        f"SL: `{sl:.2f}` | TP1: `{tp1:.2f}`"
    )


async def notify_tp(side: str, level: int, qty_closed: float, pnl_usdt: float) -> None:
    icon = "\U0001f7e2" if side == "long" else "\U0001f534"
    pnl_icon = "\u2b06\ufe0f" if pnl_usdt >= 0 else "\u2b07\ufe0f"
    await send_message(
        f"{icon} *TP{level} HIT \u2014 {side.upper()}*\n"
        f"Inchis qty: `{qty_closed}` {pnl_icon} PnL: `{pnl_usdt:+.4f} USDT`"
    )


async def notify_sl(side: str, qty: float, pnl_usdt: float) -> None:
    await send_message(
        f"\u274c *SL HIT \u2014 {side.upper()}*\n"
        f"Qty: `{qty}` | PnL: `{pnl_usdt:+.4f} USDT`"
    )


async def notify_close(side: str, qty: float, pnl_usdt: float, reason: str = "") -> None:
    pnl_icon = "\u2b06\ufe0f" if pnl_usdt >= 0 else "\u2b07\ufe0f"
    reason_map = {
        "TRAIL":   "\U0001f6d1 Trail stop",
        "TIMEOUT": "\u23f0 Timeout (max candle)",
        "MANUAL":  "\U0001f91a Manual close",
    }
    reason_str = reason_map.get(reason, f"`{reason}`") if reason else ""
    await send_message(
        f"\U0001f4e4 *POZITIE INCHISA \u2014 {side.upper()}*\n"
        f"Qty: `{qty}` {pnl_icon} PnL: `{pnl_usdt:+.4f} USDT`\n"
        f"{reason_str}"
    )


# ---------------------------------------------------------------------------
# MENIU INLINE
# ---------------------------------------------------------------------------

def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4ca Status",     callback_data="status"),
         InlineKeyboardButton("\U0001f4b0 PnL",        callback_data="pnl")],
        [InlineKeyboardButton("\U0001f4b3 Balance",    callback_data="balance"),
         InlineKeyboardButton("\U0001f52e Signals",    callback_data="signals")],
        [InlineKeyboardButton("\U0001f3af Score",      callback_data="score"),
         InlineKeyboardButton("\U0001f4c8 Regime",     callback_data="regime")],
        [InlineKeyboardButton("\U0001f4cc TP/SL",      callback_data="tp"),
         InlineKeyboardButton("\U0001f4b8 Funding",    callback_data="funding")],
        [InlineKeyboardButton("\u2699\ufe0f Config",   callback_data="config"),
         InlineKeyboardButton("\U0001f6e1 Risk",       callback_data="risk")],
        [InlineKeyboardButton("\U0001f4ca Metrics",    callback_data="metrics"),
         InlineKeyboardButton("\U0001f4dc History",    callback_data="history")],
        [InlineKeyboardButton("\u23f8 Pause",          callback_data="pause"),
         InlineKeyboardButton("\u25b6\ufe0f Resume",   callback_data="resume")],
        [InlineKeyboardButton("\U0001f504 Warmup",     callback_data="warmup"),
         InlineKeyboardButton("\U0001f441 Watchdog",   callback_data="watchdog")],
        [InlineKeyboardButton("\U0001f6d1 Stop + Close", callback_data="stop")],
    ])


async def cmd_menu(u: Update, c: ContextTypes.DEFAULT_TYPE):
    with state.lock:
        pos     = state.open_position or "none"
        price   = state.last_price
        running = state.running
        paused  = state.paused
    bot_status = "\u2705 ACTIV" if (running and not paused) else ("\u23f8 PAUZA" if paused else "\U0001f6d1 OPRIT")
    text = (
        f"\u26a1 *Apex Scalper v1.0.6*\n"
        f"`{config.symbol}` | {bot_status}\n"
        f"Price: `{price}` | Pozitie: `{pos}`"
    )
    await u.message.reply_text(text, parse_mode="Markdown", reply_markup=_main_menu_keyboard())


async def _handle_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query
    await query.answer()
    data = query.data

    if data in ("stop", "pause") and not _check_owner(u):
        await query.edit_message_text("\u26d4 Unauthorized.")
        return

    dispatch = {
        "status":  _send_status,
        "pnl":     _send_pnl,
        "signals": _send_signals,
        "score":   _send_score,
        "regime":  _send_regime,
        "tp":      _send_tp,
        "funding": _send_funding,
        "config":  _send_config,
        "metrics": _send_metrics,
        "warmup":  _do_warmup,
        "watchdog":_send_watchdog,
        "risk":    _send_risk,
        "history": lambda t: _send_history(t, n=10),
    }
    if data in dispatch:
        await dispatch[data](query)
        return

    if data == "balance":
        bal = await trader.get_balance()
        await query.edit_message_text(
            f"\U0001f4b3 Balance: `{bal:.4f} USDT`",
            parse_mode="Markdown", reply_markup=_main_menu_keyboard()
        )
    elif data == "pause":
        state.paused = True
        await query.edit_message_text(
            "\u23f8 *PAUZAT* \u2014 fara intrari noi.",
            parse_mode="Markdown", reply_markup=_main_menu_keyboard()
        )
    elif data == "resume":
        from .risk import risk
        state.paused = False
        risk.reset_consecutive_losses()
        await query.edit_message_text(
            "\u25b6\ufe0f *RELUAT* \u2014 trading activ.",
            parse_mode="Markdown", reply_markup=_main_menu_keyboard()
        )
    elif data == "stop":
        state.running = False
        await trader.close_position()
        await query.edit_message_text(
            "\U0001f6d1 *Bot OPRIT* \u2014 pozitie inchisa.",
            parse_mode="Markdown"
        )


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _get_feed_elapsed() -> float:
    try:
        from .watchdog import get_last_kline_ts
        last = get_last_kline_ts()
    except ImportError:
        try:
            from .watchdog import _last_kline_ts
            last = _last_kline_ts
        except ImportError:
            return -1.0
    return time.monotonic() - last if last > 0 else -1.0


async def _send_status(target):
    from .strategy import ind
    from .orderbook_analytics import ob_signals
    from .regime_filter import regime
    elapsed = _get_feed_elapsed()
    feed_icon = "\u2705" if 0 <= elapsed < 90 else "\U0001f534"
    with state.lock:
        pos    = state.open_position or "none"
        price  = state.last_price
        spread = state.orderbook.spread
        bid_d  = state.orderbook.bid_depth(5)
        ask_d  = state.orderbook.ask_depth(5)
    msg = (
        f"\U0001f4ca *Status* `{config.symbol}`\n"
        f"Bot: {'\u2705' if state.running else '\U0001f6d1'} "
        f"{'\u23f8 PAUZA' if state.paused else '\u25b6\ufe0f ACTIV'}\n"
        f"Feed: {feed_icon} `{elapsed:.0f}s` | Spread: `{spread}`\n"
        f"Pozitie: `{pos}` | Price: `{price}`\n"
        f"Bid\u2193 `{bid_d:.3f}` Ask\u2191 `{ask_d:.3f}`\n"
        f"EMA 9/21/50: `{ind.ema_fast:.1f}`/`{ind.ema_slow:.1f}`/`{ind.ema_trend:.1f}`\n"
        f"RSI: `{ind.rsi_value:.1f}` ATR: `{ind.atr_value:.4f}`\n"
        f"Regime: `{regime.label}` ADX `{regime.adx}` sz\u00d7`{regime.size_factor():.2f}`"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_pnl(target):
    msg = (
        f"\U0001f4b0 *PnL*\n"
        f"Realized: `{state.realized_pnl:+.4f} USDT`\n"
        f"Daily: `{state.daily_pnl:+.4f} USDT`\n"
        f"Trades: `{state.total_trades}` | WR: `{state.winrate}%`"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_signals(target):
    from .strategy import ind
    from .orderbook_analytics import ob_signals
    bb    = f"`{ind.bb_lower:.1f}`\u2026`{ind.bb_upper:.1f}`" if ind.bb_ready  else "`warmup`"
    macd  = f"hist=`{ind.macd_histogram:+.5f}`"               if ind.macd_ready else "`warmup`"
    stoch = f"%K=`{ind.stoch_k:.1f}` %D=`{ind.stoch_d:.1f}`" if ind.stoch_ready else "`warmup`"
    msg = (
        f"\U0001f52e *Signals* `{config.symbol}`\n"
        f"EMA 9/21/50: `{ind.ema_fast:.2f}`/`{ind.ema_slow:.2f}`/`{ind.ema_trend:.2f}`\n"
        f"RSI(14): `{ind.rsi_value:.2f}` ({'\u2705' if ind.rsi_ready else '\u23f3 warmup'})\n"
        f"ATR(14): `{ind.atr_value:.4f}` ({'\u2705' if ind.atr_ready else '\u23f3 warmup'})\n"
        f"MACD: {macd} ({'\u2705' if ind.macd_ready else '\u23f3 warmup'})\n"
        f"StochRSI: {stoch}\n"
        f"BB(20,2): {bb}\n"
        f"Vol Z: `{ind.vol_zscore:.2f}` ({'\u2705' if ind.vol_ready else '\u23f3 warmup'}) VWAP: `{ind.vwap:.2f}`\n"
        f"OB imbalance: `{ob_signals.imbalance:.4f}` | pressure: `{ob_signals.pressure_score:.4f}`"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_score(target):
    from .strategy import ind, score_snapshot, ENTRY_THRESHOLD
    from .orderbook_analytics import compute as compute_ob
    with state.lock:
        price = state.last_price
    ob = compute_ob()
    score_l, score_s = await score_snapshot(price, ob)
    def bar(v, w=10):
        f = int(round(min(v, 1.0) * w))
        return "\u2588" * f + "\u2591" * (w - f)
    l_str = "\u2705 INTRARE" if score_l >= ENTRY_THRESHOLD else f"`{ENTRY_THRESHOLD - score_l:.3f}` lipsa"
    s_str = "\u2705 INTRARE" if score_s >= ENTRY_THRESHOLD else f"`{ENTRY_THRESHOLD - score_s:.3f}` lipsa"
    msg = (
        f"\U0001f3af *Score live* (prag `{ENTRY_THRESHOLD}`)\n"
        f"LONG:  `{score_l:.4f}` {bar(score_l)} {l_str}\n"
        f"SHORT: `{score_s:.4f}` {bar(score_s)} {s_str}"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_regime(target):
    from .regime_filter import regime
    from .strategy import ind
    icon = {"TRENDING": "\U0001f7e2", "RANGING": "\U0001f534", "VOLATILE": "\U0001f7e1", "NEUTRAL": "\U0001f7e4"}.get(regime.label, "\u26ab")
    msg = (
        f"{icon} *Regime: {regime.label}*\n"
        f"ADX(14): `{regime.adx}` | ATR: `{ind.atr_value:.4f}`\n"
        f"Entry: `{'\u2705 permis' if regime.allow_entry() else '\u274c BLOCAT'}` | sz\u00d7: `{regime.size_factor():.2f}`"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_tp(target):
    """FIX v1.0.6: citeste TP/SL din _get_pm_vals() nu din pm_mod.SL_PCT etc."""
    from .position_manager import position_manager as pm
    pv = _get_pm_vals()
    with state.lock:
        pos   = state.open_position
        price = state.last_price
        qty   = state.open_qty
    if not pos or pm._entry_price == 0:
        msg = "\u25ab\ufe0f Nicio pozitie deschisa."
    else:
        entry = pm._entry_price
        is_long = pos == "long"
        sl_p  = round(entry * (1 - pv["SL_PCT"]  if is_long else 1 + pv["SL_PCT"]),  6)
        tp1_p = round(entry * (1 + pv["TP1_PCT"] if is_long else 1 - pv["TP1_PCT"]), 6)
        tp2_p = round(entry * (1 + pv["TP2_PCT"] if is_long else 1 - pv["TP2_PCT"]), 6)
        tp3_p = round(entry * (1 + pv["TP3_PCT"] if is_long else 1 - pv["TP3_PCT"]), 6)
        pnl_pct  = (price - entry) / entry if is_long else (entry - price) / entry
        pnl_u    = round(pnl_pct * entry * qty, 4)
        pnl_icon = "\u2b06\ufe0f" if pnl_pct >= 0 else "\u2b07\ufe0f"
        msg = (
            f"{'\U0001f7e2' if is_long else '\U0001f534'} *{pos.upper()}* entry `{entry}` acum `{price}` {pnl_icon}\n"
            f"PnL: `{pnl_u:+.4f} USDT` (`{pnl_pct*100:+.4f}%`) | hold `{pm._hold_candles}/{pv['MAX_HOLD']}`\n"
            f"SL: `{sl_p}` | TP1: {'\u2705' if pm._tp1_hit else f'`{tp1_p}`'} "
            f"TP2: {'\u2705' if pm._tp2_hit else f'`{tp2_p}`'} "
            f"TP3: {'\u2705' if pm._tp3_hit else f'`{tp3_p}`'}\n"
            f"Trail: {'\U0001f534 ON' if pm._trail_active else '\u26aa off'} | Pyramid adds: `{pm._pyramid_adds}`"
        )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_funding(target):
    """FIX v1.0.6: funding v1.1.1 nu are _next_funding_ms / _near_funding()."""
    from .funding_rate import funding
    msg = (
        f"\U0001f4b8 *Funding* `{config.symbol}`\n"
        f"Rata: `{funding.rate_pct}` (`{funding.rate:.8f}`)\n"
        f"LONG:  {'\u2705 permis' if funding.can_enter_long()  else '\u274c blocat'}\n"
        f"SHORT: {'\u2705 permis' if funding.can_enter_short() else '\u274c blocat'}"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_config(target):
    """FIX v1.0.6: citeste TP/SL din _get_pm_vals() nu din pm_mod.TP1_PCT etc."""
    import apex_scalper.strategy as sm
    from .mtf_filter import mtf
    from .risk import risk
    pv = _get_pm_vals()
    msg = (
        f"\u2699\ufe0f *Config live* `{config.symbol}`\n\n"
        f"*Exchange*\n"
        f"  Leverage: `{config.leverage}x` | Size: `{config.order_size_usdt} USDT`\n"
        f"  Mode: `{'\u26a0\ufe0f MAINNET' if not config.testnet else 'TESTNET'}`\n\n"
        f"*Strategie*\n"
        f"  Entry threshold: `{sm.ENTRY_THRESHOLD}`\n"
        f"  RSI long/short: `{sm.RSI_LONG_MIN}` / `{sm.RSI_SHORT_MAX}`\n"
        f"  ATR: `{sm.ATR_MIN_PCT}` \u2014 `{sm.ATR_MAX_PCT}` | Vol Z min: `{sm.VOL_ZSCORE_MIN}`\n"
        f"  Spread max: `{sm.BASE_SPREAD_BPS}` bps\n\n"
        f"*Scale-out*\n"
        f"  TP1: `{pv['TP1_PCT']:.4f}` ({pv['TP1_FRACTION']:.0%}) | TP2: `{pv['TP2_PCT']:.4f}` ({pv['TP2_FRACTION']:.0%})\n"
        f"  TP3: `{pv['TP3_PCT']:.4f}` ({pv['TP3_FRACTION']:.0%}) | SL: `{pv['SL_PCT']:.4f}`\n"
        f"  Trail: `{pv['TRAIL_PCT']:.4f}` \u0394`{pv['TRAIL_DELTA']:.4f}` | Max hold: `{pv['MAX_HOLD']}` candle\n"
        f"  Max pyramid: `{pv['MAX_PYRAMID']}`\n\n"
        f"*Risk*\n"
        f"  Daily loss limit: `{risk._daily_limit:.2f} USDT`\n"
        f"  Consecutive losses: `{risk.consecutive_losses}` / `5`\n\n"
        f"*MTF*\n"
        f"  EMA50(15m): `{mtf.ema50:.2f}` ({'\u2705 ready' if mtf.ready else '\u274c not ready'})"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_metrics(target):
    from .risk import risk
    with risk._lock:
        kelly_trades = len(risk._trade_results)
    msg = (
        f"\U0001f4ca *Performance*\n"
        f"Trades: `{len(perf.trades)}`\n"
        f"Win Rate: `{perf.win_rate:.1f}%`\n"
        f"Sharpe: `{perf.sharpe:.2f}`\n"
        f"Profit Factor: `{perf.profit_factor:.2f}`\n"
        f"Expectancy: `{perf.expectancy:+.4f} USDT`\n"
        f"Avg Win: `{perf.avg_win:+.4f}` | Avg Loss: `{perf.avg_loss:+.4f}`\n"
        f"Max DD: `{perf.max_drawdown:.4f} USDT` (`{perf.max_drawdown_pct:.2f}%`)\n"
        f"Kelly trades: `{kelly_trades}` (activ dupa 20)"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_watchdog(target):
    elapsed = _get_feed_elapsed()
    status = "\u2705 OK" if 0 <= elapsed < 90 else "\U0001f534 DEAD"
    msg = f"\U0001f441 *Watchdog* {status}\nLast kline: `{elapsed:.0f}s` ago"
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_risk(target):
    from .risk import risk
    can = risk.can_open()
    msg = (
        f"\U0001f6e1 *Risk Manager*\n"
        f"Can open: `{'\u2705 DA' if can else '\u274c NU'}`\n"
        f"Daily loss: `{risk._daily_loss:.4f}` / `{risk._daily_limit:.2f} USDT`\n"
        f"Consec losses: `{risk.consecutive_losses}` / `5`\n"
        f"Open positions: `{risk._open_count}` / `1`\n"
        f"Kelly factor: `{risk.kelly_f:.3f}` (activ dupa 20 trades)"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_history(target, n: int = 10):
    try:
        from .persistence import db
        trades = db.get_last_trades(config.symbol, limit=n)
    except Exception as e:
        trades = []
        logger.debug(f"history fetch error: {e}")

    if not trades:
        msg = "\u2139\ufe0f Nu sunt trade-uri inchise inca."
    else:
        lines = [f"\U0001f4dc *Ultimele {len(trades)} trade-uri* `{config.symbol}`\n"]
        for t in trades:
            side   = t.get("side",     "?")
            entry  = t.get("entry",    0)
            pnl    = t.get("pnl_usdt", 0)
            reason = t.get("reason",   "?")
            ts     = t.get("closed_at", "")[:16]
            icon   = "\u2705" if pnl >= 0 else "\u274c"
            lines.append(
                f"{icon} `{side.upper()}` @ `{entry}` \u2192 `{pnl:+.4f} USDT` | {reason} | {ts}"
            )
        msg = "\n".join(lines)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _do_warmup(target):
    msg_start = "\U0001f504 *Warmup pornit...* descarca 60 candle-uri historice."
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg_start, parse_mode="Markdown")
    else:
        await target.message.reply_text(msg_start, parse_mode="Markdown")
    try:
        from .indicator_warmup import warmup_indicators
        ok = await warmup_indicators(config.symbol)
        result = "\u2705 Warmup complet \u2014 toti indicatorii ready!" if ok else "\u26a0\ufe0f Warmup partial \u2014 verifica log-ul."
    except Exception as e:
        result = f"\u274c Warmup error: {e}"
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(result, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(result, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# COMENZI TEXT
# ---------------------------------------------------------------------------

async def cmd_menu_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await cmd_menu(u, c)

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    elapsed  = _get_feed_elapsed()
    feed_ok  = 0 <= elapsed < 90
    state.running = True
    state.paused  = False
    feed_warn = (
        "\n\u26a0\ufe0f Feed WS inactiv \u2014 nu au venit candle-uri in ultimele 90s."
    ) if not feed_ok else ""
    await u.message.reply_text(
        f"\u2705 *Apex Scalper STARTED*{feed_warn}",
        parse_mode="Markdown"
    )

async def cmd_stop(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _check_owner(u):
        await u.message.reply_text("\u26d4 Unauthorized.")
        return
    state.running = False
    await trader.close_position()
    await u.message.reply_text("\U0001f6d1 *Bot STOPPED* \u2014 position closed", parse_mode="Markdown")

async def cmd_pause(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _check_owner(u):
        await u.message.reply_text("\u26d4 Unauthorized.")
        return
    state.paused = True
    await u.message.reply_text("\u23f8 *PAUSED* \u2014 no new entries", parse_mode="Markdown")

async def cmd_resume(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from .risk import risk
    state.paused = False
    risk.reset_consecutive_losses()
    await u.message.reply_text("\u25b6\ufe0f *RESUMED*", parse_mode="Markdown")

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_status(u)

async def cmd_pnl(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_pnl(u)

async def cmd_balance(u: Update, c: ContextTypes.DEFAULT_TYPE):
    bal = await trader.get_balance()
    await u.message.reply_text(f"\U0001f4b3 Balance: `{bal:.4f} USDT`", parse_mode="Markdown")

async def cmd_close(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _check_owner(u):
        await u.message.reply_text("\u26d4 Unauthorized.")
        return
    await trader.close_position()
    await u.message.reply_text("\U0001f4e4 *Close executed*", parse_mode="Markdown")

async def cmd_signals(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_signals(u)

async def cmd_regime(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_regime(u)

async def cmd_tp(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_tp(u)

async def cmd_funding(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_funding(u)

async def cmd_config(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_config(u)

async def cmd_warmup(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _do_warmup(u)

async def cmd_metrics(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_metrics(u)

async def cmd_watchdog(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_watchdog(u)

async def cmd_risk(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await _send_risk(u)

async def cmd_history(u: Update, c: ContextTypes.DEFAULT_TYPE):
    n = 10
    if c.args:
        try:
            n = max(1, min(int(c.args[0]), 30))
        except ValueError:
            pass
    await _send_history(u, n=n)

async def cmd_daily(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("\U0001f4c5 *Generez raportul zilnic...*", parse_mode="Markdown")
    try:
        from .daily_report import send_daily_report
        await send_daily_report(config.symbol)
    except Exception as e:
        await u.message.reply_text(f"\u274c Eroare: `{e}`", parse_mode="Markdown")

async def cmd_analytics(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from .analytics import analytics
    msg = analytics.telegram_breakdown(config.symbol, days=7)
    if not msg:
        msg = "\u2139\ufe0f Nu sunt suficiente date (minim 1 trade inchis)."
    await u.message.reply_text(msg, parse_mode="Markdown")

async def cmd_pulse(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from .pulse import set_pulse_active, is_pulse_active
    args = c.args
    if args and args[0].lower() == "off":
        set_pulse_active(False)
        await u.message.reply_text("\u23f8 *Pulse OPRIT*", parse_mode="Markdown")
    elif args and args[0].lower() == "on":
        set_pulse_active(True)
        await u.message.reply_text("\u2705 *Pulse PORNIT*", parse_mode="Markdown")
    else:
        status = "activ" if is_pulse_active() else "oprit"
        await u.message.reply_text(f"\u26a1 Pulse e `{status}`. `/pulse on` sau `/pulse off`.", parse_mode="Markdown")

async def cmd_setparam(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _check_owner(u):
        await u.message.reply_text("\u26d4 Unauthorized.")
        return
    import apex_scalper.strategy as sm
    import apex_scalper.risk as rm
    import apex_scalper.position_manager as pm
    args = c.args
    if len(args) != 2:
        await u.message.reply_text("Usage: `/setparam <PARAM> <value>`", parse_mode="Markdown")
        return
    key, val = args[0].upper(), args[1]
    # Mapeaza parametrii la module si tipuri
    targets = {
        "RSI_LONG_MIN":    (sm, float), "RSI_SHORT_MAX":   (sm, float),
        "IMBALANCE_LONG":  (sm, float), "IMBALANCE_SHORT": (sm, float),
        "VOL_ZSCORE_MIN":  (sm, float), "ATR_MIN_PCT":     (sm, float),
        "ATR_MAX_PCT":     (sm, float), "ENTRY_THRESHOLD": (sm, float),
    }
    if key not in targets:
        await u.message.reply_text(
            f"\u274c Unknown `{key}`\nAvailable: {', '.join(f'`{k}`' for k in sorted(targets))}",
            parse_mode="Markdown"
        )
        return
    mod, cast = targets[key]
    old_val = getattr(mod, key, "?")
    setattr(mod, key, cast(val))
    await u.message.reply_text(
        f"\u2705 `{key}` schimbat: `{old_val}` \u2192 `{val}`",
        parse_mode="Markdown"
    )


# ---------------------------------------------------------------------------
# BUILD APP
# ---------------------------------------------------------------------------

def build_app():
    app = ApplicationBuilder().token(config.telegram_token).build()
    app.add_handler(CallbackQueryHandler(_handle_callback))
    for name, fn in [
        ("menu",      cmd_menu),
        ("start",     cmd_start),
        ("stop",      cmd_stop),
        ("pause",     cmd_pause),
        ("resume",    cmd_resume),
        ("status",    cmd_status),
        ("pnl",       cmd_pnl),
        ("balance",   cmd_balance),
        ("close",     cmd_close),
        ("signals",   cmd_signals),
        ("regime",    cmd_regime),
        ("tp",        cmd_tp),
        ("funding",   cmd_funding),
        ("config",    cmd_config),
        ("warmup",    cmd_warmup),
        ("metrics",   cmd_metrics),
        ("watchdog",  cmd_watchdog),
        ("risk",      cmd_risk),
        ("history",   cmd_history),
        ("daily",     cmd_daily),
        ("analytics", cmd_analytics),
        ("pulse",     cmd_pulse),
        ("setparam",  cmd_setparam),
    ]:
        app.add_handler(CommandHandler(name, fn))
    return app
