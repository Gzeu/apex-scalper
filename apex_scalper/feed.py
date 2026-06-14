"""Public WebSocket feed: orderbook (L2-50) + klines (1m) with OHLCV.

All state mutations happen under threading.Lock.
Strategy evaluation is dispatched to the async event loop thread-safely.
"""
from __future__ import annotations

import asyncio
from loguru import logger
from pybit.unified_trading import WebSocket as BybitWS

from .config import config
from .state import state

_loop: asyncio.AbstractEventLoop | None = None


def _handle_orderbook(msg: dict) -> None:
    data     = msg.get("data", {})
    msg_type = msg.get("type", "")
    if not data:
        return
    with state.lock:
        if msg_type == "snapshot":
            state.orderbook.apply_snapshot(data.get("b", []), data.get("a", []))
        else:
            for item in data.get("b", []):
                state.orderbook.apply_delta("b", item[0], item[1])
            for item in data.get("a", []):
                state.orderbook.apply_delta("a", item[0], item[1])


def _handle_kline(msg: dict) -> None:
    """Only process confirmed (closed) candles — include OHLCV."""
    data = msg.get("data", [])
    if not data or not data[0].get("confirm", False):
        return

    candle = data[0]
    close  = float(candle["close"])
    high   = float(candle["high"])
    low    = float(candle["low"])
    volume = float(candle["volume"])

    with state.lock:
        state.last_price = close

    # Update all indicators (thread-safe, no lock needed inside indicators)
    from .strategy import update_indicators
    update_indicators(close, high, low, volume)

    # Dispatch strategy evaluation to event loop
    if _loop and _loop.is_running():
        from .strategy import strategy
        asyncio.run_coroutine_threadsafe(strategy.evaluate(), _loop)


async def start_feed() -> None:
    global _loop
    _loop = asyncio.get_running_loop()

    logger.info(f"Starting public WS feed: {config.symbol} testnet={config.testnet}")

    ws = BybitWS(channel_type="linear", testnet=config.testnet)
    ws.orderbook_stream(depth=50, symbol=config.symbol, callback=_handle_orderbook)
    ws.kline_stream(interval=1, symbol=config.symbol, callback=_handle_kline)

    logger.info("WS subscribed. Waiting for confirmed candles...")
    while True:
        await asyncio.sleep(30)
