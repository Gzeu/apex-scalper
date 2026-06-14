"""Anti-manipulation filter v0.4.1 — per-symbol wall thresholds.

Changes vs v0.4.0:
- WALL_RATIO and WALL_DISTANCE_TICKS are now read from SYMBOL_PROFILES
  (injected via inject_wall_params() called from main.py)
- Different thresholds per symbol: BTC (deep book) vs DOGE (thin book)

BTC:  wall_ratio=8.0, wall_distance=5  (deep book, need big orders to be suspicious)
ETH:  wall_ratio=7.0, wall_distance=4
HYPE: wall_ratio=5.0, wall_distance=3  (thin book, 5x already suspicious)
DOGE: wall_ratio=4.0, wall_distance=3  (very thin)
NEAR: wall_ratio=5.0, wall_distance=3
"""
from __future__ import annotations

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
    """Called from main.inject_profile() to set per-symbol thresholds."""
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


_signals = ManipulationSignals()


class AntiManipulation:
    def __init__(self):
        self._prev_close: float = 0.0

    def analyze(
        self,
        vol_zscore: float = 0.0,
        current_close: Optional[float] = None,
    ) -> ManipulationSignals:
        with state.lock:
            bids = state.orderbook.top_bids(20)
            asks = state.orderbook.top_asks(20)
            price = current_close or state.last_price

        _signals.spoof_bid_wall    = False
        _signals.spoof_ask_wall    = False
        _signals.momentum_ignition = False

        # Spoof wall detection — uses per-symbol thresholds
        if bids:
            mean_bid = sum(s for _, s in bids) / len(bids)
            for idx, (_, size) in enumerate(bids):
                if size > mean_bid * WALL_RATIO and idx >= WALL_DISTANCE_TICKS:
                    _signals.spoof_bid_wall = True
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
                    _signals.spoof_ask_wall = True
                    logger.debug(
                        f"Spoof ASK wall @ level {idx}: "
                        f"size={size:.2f} mean={mean_ask:.2f} "
                        f"ratio={size/mean_ask:.1f}x (threshold={WALL_RATIO}x)"
                    )
                    break

        # Momentum ignition detection
        if self._prev_close > 0 and price > 0:
            move_pct = abs(price - self._prev_close) / self._prev_close
            if move_pct > MOMENTUM_IGNITION_PCT and vol_zscore < MOMENTUM_VOL_THRESHOLD:
                _signals.momentum_ignition = True
                logger.debug(
                    f"Momentum ignition: move={move_pct:.4%} vol_z={vol_zscore:.2f}"
                )

        _signals.suspicious = (
            _signals.spoof_bid_wall
            or _signals.spoof_ask_wall
            or _signals.momentum_ignition
        )

        self._prev_close = price
        return _signals

    def clear_for_entry(self, side: str) -> bool:
        if _signals.momentum_ignition:
            return False
        if side == "long"  and _signals.spoof_bid_wall:
            return False
        if side == "short" and _signals.spoof_ask_wall:
            return False
        return True


anti_manip = AntiManipulation()
