"""Shared in-memory state: orderbook, last price, PnL, positions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderBook:
    """Local L2 orderbook maintained from WS delta updates."""
    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    seq: int = 0

    def apply_snapshot(self, bids: list, asks: list) -> None:
        self.bids = {float(p): float(s) for p, s in bids}
        self.asks = {float(p): float(s) for p, s in asks}

    def apply_delta(self, side: str, price: str, size: str) -> None:
        book = self.bids if side == "b" else self.asks
        p, s = float(price), float(size)
        if s == 0:
            book.pop(p, None)
        else:
            book[p] = s

    @property
    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None


@dataclass
class BotState:
    running: bool = False
    paused: bool = False
    orderbook: OrderBook = field(default_factory=OrderBook)
    last_price: float = 0.0
    ema_fast: float = 0.0  # EMA(9)
    ema_slow: float = 0.0  # EMA(21)
    open_position: Optional[str] = None  # "long" | "short" | None
    open_qty: float = 0.0
    open_entry: float = 0.0
    realized_pnl: float = 0.0
    daily_pnl: float = 0.0
    total_trades: int = 0
    win_trades: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def winrate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return round(self.win_trades / self.total_trades * 100, 1)


state = BotState()
