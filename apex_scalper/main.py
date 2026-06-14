"""Entrypoint — bootstraps feed + telegram, graceful shutdown."""
from __future__ import annotations

import asyncio
import signal
import sys
from loguru import logger

from .config import config
from .feed import start_feed
from .telegram_ui import build_app
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
        retention="7 days",
        level=config.log_level,
    )


async def shutdown(loop: asyncio.AbstractEventLoop, tg_app=None):
    logger.warning("🛑 Shutdown signal received — closing position...")
    state.running = False
    await trader.close_position()
    if tg_app:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
    tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


async def main():
    setup_logging()
    logger.info(
        f"⚡ Apex Scalper v0.2.0 | {config.symbol} | "
        f"{'TESTNET' if config.testnet else 'MAINNET'} | "
        f"leverage={config.leverage}x size={config.order_size_usdt}USDT"
    )

    loop = asyncio.get_running_loop()

    # Graceful shutdown on SIGINT / SIGTERM
    tg_app = None
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.create_task(shutdown(loop, tg_app))
        )

    # Telegram
    if config.telegram_token:
        tg_app = build_app()
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot listening")
    else:
        logger.warning("TELEGRAM_TOKEN not set — UI disabled")

    await start_feed()


if __name__ == "__main__":
    asyncio.run(main())
