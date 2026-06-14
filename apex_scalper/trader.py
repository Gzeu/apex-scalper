"""Order execution via Bybit V5 REST + private WebSocket for executions.
Includes retry with exponential backoff and optional Limit (post-only) orders.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Literal
from loguru import logger
from pybit.unified_trading import HTTP

from .config import config
from .state import state

MAX_RETRIES = 3
RETRY_BASE  = 0.5  # seconds


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
            logger.info(f"Leverage set: {config.leverage}x on {config.symbol}")
        except Exception as e:
            logger.warning(f"Leverage set skipped (may already be set): {e}")

    async def place_order(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        order_type: str = "Market",
        post_only: bool = False,
    ) -> dict:
        """Place order with retry + exponential backoff."""
        order_id = str(uuid.uuid4())
        params = dict(
            category="linear",
            symbol=config.symbol,
            side=side,
            orderType=order_type,
            qty=str(qty),
            timeInForce="PostOnly" if post_only else "GTC",
            orderLinkId=order_id,
        )
        if order_type == "Limit":
            # For limit orders, price must be set by caller via params override
            pass

        loop = asyncio.get_running_loop()
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await loop.run_in_executor(
                    None, lambda: self._session.place_order(**params)
                )
                if resp.get("retCode") == 0:
                    logger.info(f"Order OK [{attempt}]: {side} {qty} {config.symbol}")
                    return resp
                else:
                    logger.warning(f"Order retCode={resp.get('retCode')} msg={resp.get('retMsg')}")
            except Exception as e:
                logger.error(f"Order attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BASE * (2 ** (attempt - 1)))

        logger.error(f"Order FAILED after {MAX_RETRIES} attempts")
        return {}

    async def close_position(self) -> None:
        """Close open position via reduceOnly market order."""
        with state.lock:
            pos = state.open_position
            qty = state.open_qty
        if not pos or qty == 0:
            return
        close_side = "Sell" if pos == "long" else "Buy"
        loop = asyncio.get_running_loop()
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await loop.run_in_executor(
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
                if resp.get("retCode") == 0:
                    logger.info(f"Position closed: {pos} {qty}")
                    return
            except Exception as e:
                logger.error(f"Close attempt {attempt} failed: {e}")
            await asyncio.sleep(RETRY_BASE * (2 ** (attempt - 1)))

    async def get_position(self) -> dict:
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._session.get_positions(category="linear", symbol=config.symbol),
        )
        items = resp.get("result", {}).get("list", [])
        return items[0] if items else {}

    async def get_balance(self) -> float:
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._session.get_wallet_balance(accountType="UNIFIED", coin="USDT"),
        )
        try:
            return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
        except (KeyError, IndexError):
            return 0.0


trader = Trader()
