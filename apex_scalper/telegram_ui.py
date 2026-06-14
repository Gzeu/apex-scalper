"""Telegram bot UI v0.3.
Commands:
  /start /stop /pause /resume /status /pnl /balance /close
  /setparam KEY VALUE   — live strategy tuning
  /metrics              — full performance report
  /watchdog             — WS health status
  /tp1 /tp2             — set TP1/TP2 percentages
  /signals              — current indicator snapshot
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
    state.paused = True
    await u.message.reply_text("⏸ *PAUSED* — no new entries", parse_mode="Markdown")


async def cmd_resume(u: Update, c: ContextTypes.DEFAULT_TYPE):
    state.paused  = False
    state.daily_pnl = 0.0
    await u.message.reply_text("▶️ *RESUMED* (daily PnL reset)", parse_mode="Markdown")


async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from .strategy import ind
    from .orderbook_analytics import ob_signals
    with state.lock:
        pos    = state.open_position or "none"
        price  = state.last_price
        spread = state.orderbook.spread
        bid_d  = state.orderbook.bid_depth(5)
        ask_d  = state.orderbook.ask_depth(5)
    msg = (
        f"📊 *Status* `{config.symbol}`\n"
        f"Running: {'\u2705' if state.running else '\ud83d\uded1'} Paused: {'\u23f8' if state.paused else '\u25b6\ufe0f'}\n"
        f"Position: `{pos}`\n"
        f"Price: `{price}` | Spread: `{spread}`\n"
        f"Bid↓ `{bid_d:.3f}` Ask↑ `{ask_d:.3f}`\n"
        f"EMA 9/21/50: `{ind.ema_fast:.1f}`/`{ind.ema_slow:.1f}`/`{ind.ema_trend:.1f}`\n"
        f"RSI(14): `{ind.rsi_value:.1f}` ATR: `{ind.atr_value:.2f}`\n"
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
    msg = (
        f"📊 *Performance Metrics*\n"
        f"Trades: `{len(perf.trades)}`\n"
        f"Win Rate: `{perf.win_rate:.1f}%`\n"
        f"Sharpe: `{perf.sharpe:.2f}`\n"
        f"Profit Factor: `{perf.profit_factor:.2f}`\n"
        f"Expectancy: `{perf.expectancy:+.4f} USDT`\n"
        f"Avg Win: `{perf.avg_win:+.4f}` Avg Loss: `{perf.avg_loss:+.4f}`\n"
        f"Max DD: `{perf.max_drawdown:.4f} USDT` (`{perf.max_drawdown_pct:.2f}%`)\n"
        f"Win Streak: `{perf.win_streak}` Lose Streak: `{perf.lose_streak}`"
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
    bb = f"{ind.bb_lower:.1f} / {ind.bb_mid:.1f} / {ind.bb_upper:.1f}" if ind.bb_ready else "warming"
    msg = (
        f"🔮 *Signals Snapshot*\n"
        f"EMA 9: `{ind.ema_fast:.2f}` 21: `{ind.ema_slow:.2f}` 50: `{ind.ema_trend:.2f}`\n"
        f"RSI(14): `{ind.rsi_value:.2f}` ({'ready' if ind.rsi_ready else 'warmup'})\n"
        f"ATR(14): `{ind.atr_value:.4f}` ({'ready' if ind.atr_ready else 'warmup'})\n"
        f"BB(20,2): `{bb}`\n"
        f"Vol Z-Score: `{ind.vol_zscore:.2f}` ({'ready' if ind.vol_ready else 'warmup'})\n"
        f"VWAP: `{ind.vwap:.2f}`\n"
        f"OB Imbalance: `{ob_signals.imbalance:.4f}`\n"
        f"OB Pressure: `{ob_signals.pressure_score:.4f}`\n"
        f"Large Bid Wall: `{ob_signals.large_bid}` Large Ask Wall: `{ob_signals.large_ask}`"
    )
    await u.message.reply_text(msg, parse_mode="Markdown")


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
        "RSI_LONG_MIN":    (sm, float), "RSI_SHORT_MAX":  (sm, float),
        "RSI_OB_LIMIT":    (sm, float), "RSI_OS_LIMIT":   (sm, float),
        "IMBALANCE_LONG":  (sm, float), "IMBALANCE_SHORT":(sm, float),
        "VOL_ZSCORE_MIN":  (sm, float), "ATR_MIN_PCT":    (sm, float),
        "ATR_MAX_PCT":     (sm, float), "ENTRY_THRESHOLD":(sm, float),
        "MAX_SPREAD_BPS":  (rm, float), "MIN_BID_DEPTH":  (rm, float),
        "MIN_ASK_DEPTH":   (rm, float),
        "TP1_PCT":         (pm, float), "TP2_PCT":        (pm, float),
        "SL_PCT":          (pm, float), "TRAIL_PCT":      (pm, float),
        "TRAIL_DELTA":     (pm, float), "MAX_HOLD_CANDLES":(pm, int),
        "MAX_PYRAMID_ADDS":(pm, int),
    }
    if key not in targets:
        await u.message.reply_text(f"❌ Unknown `{key}`", parse_mode="Markdown")
        return
    mod, cast = targets[key]
    setattr(mod, key, cast(val))
    await u.message.reply_text(f"✅ `{key}` = `{val}`", parse_mode="Markdown")


def build_app():
    app = ApplicationBuilder().token(config.telegram_token).build()
    for name, fn in [
        ("start",    cmd_start),
        ("stop",     cmd_stop),
        ("pause",    cmd_pause),
        ("resume",   cmd_resume),
        ("status",   cmd_status),
        ("pnl",      cmd_pnl),
        ("balance",  cmd_balance),
        ("close",    cmd_close),
        ("metrics",  cmd_metrics),
        ("watchdog", cmd_watchdog),
        ("signals",  cmd_signals),
        ("setparam", cmd_setparam),
    ]:
        app.add_handler(CommandHandler(name, fn))
    return app
