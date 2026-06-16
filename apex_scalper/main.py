"""Main entry point v1.4.8 — start_telegram_bot wrapper corect.

Changelog:
  v1.4.8 —
    FIX: telegram_ui nu are start_telegram_bot() -> adaugat run_polling wrapper.
    FIX: run_dashboard() apelat corect (daemon thread deja).
  v1.4.7 — run_funding_refresh_loop adaugat, port 8080 fix, set_main_loop fix.
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


async def _run_telegram_polling() -> None:
    """Wrapper asyncio pentru build_app().run_polling()."""
    from .telegram_ui import build_app
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    # Tine in viata pana la cancel
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


async def main() -> None:
    from .config import config
    from .trader import trader
    from .mtf_filter import run_mtf_refresh_loop
    from .funding_rate import run_funding_refresh_loop, funding
    from .daily_report import run_daily_report_loop
    from .anti_manipulation import inject_wall_params
    from .state import state
    from .health import start_health_server
    from .watchdog import watchdog_loop
    from .pulse import run_pulse_loop
    from .dashboard import run_dashboard
    from .indicator_warmup import warmup_indicators
    from .feed import start_feed
    from .strategy import set_main_loop

    _setup_logging()
    config.validate()

    inject_wall_params(config.wall_ratio, config.wall_distance_ticks)
    logger.info(f"Apex Scalper pornit: {config.symbol} | testnet={config.testnet}")

    await trader.setup()

    logger.info("[warmup] Pornire indicator warmup...")
    try:
        ok = await warmup_indicators(config.symbol)
        if ok:
            logger.info("[warmup] Indicatori ready \u2014 RSI/ATR/EMA incalziti")
        else:
            logger.warning("[warmup] Warmup esuat \u2014 indicatorii vor fi ready dupa ~50 candle-uri live")
    except Exception as e:
        logger.warning(f"[warmup] Warmup exceptie (continuam fara): {e}")

    await trader.sync_position_from_exchange()
    await funding.maybe_refresh(config.symbol)

    state.running = True
    state.paused  = False

    # Non-blocking: daemon threads
    start_health_server()
    run_dashboard(port=8050)

    loop = asyncio.get_event_loop()
    set_main_loop(loop)

    tasks = [
        asyncio.ensure_future(run_pulse_loop()),
        asyncio.ensure_future(watchdog_loop()),
        asyncio.ensure_future(run_mtf_refresh_loop(config.symbol)),
        asyncio.ensure_future(run_funding_refresh_loop(config.symbol)),
        asyncio.ensure_future(run_daily_report_loop(config.symbol)),
        asyncio.ensure_future(_run_telegram_polling()),
        asyncio.ensure_future(start_feed()),
    ]

    logger.info(
        f"\n{'='*50}\n"
        f"  Health:    http://localhost:8080/health\n"
        f"  Dashboard: http://localhost:8050\n"
        f"  Feed:      feed.py (native async WS)\n"
        f"  Pulse:     fiecare {int(__import__('os').getenv('PULSE_INTERVAL_S', 60))}s\n"
        f"{'='*50}"
    )

    def _shutdown(sig, frame):
        logger.info(f"Semnal {sig} primit \u2014 oprire...")
        for t in tasks:
            t.cancel()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Apex Scalper oprit.")


def run() -> None:
    asyncio.run(main())
