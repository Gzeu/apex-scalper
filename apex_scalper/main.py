"""Entrypoint v0.3 — feed + watchdog + telegram + graceful shutdown."""
from __future__ import annotations

import asyncio
import signal
import sys
from loguru import logger

from .config import config
from .feed import start_feed
from .telegram_ui import build_app
from .watchdog import run_watchdog
from .state import state
from .trader import trader


def setup_logging():
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )
    logger.add(
        "logs/apex_scalper.log",
        rotation="10 MB",
        retention="14 days",
        level=config.log_level,
    )


async def _shutdown(loop: asyncio.AbstractEventLoop, tg_app=None):
    logger.warning("🛑 Shutdown — closing position if open...")
    state.running = False
    await trader.close_position()
    if tg_app:
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            pass
    tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


async def main():
    setup_logging()
    logger.info(
        f"⚡ Apex Scalper v0.3.0 | {config.symbol} | "
        f"{'TESTNET' if config.testnet else '⚠️ MAINNET'} | "
        f"lev={config.leverage}x size={config.order_size_usdt}USDT"
    )

    loop = asyncio.get_running_loop()
    tg_app = None

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.create_task(_shutdown(loop, tg_app))
        )

    if config.telegram_token:
        tg_app = build_app()
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot ready")
    else:
        logger.warning("TELEGRAM_TOKEN not set")

    # Run watchdog as concurrent task
    asyncio.create_task(run_watchdog())

    # Main feed loop (blocks)
    await start_feed()


if __name__ == "__main__":
    asyncio.run(main())
