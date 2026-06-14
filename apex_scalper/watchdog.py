"""WebSocket heartbeat watchdog v0.2.0.

Changes vs v0.1.0:
  🟢 FIX: Auto-restart via os.execv after MAX_RESTART_TRIGGERS consecutive
     feed deaths. Previously the watchdog set _feed_restart_requested=True
     (handled by main.py) but if main.py was itself stuck, restarts never
     happened. Now after 3 unacknowledged feed deaths the watchdog does a
     hard process restart (replaces current process with a fresh one).

Flow:
  1. Feed death detected (no kline for HEARTBEAT_TIMEOUT seconds)
     → _feed_restart_requested = True (main.py reconnects WS)
     → Telegram alert sent
  2. If feed death count reaches MAX_RESTART_TRIGGERS within the session
     → os.execv() restarts the entire process cleanly
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from loguru import logger
from .state import state

HEARTBEAT_TIMEOUT   = 90   # seconds without kline update before alert
CHECK_INTERVAL      = 15   # how often to check
MAX_RESTART_TRIGGERS = 3   # consecutive feed deaths before hard process restart

_last_kline_ts: float = 0.0
_feed_restart_requested: bool = False
_death_count: int = 0          # consecutive feed deaths this session
_last_death_ack: float = 0.0   # time when main.py last acked a restart


def record_heartbeat() -> None:
    """Call this every time a kline message is received."""
    global _last_kline_ts, _death_count
    _last_kline_ts = time.monotonic()
    # A successful heartbeat resets the death counter
    _death_count = 0


def feed_restart_needed() -> bool:
    """Called by main.py to check if WS reconnect is needed."""
    global _feed_restart_requested, _last_death_ack
    if _feed_restart_requested:
        _feed_restart_requested = False
        _last_death_ack = time.monotonic()
        return True
    return False


def _hard_restart() -> None:
    """Replace current process with a fresh copy (os.execv)."""
    logger.critical(
        f"WATCHDOG: {MAX_RESTART_TRIGGERS} consecutive feed deaths — "
        "triggering hard process restart via os.execv"
    )
    try:
        from .telegram_ui import send_message  # best-effort
        import asyncio as _aio
        loop = _aio.new_event_loop()
        loop.run_until_complete(send_message(
            f"🔴 *WATCHDOG*: {MAX_RESTART_TRIGGERS} feed deaths — "
            "performing *hard process restart* now"
        ))
        loop.close()
    except Exception:
        pass
    # Re-execute current process with same args
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def run_watchdog() -> None:
    """Async task: checks heartbeat every CHECK_INTERVAL seconds."""
    global _feed_restart_requested, _death_count
    logger.info("Watchdog started (auto-restart enabled after "
                f"{MAX_RESTART_TRIGGERS} consecutive feed deaths)")
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        if _last_kline_ts == 0:
            continue
        elapsed = time.monotonic() - _last_kline_ts
        if elapsed > HEARTBEAT_TIMEOUT:
            _death_count += 1
            logger.critical(
                f"WS FEED DEAD — no kline in {elapsed:.0f}s — requesting restart "
                f"(death #{_death_count}/{MAX_RESTART_TRIGGERS})"
            )
            _feed_restart_requested = True
            try:
                from .telegram_ui import send_message
                await send_message(
                    f"⚠️ *WATCHDOG*: No WS data for `{elapsed:.0f}s` — "
                    f"feed restart triggered (death `#{_death_count}`)"
                )
            except Exception:
                pass

            # Hard restart if too many consecutive deaths
            if _death_count >= MAX_RESTART_TRIGGERS:
                # Run in executor so we don't block the event loop
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _hard_restart)
                return  # unreachable after execv, but safe

        elif elapsed > HEARTBEAT_TIMEOUT / 2:
            logger.warning(f"Watchdog: slow feed — {elapsed:.0f}s since last kline")
