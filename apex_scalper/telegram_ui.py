"""Telegram bot UI — commands: /start /stop /pause /resume /status /pnl /close."""
from __future__ import annotations

import asyncio
from loguru import logger
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from .config import config
from .state import state
from .trader import trader


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state.running = True
    state.paused = False
    await update.message.reply_text("✅ Apex Scalper STARTED")
    logger.info("Bot started via Telegram")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state.running = False
    await trader.close_position()
    await update.message.reply_text("🛑 Apex Scalper STOPPED — position closed")
    logger.info("Bot stopped via Telegram")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state.paused = True
    await update.message.reply_text("⏸ Bot PAUSED — no new entries")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state.paused = False
    await update.message.reply_text("▶️ Bot RESUMED")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pos = state.open_position or "none"
    price = state.last_price
    spread = state.orderbook.spread
    msg = (
        f"📊 *Apex Scalper Status*\n"
        f"Symbol: `{config.symbol}`\n"
        f"Running: {'✅' if state.running else '🛑'}\n"
        f"Paused: {'⏸' if state.paused else '▶️'}\n"
        f"Position: `{pos}`\n"
        f"Last price: `{price}`\n"
        f"Spread: `{spread}`\n"
        f"EMA fast/slow: `{state.ema_fast:.2f}` / `{state.ema_slow:.2f}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"💰 *PnL Report*\n"
        f"Realized: `{state.realized_pnl:.4f} USDT`\n"
        f"Daily: `{state.daily_pnl:.4f} USDT`\n"
        f"Trades: `{state.total_trades}`\n"
        f"Win rate: `{state.winrate}%`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await trader.close_position()
    await update.message.reply_text("📤 Close position executed")


def build_app():
    app = ApplicationBuilder().token(config.telegram_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("close", cmd_close))
    return app
