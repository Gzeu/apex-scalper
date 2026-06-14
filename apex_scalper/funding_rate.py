"""Funding rate awareness v0.7.6 — fetch + cache + risk filter.

Changelog:
  v0.7.6 — BUG FIX: trader._session -> trader._client (same class of bug
             as mtf_filter.py fixed in v0.7.5). Caused AttributeError
             on every refresh cycle — funding gate was silently disabled.

Bybit pays/charges funding every 8h (00:00, 08:00, 16:00 UTC).
Rules applied:
  - If |funding_rate| > FUNDING_RATE_SKIP_PCT: skip entry in that direction.
  - If next_funding_time < FUNDING_TIME_BUFFER_S seconds away: skip any entry.
  - Cached for CACHE_TTL_S seconds to avoid hammering the API.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional
from loguru import logger

from .config import config
from .trader import trader

FUNDING_RATE_SKIP_PCT = float(0.0001)   # 0.01%
FUNDING_TIME_BUFFER_S = 300             # 5 min before funding payment
CACHE_TTL_S           = 60


class FundingRateMonitor:
    def __init__(self):
        self._rate: float = 0.0
        self._next_funding_ms: int = 0
        self._last_fetch: float = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self, symbol: Optional[str] = None) -> None:
        """Fetch funding rate from Bybit. Called periodically."""
        sym = symbol or config.symbol
        # v0.7.6 fix: trader exposes _client, not _session
        if not trader._client:
            return
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: trader._client.get_funding_rate_history(
                    category="linear",
                    symbol=sym,
                    limit=1,
                ),
            )
            items = resp.get("result", {}).get("list", [])
            if items:
                self._rate = float(items[0].get("fundingRate", 0))
                self._next_funding_ms = (
                    int(items[0].get("fundingRateTimestamp", 0)) + 8 * 3600 * 1000
                )
                logger.debug(
                    f"Funding rate [{sym}]: {self._rate:.6f} "
                    f"next_ms={self._next_funding_ms}"
                )
        except Exception as e:
            logger.warning(f"Funding rate fetch error: {e}")
        self._last_fetch = time.time()

    async def maybe_refresh(self, symbol: Optional[str] = None) -> None:
        if time.time() - self._last_fetch > CACHE_TTL_S:
            async with self._lock:
                if time.time() - self._last_fetch > CACHE_TTL_S:
                    await self.refresh(symbol)

    def can_enter_long(self) -> bool:
        if self._near_funding():
            logger.debug("Near funding payment — skipping entry")
            return False
        if self._rate > FUNDING_RATE_SKIP_PCT:
            logger.debug(f"Funding {self._rate:.6f} too positive — skip LONG")
            return False
        return True

    def can_enter_short(self) -> bool:
        if self._near_funding():
            return False
        if self._rate < -FUNDING_RATE_SKIP_PCT:
            logger.debug(f"Funding {self._rate:.6f} too negative — skip SHORT")
            return False
        return True

    def _near_funding(self) -> bool:
        if self._next_funding_ms == 0:
            return False
        now_ms = int(time.time() * 1000)
        return (self._next_funding_ms - now_ms) < FUNDING_TIME_BUFFER_S * 1000

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def rate_pct(self) -> str:
        return f"{self._rate * 100:.4f}%"


funding = FundingRateMonitor()


async def run_funding_refresh_loop(symbol: Optional[str] = None) -> None:
    while True:
        try:
            await funding.refresh(symbol)
        except Exception as e:
            logger.warning(f"Funding refresh loop error: {e}")
        await asyncio.sleep(CACHE_TTL_S)
