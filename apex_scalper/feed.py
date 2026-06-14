"""Public WebSocket feed v0.7.1: orderbook (L2-50) + klines (1m).

FIX v0.7.1 (book pressure wiring):
  - bp.on_tick(bid_vol, ask_vol) now called on every OB update
  - bid_vol = sum of top-10 bid sizes, ask_vol = sum of top-10 ask sizes
  - book_pressure module was DEAD (never received data) — now live

FIX v0.3.1 (kept):
  - Full reconnect loop with try/except
  - Watchdog integration
"""
from __future__ import annotations

import asyncio
from loguru import logger
from pybit.unified_trading import WebSocket as BybitWS

from .config import config
from .state import state

_loop: asyncio.AbstractEventLoop | None = None
_ws:   BybitWS | None = None

OB_DEPTH_FOR_PRESSURE = 10   # top N levels to sum for bid/ask volume


def _handle_orderbook(msg: dict) -> None:
    data     = msg.get("data", {})
    msg_type = msg.get("type", "")
    if not data:
        return
    try:
        with state.lock:
            if msg_type == "snapshot":
                state.orderbook.apply_snapshot(
                    data.get("b", []), data.get("a", [])
                )
            else:
                for item in data.get("b", []):
                    state.orderbook.apply_delta("b", item[0], item[1])
                for item in data.get("a", []):
                    state.orderbook.apply_delta("a", item[0], item[1])

        # --- Book pressure feed (v0.7.1: was missing, book_pressure was DEAD) ---
        from .book_pressure import bp
        bids = data.get("b", [])
        asks = data.get("a", [])
        if bids or asks:
            # Use snapshot or delta levels; take top N by price
            with state.lock:
                all_bids = state.orderbook.top_bids(OB_DEPTH_FOR_PRESSURE)
                all_asks = state.orderbook.top_asks(OB_DEPTH_FOR_PRESSURE)
            bid_vol = sum(float(b[1]) for b in all_bids if len(b) >= 2)
            ask_vol = sum(float(a[1]) for a in all_asks if len(a) >= 2)
            bp.on_tick(bid_vol, ask_vol)

    except Exception as e:
        logger.error(f"OB handler error: {e}")


def _handle_kline(msg: dict) -> None:
    """Process confirmed (closed) 1m candles only."""
    try:
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

        from .strategy import update_indicators
        update_indicators(close, high, low, volume)

        if _loop and _loop.is_running():
            from .strategy import strategy
            asyncio.run_coroutine_threadsafe(strategy.evaluate(), _loop)

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
            logger.info("WS subscribed — listening for confirmed candles + book pressure live")

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
