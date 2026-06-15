"""Indicator warmup v0.9.8 — pre-incarca candle-uri istorice la startup.

Problema rezolvata:
  Dupa fiecare restart, toti indicatorii (EMA50, RSI, ATR, BB, MACD, StochRSI)
  porneau de la zero si necesitau 50+ candle-uri live (~50 minute) inainte
  de a fi ready. In acest timp botul nu putea intra in nicio tranzactie.

Solutie:
  La startup, inainte de a porni WS feed-ul, se descarca ultimele
  WARMUP_CANDLES (60) candle-uri 1m via Bybit REST API (get_kline).
  Acestea sunt procesate prin update_all() in aceeasi ordine ca feed-ul live,
  astfel incat toti indicatorii sunt ready in <1 secunda de la pornire.

Fara dependente noi: pybit e deja instalat.
"""
from __future__ import annotations

import time
from loguru import logger

WARMUP_CANDLES = 60   # suficient pentru EMA50 + MACD(26) + StochRSI


async def warmup_indicators(symbol: str) -> bool:
    """Descarca ultimele WARMUP_CANDLES candle-uri 1m si populeaza indicatorii.

    Returneaza True daca warmup a reusit, False daca a esuat (non-fatal).
    Dupa apel, toti indicatorii din strategy.py sunt ready si botul poate
    intra in tranzactii imediat dupa prima candle confirmata live.
    """
    try:
        from .trader import trader
        from .strategy import update_indicators, _get_ind_state, _apply_ind_from_state, ind
        from .indicators import update_all

        logger.info(f"Indicator warmup: descarc {WARMUP_CANDLES} candle-uri 1m [{symbol}]...")
        t0 = time.monotonic()

        result = await _fetch_klines(trader, symbol, WARMUP_CANDLES)
        if not result:
            logger.warning("Indicator warmup: fetch esuat — indicatorii vor fi ready dupa ~50 candle-uri live")
            return False

        # Proceseaza candle-urile in ordine cronologica (cel mai vechi primul)
        s = _get_ind_state()
        for candle in result:
            close  = candle["close"]
            high   = candle["high"]
            low    = candle["low"]
            volume = candle["volume"]
            update_all(s, close, high, low, volume)

        # Copiaza in strategy.ind (acelasi lucru pe care il face update_indicators)
        _apply_ind_from_state(s, result[-1]["close"])

        elapsed = time.monotonic() - t0
        last_close = result[-1]["close"]
        logger.info(
            f"Indicator warmup complet in {elapsed:.2f}s | "
            f"{len(result)} candle-uri procesate | last_close={last_close:.2f} | "
            f"RSI={ind.rsi_value:.1f}({'ready' if ind.rsi_ready else 'warmup'}) "
            f"ATR={ind.atr_value:.2f}({'ready' if ind.atr_ready else 'warmup'}) "
            f"EMA9={ind.ema_fast:.2f} EMA21={ind.ema_slow:.2f} EMA50={ind.ema_trend:.2f} "
            f"MACD_hist={ind.macd_histogram:.4f}({'ready' if ind.macd_ready else 'warmup'}) "
            f"BB={'ready' if ind.bb_ready else 'warmup'} "
            f"StochRSI={'ready' if ind.stoch_ready else 'warmup'}"
        )
        return True

    except Exception as e:
        logger.warning(f"Indicator warmup error: {e} — continuam fara warmup")
        return False


async def _fetch_klines(trader, symbol: str, limit: int) -> list[dict] | None:
    """Descarca ultimele `limit` candle-uri 1m via Bybit REST.

    Returneaza lista de dict {close, high, low, volume} in ordine cronologica
    (cel mai vechi primul), sau None la eroare.
    Bybit returneaza candle-urile in ordine inversa (cel mai nou primul).
    """
    try:
        from .trader import _api_call_with_retry
        result = await _api_call_with_retry(
            trader._client.get_kline,
            category="linear",
            symbol=symbol,
            interval="1",
            limit=limit,
        )
        raw = result.get("result", {}).get("list", [])
        if not raw:
            return None

        # Bybit format: [startTime, open, high, low, close, volume, turnover]
        # raw[0] = cel mai nou, raw[-1] = cel mai vechi -> inversam
        candles = []
        for row in reversed(raw):
            candles.append({
                "close":  float(row[4]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "volume": float(row[5]),
            })
        return candles

    except Exception as e:
        logger.warning(f"_fetch_klines error: {e}")
        return None
