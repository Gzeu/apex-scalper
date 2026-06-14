"""Shared in-memory state: orderbook, last price, PnL, positions.

IMPORTANT: pybit WebSocket callbacks run in a separate thread (not the asyncio
event loop). We use a threading.Lock here so both the WS thread and the async
strategy coroutine can safely access shared state without deadlock.
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


@dataclass
class BotState:
    running: bool = False
    paused: bool = False
    orderbook: OrderBook = field(default_factory=OrderBook)
    last_price: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    rsi_gains: list = field(default_factory=list)
    rsi_losses: list = field(default_factory=list)
    rsi_avg_gain: float = 0.0
    rsi_avg_loss: float = 0.0
    rsi_value: float = 50.0
    rsi_ready: bool = False
    rsi_prev_price: float = 0.0
    rsi_count: int = 0
    open_position: Optional[str] = None
    open_qty: float = 0.0
    open_entry: float = 0.0
    trailing_stop: float = 0.0
    realized_pnl: float = 0.0
    daily_pnl: float = 0.0
    total_trades: int = 0
    win_trades: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def symbol_str(self) -> str:
        from .config import config
        return config.symbol

    @property
    def winrate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return round(self.win_trades / self.total_trades * 100, 1)


state = BotState()
