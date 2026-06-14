"""Shared in-memory state v0.8.4 — adaugat last_tick_ts pentru healthcheck.

Changelog:
  v0.8.4 — BUG 20 FIX: last_tick_ts lipsea din BotState.
    health.py folosea getattr(state, 'last_tick_ts', 0.0) ca fallback
    dar campul nu exista -> tick_age=inf, feed_stale=True permanent
    -> Docker HEALTHCHECK returna 503 la infinit.
    Fix: last_tick_ts: float = 0.0 adaugat explicit in BotState.
    feed.py trebuie sa actualizeze state.last_tick_ts la fiecare OB tick.

All indicator state is owned by indicators.IndicatorState (NOT here).
This module only holds execution/position/PnL state + the L2 orderbook.
Mutations from WS thread use threading.Lock.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional
from sortedcontainers import SortedDict


class OrderBook:
    """Local L2 orderbook with O(log n) insert/delete via SortedDict."""

    def __init__(self):
        self._bids: SortedDict = SortedDict(lambda k: -k)
        self._asks: SortedDict = SortedDict()
        self.seq: int = 0

    def apply_snapshot(self, bids: list, asks: list) -> None:
        self._bids.clear()
        self._asks.clear()
        for p, s in bids:
            self._bids[float(p)] = float(s)
        for p, s in asks:
            self._asks[float(p)] = float(s)

    def apply_delta(self, side: str, price: str, size: str) -> None:
        book = self._bids if side == "b" else self._asks
        p, s = float(price), float(size)
        if s == 0.0:
            book.pop(p, None)
        else:
            book[p] = s

    @property
    def best_bid(self) -> Optional[float]:
        return self._bids.keys()[0] if self._bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self._asks.keys()[0] if self._asks else None

    @property
    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2 if bb and ba else None

    @property
    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        return (ba - bb) if bb and ba else None

    def bid_depth(self, levels: int = 5) -> float:
        return sum(list(self._bids.values())[:levels])

    def ask_depth(self, levels: int = 5) -> float:
        return sum(list(self._asks.values())[:levels])

    def top_bids(self, levels: int = 10) -> list[tuple[float, float]]:
        return list(self._bids.items())[:levels]

    def top_asks(self, levels: int = 10) -> list[tuple[float, float]]:
        return list(self._asks.items())[:levels]


@dataclass
class BotState:
    # Control
    running: bool = False
    paused: bool = False

    # Market data
    orderbook: OrderBook = field(default_factory=OrderBook)
    last_price: float = 0.0

    # BUG 20 FIX: last_tick_ts adaugat explicit
    # Actualizat de feed.py la fiecare OB tick primit de la WebSocket
    # Folosit de health.py pentru feed_stale detection si Docker healthcheck
    last_tick_ts: float = 0.0

    # Open position
    open_position: Optional[str] = None
    open_qty: float = 0.0
    open_entry: float = 0.0
    trailing_stop: float = 0.0

    # PnL accounting
    realized_pnl: float = 0.0
    daily_pnl: float = 0.0
    total_trades: int = 0
    win_trades: int = 0

    # Thread lock
    lock: threading.Lock = field(default_factory=threading.Lock)

    def symbol_str(self) -> str:
        from .config import config
        return config.symbol

    @property
    def winrate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return round(self.win_trades / self.total_trades * 100, 1)

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0


state = BotState()
