"""Anti-manipulation filter v0.4.2 — thread-safe signals singleton.

Fixes vs v0.4.1:
  FIX #6 — Race condition on _signals singleton:
    _signals (ManipulationSignals) is a module-level mutable object read by
    clear_for_entry() and written by analyze(). In asyncio, strategy.evaluate()
    and Telegram message handlers can be interleaved on the same event loop.
    analyze() runs in the candle callback; clear_for_entry() runs in the entry
    logic, both potentially within the same event loop iteration.
    Fix: _signals_lock = threading.Lock() guards all reads and writes.
    analyze() holds the lock for the full mutation window.
    clear_for_entry() copies the three relevant fields under lock and works
    on the snapshot — no reference to _signals outside the lock.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional
from loguru import logger

from .state import state

# Defaults (overridden by inject_wall_params() on startup)
WALL_RATIO            = 8.0
WALL_DISTANCE_TICKS   = 5
MOMENTUM_IGNITION_PCT = 0.003
MOMENTUM_VOL_THRESHOLD = 0.5


def inject_wall_params(wall_ratio: float, wall_distance_ticks: int) -> None:
    global WALL_RATIO, WALL_DISTANCE_TICKS
    WALL_RATIO          = wall_ratio
    WALL_DISTANCE_TICKS = wall_distance_ticks
    logger.info(
        f"AntiManip params: wall_ratio={WALL_RATIO} "
        f"wall_distance_ticks={WALL_DISTANCE_TICKS}"
    )


@dataclass
class ManipulationSignals:
    spoof_bid_wall:    bool = False
    spoof_ask_wall:    bool = False
    momentum_ignition: bool = False
    suspicious:        bool = False


# FIX #6: lock protects all reads and writes of _signals
_signals      = ManipulationSignals()
_signals_lock = threading.Lock()


class AntiManipulation:
    def __init__(self):
        self._prev_close: float = 0.0

    def analyze(
        self,
        vol_zscore: float = 0.0,
        current_close: Optional[float] = None,
    ) -> ManipulationSignals:
        with state.lock:
            bids  = state.orderbook.top_bids(20)
            asks  = state.orderbook.top_asks(20)
            price = current_close or state.last_price

        # Compute results outside lock to minimise contention
        spoof_bid = False
        spoof_ask = False
        mom_ign   = False

        if bids:
            mean_bid = sum(s for _, s in bids) / len(bids)
            for idx, (_, size) in enumerate(bids):
                if size > mean_bid * WALL_RATIO and idx >= WALL_DISTANCE_TICKS:
                    spoof_bid = True
                    logger.debug(
                        f"Spoof BID wall @ level {idx}: "
                        f"size={size:.2f} mean={mean_bid:.2f} "
                        f"ratio={size/mean_bid:.1f}x (threshold={WALL_RATIO}x)"
                    )
                    break

        if asks:
            mean_ask = sum(s for _, s in asks) / len(asks)
            for idx, (_, size) in enumerate(asks):
                if size > mean_ask * WALL_RATIO and idx >= WALL_DISTANCE_TICKS:
                    spoof_ask = True
                    logger.debug(
                        f"Spoof ASK wall @ level {idx}: "
                        f"size={size:.2f} mean={mean_ask:.2f} "
                        f"ratio={size/mean_ask:.1f}x (threshold={WALL_RATIO}x)"
                    )
                    break

        if self._prev_close > 0 and price > 0:
            move_pct = abs(price - self._prev_close) / self._prev_close
            if move_pct > MOMENTUM_IGNITION_PCT and vol_zscore < MOMENTUM_VOL_THRESHOLD:
                mom_ign = True
                logger.debug(
                    f"Momentum ignition: move={move_pct:.4%} vol_z={vol_zscore:.2f}"
                )

        self._prev_close = price

        # FIX #6: single atomic write under lock
        with _signals_lock:
            _signals.spoof_bid_wall    = spoof_bid
            _signals.spoof_ask_wall    = spoof_ask
            _signals.momentum_ignition = mom_ign
            _signals.suspicious        = spoof_bid or spoof_ask or mom_ign

        return _signals

    def clear_for_entry(self, side: str) -> bool:
        """FIX #6: read a snapshot under lock — no TOCTOU with analyze()."""
        with _signals_lock:
            mom_ign   = _signals.momentum_ignition
            spoof_bid = _signals.spoof_bid_wall
            spoof_ask = _signals.spoof_ask_wall

        if mom_ign:
            return False
        if side == "long"  and spoof_bid:
            return False
        if side == "short" and spoof_ask:
            return False
        return True


anti_manip = AntiManipulation()
