"""Main entry point v1.4.3 — indicator_warmup la startup (fix #4).

Changelog:
  v1.4.3 — FIX #4: indicator_warmup.run() apelat inainte de pornirea
    task-urilor asyncio. Inainte indicatorii nu erau warm la startup
    -> primele candle-uri aveau rsi_ready=False, atr_ready=False
    -> GATE4 (ATR) si scoring partial puteau genera semnale false.
  v1.4.2 — inlocuit get_open_position() cu sync_position_from_exchange().
  v1.4.1 — inject_wall_params via config.wall_ratio + trader.setup().
  v1.4.0 — dashboard GUI integrat.
"""
from __future__ import annotations

import asyncio
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
    from .mtf_filter import run_mtf_refresh_loop
    from .funding_rate import run_funding_refresh_loop, funding
    from .daily_report import run_daily_report_loop
    from .anti_manipulation import inject_wall_params
    from .state import state
    from .telegram_ui import start_telegram_bot
    from .health import run_health_server
    from .watchdog import run_watchdog
    from .pulse import run_pulse_loop
    from .dashboard import run_dashboard
    from .indicator_warmup import run_warmup  # FIX #4

    _setup_logging()
    config.validate()

    inject_wall_params(config.wall_ratio, config.wall_distance_ticks)

    logger.info(f"Apex Scalper pornit: {config.symbol} | testnet={config.testnet}")

    await trader.setup()

    # FIX #4: warm up indicatori cu date istorice inainte de pornirea loop-ului
    logger.info("[warmup] Pornire indicator warmup...")
    try:
        await run_warmup(config.symbol)
        logger.info("[warmup] Indicatori ready — RSI/ATR/EMA incalziti")
    except Exception as e:
        logger.warning(f"[warmup] Warmup esuat (continuam fara): {e}")

    # Sincronizeaza pozitia existenta la restart
    await trader.sync_position_from_exchange()

    logger.info("Watchdog started (timeout=120s, max_restarts=5)")

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
