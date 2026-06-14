"""WebSocket heartbeat watchdog.

Monitors the time since last kline update. If no update received within
HEARTBEAT_TIMEOUT seconds, logs a critical alert and optionally restarts
the feed by setting a flag that main.py monitors.
"""
from __future__ import annotations

import asyncio
import time
from loguru import logger
from .state import state

HEARTBEAT_TIMEOUT = 90   # seconds without kline update before alert
CHECK_INTERVAL    = 15   # how often to check

_last_kline_ts: float = 0.0
_feed_restart_requested: bool = False


def record_heartbeat() -> None:
    """Call this every time a kline message is received."""
    global _last_kline_ts
    _last_kline_ts = time.monotonic()


def feed_restart_needed() -> bool:
    global _feed_restart_requested
    if _feed_restart_requested:
        _feed_restart_requested = False
        return True
    return False


async def run_watchdog() -> None:
    """Async task: checks heartbeat every CHECK_INTERVAL seconds."""
    global _feed_restart_requested
    logger.info("Watchdog started")
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        if _last_kline_ts == 0:
            continue
        elapsed = time.monotonic() - _last_kline_ts
        if elapsed > HEARTBEAT_TIMEOUT:
            logger.critical(
                f"WS FEED DEAD — no kline in {elapsed:.0f}s — requesting restart"
            )
            _feed_restart_requested = True
            try:
                from .telegram_ui import send_message
                await send_message(
                    f"⚠️ *WATCHDOG*: No WS data for `{elapsed:.0f}s` — feed restart triggered"
                )
            except Exception:
                pass
        elif elapsed > HEARTBEAT_TIMEOUT / 2:
            logger.warning(f"Watchdog: slow feed — {elapsed:.0f}s since last kline")
