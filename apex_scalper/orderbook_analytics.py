"""Real-time orderbook analytics: imbalance, VWAP, pressure score.

Changelog:
  v0.8.3 — BUG 17 FIX: compute() modifica si returna acelasi ob_signals
    singleton global -> orice caller care pastreaza referinta vedea valorile
    suprascrise la apelul urmator.
    Fix: compute() returneaza un nou OBSignals() la fiecare apel.
    Singleton global ob_signals eliminat (era anti-pattern).
  v0.3.1 — state.orderbook._bids/_asks -> top_bids()/top_asks() public API.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from .state import state


@dataclass
class OBSignals:
    imbalance: float = 0.0
    bid_vwap: float = 0.0
    ask_vwap: float = 0.0
    pressure_score: float = 0.0
    large_bid: bool = False
    large_ask: bool = False


IMBALANCE_LEVELS = 10
WALL_MULTIPLIER  = 5.0


def compute(levels: int = IMBALANCE_LEVELS) -> OBSignals:
    """Compute all OB signals. Returns a fresh OBSignals() each call.

    v0.8.3 BUG 17 FIX: nu mai modifica un singleton global.
    Fiecare caller primeste o instanta independenta.
    """
    with state.lock:
        bids = state.orderbook.top_bids(levels)
        asks = state.orderbook.top_asks(levels)

    # BUG 17 FIX: obiect nou la fiecare apel
    sig = OBSignals()

    if not bids or not asks:
        return sig

    bid_vol = sum(s for _, s in bids)
    ask_vol = sum(s for _, s in asks)
    total   = bid_vol + ask_vol

    sig.imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0

    sig.bid_vwap = (
        sum(p * s for p, s in bids) / bid_vol if bid_vol > 0 else 0.0
    )
    sig.ask_vwap = (
        sum(p * s for p, s in asks) / ask_vol if ask_vol > 0 else 0.0
    )

    sig.pressure_score = sig.imbalance * math.log1p(total)

    if bids:
        mean_bid = bid_vol / len(bids)
        sig.large_bid = any(s > mean_bid * WALL_MULTIPLIER for _, s in bids)
    if asks:
        mean_ask = ask_vol / len(asks)
        sig.large_ask = any(s > mean_ask * WALL_MULTIPLIER for _, s in asks)

    return sig
