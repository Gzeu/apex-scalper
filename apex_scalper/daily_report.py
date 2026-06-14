"""Daily report v0.7.5 — signal breakdown appended to Telegram summary.

Changelog:
  v0.7.5 — analytics.telegram_breakdown() appended to nightly report.
             Answers: which reason/score/hour drove today's losses?
  v0.7.0 — initial daily summary at 23:59 UTC
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from loguru import logger


async def send_daily_report(symbol: str) -> None:
    """Build and send the nightly performance summary + signal breakdown."""
    try:
        from .persistence import db
        from .performance import perf
        from .risk import risk
        from .analytics import analytics
        from .telegram_ui import send_message
        from .config import config

        pnl, total, wins = db.load_daily_pnl(symbol)
        win_rate = (wins / total * 100) if total > 0 else 0.0
        losses   = total - wins

        daily_summary = db.daily_summary(symbol, days=7)
        week_pnl = sum(d["pnl"] for d in daily_summary)

        msg = (
            f"\U0001f4c5 *Daily Report — {symbol}*\n"
            f"`{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
            f"*Today*\n"
            f"  PnL: `{pnl:+.4f} USDT`\n"
            f"  Trades: `{total}` | Wins: `{wins}` | Losses: `{losses}`\n"
            f"  Win rate: `{win_rate:.1f}%`\n\n"
            f"*7-Day*\n"
            f"  Total PnL: `{week_pnl:+.4f} USDT`\n"
            f"  Sharpe: `{perf.sharpe:.3f}`\n"
            f"  Max DD: `{perf.max_drawdown:.4f}`\n"
            f"  Profit Factor: `{perf.profit_factor:.3f}`\n"
        )

        # v0.7.5: append signal breakdown
        breakdown = analytics.telegram_breakdown(symbol, days=1)
        if breakdown:
            msg += breakdown

        await send_message(msg)
        logger.info(f"[daily_report] sent for {symbol}")

    except Exception as e:
        logger.error(f"[daily_report] failed: {e}")


async def schedule_daily_report(symbol: str) -> None:
    """Run forever, sending report at 23:59:05 UTC each day."""
    while True:
        now = datetime.now(timezone.utc)
        # Next 23:59:05 UTC
        target = now.replace(hour=23, minute=59, second=5, microsecond=0)
        if now >= target:
            target = target.replace(day=target.day + 1)
        wait_s = (target - now).total_seconds()
        logger.debug(f"[daily_report] next report in {wait_s/3600:.2f}h")
        await asyncio.sleep(wait_s)
        await send_daily_report(symbol)
