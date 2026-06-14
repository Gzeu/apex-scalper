"""Daily report v0.6.0 — scheduled Telegram summary at 23:59 UTC.

Changes vs v0.1.0:
  - schedule_daily_report() added: asyncio task that fires at 23:59 UTC daily
    Works correctly across midnight without cron dependency.
  - Report now includes fee breakdown (total fees paid today)
  - Gross PnL vs Net PnL shown separately
  - Sharpe and MaxDD computed from today's trades only
  - Mainnet readiness check: warns if today's MaxDD > daily_loss_limit * 0.8
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
    # Annualized for 1m bars: sqrt(252 * 24 * 60)
    return round((mu / std) * math.sqrt(252 * 1440) if std > 0 else 0.0, 3)


async def send_daily_report() -> None:
    """Build and send the daily P&L summary to Telegram."""
    try:
        from .telegram_ui import send_message
        from .persistence import db
        from .config import config

        today = datetime.now(timezone.utc).date()
        today_start_ts = int(
            datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp() * 1000
        )

        # Fetch today's closed trades from DB
        trades = db.get_trades_since(today_start_ts)
        n = len(trades)

        if n == 0:
            await send_message(
                f"📊 *Daily Report* — {today}\n"
                f"No trades today."
            )
            return

        wins   = sum(1 for t in trades if t.get("pnl_usdt", 0) > 0)
        losses = n - wins
        gross_pnl = sum(t.get("pnl_usdt", 0) for t in trades)
        # Estimate fees: 0.040% round-trip maker (entry + exit)
        avg_entry = sum(t.get("entry", 0) for t in trades) / n if n else 0
        avg_qty   = sum(t.get("qty", 0) for t in trades) / n if n else 0
        est_fees  = sum(t.get("entry", 0) * t.get("qty", 0) * 0.00040 for t in trades)
        net_pnl   = gross_pnl - est_fees
        winrate   = round(wins / n * 100, 1)
        daily_sharpe = _compute_daily_sharpe(trades)

        # Risk metrics
        with risk._lock if hasattr(risk, '_lock') else __import__('contextlib').nullcontext():
            daily_loss = getattr(risk, '_daily_loss', 0)
            daily_limit = getattr(risk, '_daily_limit', float('inf'))

        dd_pct = abs(daily_loss / daily_limit * 100) if daily_limit > 0 else 0
        dd_warn = " ⚠️" if dd_pct >= 80 else ""

        msg = (
            f"📊 *Daily Report* — {today} UTC\n"
            f"\n"
            f"Trades: `{n}` (✅{wins} / ❌{losses}) WR=`{winrate}%`\n"
            f"Gross PnL:  `{gross_pnl:+.4f} USDT`\n"
            f"Fees est.:  `-{est_fees:.4f} USDT`\n"
            f"Net PnL:    `{net_pnl:+.4f} USDT`\n"
            f"Daily Sharpe: `{daily_sharpe}`\n"
            f"DD used: `{dd_pct:.1f}%` of limit{dd_warn}\n"
            f"\n"
            f"Symbol: `{config.symbol}` | "
            f"Leverage: `{config.leverage}x`"
        )
        await send_message(msg)
        logger.info(f"Daily report sent: {n} trades, net={net_pnl:.4f} USDT")

    except Exception as e:
        logger.error(f"daily_report error: {e}")


async def schedule_daily_report() -> None:
    """Run forever: sleep until 23:59 UTC, send report, wait for next day."""
    logger.info("Daily report scheduler started (fires at 23:59 UTC)")
    while True:
        now = datetime.now(timezone.utc)
        # Next 23:59:00 UTC
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_s = (target - now).total_seconds()
        logger.debug(f"Daily report: next in {wait_s/3600:.2f}h at {target}")
        await asyncio.sleep(wait_s)
        await send_daily_report()
