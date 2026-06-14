"""Real-time orderbook analytics: imbalance, VWAP, pressure score.

Called after every orderbook delta to compute derived signals used by strategy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from .state import state


@dataclass
class OBSignals:
    """Snapshot of orderbook-derived signals."""
    imbalance: float = 0.0      # (bid_vol - ask_vol) / (bid_vol + ask_vol), range [-1, 1]
    bid_vwap: float = 0.0       # Volume-weighted avg price of top-N bid levels
    ask_vwap: float = 0.0       # Volume-weighted avg price of top-N ask levels
    pressure_score: float = 0.0 # Composite buy/sell pressure [-1=max sell, +1=max buy]
    large_bid: bool = False     # Unusually large bid wall detected
    large_ask: bool = False     # Unusually large ask wall detected


# Module-level signals updated in place
ob_signals = OBSignals()

IMBALANCE_LEVELS = 10   # how many levels to consider for imbalance
WALL_MULTIPLIER  = 5.0  # a level is a "wall" if its size > WALL_MULTIPLIER * mean_size


def compute(levels: int = IMBALANCE_LEVELS) -> OBSignals:
    """Compute all orderbook signals from current state. Thread-safe read."""
    with state.lock:
        ob = state.orderbook
        bids = list(ob._bids.items())[:levels]  # [(price, size), ...] desc
        asks = list(ob._asks.items())[:levels]  # [(price, size), ...] asc

    if not bids or not asks:
        return ob_signals

    bid_vol = sum(s for _, s in bids)
    ask_vol = sum(s for _, s in asks)
    total   = bid_vol + ask_vol

    ob_signals.imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0

    # VWAP per side
    ob_signals.bid_vwap = (
        sum(p * s for p, s in bids) / bid_vol if bid_vol > 0 else 0.0
    )
    ob_signals.ask_vwap = (
        sum(p * s for p, s in asks) / ask_vol if ask_vol > 0 else 0.0
    )

    # Pressure score: imbalance weighted by log(total_volume)
    ob_signals.pressure_score = ob_signals.imbalance * math.log1p(total)

    # Wall detection
    if bids:
        mean_bid = bid_vol / len(bids)
        ob_signals.large_bid = any(s > mean_bid * WALL_MULTIPLIER for _, s in bids)
    if asks:
        mean_ask = ask_vol / len(asks)
        ob_signals.large_ask = any(s > mean_ask * WALL_MULTIPLIER for _, s in asks)

    return ob_signals
