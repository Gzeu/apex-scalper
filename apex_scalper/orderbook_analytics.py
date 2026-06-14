"""Real-time orderbook analytics: imbalance, VWAP, pressure score.

FIX vs v0.3.0: was accessing state.orderbook._bids/_asks directly (private attrs).
Now uses public top_bids() / top_asks() methods on OrderBook.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from .state import state


@dataclass
class OBSignals:
    imbalance: float = 0.0        # (bid_vol - ask_vol) / total, range [-1, 1]
    bid_vwap: float = 0.0         # Volume-weighted avg price of top-N bid levels
    ask_vwap: float = 0.0         # Volume-weighted avg price of top-N ask levels
    pressure_score: float = 0.0   # Composite buy/sell pressure [-1, +1]
    large_bid: bool = False        # Unusually large bid wall detected
    large_ask: bool = False        # Unusually large ask wall detected


ob_signals = OBSignals()

IMBALANCE_LEVELS = 10
WALL_MULTIPLIER  = 5.0


def compute(levels: int = IMBALANCE_LEVELS) -> OBSignals:
    """Compute all OB signals. Uses public OrderBook API (no private access)."""
    with state.lock:
        # FIX: use public methods instead of ._bids / ._asks
        bids = state.orderbook.top_bids(levels)   # [(price, size), ...] desc
        asks = state.orderbook.top_asks(levels)   # [(price, size), ...] asc

    if not bids or not asks:
        return ob_signals

    bid_vol = sum(s for _, s in bids)
    ask_vol = sum(s for _, s in asks)
    total   = bid_vol + ask_vol

    ob_signals.imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0

    ob_signals.bid_vwap = (
        sum(p * s for p, s in bids) / bid_vol if bid_vol > 0 else 0.0
    )
    ob_signals.ask_vwap = (
        sum(p * s for p, s in asks) / ask_vol if ask_vol > 0 else 0.0
    )

    ob_signals.pressure_score = ob_signals.imbalance * math.log1p(total)

    if bids:
        mean_bid = bid_vol / len(bids)
        ob_signals.large_bid = any(s > mean_bid * WALL_MULTIPLIER for _, s in bids)
    if asks:
        mean_ask = ask_vol / len(asks)
        ob_signals.large_ask = any(s > mean_ask * WALL_MULTIPLIER for _, s in asks)

    return ob_signals
