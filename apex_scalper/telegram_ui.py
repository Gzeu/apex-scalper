"""Telegram bot UI.
Commands: /start /stop /pause /resume /status /pnl /close /balance /setparam
"""
from __future__ import annotations

import os
import asyncio
from loguru import logger
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from .config import config
from .state import state
from .trader import trader

_bot: Bot | None = None


async def send_message(text: str) -> None:
    """Fire-and-forget message to configured chat. Used by strategy notifications."""
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


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state.running = True
    state.paused  = False
    await update.message.reply_text("✅ *Apex Scalper STARTED*", parse_mode="Markdown")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state.running = False
    await trader.close_position()
    await update.message.reply_text("🛑 *Bot STOPPED* — all positions closed", parse_mode="Markdown")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state.paused = True
    await update.message.reply_text("⏸ *Bot PAUSED* — no new entries", parse_mode="Markdown")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state.paused = False
    state.daily_pnl = 0.0  # reset daily loss guard on manual resume
    await update.message.reply_text("▶️ *Bot RESUMED* (daily PnL reset)", parse_mode="Markdown")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with state.lock:
        pos    = state.open_position or "none"
        price  = state.last_price
        spread = state.orderbook.spread
        fast   = state.ema_fast
        slow   = state.ema_slow
        rsi    = state.rsi_value
        rsi_ok = state.rsi_ready
    bid_d  = state.orderbook.bid_depth(5)
    ask_d  = state.orderbook.ask_depth(5)
    msg = (
        f"📊 *Apex Scalper Status*\n"
        f"Symbol : `{config.symbol}`\n"
        f"Running: {'\u2705' if state.running else '\ud83d\uded1'} | "
        f"Paused: {'\u23f8' if state.paused else '\u25b6\ufe0f'}\n"
        f"Position: `{pos}`\n"
        f"Price  : `{price}`\n"
        f"Spread : `{spread}` | Bid↓ `{bid_d:.3f}` Ask↑ `{ask_d:.3f}`\n"
        f"EMA9/21: `{fast:.2f}` / `{slow:.2f}`\n"
        f"RSI(14): `{rsi:.1f}` ({'ready' if rsi_ok else 'warming up'})"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"💰 *PnL Report*\n"
        f"Realized : `{state.realized_pnl:+.4f} USDT`\n"
        f"Daily    : `{state.daily_pnl:+.4f} USDT`\n"
        f"Trades   : `{state.total_trades}`\n"
        f"Win rate : `{state.winrate}%`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bal = await trader.get_balance()
    await update.message.reply_text(f"💳 Balance: `{bal:.4f} USDT`", parse_mode="Markdown")


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await trader.close_position()
    await update.message.reply_text("📤 *Close position executed*", parse_mode="Markdown")


async def cmd_setparam(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /setparam TP_PCT 0.002"""
    import apex_scalper.strategy as strat_mod
    import apex_scalper.risk as risk_mod
    args = ctx.args
    if len(args) != 2:
        await update.message.reply_text("Usage: `/setparam <PARAM> <value>`", parse_mode="Markdown")
        return
    key, val = args[0].upper(), args[1]
    targets = {
        "TP_PCT":           (strat_mod, float),
        "SL_PCT":           (strat_mod, float),
        "TRAIL_PCT":        (strat_mod, float),
        "TRAIL_DELTA":      (strat_mod, float),
        "MAX_HOLD_CANDLES": (strat_mod, int),
        "RSI_LONG_MIN":     (strat_mod, float),
        "RSI_SHORT_MAX":    (strat_mod, float),
        "MAX_SPREAD_BPS":   (risk_mod,  float),
        "MIN_BID_DEPTH":    (risk_mod,  float),
        "MIN_ASK_DEPTH":    (risk_mod,  float),
    }
    if key not in targets:
        await update.message.reply_text(f"❌ Unknown param `{key}`", parse_mode="Markdown")
        return
    mod, cast = targets[key]
    setattr(mod, key, cast(val))
    await update.message.reply_text(f"✅ `{key}` set to `{val}`", parse_mode="Markdown")


def build_app():
    app = ApplicationBuilder().token(config.telegram_token).build()
    for name, handler in [
        ("start",    cmd_start),
        ("stop",     cmd_stop),
        ("pause",    cmd_pause),
        ("resume",   cmd_resume),
        ("status",   cmd_status),
        ("pnl",      cmd_pnl),
        ("balance",  cmd_balance),
        ("close",    cmd_close),
        ("setparam", cmd_setparam),
    ]:
        app.add_handler(CommandHandler(name, handler))
    return app
