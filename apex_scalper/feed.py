"""Public WebSocket feed v1.0.5 — update_indicators async (await).

Changelog:
  v1.0.5 —
    FIX: update_indicators() e acum async in strategy v1.1.5.
      Feed apela versiunea sync care folosea run_coroutine_threadsafe
      -> deadlock pe acelasi event loop -> timeout 1s/candle -> 0 trade-uri.
      Fix: 'await update_indicators(...)' direct.
  v1.0.4 — websockets nativ async, OB + kline multiplexat.
"""
from __future__ import annotations

import asyncio
import json
import time
import os
from loguru import logger

from .config import config
from .state import state

OB_DEPTH_FOR_PRESSURE = 10
FEED_STALE_S          = float(os.getenv("FEED_STALE_S", "30.0"))

_WS_PUBLIC_MAINNET = "wss://stream.bybit.com/v5/public/linear"
_WS_PUBLIC_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"


def _ws_url() -> str:
    return _WS_PUBLIC_TESTNET if config.testnet else _WS_PUBLIC_MAINNET


# ---------------------------------------------------------------------------
# OB handler
# ---------------------------------------------------------------------------

def _handle_orderbook(data: dict, msg_type: str) -> None:
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


# ---------------------------------------------------------------------------
# Kline handler
# ---------------------------------------------------------------------------

async def _handle_kline(items: list) -> None:
    from .watchdog import record_heartbeat, record_kline
    record_heartbeat()

    try:
        if not items:
            return

        candle  = items[0]
        close   = float(candle["close"])
        confirm = candle.get("confirm", False)

        with state.lock:
            state.last_price   = close
            state.last_tick_ts = time.time()

        if not confirm:
            # Tick live — evaluate fara update indicatori
            from .strategy import ind, evaluate
            if ind.rsi_ready and ind.atr_ready:
                await evaluate(close)
            return

        # Candle closed — FIX v1.0.5: await update_indicators (e acum async)
        record_kline()
        high   = float(candle["high"])
        low    = float(candle["low"])
        volume = float(candle["volume"])

        from .strategy import update_indicators, evaluate
        await update_indicators(close, {"high": high, "low": low, "volume": volume})
        await evaluate(close)

    except Exception as e:
        logger.error(f"Kline handler error: {e}")


# ---------------------------------------------------------------------------
# Main feed loop
# ---------------------------------------------------------------------------

async def start_feed() -> None:
    import websockets

    url  = _ws_url()
    sym  = config.symbol
    subs = [
        f"orderbook.50.{sym}",
        f"kline.1.{sym}",
    ]

    logger.info(f"Starting native async WS feed: {sym} url={url}")

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=30,
                close_timeout=10,
            ) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": subs}))
                logger.info(
                    f"WS connected + subscribed: {subs} | "
                    f"FEED_STALE_S={FEED_STALE_S}s | evaluate pe tick live"
                )

                async for raw in ws:
                    from .watchdog import feed_restart_needed
                    if feed_restart_needed():
                        logger.warning("Watchdog: feed restart requested")
                        await ws.close()
                        break

                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    if msg.get("op") == "subscribe":
                        if msg.get("success"):
                            logger.info(f"WS subscribe confirmed: {msg.get('ret_msg', '')}")
                        else:
                            logger.error(f"WS subscribe FAILED: {msg}")
                        continue

                    topic = msg.get("topic", "")
                    data  = msg.get("data")
                    mtype = msg.get("type", "delta")

                    if not topic or data is None:
                        continue

                    if topic.startswith("orderbook"):
                        _handle_orderbook(data, mtype)

                    elif topic.startswith("kline"):
                        await _handle_kline(data if isinstance(data, list) else [data])

        except Exception as e:
            logger.error(f"WS feed error: {e} — reconnect in 3s")
            await asyncio.sleep(3)
