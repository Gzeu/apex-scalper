"""Order execution via Bybit V5 REST + private WebSocket for executions."""
from __future__ import annotations

import uuid
import asyncio
from typing import Literal
from loguru import logger
from pybit.unified_trading import HTTP, WebSocket as BybitWS

from .config import config
from .state import state


class Trader:
    def __init__(self):
        self._session = HTTP(
            testnet=config.testnet,
            api_key=config.api_key,
            api_secret=config.api_secret,
        )
        self._set_leverage()

    def _set_leverage(self) -> None:
        try:
            self._session.set_leverage(
                category="linear",
                symbol=config.symbol,
                buyLeverage=str(config.leverage),
                sellLeverage=str(config.leverage),
            )
            logger.info(f"Leverage set to {config.leverage}x for {config.symbol}")
        except Exception as e:
            logger.warning(f"Leverage set skipped: {e}")

    async def place_order(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        order_type: str = "Market",
    ) -> dict:
        """Place a market order. Returns the response dict."""
        order_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._session.place_order(
                category="linear",
                symbol=config.symbol,
                side=side,
                orderType=order_type,
                qty=str(qty),
                timeInForce="GTC",
                orderLinkId=order_id,
            ),
        )
        logger.info(f"Order placed: {side} {qty} {config.symbol} → {resp}")
        return resp

    async def close_position(self) -> None:
        """Close current open position via reduceOnly market order."""
        async with state.lock:
            pos = state.open_position
            qty = state.open_qty
        if not pos:
            return
        close_side = "Sell" if pos == "long" else "Buy"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._session.place_order(
                category="linear",
                symbol=config.symbol,
                side=close_side,
                orderType="Market",
                qty=str(qty),
                timeInForce="GTC",
                reduceOnly=True,
            ),
        )
        logger.info(f"Position closed: {pos} {qty} {config.symbol}")

    async def get_position(self) -> dict:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._session.get_positions(
                category="linear",
                symbol=config.symbol,
            ),
        )
        items = resp.get("result", {}).get("list", [])
        return items[0] if items else {}

    async def get_balance(self) -> float:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._session.get_wallet_balance(
                accountType="UNIFIED",
                coin="USDT",
            ),
        )
        try:
            return float(
                resp["result"]["list"][0]["coin"][0]["walletBalance"]
            )
        except (KeyError, IndexError):
            return 0.0


trader = Trader()
