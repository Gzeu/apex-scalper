"""Main entry point v1.4.0 — dashboard GUI integrat.

Changelog:
  v1.4.0 — adaugat run_dashboard() la startup (port 8050).
  v1.3.x — funding rate, drawdown sizing, etc.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from loguru import logger


def _setup_logging() -> None:
    from .config import config
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        colorize=True,
    )
    logger.add(
        "logs/apex.log",
        rotation="10 MB",
        retention="14 days",
        level="DEBUG",
        enqueue=True,
    )


async def main() -> None:
    from .config import config
    from .trader import trader
    from .persistence import db
    from .mtf_filter import mtf, run_mtf_refresh_loop
    from .funding_rate import run_funding_refresh_loop, funding
    from .daily_report import run_daily_report_loop
    from .anti_manipulation import inject_wall_params
    from .regime_filter import regime
    from .state import state
    from .telegram_ui import start_telegram_bot
    from .health import run_health_server
    from .watchdog import run_watchdog
    from .pulse import run_pulse_loop
    from .dashboard import run_dashboard

    _setup_logging()
    config.validate()

    inject_wall_params(config.symbol)

    logger.info(f"Apex Scalper pornit: {config.symbol} | testnet={config.testnet}")

    await trader.connect()
    await trader.setup_symbol(config.symbol)

    # Sincronizeaza pozitia existenta la restart
    existing = await trader.get_open_position(config.symbol)
    if existing:
        side     = existing.get("side", "").lower()
        qty      = float(existing.get("size",        0))
        entry    = float(existing.get("avgPrice",    0))
        trade_id = int(existing.get("tradeId", 0)) if existing.get("tradeId") else 0
        if side in ("buy", "sell") and qty > 0:
            norm_side = "long" if side == "buy" else "short"
            with state.lock:
                state.open_position = norm_side
                state.open_qty      = qty
                state.open_entry    = entry
            logger.info(f"Pozitie existenta restaurata: {norm_side} {qty} @ {entry}")

    logger.info(f"Watchdog started (timeout=120s, max_restarts=5)")

    # Incarca funding rate initial
    await funding.maybe_refresh(config.symbol)

    state.running = True
    state.paused  = False

    # Dashboard GUI (non-blocking, thread separat)
    run_dashboard(port=8050)

    loop = asyncio.get_event_loop()
    tasks = [
        asyncio.ensure_future(run_health_server()),
        asyncio.ensure_future(run_pulse_loop()),
        asyncio.ensure_future(run_watchdog(trader, state)),
        asyncio.ensure_future(run_mtf_refresh_loop(config.symbol)),
        asyncio.ensure_future(run_funding_refresh_loop(config.symbol)),
        asyncio.ensure_future(run_daily_report_loop()),
        asyncio.ensure_future(start_telegram_bot()),
        asyncio.ensure_future(_run_ws_feed(config, trader, state, loop)),
    ]

    logger.info(
        f"\n{'='*50}\n"
        f"  Health:      http://localhost:8080/health\n"
        f"  Dashboard:   http://localhost:8050\n"
        f"  Pulse:       fiecare 60s\n"
        f"{'='*50}"
    )

    def _shutdown(sig, frame):
        logger.info(f"Semnal {sig} primit — oprire...")
        for t in tasks:
            t.cancel()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Apex Scalper oprit.")


async def _run_ws_feed(config, trader, state, loop) -> None:
    from .ws_feed import run_ws_feed
    from .strategy import set_main_loop
    set_main_loop(loop)
    await run_ws_feed(config, trader, state)


def run() -> None:
    asyncio.run(main())
