"""Multi-TimeFrame (MTF) trend filter v0.4.1.

Fix vs v0.4.0:
- _ready is initialized to False and the background loop is separate
- main.py calls await mtf.refresh() SYNCHRONOUSLY before state.running=True
  so the first candle is never processed without MTF confirmation
- If refresh fails at startup, MTF is still not ready (_ready=False)
  and allow_long/allow_short return False (block entries) until ready.
  This is safer than the previous True (pass-through) on not ready.

Note: _ready=False blocks entries until first successful MTF fetch.
This means the bot waits up to MTF_REFRESH_S before first entry, which
is acceptable (avoids unfiltered entries at startup).
"""
from __future__ import annotations

import asyncio
from typing import Optional
from loguru import logger

from .config import config
from .trader import trader

MTF_INTERVAL    = "15"
MTF_EMA_PERIOD  = 50
MTF_REFRESH_S   = 60
MTF_LOOKBACK    = 100


class MTFFilter:
    def __init__(self):
        self._ema50: float = 0.0
        self._ready: bool = False      # False = block entries (safe default)
        self._lock = asyncio.Lock()

    async def refresh(self, symbol: Optional[str] = None) -> None:
        """Fetch 15m candles and compute EMA50. Sets _ready=True on success."""
        sym = symbol or config.symbol
        if not trader._session:
            logger.warning("MTF: trader session not ready")
            return
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: trader._session.get_kline(
                    category="linear",
                    symbol=sym,
                    interval=MTF_INTERVAL,
                    limit=MTF_LOOKBACK,
                ),
            )
            rows = resp.get("result", {}).get("list", [])
            if not rows:
                logger.warning("MTF: no klines returned")
                return

            closes = [float(r[4]) for r in reversed(rows)]
            ema = closes[0]
            k = 2.0 / (MTF_EMA_PERIOD + 1)
            for c in closes[1:]:
                ema = c * k + ema * (1 - k)

            self._ema50  = ema
            self._ready  = True
            logger.info(
                f"MTF EMA50(15m) [{sym}]: {ema:.4f} | "
                f"price_ref={closes[-1]:.4f} "
                f"bias={'BULL' if closes[-1] > ema else 'BEAR'}"
            )
        except Exception as e:
            logger.warning(f"MTF refresh error: {e}")
            # _ready stays False — entries blocked until next successful fetch

    def allow_long(self, price: float) -> bool:
        """Block LONG if MTF not ready or price below 15m EMA50."""
        if not self._ready:
            return False   # safer: block until we have MTF data
        return price > self._ema50

    def allow_short(self, price: float) -> bool:
        """Block SHORT if MTF not ready or price above 15m EMA50."""
        if not self._ready:
            return False
        return price < self._ema50

    @property
    def ema50(self) -> float:
        return self._ema50

    @property
    def ready(self) -> bool:
        return self._ready


mtf = MTFFilter()


async def run_mtf_refresh_loop(symbol: Optional[str] = None) -> None:
    """Background task: refresh MTF EMA every MTF_REFRESH_S seconds."""
    while True:
        try:
            await mtf.refresh(symbol)
        except Exception as e:
            logger.warning(f"MTF refresh loop error: {e}")
        await asyncio.sleep(MTF_REFRESH_S)
