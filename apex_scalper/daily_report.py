"""Daily report v0.4.0 — automated Telegram PnL summary at 23:59 UTC.

Runs as a background asyncio task. At 23:59:00 UTC each day it sends:
  - Today's realized PnL (per symbol + total)
  - Trades count + win rate
  - Sharpe, MaxDD, Profit Factor (from live performance tracker)
  - 7-day PnL trend table (from SQLite)
  - Equity curve summary

Also saves a metrics snapshot to the DB for historical tracking.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from loguru import logger

from .config import config
from .state import state
from .performance import perf
from .persistence import db


async def _build_report(symbol: str) -> str:
    """Build the daily Telegram markdown report string."""
    with state.lock:
        daily_pnl   = state.daily_pnl
        total_pnl   = state.realized_pnl
        total_trades = state.total_trades
        win_trades  = state.win_trades
        winrate     = state.winrate

    sharpe  = round(perf.sharpe, 3)
    max_dd  = round(perf.max_drawdown, 4)
    pf      = round(perf.profit_factor, 3)

    # 7-day history from SQLite
    history = db.daily_summary(symbol, days=7)
    history_lines = []
    for row in history:
        emoji = "🟢" if row["pnl"] >= 0 else "🔴"
        wr = round(row["wins"] / row["trades"] * 100, 1) if row["trades"] else 0
        history_lines.append(
            f"{emoji} `{row['date']}` | "
            f"`{row['pnl']:+.2f} USDT` | "
            f"`{row['trades']}t` | "
            f"`{wr}%wr`"
        )

    history_str = "\n".join(history_lines) if history_lines else "_No trades today_"
    today_emoji = "🟢" if daily_pnl >= 0 else "🔴"

    report = (
        f"📊 *Daily Report — {symbol}*\n"
        f"`{datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC`\n\n"
        f"{today_emoji} *Today PnL:* `{daily_pnl:+.4f} USDT`\n"
        f"📈 *Total PnL:* `{total_pnl:+.4f} USDT`\n"
        f"🎯 *Trades:* `{total_trades}` | *Wins:* `{win_trades}` | *WR:* `{winrate}%`\n\n"
        f"⚡ *Sharpe:* `{sharpe}` | *MaxDD:* `{max_dd} USDT`\n"
        f"💹 *Profit Factor:* `{pf}`\n\n"
        f"📅 *Last 7 days:*\n{history_str}"
    )
    return report


async def run_daily_report_loop(symbol: Optional[str] = None) -> None:  # type: ignore
    """Background task: send Telegram report at 23:59 UTC daily."""
    from typing import Optional as Opt
    sym = symbol or config.symbol

    while True:
        now = datetime.now(timezone.utc)
        # Calculate seconds until 23:59:00 UTC today
        target_h, target_m = 23, 59
        target_s = target_h * 3600 + target_m * 60
        current_s = now.hour * 3600 + now.minute * 60 + now.second
        wait = target_s - current_s
        if wait <= 0:
            wait += 86400  # next day

        logger.info(f"Daily report scheduled in {wait//3600}h {(wait%3600)//60}m")
        await asyncio.sleep(wait)

        try:
            report = await _build_report(sym)
            from .telegram_ui import send_message
            await send_message(report)
            logger.info(f"Daily report sent for {sym}")

            # Save metrics snapshot to DB
            with state.lock:
                total_pnl = state.realized_pnl
            db.save_metrics_snapshot(
                symbol=sym,
                sharpe=perf.sharpe,
                max_dd=perf.max_drawdown,
                profit_factor=perf.profit_factor,
                total_pnl=total_pnl,
            )

            # Reset daily PnL counter after report
            with state.lock:
                state.reset_daily()

        except Exception as e:
            logger.error(f"Daily report error: {e}")

        await asyncio.sleep(60)  # avoid double-send
