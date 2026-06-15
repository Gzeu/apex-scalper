"""Public WebSocket feed v0.9.7: orderbook (L2-50) + klines (1m).

Changelog:
  v0.9.7 — BUG FIX: record_heartbeat() si record_kline() nu erau apelate
    niciodata din feed.py -> watchdog expira la 120s de la startup garantat,
    restartand botul la nesfarsit. Indicatorii ramanaeau in warmup permanent.
    Fix: record_heartbeat() apelat in _handle_kline() la orice mesaj WS
    (inclusiv candle neclosed), record_kline() apelat la candle confirmat.
    Astfel watchdog stie ca feed-ul e viu si nu mai face restart inutil.
  v0.8.1 — BUG 7 FIX: semnatura corecta update_indicators + evaluate.
  v0.7.4 — Feed latency guard.
  v0.7.2 — bp.on_tick() cu level lists.
  v0.3.1 — Full reconnect loop cu watchdog integration.
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
FEED_STALE_S = float(os.getenv("FEED_STALE_S", "2.0"))


def _handle_orderbook(msg: dict) -> None:
    data     = msg.get("data", {})
    msg_type = msg.get("type", "")
    if not data:
        return
    try:
        # Orderbook activ = feed viu = heartbeat reset
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
    """Process confirmed (closed) 1m candles only.

    v0.9.7 FIX: record_heartbeat() la orice mesaj kline (confirmat sau nu)
      si record_kline() la candle confirmat. Fara aceste apeluri, watchdog
      nu stia ca feed-ul e viu si restartat botul la fiecare 120s.
    v0.8.1 BUG 7 FIX: semnatura corecta.
    v0.7.4: Feed staleness guard.
    """
    from .watchdog import record_heartbeat, record_kline

    # Orice mesaj kline = WS activ = heartbeat viu
    record_heartbeat()

    try:
        data = msg.get("data", [])
        if not data or not data[0].get("confirm", False):
            return

        # Candle confirmat (closed)
        record_kline()

        # Feed staleness guard
        with state.lock:
            last_tick_ts = getattr(state, "last_tick_ts", 0.0)
        tick_age = time.time() - last_tick_ts
        if tick_age > FEED_STALE_S:
            logger.warning(
                f"Feed stale: last OB tick {tick_age:.2f}s ago "
                f"(threshold={FEED_STALE_S}s) — skipping candle, entries blocked"
            )
            return

        candle = data[0]
        close  = float(candle["close"])
        high   = float(candle["high"])
        low    = float(candle["low"])
        volume = float(candle["volume"])

        with state.lock:
            state.last_price = close

        from .strategy import update_indicators, evaluate
        update_indicators(close, {"high": high, "low": low, "volume": volume})

        if _loop and _loop.is_running():
            asyncio.run_coroutine_threadsafe(evaluate(close), _loop)

    except Exception as e:
        logger.error(f"Kline handler error: {e}")


async def start_feed() -> None:
    """WS feed with automatic reconnect loop on error or watchdog trigger."""
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
                "WS subscribed — listening for confirmed candles + "
                "book pressure live (feed latency guard active: "
                f"FEED_STALE_S={FEED_STALE_S}s)"
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
