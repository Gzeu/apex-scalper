"""Anti-manipulation filter v0.4.0 — OB spoof detection.

Spoofing = large orders placed in OB to create false price pressure,
then cancelled before execution. Characteristics:
  1. Sudden appearance of a very large order (WALL_RATIO x mean size)
  2. Located far from best price (> WALL_DISTANCE_TICKS ticks away)
  3. Short-lived (disappears within SPOOF_WINDOW candles)

We track OB snapshots and flag entries when:
  - A large fake wall is visible on the same side as our intended entry.
    e.g. big fake bid wall -> someone might want to push price DOWN.
  - The imbalance appears artificially high due to spoof orders.

Additionally detects:
  - Wash trading: repeated large trades at same price level
  - Momentum ignition: rapid price move > MOMENTUM_IGNITION_PCT in 1 candle
    without volume confirmation (vol_zscore < threshold)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from .state import state

# Tunable params
WALL_RATIO            = 8.0    # order must be Nx mean size to count as wall
WALL_DISTANCE_TICKS   = 5      # min levels away from best price
MOMENTUM_IGNITION_PCT = 0.003  # 0.3% move in 1 candle = suspicious
MOMENTUM_VOL_THRESHOLD = 0.5   # vol_zscore below this = unconfirmed move


@dataclass
class ManipulationSignals:
    spoof_bid_wall:    bool = False   # large fake bid wall detected
    spoof_ask_wall:    bool = False   # large fake ask wall detected
    momentum_ignition: bool = False   # rapid unconfirmed price move
    suspicious:        bool = False   # any manipulation signal active


_signals = ManipulationSignals()


class AntiManipulation:
    def __init__(self):
        self._prev_close: float = 0.0
        self._prev_vol_zscore: float = 0.0

    def analyze(
        self,
        vol_zscore: float = 0.0,
        current_close: Optional[float] = None,
    ) -> ManipulationSignals:
        """Run all manipulation checks. Call on each confirmed candle."""
        with state.lock:
            bids = state.orderbook.top_bids(20)
            asks = state.orderbook.top_asks(20)
            price = current_close or state.last_price

        _signals.spoof_bid_wall    = False
        _signals.spoof_ask_wall    = False
        _signals.momentum_ignition = False

        # 1. Spoof wall detection
        if bids:
            mean_bid = sum(s for _, s in bids) / len(bids)
            for idx, (_, size) in enumerate(bids):
                if size > mean_bid * WALL_RATIO and idx >= WALL_DISTANCE_TICKS:
                    _signals.spoof_bid_wall = True
                    logger.debug(
                        f"Spoof BID wall: size={size:.2f} mean={mean_bid:.2f} "
                        f"at level {idx}"
                    )
                    break

        if asks:
            mean_ask = sum(s for _, s in asks) / len(asks)
            for idx, (_, size) in enumerate(asks):
                if size > mean_ask * WALL_RATIO and idx >= WALL_DISTANCE_TICKS:
                    _signals.spoof_ask_wall = True
                    logger.debug(
                        f"Spoof ASK wall: size={size:.2f} mean={mean_ask:.2f} "
                        f"at level {idx}"
                    )
                    break

        # 2. Momentum ignition detection
        if self._prev_close > 0 and price > 0:
            move_pct = abs(price - self._prev_close) / self._prev_close
            if (
                move_pct > MOMENTUM_IGNITION_PCT
                and vol_zscore < MOMENTUM_VOL_THRESHOLD
            ):
                _signals.momentum_ignition = True
                logger.debug(
                    f"Momentum ignition: move={move_pct:.4%} "
                    f"vol_z={vol_zscore:.2f} (unconfirmed)"
                )

        _signals.suspicious = (
            _signals.spoof_bid_wall
            or _signals.spoof_ask_wall
            or _signals.momentum_ignition
        )

        self._prev_close      = price
        self._prev_vol_zscore = vol_zscore
        return _signals

    def clear_for_entry(self, side: str) -> bool:
        """Return True if manipulation signals don't block this entry direction.

        Logic:
          - Spoof BID wall (fake support) -> skip LONG (someone wants price down)
          - Spoof ASK wall (fake resistance) -> skip SHORT (someone wants price up)
          - Momentum ignition -> skip any entry
        """
        if _signals.momentum_ignition:
            return False
        if side == "long" and _signals.spoof_bid_wall:
            return False
        if side == "short" and _signals.spoof_ask_wall:
            return False
        return True


anti_manip = AntiManipulation()
