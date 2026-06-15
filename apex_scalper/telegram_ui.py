"""Telegram bot UI v0.9.9 — meniu inline + notificari automate.

Comands:
  /menu                 — meniu principal cu butoane inline
  /start /stop /pause /resume
  /status /pnl /balance /close
  /config               — configuratie live (symbol, leverage, size, thresholds)
  /warmup               — forteaza re-incarcare indicatori din date istorice
  /signals              — snapshot complet indicatori
  /regime               — market regime + ADX + Hurst + size factor
  /tp                   — status TP1/2/3 hit, trail, hold candles
  /funding              — funding rate + directii blocate
  /metrics              — performance report
  /analytics            — breakdown trades by reason/score/streak
  /watchdog             — WS health status
  /setparam KEY VALUE   — live strategy tuning (owner only)
  /pulse on|off         — toggle 1-minute pulse loop

Notificari automate:
  notify_open(side, qty, price, sl, tp1)   — apelata din strategy.py la deschidere
  notify_tp(side, level, qty, pnl_usdt)    — apelata din position_manager.py la TP hit
  notify_sl(side, qty, pnl_usdt)           — apelata din position_manager.py la SL hit
  notify_close(side, qty, pnl_usdt, reason)— apelata la inchidere pozitie

Changelog:
  v0.9.9 — /menu cu butoane inline, /config, /warmup,
    notificari automate open/TP1/TP2/TP3/SL/close.
  v0.8.7 — BUG 34 FIX: auth check comenzi distructive.
"""
from __future__ import annotations

from loguru import logger
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

from .config import config
from .state import state
from .trader import trader
from .performance import perf

_bot: Bot | None = None


async def send_message(text: str) -> None:
    if not config.telegram_token or not config.telegram_chat_id:
        return
    global _bot
    if _bot is None:
        _bot = Bot(token=config.telegram_token)
    try:
        await _bot.send_message(
            chat_id=config.telegram_chat_id,
            text=text,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def _check_owner(u: Update) -> bool:
    if not config.telegram_chat_id:
        return True
    user = u.effective_user
    if user is None:
        return False
    return str(user.id) == str(config.telegram_chat_id)


# ---------------------------------------------------------------------------
# NOTIFICARI AUTOMATE
# ---------------------------------------------------------------------------

async def notify_open(side: str, qty: float, price: float, sl: float, tp1: float) -> None:
    icon = "\U0001f7e2" if side == "long" else "\U0001f534"
    await send_message(
        f"{icon} *POZITIE DESCHISA — {side.upper()}*\n"
        f"Entry: `{price}` | Qty: `{qty}`\n"
        f"SL: `{sl:.2f}` | TP1: `{tp1:.2f}`"
    )


async def notify_tp(side: str, level: int, qty_closed: float, pnl_usdt: float) -> None:
    icon = "\U0001f7e2" if side == "long" else "\U0001f534"
    pnl_icon = "\u2b06\ufe0f" if pnl_usdt >= 0 else "\u2b07\ufe0f"
    await send_message(
        f"{icon} *TP{level} HIT — {side.upper()}*\n"
        f"Inchis qty: `{qty_closed}` {pnl_icon} PnL: `{pnl_usdt:+.4f} USDT`"
    )


async def notify_sl(side: str, qty: float, pnl_usdt: float) -> None:
    icon = "\U0001f534" if side == "long" else "\U0001f7e2"
    await send_message(
        f"{icon} *\u274c SL HIT — {side.upper()}*\n"
        f"Qty: `{qty}` | PnL: `{pnl_usdt:+.4f} USDT`"
    )


async def notify_close(side: str, qty: float, pnl_usdt: float, reason: str = "") -> None:
    pnl_icon = "\u2b06\ufe0f" if pnl_usdt >= 0 else "\u2b07\ufe0f"
    reason_str = f" | Motiv: `{reason}`" if reason else ""
    await send_message(
        f"\U0001f4e4 *POZITIE INCHISA — {side.upper()}*\n"
        f"Qty: `{qty}` {pnl_icon} PnL: `{pnl_usdt:+.4f} USDT`{reason_str}"
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
         InlineKeyboardButton("\U0001f4ca Metrics",    callback_data="metrics")],
        [InlineKeyboardButton("\u23f8 Pause",          callback_data="pause"),
         InlineKeyboardButton("\u25b6\ufe0f Resume",   callback_data="resume")],
        [InlineKeyboardButton("\U0001f504 Warmup",     callback_data="warmup"),
         InlineKeyboardButton("\U0001f441 Watchdog",   callback_data="watchdog")],
        [InlineKeyboardButton("\U0001f6d1 Stop + Close", callback_data="stop")],
    ])


async def cmd_menu(u: Update, c: ContextTypes.DEFAULT_TYPE):
    with state.lock:
        pos   = state.open_position or "none"
        price = state.last_price
        running = state.running
        paused  = state.paused
    bot_status = "\u2705 ACTIV" if (running and not paused) else ("\u23f8 PAUZA" if paused else "\U0001f6d1 OPRIT")
    text = (
        f"\u26a1 *Apex Scalper Menu*\n"
        f"Symbol: `{config.symbol}` | {bot_status}\n"
        f"Price: `{price}` | Pozitie: `{pos}`"
    )
    await u.message.reply_text(text, parse_mode="Markdown", reply_markup=_main_menu_keyboard())


async def _handle_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Handler pentru toate butoanele inline din meniu."""
    query = u.callback_query
    await query.answer()
    data = query.data

    # Comenzi distructive necesita auth
    if data in ("stop", "pause") and not _check_owner(u):
        await query.edit_message_text("\u26d4 Unauthorized.")
        return

    if data == "status":
        await _send_status(query)
    elif data == "pnl":
        await _send_pnl(query)
    elif data == "balance":
        bal = await trader.get_balance()
        await query.edit_message_text(
            f"\U0001f4b3 Balance: `{bal:.4f} USDT`",
            parse_mode="Markdown", reply_markup=_main_menu_keyboard()
        )
    elif data == "signals":
        await _send_signals(query)
    elif data == "score":
        await _send_score(query)
    elif data == "regime":
        await _send_regime(query)
    elif data == "tp":
        await _send_tp(query)
    elif data == "funding":
        await _send_funding(query)
    elif data == "config":
        await _send_config(query)
    elif data == "metrics":
        await _send_metrics(query)
    elif data == "warmup":
        await _do_warmup(query)
    elif data == "watchdog":
        await _send_watchdog(query)
    elif data == "pause":
        state.paused = True
        await query.edit_message_text(
            "\u23f8 *PAUZAT* — fara intrari noi.",
            parse_mode="Markdown", reply_markup=_main_menu_keyboard()
        )
    elif data == "resume":
        from .risk import risk
        state.paused = False
        risk.reset_consecutive_losses()
        await query.edit_message_text(
            "\u25b6\ufe0f *RELUAT* — trading activ.",
            parse_mode="Markdown", reply_markup=_main_menu_keyboard()
        )
    elif data == "stop":
        state.running = False
        await trader.close_position()
        await query.edit_message_text(
            "\U0001f6d1 *Bot OPRIT* — pozitie inchisa.",
            parse_mode="Markdown"
        )


# ---------------------------------------------------------------------------
# HELPERS PENTRU CALLBACK SI COMENZI
# ---------------------------------------------------------------------------

async def _send_status(target):
    from .strategy import ind
    from .orderbook_analytics import ob_signals
    from .regime_filter import regime
    with state.lock:
        pos    = state.open_position or "none"
        price  = state.last_price
        spread = state.orderbook.spread
        bid_d  = state.orderbook.bid_depth(5)
        ask_d  = state.orderbook.ask_depth(5)
    msg = (
        f"\U0001f4ca *Status* `{config.symbol}`\n"
        f"Running: {'\u2705' if state.running else '\U0001f6d1'} "
        f"Paused: {'\u23f8' if state.paused else '\u25b6\ufe0f'}\n"
        f"Pozitie: `{pos}` | Price: `{price}`\n"
        f"Spread: `{spread}` | Bid\u2193 `{bid_d:.3f}` Ask\u2191 `{ask_d:.3f}`\n"
        f"EMA 9/21/50: `{ind.ema_fast:.1f}`/`{ind.ema_slow:.1f}`/`{ind.ema_trend:.1f}`\n"
        f"RSI(14): `{ind.rsi_value:.1f}` | ATR: `{ind.atr_value:.2f}`\n"
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
    from .book_pressure import bp
    from .regime_filter import regime
    bb    = f"`{ind.bb_lower:.1f}` \u2026 `{ind.bb_upper:.1f}`" if ind.bb_ready else "`warmup`"
    macd  = f"hist=`{ind.macd_histogram:+.5f}`"                if ind.macd_ready  else "`warmup`"
    stoch = f"%K=`{ind.stoch_k:.1f}` %D=`{ind.stoch_d:.1f}`" if ind.stoch_ready else "`warmup`"
    msg = (
        f"\U0001f52e *Signals* `{config.symbol}`\n"
        f"EMA 9/21/50: `{ind.ema_fast:.2f}`/`{ind.ema_slow:.2f}`/`{ind.ema_trend:.2f}`\n"
        f"RSI(14): `{ind.rsi_value:.2f}` ({'ready' if ind.rsi_ready else 'warmup'})\n"
        f"ATR(14): `{ind.atr_value:.4f}` ({'ready' if ind.atr_ready else 'warmup'})\n"
        f"MACD(12,26,9): {macd}\n"
        f"StochRSI: {stoch}\n"
        f"BB(20,2): {bb}\n"
        f"Vol Z: `{ind.vol_zscore:.2f}` ({'ready' if ind.vol_ready else 'warmup'}) | VWAP: `{ind.vwap:.2f}`\n"
        f"OB imbalance: `{ob_signals.imbalance:.4f}` | pressure: `{ob_signals.pressure_score:.4f}`"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_score(target):
    from .strategy import ind, score_snapshot, ENTRY_THRESHOLD
    from .orderbook_analytics import compute as compute_ob
    from .state import state
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
        f"\U0001f3af *Score* (prag `{ENTRY_THRESHOLD}`)\n"
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
        f"Entry: `{'permis' if regime.allow_entry() else 'BLOCAT'}` | sz\u00d7: `{regime.size_factor():.2f}`"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_tp(target):
    import apex_scalper.position_manager as pm_mod
    from .position_manager import position_manager as pm
    with state.lock:
        pos   = state.open_position
        price = state.last_price
        qty   = state.open_qty
    if not pos or pm._entry_price == 0:
        msg = "\u25ab\ufe0f Nicio pozitie deschisa."
    else:
        entry = pm._entry_price
        sl_p  = round(entry * (1 - pm_mod.SL_PCT  if pos == "long" else 1 + pm_mod.SL_PCT), 2)
        tp1_p = round(entry * (1 + pm_mod.TP1_PCT if pos == "long" else 1 - pm_mod.TP1_PCT), 2)
        tp2_p = round(entry * (1 + pm_mod.TP2_PCT if pos == "long" else 1 - pm_mod.TP2_PCT), 2)
        tp3_p = round(entry * (1 + pm_mod.TP3_PCT if pos == "long" else 1 - pm_mod.TP3_PCT), 2)
        pnl_pct = (price - entry) / entry if pos == "long" else (entry - price) / entry
        pnl_u   = round(pnl_pct * entry * qty, 4)
        msg = (
            f"{'\U0001f7e2' if pos=='long' else '\U0001f534'} *{pos.upper()}* entry `{entry}` acum `{price}`\n"
            f"PnL: `{pnl_u:+.4f} USDT` | hold `{pm._hold_candles}/{pm_mod.MAX_HOLD_CANDLES}`\n"
            f"SL: `{sl_p}` | TP1: {'\u2705' if pm._tp1_hit else f'`{tp1_p}`'} "
            f"TP2: {'\u2705' if pm._tp2_hit else f'`{tp2_p}`'} "
            f"TP3: {'\u2705' if pm._tp3_hit else f'`{tp3_p}`'}\n"
            f"Trail: {'\U0001f534 ON' if pm._trail_active else 'off'} | Pyramid: `{pm._pyramid_adds}`"
        )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_funding(target):
    from .funding_rate import funding
    import time
    ttn = funding._next_funding_ms
    now_ms = int(time.time() * 1000)
    ttf = max(0, (ttn - now_ms) // 1000) if ttn else -1
    ttf_str = f"`{ttf // 3600}h {(ttf % 3600) // 60}m`" if ttf >= 0 else "`necunoscut`"
    msg = (
        f"\U0001f4b8 *Funding* `{config.symbol}`\n"
        f"Rata: `{funding.rate_pct}` (`{funding.rate:.8f}`)\n"
        f"LONG: {'\u2705' if funding.can_enter_long() else '\u274c blocat'}\n"
        f"SHORT: {'\u2705' if funding.can_enter_short() else '\u274c blocat'}\n"
        f"Aproape de plata: `{'DA \u26a0\ufe0f' if funding._near_funding() else 'NU'}`\n"
        f"Urmatoarea plata in: {ttf_str}"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_config(target):
    import apex_scalper.strategy as sm
    import apex_scalper.position_manager as pm
    from .mtf_filter import mtf
    msg = (
        f"\u2699\ufe0f *Config live* `{config.symbol}`\n\n"
        f"*Exchange*\n"
        f"  Leverage: `{config.leverage}x` | Size: `{config.order_size_usdt} USDT`\n"
        f"  Testnet: `{config.testnet}` | Mode: `{'\u26a0\ufe0f MAINNET' if not config.testnet else 'TESTNET'}`\n\n"
        f"*Strategie*\n"
        f"  Entry threshold: `{sm.ENTRY_THRESHOLD}`\n"
        f"  RSI long min/short max: `{sm.RSI_LONG_MIN}` / `{sm.RSI_SHORT_MAX}`\n"
        f"  ATR min/max: `{sm.ATR_MIN_PCT}` / `{sm.ATR_MAX_PCT}`\n"
        f"  Vol Z-score min: `{sm.VOL_ZSCORE_MIN}`\n"
        f"  Spread max: `{sm.BASE_SPREAD_BPS}` bps\n\n"
        f"*Scale-out*\n"
        f"  TP1: `{pm.TP1_PCT:.4f}` ({pm.TP1_FRACTION:.0%}) | TP2: `{pm.TP2_PCT:.4f}` ({pm.TP2_FRACTION:.0%})\n"
        f"  TP3: `{pm.TP3_PCT:.4f}` ({pm.TP3_FRACTION:.0%}) | SL: `{pm.SL_PCT:.4f}`\n"
        f"  Trail: `{pm.TRAIL_PCT:.4f}` \u0394`{pm.TRAIL_DELTA:.4f}` | Max hold: `{pm.MAX_HOLD_CANDLES}` candle\n"
        f"  Max pyramid adds: `{pm.MAX_PYRAMID_ADDS}`\n\n"
        f"*MTF*\n"
        f"  EMA50(15m): `{mtf.ema50:.2f}` ({'ready' if mtf.ready else 'not ready'})"
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
        f"Kelly trades: `{kelly_trades}`"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _send_watchdog(target):
    import time
    from .watchdog import _last_kline_ts
    elapsed = time.monotonic() - _last_kline_ts if _last_kline_ts > 0 else -1
    status = "\u2705 OK" if 0 <= elapsed < 90 else "\U0001f534 DEAD"
    msg = f"\U0001f441 *Watchdog* {status}\nLast kline: `{elapsed:.0f}s` ago"
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")


async def _do_warmup(target):
    msg = "\U0001f504 *Warmup pornit...* descarca 60 candle-uri historice."
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(msg, parse_mode="Markdown")
    else:
        await target.message.reply_text(msg, parse_mode="Markdown")
    try:
        from .indicator_warmup import warmup_indicators
        ok = await warmup_indicators(config.symbol)
        result = "\u2705 Warmup complet — toti indicatorii ready!" if ok else "\u26a0\ufe0f Warmup partial — verifica log-ul."
    except Exception as e:
        result = f"\u274c Warmup error: {e}"
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(result, parse_mode="Markdown", reply_markup=_main_menu_keyboard())
    else:
        await target.message.reply_text(result, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# COMENZI TEXT (alias-uri pentru comenzile inline)
# ---------------------------------------------------------------------------

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    state.running = True
    state.paused  = False
    await u.message.reply_text("\u2705 *Apex Scalper STARTED*", parse_mode="Markdown")


async def cmd_stop(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _check_owner(u):
        await u.message.reply_text("\u26d4 Unauthorized.", parse_mode="Markdown")
        return
    state.running = False
    await trader.close_position()
    await u.message.reply_text("\U0001f6d1 *Bot STOPPED* — position closed", parse_mode="Markdown")


async def cmd_pause(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _check_owner(u):
        await u.message.reply_text("\u26d4 Unauthorized.", parse_mode="Markdown")
        return
    state.paused = True
    await u.message.reply_text("\u23f8 *PAUSED* — no new entries", parse_mode="Markdown")


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
        await u.message.reply_text("\u26d4 Unauthorized.", parse_mode="Markdown")
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
        await u.message.reply_text("\u26d4 Unauthorized.", parse_mode="Markdown")
        return
    import apex_scalper.strategy as sm
    import apex_scalper.risk as rm
    import apex_scalper.position_manager as pm
    args = c.args
    if len(args) != 2:
        await u.message.reply_text("Usage: `/setparam <PARAM> <value>`", parse_mode="Markdown")
        return
    key, val = args[0].upper(), args[1]
    targets = {
        "RSI_LONG_MIN": (sm, float), "RSI_SHORT_MAX":  (sm, float),
        "IMBALANCE_LONG": (sm, float), "IMBALANCE_SHORT": (sm, float),
        "VOL_ZSCORE_MIN": (sm, float), "ATR_MIN_PCT":    (sm, float),
        "ATR_MAX_PCT":  (sm, float),   "ENTRY_THRESHOLD": (sm, float),
        "MAX_SPREAD_BPS": (rm, float), "MIN_BID_DEPTH":  (rm, float),
        "MIN_ASK_DEPTH": (rm, float),  "KELLY_FRACTION": (rm, float),
        "TP1_PCT":  (pm, float), "TP2_PCT":  (pm, float), "TP3_PCT":  (pm, float),
        "TP1_FRACTION": (pm, float), "TP2_FRACTION": (pm, float), "TP3_FRACTION": (pm, float),
        "SL_PCT": (pm, float), "TRAIL_PCT": (pm, float), "TRAIL_DELTA": (pm, float),
        "MAX_HOLD_CANDLES": (pm, int), "MAX_PYRAMID_ADDS": (pm, int),
    }
    if key not in targets:
        await u.message.reply_text(
            f"\u274c Unknown `{key}`\nAvailable: {', '.join(f'`{k}`' for k in sorted(targets))}",
            parse_mode="Markdown"
        )
        return
    mod, cast = targets[key]
    setattr(mod, key, cast(val))
    await u.message.reply_text(f"\u2705 `{key}` = `{val}`", parse_mode="Markdown")


async def cmd_menu_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await cmd_menu(u, c)


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
        ("analytics", cmd_analytics),
        ("pulse",     cmd_pulse),
        ("setparam",  cmd_setparam),
    ]:
        app.add_handler(CommandHandler(name, fn))
    return app
