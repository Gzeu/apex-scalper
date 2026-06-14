"""Public WebSocket feed v0.7.2: orderbook (L2-50) + klines (1m).

Changelog:
  v0.7.2 — GAP #1 fix: bp.on_tick() now passes level lists [(price,size)]
             instead of scalar totals. Activates book_pressure Check B
             (deep wall spoof detection). Backward-compat scalar path
             in book_pressure is no longer used.
  v0.7.1 — bp.on_tick() wired up (was DEAD — never received data)
  v0.3.1 — Full reconnect loop with watchdog integration
"""
from __future__ import annotations

import asyncio
from loguru import logger
from pybit.unified_trading import WebSocket as BybitWS

from .config import config
from .state import state

_loop: asyncio.AbstractEventLoop | None = None
_ws:   BybitWS | None = None

OB_DEPTH_FOR_PRESSURE = 10   # top N levels passed to book_pressure


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

        # GAP #1 FIX v0.7.2: pass level lists, not scalar totals
        # Enables book_pressure Check B (deep wall / spoof detection)
        from .book_pressure import bp
        bids = data.get("b", [])
        asks = data.get("a", [])
        if bids or asks:
            with state.lock:
                all_bids = state.orderbook.top_bids(OB_DEPTH_FOR_PRESSURE)
                all_asks = state.orderbook.top_asks(OB_DEPTH_FOR_PRESSURE)
            # Convert SortedDict entries to list[(price, size)] for bp
            bid_levels = [(float(p), float(s)) for p, s in all_bids]
            ask_levels = [(float(p), float(s)) for p, s in all_asks]
            bp.on_tick(bid_levels, ask_levels)

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
            logger.info(
                "WS subscribed — listening for confirmed candles + "
                "book pressure live (level-granular absorption active)"
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
