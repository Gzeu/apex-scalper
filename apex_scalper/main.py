"""Entrypoint — bootstraps feed, telegram, and strategy loop."""
from __future__ import annotations

import asyncio
import sys
from loguru import logger

from .config import config
from .feed import start_feed
from .telegram_ui import build_app


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


async def main():
    setup_logging()
    logger.info(f"⚡ Apex Scalper v0.1.0 starting — {config.symbol} @ {'TESTNET' if config.testnet else 'MAINNET'}")

    # Build Telegram app (non-blocking)
    if config.telegram_token:
        tg_app = build_app()
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
        logger.info("Telegram bot listening")
    else:
        logger.warning("TELEGRAM_TOKEN not set — UI disabled")

    # Start public feed (blocking loop)
    await start_feed()


if __name__ == "__main__":
    asyncio.run(main())
