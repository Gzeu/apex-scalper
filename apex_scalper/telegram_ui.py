"""Telegram bot UI v0.7.7
Commands:
  /start /stop /pause /resume /status /pnl /balance /close
  /setparam KEY VALUE   — live strategy tuning
  /metrics              — full performance report
  /watchdog             — WS health status
  /signals              — full indicator snapshot
  /regime               — market regime label + ADX + Hurst + size factor
  /analytics            — breakdown trades by reason/score/streak
  /tp                   — status TP1/2/3 hit, trail, hold candles
  /funding              — funding rate + blocked directions
  /pulse on|off         — toggle 1-minute pulse loop

Changelog:
  v0.7.7 — FIX: /pause apela cmd_resume (copy-paste bug) — acum corect.
             NEW: /analytics, /tp, /funding, /pulse on|off
  v0.7.2 — GAP #3 fix: /resume reseteaza consecutive_losses
"""
from __future__ import annotations

from loguru import logger
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

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


async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    state.running = True
    state.paused  = False
    await u.message.reply_text("✅ *Apex Scalper STARTED*", parse_mode="Markdown")


async def cmd_stop(u: Update, c: ContextTypes.DEFAULT_TYPE):
    state.running = False
    await trader.close_position()
    await u.message.reply_text("🛑 *Bot STOPPED* — position closed", parse_mode="Markdown")


async def cmd_pause(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """v0.7.7 FIX: apela cmd_resume in loc de cmd_pause."""
    state.paused = True
    await u.message.reply_text("⏸ *PAUSED* — no new entries", parse_mode="Markdown")


async def cmd_resume(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from .risk import risk
    state.paused    = False
    state.daily_pnl = 0.0
    risk.reset_consecutive_losses()
    await u.message.reply_text(
        "▶️ *RESUMED*\n"
        "`daily_pnl` reset la 0\n"
        "`consecutive_losses` reset la 0",
        parse_mode="Markdown",
    )


async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
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
        f"📊 *Status* `{config.symbol}`\n"
        f"Running: {'\u2705' if state.running else '\ud83d\uded1'} "
        f"Paused: {'\u23f8' if state.paused else '\u25b6\ufe0f'}\n"
        f"Position: `{pos}`\n"
        f"Price: `{price}` | Spread: `{spread}`\n"
        f"Bid↓ `{bid_d:.3f}` Ask↑ `{ask_d:.3f}`\n"
        f"EMA 9/21/50: `{ind.ema_fast:.1f}`/`{ind.ema_slow:.1f}`/`{ind.ema_trend:.1f}`\n"
        f"RSI(14): `{ind.rsi_value:.1f}` ATR: `{ind.atr_value:.2f}`\n"
        f"Regime: `{regime.label}` (ADX `{regime.adx}` sz×`{regime.size_factor():.2f}`)\n"
        f"Imbalance: `{ob_signals.imbalance:.3f}` Pressure: `{ob_signals.pressure_score:.3f}`"
    )
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_pnl(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"💰 *PnL*\n"
        f"Realized: `{state.realized_pnl:+.4f} USDT`\n"
        f"Daily: `{state.daily_pnl:+.4f} USDT`\n"
        f"Trades: `{state.total_trades}` | WR: `{state.winrate}%`"
    )
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_balance(u: Update, c: ContextTypes.DEFAULT_TYPE):
    bal = await trader.get_balance()
    await u.message.reply_text(f"💳 Balance: `{bal:.4f} USDT`", parse_mode="Markdown")


async def cmd_close(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await trader.close_position()
    await u.message.reply_text("📤 *Close executed*", parse_mode="Markdown")


async def cmd_metrics(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from .risk import risk
    with risk._lock:
        kelly_trades = len(risk._trade_results)
    msg = (
        f"📊 *Performance Metrics*\n"
        f"Trades: `{len(perf.trades)}`\n"
        f"Win Rate: `{perf.win_rate:.1f}%`\n"
        f"Sharpe: `{perf.sharpe:.2f}`\n"
        f"Profit Factor: `{perf.profit_factor:.2f}`\n"
        f"Expectancy: `{perf.expectancy:+.4f} USDT`\n"
        f"Avg Win: `{perf.avg_win:+.4f}` Avg Loss: `{perf.avg_loss:+.4f}`\n"
        f"Max DD: `{perf.max_drawdown:.4f} USDT` (`{perf.max_drawdown_pct:.2f}%`)\n"
        f"Win Streak: `{perf.win_streak}` Lose Streak: `{perf.lose_streak}`\n"
        f"Kelly trades tracked: `{kelly_trades}`"
    )
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_watchdog(u: Update, c: ContextTypes.DEFAULT_TYPE):
    import time
    from .watchdog import _last_kline_ts
    elapsed = time.monotonic() - _last_kline_ts if _last_kline_ts > 0 else -1
    status = "✅ OK" if elapsed < 90 else "🔴 DEAD"
    await u.message.reply_text(
        f"👁 *Watchdog* {status}\nLast kline: `{elapsed:.0f}s` ago",
        parse_mode="Markdown",
    )


async def cmd_signals(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from .strategy import ind
    from .orderbook_analytics import ob_signals
    from .book_pressure import bp
    from .regime_filter import regime

    bb  = (f"{ind.bb_lower:.1f} / {ind.bb_mid:.1f} / {ind.bb_upper:.1f}"
           if ind.bb_ready else "warming up")
    macd = (f"line=`{ind.macd_line:.4f}` sig=`{ind.macd_signal:.4f}` hist=`{ind.macd_histogram:.4f}`"
            if ind.macd_ready else "warming up (needs 26 bars)")
    stoch = (f"%K=`{ind.stoch_k:.1f}` %D=`{ind.stoch_d:.1f}`"
             if ind.stoch_ready else "warming up")

    msg = (
        f"🔮 *Signals Snapshot* `{config.symbol}`\n\n"
        f"📈 *Trend*\n"
        f"EMA 9/21/50: `{ind.ema_fast:.2f}` / `{ind.ema_slow:.2f}` / `{ind.ema_trend:.2f}`\n"
        f"Regime: `{regime.label}` | ADX: `{regime.adx}` | sz×: `{regime.size_factor():.2f}`\n\n"
        f"📊 *Momentum*\n"
        f"RSI(14): `{ind.rsi_value:.2f}` ({'ready' if ind.rsi_ready else 'warmup'})\n"
        f"MACD(12,26,9): {macd}\n"
        f"Stoch RSI(14,3,3): {stoch}\n\n"
        f"📌 *Volatility*\n"
        f"ATR(14): `{ind.atr_value:.4f}` ({'ready' if ind.atr_ready else 'warmup'})\n"
        f"BB(20,2): `{bb}`\n\n"
        f"💧 *Volume*\n"
        f"Vol Z-Score: `{ind.vol_zscore:.2f}` ({'ready' if ind.vol_ready else 'warmup'})\n"
        f"VWAP: `{ind.vwap:.2f}`\n\n"
        f"📖 *Order Book*\n"
        f"Imbalance: `{ob_signals.imbalance:.4f}`\n"
        f"Pressure score: `{ob_signals.pressure_score:.4f}`\n"
        f"Book Δ (cum delta): `{bp.cum_delta:.1f}`\n"
        f"Large Bid Wall: `{ob_signals.large_bid}` | Large Ask Wall: `{ob_signals.large_ask}`"
    )
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_regime(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from .regime_filter import regime
    from .strategy import ind
    label  = regime.label
    adx    = regime.adx
    sz_f   = regime.size_factor()
    allow  = regime.allow_entry()
    emoji  = {
        "TRENDING": "🟢",
        "RANGING":  "🔴",
        "VOLATILE": "🟡",
        "NEUTRAL":  "🟤",
        "UNKNOWN":  "⚫",
    }.get(label, "⚫")
    msg = (
        f"{emoji} *Market Regime: {label}*\n\n"
        f"ADX(14): `{adx}`\n"
        f"ATR(14): `{ind.atr_value:.4f}`\n"
        f"Entry allowed: `{'YES' if allow else 'NO — BLOCKED'}`\n"
        f"Size factor: `{sz_f:.2f}×`\n\n"
        f"_TRENDING → full size | VOLATILE → 50% size_\n"
        f"_RANGING → entries blocked | NEUTRAL → 75% size_"
    )
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_analytics(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Breakdown trades by reason, score bucket, worst streak."""
    from .analytics import analytics
    msg = analytics.telegram_breakdown(config.symbol, days=7)
    if not msg:
        msg = "ℹ️ Nu sunt suficiente date (minim 1 trade inchis)."
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_tp(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Status detaliat al pozitiei: TP1/2/3 hit, trail, hold candles."""
    from .position_manager import (
        position_manager as pm, TP1_PCT, TP2_PCT, TP3_PCT, SL_PCT, MAX_HOLD_CANDLES
    )
    with state.lock:
        pos   = state.open_position
        price = state.last_price
        qty   = state.open_qty

    if not pos or pm._entry_price == 0:
        await u.message.reply_text("▫️ Nicio pozitie deschisa.", parse_mode="Markdown")
        return

    entry   = pm._entry_price
    pnl_pct = pm._unrealised_pnl_pct(price)
    pnl_u   = round(pnl_pct * entry * qty, 4)
    pnl_i   = "⬆️" if pnl_pct >= 0 else "⬇️"
    tp1_p   = round(entry * (1 + TP1_PCT if pos == "long" else 1 - TP1_PCT), 2)
    tp2_p   = round(entry * (1 + TP2_PCT if pos == "long" else 1 - TP2_PCT), 2)
    tp3_p   = round(entry * (1 + TP3_PCT if pos == "long" else 1 - TP3_PCT), 2)
    sl_p    = round(entry * (1 - SL_PCT  if pos == "long" else 1 + SL_PCT),  2)

    msg = (
        f"{'\U0001f7e2' if pos == 'long' else '\U0001f534'} *Pozitie {pos.upper()}*\n"
        f"Entry: `{entry}` | Acum: `{price}` {pnl_i} `{pnl_pct*100:+.4f}%` (`{pnl_u:+.4f}` USDT)\n"
        f"Qty: `{qty}` | Hold: `{pm._hold_candles}/{MAX_HOLD_CANDLES}` candle(s)\n\n"
        f"*Scale-out:*\n"
        f"  TP1 {'\u2705 HIT' if pm._tp1_hit else f'`{tp1_p}` (Δ`{round(tp1_p - price if pos == chr(108)+chr(111)+chr(110)+chr(103) else price - tp1_p, 2)}`)'} \n"
        f"  TP2 {'\u2705 HIT' if pm._tp2_hit else f'`{tp2_p}`'} \n"
        f"  TP3 {'\u2705 HIT' if pm._tp3_hit else f'`{tp3_p}`'}\n"
        f"  SL: `{sl_p}`\n"
        f"  Trail: {'\U0001f534 ACTIV' if pm._trail_active else 'inactiv'} "
        f"| Pyramid adds: `{pm._pyramid_adds}`"
    )
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_funding(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Funding rate curent + directii blocate."""
    from .funding_rate import funding
    import time
    can_l = funding.can_enter_long()
    can_s = funding.can_enter_short()
    near  = funding._near_funding()
    now_ms = int(time.time() * 1000)
    time_to_next = max(0, (funding._next_funding_ms - now_ms) // 1000) if funding._next_funding_ms else -1
    ttf_str = f"`{time_to_next // 3600}h {(time_to_next % 3600) // 60}m`" if time_to_next >= 0 else "`necunoscut`"

    msg = (
        f"💸 *Funding Rate* `{config.symbol}`\n"
        f"Rata: `{funding.rate_pct}` (`{funding.rate:.8f}`)\n"
        f"LONG: {'\u2705 permis' if can_l else '\u274c blocat'}\n"
        f"SHORT: {'\u2705 permis' if can_s else '\u274c blocat'}\n"
        f"Aproape de plata: `{'DA \u26a0\ufe0f' if near else 'NU'}`\n"
        f"Urmatoarea plata in: {ttf_str}"
    )
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_pulse(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Toggle pulse loop on/off."""
    from .pulse import set_pulse_active, is_pulse_active
    args = c.args
    if args and args[0].lower() == "off":
        set_pulse_active(False)
        await u.message.reply_text("⏸ *Pulse OPRIT* — nu mai primesti rapoarte la 1 min.", parse_mode="Markdown")
    elif args and args[0].lower() == "on":
        set_pulse_active(True)
        await u.message.reply_text("✅ *Pulse PORNIT* — raport la fiecare 1 min.", parse_mode="Markdown")
    else:
        status = "activ" if is_pulse_active() else "oprit"
        await u.message.reply_text(f"⚡ Pulse e `{status}`. Foloseste `/pulse on` sau `/pulse off`.", parse_mode="Markdown")


async def cmd_setparam(u: Update, c: ContextTypes.DEFAULT_TYPE):
    import apex_scalper.strategy as sm
    import apex_scalper.risk as rm
    import apex_scalper.position_manager as pm
    args = c.args
    if len(args) != 2:
        await u.message.reply_text("Usage: `/setparam <PARAM> <value>`", parse_mode="Markdown")
        return
    key, val = args[0].upper(), args[1]
    targets = {
        "RSI_LONG_MIN":     (sm, float), "RSI_SHORT_MAX":    (sm, float),
        "RSI_OB_LIMIT":     (sm, float), "RSI_OS_LIMIT":     (sm, float),
        "IMBALANCE_LONG":   (sm, float), "IMBALANCE_SHORT":  (sm, float),
        "VOL_ZSCORE_MIN":   (sm, float), "ATR_MIN_PCT":      (sm, float),
        "ATR_MAX_PCT":      (sm, float), "ENTRY_THRESHOLD":  (sm, float),
        "MAX_SPREAD_BPS":   (rm, float), "MIN_BID_DEPTH":    (rm, float),
        "MIN_ASK_DEPTH":    (rm, float), "KELLY_FRACTION":   (rm, float),
        "TP1_PCT":          (pm, float), "TP2_PCT":          (pm, float),
        "TP3_PCT":          (pm, float),
        "TP1_FRACTION":     (pm, float), "TP2_FRACTION":     (pm, float),
        "TP3_FRACTION":     (pm, float),
        "SL_PCT":           (pm, float), "TRAIL_PCT":        (pm, float),
        "TRAIL_DELTA":      (pm, float), "MAX_HOLD_CANDLES": (pm, int),
        "MAX_PYRAMID_ADDS": (pm, int),
    }
    if key not in targets:
        available = ", ".join(f"`{k}`" for k in sorted(targets))
        await u.message.reply_text(
            f"❌ Unknown `{key}`\nAvailable: {available}",
            parse_mode="Markdown",
        )
        return
    mod, cast = targets[key]
    setattr(mod, key, cast(val))
    await u.message.reply_text(f"✅ `{key}` = `{val}`", parse_mode="Markdown")


def build_app():
    app = ApplicationBuilder().token(config.telegram_token).build()
    for name, fn in [
        ("start",     cmd_start),
        ("stop",      cmd_stop),
        ("pause",     cmd_pause),     # v0.7.7 FIX: era cmd_resume
        ("resume",    cmd_resume),
        ("status",    cmd_status),
        ("pnl",       cmd_pnl),
        ("balance",   cmd_balance),
        ("close",     cmd_close),
        ("metrics",   cmd_metrics),
        ("watchdog",  cmd_watchdog),
        ("signals",   cmd_signals),
        ("regime",    cmd_regime),
        ("setparam",  cmd_setparam),
        ("analytics", cmd_analytics),
        ("tp",        cmd_tp),
        ("funding",   cmd_funding),
        ("pulse",     cmd_pulse),
    ]:
        app.add_handler(CommandHandler(name, fn))
    return app
