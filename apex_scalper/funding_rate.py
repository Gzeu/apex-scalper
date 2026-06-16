"""Funding Rate Monitor v1.1.1 — adaugat run_funding_refresh_loop().

Changelog:
  v1.1.1 — FIX: adaugat run_funding_refresh_loop(symbol) folosit de main.py.
    Inainte lipsea complet -> ImportError la startup.
  v1.1.0 — hard block entry la funding excesiv.
  v1.0.0 — funding rate fetch initial.
"""
from __future__ import annotations

import asyncio
import os
import time
from loguru import logger

FUNDING_LONG_BLOCK  = float(os.getenv("FUNDING_LONG_BLOCK",  "0.0005"))
FUNDING_SHORT_BLOCK = float(os.getenv("FUNDING_SHORT_BLOCK", "-0.0005"))
REFRESH_INTERVAL    = int(os.getenv("FUNDING_REFRESH_INTERVAL", "300"))
DEFAULT_FUNDING     = 0.0


class FundingRateMonitor:
    def __init__(self):
        self._funding_rate: float = DEFAULT_FUNDING
        self._last_refresh: float = 0.0
        self._lock  = asyncio.Lock()
        self._ready = False

    async def maybe_refresh(self, symbol: str) -> None:
        now = time.monotonic()
        if now - self._last_refresh < REFRESH_INTERVAL:
            return
        await self._fetch(symbol)

    async def _fetch(self, symbol: str) -> None:
        async with self._lock:
            try:
                from .trader import trader, _api_call_with_retry
                if trader._client is None:
                    return
                result = await _api_call_with_retry(
                    trader._client.get_tickers,
                    category="linear",
                    symbol=symbol,
                )
                if result.get("retCode") == 0:
                    tickers = result.get("result", {}).get("list", [])
                    if tickers:
                        fr_raw = tickers[0].get("fundingRate", None)
                        if fr_raw is not None:
                            old = self._funding_rate
                            self._funding_rate = float(fr_raw)
                            self._last_refresh  = time.monotonic()
                            self._ready         = True
                            if abs(self._funding_rate - old) > 0.00005:
                                logger.info(
                                    f"[Funding] {symbol}: {self._funding_rate:+.5%} "
                                    f"(long_block={FUNDING_LONG_BLOCK:+.4%} "
                                    f"short_block={FUNDING_SHORT_BLOCK:+.4%})"
                                )
            except Exception as e:
                logger.warning(f"[Funding] fetch error: {e} — folosesc ultima valoare")

    def can_enter_long(self) -> bool:
        if not self._ready:
            return True
        if self._funding_rate > FUNDING_LONG_BLOCK:
            logger.debug(f"[Funding] LONG blocat: {self._funding_rate:+.5%} > {FUNDING_LONG_BLOCK:+.4%}")
            return False
        return True

    def can_enter_short(self) -> bool:
        if not self._ready:
            return True
        if self._funding_rate < FUNDING_SHORT_BLOCK:
            logger.debug(f"[Funding] SHORT blocat: {self._funding_rate:+.5%} < {FUNDING_SHORT_BLOCK:+.4%}")
            return False
        return True

    @property
    def rate(self) -> float:
        return self._funding_rate

    @property
    def rate_pct(self) -> str:
        return f"{self._funding_rate:+.5%}"

    @property
    def ready(self) -> bool:
        return self._ready


funding = FundingRateMonitor()


async def run_funding_refresh_loop(symbol: str) -> None:
    """Loop care refresheaza funding rate la fiecare REFRESH_INTERVAL secunde."""
    logger.info(f"[Funding] refresh loop pornit (interval={REFRESH_INTERVAL}s)")
    while True:
        try:
            await funding._fetch(symbol)
        except Exception as e:
            logger.warning(f"[Funding] loop error: {e}")
        await asyncio.sleep(REFRESH_INTERVAL)
