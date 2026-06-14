"""Public WebSocket feed: orderbook (L2-50) + klines (1m) for the symbol."""
from __future__ import annotations

import asyncio
import json
from loguru import logger
from pybit.unified_trading import WebSocket as BybitWS

from .config import config
from .state import state
from .strategy import strategy


def _handle_orderbook(msg: dict) -> None:
    data = msg.get("data", {})
    topic = msg.get("topic", "")
    msg_type = msg.get("type", "")

    if not data:
        return

    async def _update():
        async with state.lock:
            if msg_type == "snapshot":
                state.orderbook.apply_snapshot(data.get("b", []), data.get("a", []))
            else:
                for item in data.get("b", []):
                    state.orderbook.apply_delta("b", item[0], item[1])
                for item in data.get("a", []):
                    state.orderbook.apply_delta("a", item[0], item[1])

    asyncio.get_event_loop().create_task(_update())


def _handle_kline(msg: dict) -> None:
    data = msg.get("data", [])
    if not data:
        return
    close = float(data[0]["close"])

    async def _update():
        async with state.lock:
            state.last_price = close
            # EMA(9) and EMA(21) — simple streaming update
            k9 = 2 / (9 + 1)
            k21 = 2 / (21 + 1)
            if state.ema_fast == 0:
                state.ema_fast = close
                state.ema_slow = close
            else:
                state.ema_fast = close * k9 + state.ema_fast * (1 - k9)
                state.ema_slow = close * k21 + state.ema_slow * (1 - k21)
        # Feed updated price into strategy loop
        await strategy.evaluate()

    asyncio.get_event_loop().create_task(_update())


async def start_feed() -> None:
    """Connect public WebSocket and subscribe to orderbook + klines."""
    logger.info(f"Connecting public WS [{config.ws_public_url}] for {config.symbol}")
    ws = BybitWS(
        channel_type="linear",
        testnet=config.testnet,
    )
    ws.orderbook_stream(
        depth=50,
        symbol=config.symbol,
        callback=_handle_orderbook,
    )
    ws.kline_stream(
        interval=1,
        symbol=config.symbol,
        callback=_handle_kline,
    )
    # Keep alive — pybit handles reconnects internally
    while True:
        await asyncio.sleep(60)
