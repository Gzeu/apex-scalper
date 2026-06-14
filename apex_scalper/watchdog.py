"""Watchdog v0.7.6 — heartbeat monitor + feed_restart_needed export.

Changelog:
  v0.7.6 — BUG FIX: feed_restart_needed() added.
             feed.py imports this function to decide when to reconnect WS.
             Was missing — caused ImportError every 10s — WS reconnect storm.
             Also exposes _last_kline_ts (read by /watchdog Telegram command).
  v0.6.1 — run_watchdog() alias added for main.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from loguru import logger

HEARTBEAT_TIMEOUT     = int(os.getenv("HEARTBEAT_TIMEOUT", "120"))
MAX_RESTARTS_PER_HOUR = 3
COOLDOWN_S            = 300
FEED_STALE_S          = float(os.getenv("FEED_STALE_S", "2.0"))

_last_heartbeat: float  = time.monotonic()
_last_kline_ts:  float  = 0.0          # updated by feed.py on every confirmed kline
_restart_timestamps: deque = deque(maxlen=MAX_RESTARTS_PER_HOUR)


def record_heartbeat() -> None:
    global _last_heartbeat
    _last_heartbeat = time.monotonic()


def record_kline() -> None:
    """Called by feed.py on every confirmed kline. Used by feed_restart_needed()."""
    global _last_kline_ts
    _last_kline_ts = time.monotonic()


def feed_restart_needed() -> bool:
    """Return True when no kline has arrived for HEARTBEAT_TIMEOUT seconds.

    Called by feed.py every 10s inside the WS loop to detect a dead feed
    and trigger a WebSocket reconnect.
    Returns False if we haven't received any kline yet (bot just started).
    """
    if _last_kline_ts == 0.0:
        return False   # not started yet, don't restart immediately
    return (time.monotonic() - _last_kline_ts) > HEARTBEAT_TIMEOUT


def seconds_since_heartbeat() -> float:
    return time.monotonic() - _last_heartbeat


async def _send_alert(msg: str) -> None:
    try:
        from .telegram_ui import send_message
        await send_message(msg)
    except Exception:
        pass


async def _restart_bot(reason: str) -> None:
    now = time.time()
    while _restart_timestamps and now - _restart_timestamps[0] > 3600:
        _restart_timestamps.popleft()

    if len(_restart_timestamps) >= MAX_RESTARTS_PER_HOUR:
        msg = (
            f"\U0001f6a8 *Watchdog*: {MAX_RESTARTS_PER_HOUR} restarts in 1h!\n"
            f"Reason: `{reason}`\n"
            f"Bot staying DOWN for {COOLDOWN_S//60} min cooldown."
        )
        logger.critical(msg)
        await _send_alert(msg)
        await asyncio.sleep(COOLDOWN_S)
        _restart_timestamps.clear()

    _restart_timestamps.append(now)
    await _send_alert(
        f"\u26a0\ufe0f *Watchdog*: restarting.\nReason: `{reason}`\n"
        f"Restart #{len(_restart_timestamps)} this hour."
    )
    logger.warning(f"Watchdog restart: {reason}")
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def watchdog_loop() -> None:
    logger.info(
        f"Watchdog started (timeout={HEARTBEAT_TIMEOUT}s, "
        f"max_restarts={MAX_RESTARTS_PER_HOUR}/h)"
    )
    while True:
        await asyncio.sleep(30)
        stale = seconds_since_heartbeat()
        if stale > HEARTBEAT_TIMEOUT:
            await _restart_bot(f"heartbeat stale {stale:.0f}s > {HEARTBEAT_TIMEOUT}s")


# Alias expected by main.py
run_watchdog = watchdog_loop
