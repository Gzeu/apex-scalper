"""Watchdog v0.9.1 — adaugat get_last_kline_ts() getter public.

Changelog:
  v0.9.1 — adaugat get_last_kline_ts() -> float pentru a permite
    telegram_ui.py sa acceseze timestamp-ul fara import direct de variabila
    privata. Elimina ImportError 'cannot import name get_last_kline_ts'.
  v0.9.0 — BUG 39 FIX: os.execv() inlocuit cu sys.exit(1) + subprocess fallback.
  v0.7.6 — feed_restart_needed() added. Also exposes _last_kline_ts.
  v0.6.1 — run_watchdog() alias added for main.py.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections import deque
from loguru import logger

HEARTBEAT_TIMEOUT     = int(os.getenv("HEARTBEAT_TIMEOUT", "120"))
MAX_RESTARTS_PER_HOUR = 3
COOLDOWN_S            = 300
FEED_STALE_S          = float(os.getenv("FEED_STALE_S", "2.0"))

# Detectam daca rulam sub Docker sau systemd (au restart management propriu)
_MANAGED = bool(
    os.getenv("DOCKER_CONTAINER")
    or os.getenv("container")          # systemd-nspawn
    or os.path.exists("/.dockerenv")
)

_last_heartbeat:      float = time.monotonic()
_last_kline_ts:       float = 0.0
_restart_timestamps:  deque = deque(maxlen=MAX_RESTARTS_PER_HOUR)


def record_heartbeat() -> None:
    global _last_heartbeat
    _last_heartbeat = time.monotonic()


def record_kline() -> None:
    """Called by feed.py on every confirmed kline."""
    global _last_kline_ts
    _last_kline_ts = time.monotonic()


def get_last_kline_ts() -> float:
    """Getter public pentru _last_kline_ts.

    Folosit de telegram_ui.py si orice alt modul care vrea sa verifice
    varsta ultimului kline fara import direct de variabila privata.
    Returneaza 0.0 daca nu a venit niciun kline de la pornire.
    """
    return _last_kline_ts


def feed_restart_needed() -> bool:
    """Return True when no kline has arrived for HEARTBEAT_TIMEOUT seconds.

    Returns False if we haven't received any kline yet (bot just started).
    """
    if _last_kline_ts == 0.0:
        return False
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
    """Restart curat — sys.exit(1) pentru Docker/systemd, subprocess fallback altfel.

    BUG 39 FIX: os.execv() inlocuit complet.
    """
    now = time.time()
    while _restart_timestamps and now - _restart_timestamps[0] > 3600:
        _restart_timestamps.popleft()

    if len(_restart_timestamps) >= MAX_RESTARTS_PER_HOUR:
        msg = (
            f"\U0001f6a8 *Watchdog*: {MAX_RESTARTS_PER_HOUR} restarts in 1h!\n"
            f"Reason: `{reason}`\n"
            f"Bot staying DOWN for {COOLDOWN_S // 60} min cooldown."
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
    logger.warning(f"Watchdog restart: {reason} | managed={_MANAGED}")

    if _MANAGED:
        logger.info("Watchdog: sys.exit(1) — Docker/systemd will restart.")
        sys.exit(1)
    else:
        cmd = [sys.executable, "-m", "apex_scalper"] + sys.argv[1:]
        logger.info(f"Watchdog: subprocess.Popen({cmd}) + sys.exit(0)")
        try:
            subprocess.Popen(cmd, cwd=os.getcwd())
        except Exception as e:
            logger.error(f"Watchdog: subprocess.Popen failed: {e}")
        sys.exit(0)


async def watchdog_loop() -> None:
    logger.info(
        f"Watchdog started (timeout={HEARTBEAT_TIMEOUT}s, "
        f"max_restarts={MAX_RESTARTS_PER_HOUR}/h, "
        f"managed={'yes — exit(1)' if _MANAGED else 'no — subprocess'})"
    )
    while True:
        await asyncio.sleep(30)
        stale = seconds_since_heartbeat()
        if stale > HEARTBEAT_TIMEOUT:
            await _restart_bot(f"heartbeat stale {stale:.0f}s > {HEARTBEAT_TIMEOUT}s")


# Alias expected by main.py
run_watchdog = watchdog_loop
