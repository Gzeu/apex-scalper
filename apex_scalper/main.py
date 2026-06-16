"""Main entry point v1.4.5 — feed fix: ws_feed -> feed.start_feed().

Changelog:
  v1.4.5 — FIX CRITIC: _run_ws_feed importa din ws_feed (modul inexistent)
    -> feed nu pornea niciodata -> feed_stale permanent -> 0 tranzactii.
    Fix: importat start_feed din feed.py (modulul real, v1.0.4).
    _run_ws_feed devine _run_feed() care apeleaza start_feed() direct.
  v1.4.4 — watchdog_loop() fara argumente, run_daily_report_loop(symbol).
  v1.4.3 — indicator_warmup la startup (fix #4).
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
    from .watchdog import watchdog_loop
    from .pulse import run_pulse_loop
    from .dashboard import run_dashboard
    from .indicator_warmup import warmup_indicators
    from .feed import start_feed              # FIX v1.4.5: modul real
    from .strategy import set_main_loop

    _setup_logging()
    config.validate()

    inject_wall_params(config.wall_ratio, config.wall_distance_ticks)

    logger.info(f"Apex Scalper pornit: {config.symbol} | testnet={config.testnet}")

    await trader.setup()

    # Warm up indicatori cu date istorice inainte de pornirea feed-ului
    logger.info("[warmup] Pornire indicator warmup...")
    try:
        ok = await warmup_indicators(config.symbol)
        if ok:
            logger.info("[warmup] Indicatori ready — RSI/ATR/EMA incalziti")
        else:
            logger.warning("[warmup] Warmup esuat — indicatorii vor fi ready dupa ~50 candle-uri live")
    except Exception as e:
        logger.warning(f"[warmup] Warmup exceptie (continuam fara): {e}")

    # Sincronizeaza pozitia existenta la restart
    await trader.sync_position_from_exchange()

    await funding.maybe_refresh(config.symbol)

    state.running = True
    state.paused  = False

    # Dashboard GUI (non-blocking, thread separat)
    run_dashboard(port=8050)

    loop = asyncio.get_event_loop()
    set_main_loop(loop)   # seteaza loop pentru strategy.update_indicators thread-safety

    tasks = [
        asyncio.ensure_future(run_health_server()),
        asyncio.ensure_future(run_pulse_loop()),
        asyncio.ensure_future(watchdog_loop()),
        asyncio.ensure_future(run_mtf_refresh_loop(config.symbol)),
        asyncio.ensure_future(run_funding_refresh_loop(config.symbol)),
        asyncio.ensure_future(run_daily_report_loop(config.symbol)),
        asyncio.ensure_future(start_telegram_bot()),
        asyncio.ensure_future(start_feed()),   # FIX v1.4.5: feed real
    ]

    logger.info(
        f"\n{'='*50}\n"
        f"  Health:      http://localhost:8080/health\n"
        f"  Dashboard:   http://localhost:8050\n"
        f"  Feed:        feed.py v1.0.4 (native async WS)\n"
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


def run() -> None:
    asyncio.run(main())
