"""Funding Rate Monitor v1.1.0 — hard block la funding excesiv.

Changelog:
  v1.1.0 — FEATURE: hard block entry la funding rate excesiv.
    PROBLEMA: la DOGE funding rate poate fi >0.05%/8h in bull runs.
    Un long tinut 4 minute plateste fractie din funding dar DIRECTIA
    e contra ta — semnaleaza ca piata e prea long-biased = risc reversal.
    BLOCARE:
      can_enter_long()  -> False daca funding > FUNDING_LONG_BLOCK  (+0.05%)
      can_enter_short() -> False daca funding < FUNDING_SHORT_BLOCK (-0.05%)
    LOGICA: funding pozitiv mare = toata lumea e long = piata supraincalzita.
    Intrare long in aceasta situatie = risc de reversal imediat.
    Refresh: la fiecare REFRESH_INTERVAL secunde (default 300s = 5 minute).
    Cache: daca API-ul esueaza, foloseste ultima valoare cunoscuta.
  v1.0.0 — funding rate fetch initial, can_enter_long/short basic.
"""
from __future__ import annotations

import asyncio
import os
import time
from loguru import logger

# Threshold-uri pentru blocare entry
# Funding > +0.05%/8h = piata prea long-biased -> nu mai deschidem long-uri
# Funding < -0.05%/8h = piata prea short-biased -> nu mai deschidem short-uri
FUNDING_LONG_BLOCK  = float(os.getenv("FUNDING_LONG_BLOCK",  "0.0005"))   # +0.05%
FUNDING_SHORT_BLOCK = float(os.getenv("FUNDING_SHORT_BLOCK", "-0.0005"))  # -0.05%

# Refresh la fiecare 5 minute (funding rate se schimba la fiecare 8h pe Bybit)
REFRESH_INTERVAL = int(os.getenv("FUNDING_REFRESH_INTERVAL", "300"))

# Daca nu putem obtine funding rate, permitem entry (fail-open)
DEFAULT_FUNDING = 0.0


class FundingRateMonitor:
    def __init__(self):
        self._funding_rate: float = DEFAULT_FUNDING
        self._last_refresh: float = 0.0
        self._lock = asyncio.Lock()
        self._ready = False

    async def maybe_refresh(self, symbol: str) -> None:
        """Refresh funding rate daca a trecut REFRESH_INTERVAL."""
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
                            return
            except Exception as e:
                logger.warning(f"[Funding] fetch error: {e} — folosesc ultima valoare")

    def can_enter_long(self) -> bool:
        """Blocheaza long-uri cand funding e prea pozitiv.

        Funding mare pozitiv = long-ii platesc short-ii = piata supraincalzita.
        Reversalul e iminent — nu deschidem long-uri noi.
        """
        if not self._ready:
            return True  # fail-open: daca nu avem date, permitem
        if self._funding_rate > FUNDING_LONG_BLOCK:
            logger.debug(
                f"[Funding] LONG blocat: funding={self._funding_rate:+.5%} "
                f"> threshold={FUNDING_LONG_BLOCK:+.4%}"
            )
            return False
        return True

    def can_enter_short(self) -> bool:
        """Blocheaza short-uri cand funding e prea negativ.

        Funding mare negativ = short-ii platesc long-ii = piata supravanduta.
        Reversalul e iminent — nu deschidem short-uri noi.
        """
        if not self._ready:
            return True
        if self._funding_rate < FUNDING_SHORT_BLOCK:
            logger.debug(
                f"[Funding] SHORT blocat: funding={self._funding_rate:+.5%} "
                f"< threshold={FUNDING_SHORT_BLOCK:+.4%}"
            )
            return False
        return True

    @property
    def rate(self) -> float:
        return self._funding_rate

    @property
    def ready(self) -> bool:
        return self._ready


funding = FundingRateMonitor()
