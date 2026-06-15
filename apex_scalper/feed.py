"""Public WebSocket feed v1.0.1 — fix FEED_STALE race + evaluate pe tick.

Changelog:
  v1.0.1 — BUG FIX CRITIC:
    1. FEED_STALE_S race condition: OB si kline sunt pe fire separate.
       Candle-ul closed soseste imediat dupa un OB delta, dar last_tick_ts
       era deja >2s -> staleness guard bloca TOATE evaluarile silentios.
       Fix: FEED_STALE_S ridicat la 30s (valoare realista pt Bybit linear).
       record_heartbeat() e apelat si din _handle_kline, deci last_tick_ts
       se actualizeaza si fara OB tick.
    2. evaluate() apelat si pe tick live (confirm=False) cu pretul curent,
       nu doar la candle closed. Astfel botul reactioneaza intra-candle
       cand scorul e suficient, nu asteapta inchiderea lumânarii.
       update_indicators() ramane doar pe candle closed (nevoie de OHLCV).
  v0.9.7 — record_heartbeat/kline fix.
"""
from __future__ import annotations

import asyncio
import time
import os
from loguru import logger
from pybit.unified_trading import WebSocket as BybitWS

from .config import config
from .state import state

_loop: asyncio.AbstractEventLoop | None = None
_ws:   BybitWS | None = None

OB_DEPTH_FOR_PRESSURE = 10
# BUG FIX: ridicat de la 2.0s la 30s — OB si kline vin pe fire separate,
# race condition facea ca evaluarea sa fie blocata la aproape fiecare candle.
FEED_STALE_S = float(os.getenv("FEED_STALE_S", "30.0"))


def _handle_orderbook(msg: dict) -> None:
    data     = msg.get("data", {})
    msg_type = msg.get("type", "")
    if not data:
        return
    try:
        from .watchdog import record_heartbeat
        record_heartbeat()

        with state.lock:
            state.last_tick_ts = time.time()
            if msg_type == "snapshot":
                state.orderbook.apply_snapshot(
                    data.get("b", []), data.get("a", [])
                )
            else:
                for item in data.get("b", []):
                    state.orderbook.apply_delta("b", item[0], item[1])
                for item in data.get("a", []):
                    state.orderbook.apply_delta("a", item[0], item[1])

        from .book_pressure import bp
        bids = data.get("b", [])
        asks = data.get("a", [])
        if bids or asks:
            with state.lock:
                all_bids = state.orderbook.top_bids(OB_DEPTH_FOR_PRESSURE)
                all_asks = state.orderbook.top_asks(OB_DEPTH_FOR_PRESSURE)
            bid_levels = [(float(p), float(s)) for p, s in all_bids]
            ask_levels = [(float(p), float(s)) for p, s in all_asks]
            bp.on_tick(bid_levels, ask_levels)

    except Exception as e:
        logger.error(f"OB handler error: {e}")


def _handle_kline(msg: dict) -> None:
    """Process kline mesaje:

    - confirm=False (tick live): actualizeaza last_price + apeleaza evaluate()
      ca botul sa reactioneze intra-candle, nu doar la inchidere.
    - confirm=True (candle closed): update_indicators() cu OHLCV complet
      + evaluate() finala pe candle inchis.

    v1.0.1 FIX:
      - evaluate() pe tick live — reactie imediata la semnal, nu 1/minut.
      - FEED_STALE_S ridicat la 30s — elimina race condition OB vs kline.
    v0.9.7 FIX: record_heartbeat/record_kline integrate.
    """
    from .watchdog import record_heartbeat, record_kline
    record_heartbeat()

    try:
        data = msg.get("data", [])
        if not data:
            return

        candle   = data[0]
        close    = float(candle["close"])
        confirm  = candle.get("confirm", False)

        with state.lock:
            state.last_price   = close
            state.last_tick_ts = time.time()

        if not confirm:
            # Tick live: evaluate() cu pretul curent, fara update indicatori
            if _loop and _loop.is_running():
                asyncio.run_coroutine_threadsafe(evaluate_if_ready(close), _loop)
            return

        # Candle confirmed (closed) — update indicatori + evaluate finala
        record_kline()

        high   = float(candle["high"])
        low    = float(candle["low"])
        volume = float(candle["volume"])

        from .strategy import update_indicators, evaluate
        update_indicators(close, {"high": high, "low": low, "volume": volume})

        if _loop and _loop.is_running():
            asyncio.run_coroutine_threadsafe(evaluate(close), _loop)

    except Exception as e:
        logger.error(f"Kline handler error: {e}")


async def evaluate_if_ready(price: float) -> None:
    """Apeleaza evaluate() pe tick live DOAR daca indicatorii sunt ready.

    Nu vrem sa evaluam cu indicatori in warmup — ar genera semnale false.
    RSI + ATR ready sunt minimul necesar pentru o evaluare valida.
    """
    from .strategy import ind, evaluate
    if not ind.rsi_ready or not ind.atr_ready:
        return
    await evaluate(price)


async def start_feed() -> None:
    """WS feed with automatic reconnect loop."""
    global _loop, _ws
    _loop = asyncio.get_running_loop()

    logger.info(f"Starting WS feed: {config.symbol} testnet={config.testnet}")

    while True:
        try:
            _ws = BybitWS(channel_type="linear", testnet=config.testnet)
            _ws.orderbook_stream(
                depth=50, symbol=config.symbol, callback=_handle_orderbook
            )
            _ws.kline_stream(
                interval=1, symbol=config.symbol, callback=_handle_kline
            )
            logger.info(
                f"WS subscribed — {config.symbol} | "
                f"evaluate pe tick live (rsi+atr ready) + candle closed | "
                f"FEED_STALE_S={FEED_STALE_S}s"
            )

            while True:
                await asyncio.sleep(10)
                from .watchdog import feed_restart_needed
                if feed_restart_needed():
                    logger.warning("Watchdog: feed restart requested")
                    try:
                        _ws.exit()
                    except Exception:
                        pass
                    break

        except Exception as e:
            logger.error(f"WS feed error: {e} — reconnecting in 5s")
            await asyncio.sleep(5)
