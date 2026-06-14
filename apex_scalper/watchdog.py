"""Watchdog v0.6.0 — heartbeat monitor + auto-restart with rate limit.

Changes vs v0.1.0:
  - restart_bot() added: kills current process and re-execs via sys.argv
    (works in Docker with restart=unless-stopped)
  - Rate limit: max MAX_RESTARTS_PER_HOUR restarts, then sends Telegram alert
    and stays down for COOLDOWN_S seconds.
  - HEARTBEAT_TIMEOUT increased to 120s (was 60s) to avoid false restarts
    during slow Bybit responses.
  - restart_count and restart_timestamps tracked to enforce rate limit.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from loguru import logger

HEARTBEAT_TIMEOUT   = int(os.getenv("HEARTBEAT_TIMEOUT", "120"))  # seconds
MAX_RESTARTS_PER_HOUR = 3
COOLDOWN_S           = 300   # 5 min cooldown after max restarts

_last_heartbeat: float = time.monotonic()
_restart_timestamps: deque = deque(maxlen=MAX_RESTARTS_PER_HOUR)


def record_heartbeat() -> None:
    global _last_heartbeat
    _last_heartbeat = time.monotonic()


def seconds_since_heartbeat() -> float:
    return time.monotonic() - _last_heartbeat


async def _send_alert(msg: str) -> None:
    try:
        from .telegram_ui import send_message
        await send_message(msg)
    except Exception:
        pass


async def _restart_bot(reason: str) -> None:
    """Rate-limited restart. In Docker: process exit triggers container restart."""
    now = time.time()
    # Purge timestamps older than 1 hour
    while _restart_timestamps and now - _restart_timestamps[0] > 3600:
        _restart_timestamps.popleft()

    if len(_restart_timestamps) >= MAX_RESTARTS_PER_HOUR:
        msg = (
            f"🚨 *Watchdog*: {MAX_RESTARTS_PER_HOUR} restarts in 1h!\n"
            f"Reason: `{reason}`\n"
            f"Bot staying DOWN for {COOLDOWN_S//60} min cooldown."
        )
        logger.critical(msg)
        await _send_alert(msg)
        await asyncio.sleep(COOLDOWN_S)
        # After cooldown, allow one more restart
        _restart_timestamps.clear()

    _restart_timestamps.append(now)
    await _send_alert(
        f"⚠️ *Watchdog*: restarting bot.\nReason: `{reason}`\n"
        f"Restart #{len(_restart_timestamps)} this hour."
    )
    logger.warning(f"Watchdog restart: {reason}")
    # Re-exec current process (works in Docker)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def watchdog_loop() -> None:
    """Main loop: check heartbeat every 30s, restart if stale."""
    logger.info(
        f"Watchdog started (timeout={HEARTBEAT_TIMEOUT}s, "
        f"max_restarts={MAX_RESTARTS_PER_HOUR}/h)"
    )
    while True:
        await asyncio.sleep(30)
        stale = seconds_since_heartbeat()
        if stale > HEARTBEAT_TIMEOUT:
            await _restart_bot(f"heartbeat stale {stale:.0f}s > {HEARTBEAT_TIMEOUT}s")
