"""Public WebSocket feed: orderbook (L2-50) + klines (1m).

Design:
  - pybit callbacks are called from a background thread.
  - We update state under threading.Lock (no asyncio.Lock in callbacks).
  - After updating, we schedule strategy.evaluate() on the running event loop
    via loop.call_soon_threadsafe() to keep all async code on one thread.
"""
from __future__ import annotations

import asyncio
from loguru import logger
from pybit.unified_trading import WebSocket as BybitWS

from .config import config
from .state import state

# Will be set in start_feed() to the running event loop
_loop: asyncio.AbstractEventLoop | None = None


def _handle_orderbook(msg: dict) -> None:
    """Called from pybit thread. Update local book under threading.Lock."""
    data = msg.get("data", {})
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
    """Called from pybit thread. Update EMA + RSI, then trigger strategy."""
    data = msg.get("data", [])
    if not data or not data[0].get("confirm", False):
        # Only process confirmed (closed) candles
        return

    close = float(data[0]["close"])

    with state.lock:
        _update_indicators(close)

    # Schedule async strategy evaluation on the event loop (thread-safe)
    if _loop and _loop.is_running():
        from .strategy import strategy
        asyncio.run_coroutine_threadsafe(strategy.evaluate(), _loop)


def _update_indicators(close: float) -> None:
    """Update EMA(9), EMA(21), RSI(14). Called under state.lock."""
    # ---- EMA ----
    k9  = 2 / (9  + 1)
    k21 = 2 / (21 + 1)
    if state.ema_fast == 0:
        state.ema_fast = close
        state.ema_slow = close
    else:
        state.ema_fast = close * k9  + state.ema_fast * (1 - k9)
        state.ema_slow = close * k21 + state.ema_slow * (1 - k21)
    state.last_price = close

    # ---- RSI(14) Wilder smoothing ----
    RSI_PERIOD = 14
    if state.rsi_prev_price == 0:
        state.rsi_prev_price = close
        return

    change = close - state.rsi_prev_price
    state.rsi_prev_price = close
    gain = max(change, 0.0)
    loss = max(-change, 0.0)

    state.rsi_count += 1
    if state.rsi_count <= RSI_PERIOD:
        state.rsi_gains.append(gain)
        state.rsi_losses.append(loss)
        if state.rsi_count == RSI_PERIOD:
            state.rsi_avg_gain = sum(state.rsi_gains) / RSI_PERIOD
            state.rsi_avg_loss = sum(state.rsi_losses) / RSI_PERIOD
            state.rsi_ready = True
    else:
        state.rsi_avg_gain = (state.rsi_avg_gain * (RSI_PERIOD - 1) + gain) / RSI_PERIOD
        state.rsi_avg_loss = (state.rsi_avg_loss * (RSI_PERIOD - 1) + loss) / RSI_PERIOD

    if state.rsi_ready:
        if state.rsi_avg_loss == 0:
            state.rsi_value = 100.0
        else:
            rs = state.rsi_avg_gain / state.rsi_avg_loss
            state.rsi_value = 100.0 - (100.0 / (1 + rs))


async def start_feed() -> None:
    """Connect public WebSocket. Sets module-level loop ref for thread-safe dispatch."""
    global _loop
    _loop = asyncio.get_running_loop()

    logger.info(f"Connecting public WS for {config.symbol} (testnet={config.testnet})")

    ws = BybitWS(channel_type="linear", testnet=config.testnet)
    ws.orderbook_stream(depth=50, symbol=config.symbol, callback=_handle_orderbook)
    ws.kline_stream(interval=1, symbol=config.symbol, callback=_handle_kline)

    logger.info("WS feed active. Waiting for confirmed candles...")
    while True:
        await asyncio.sleep(30)
