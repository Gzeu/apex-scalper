"""Daily report v0.6.1 — scheduled Telegram summary at 23:59 UTC.

Aliases added (v0.6.1):
  run_daily_report_loop(symbol) — used by main.py (wraps schedule_daily_report)
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone, timedelta
from loguru import logger

from .performance import perf
from .risk import risk


def _compute_daily_sharpe(trades: list) -> float:
    if len(trades) < 2:
        return 0.0
    returns = [t.get("pnl_pct", 0) for t in trades]
    mu  = sum(returns) / len(returns)
    std = math.sqrt(sum((r - mu) ** 2 for r in returns) / len(returns))
    return round((mu / std) * math.sqrt(252 * 1440) if std > 0 else 0.0, 3)


async def send_daily_report() -> None:
    try:
        from .telegram_ui import send_message
        from .persistence import db
        from .config import config

        today = datetime.now(timezone.utc).date()
        today_start_ts = int(
            datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp() * 1000
        )
        trades = db.get_trades_since(today_start_ts)
        n = len(trades)

        if n == 0:
            await send_message(f"📊 *Daily Report* — {today}\nNo trades today.")
            return

        wins      = sum(1 for t in trades if t.get("pnl_usdt", 0) > 0)
        losses    = n - wins
        gross_pnl = sum(t.get("pnl_usdt", 0) for t in trades)
        est_fees  = sum(t.get("entry", 0) * t.get("qty", 0) * 0.00040 for t in trades)
        net_pnl   = gross_pnl - est_fees
        winrate   = round(wins / n * 100, 1)
        daily_sharpe = _compute_daily_sharpe(trades)

        try:
            daily_loss  = risk._daily_loss
            daily_limit = risk._daily_limit
        except AttributeError:
            daily_loss, daily_limit = 0, float("inf")

        dd_pct  = abs(daily_loss / daily_limit * 100) if daily_limit > 0 else 0
        dd_warn = " ⚠️" if dd_pct >= 80 else ""

        await send_message(
            f"📊 *Daily Report* — {today} UTC\n"
            f"\n"
            f"Trades: `{n}` (✅{wins} / ❌{losses}) WR=`{winrate}%`\n"
            f"Gross PnL:  `{gross_pnl:+.4f} USDT`\n"
            f"Fees est.:  `-{est_fees:.4f} USDT`\n"
            f"Net PnL:    `{net_pnl:+.4f} USDT`\n"
            f"Daily Sharpe: `{daily_sharpe}`\n"
            f"DD used: `{dd_pct:.1f}%` of limit{dd_warn}\n"
            f"\n"
            f"Symbol: `{config.symbol}` | Leverage: `{config.leverage}x`"
        )
        logger.info(f"Daily report sent: {n} trades net={net_pnl:.4f} USDT")

    except Exception as e:
        logger.error(f"daily_report error: {e}")


async def schedule_daily_report() -> None:
    logger.info("Daily report scheduler started (fires at 23:59 UTC)")
    while True:
        now    = datetime.now(timezone.utc)
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await send_daily_report()


# Alias expected by main.py  — symbol arg accepted but not used (global config)
async def run_daily_report_loop(symbol: str = "") -> None:
    await schedule_daily_report()
