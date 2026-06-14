"""Order execution via Bybit V5 REST.

Fixes vs v0.3.0:
- _set_leverage() was sync in __init__ -> now async setup() called from main().
- close_position() did NOT clear state -> double-close on restart fixed.
- Market + PostOnly is invalid on Bybit -> guarded.
- Limit order price was not passable -> price param added.
- sync_position_from_exchange() added: survive restarts with open positions.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Literal, Optional
from loguru import logger
from pybit.unified_trading import HTTP

from .config import config
from .state import state

MAX_RETRIES = 3
RETRY_BASE  = 0.5  # seconds


class Trader:
    def __init__(self):
        self._session: Optional[HTTP] = None

    async def setup(self) -> None:
        """Async init: create HTTP session + set leverage. Must be called from main()."""
        self._session = HTTP(
            testnet=config.testnet,
            api_key=config.api_key,
            api_secret=config.api_secret,
        )
        await self._set_leverage(config.symbol, config.leverage)

    async def _set_leverage(self, symbol: str, leverage: int) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._session.set_leverage(
                    category="linear",
                    symbol=symbol,
                    buyLeverage=str(leverage),
                    sellLeverage=str(leverage),
                ),
            )
            logger.info(f"Leverage set: {leverage}x on {symbol}")
        except Exception as e:
            logger.warning(f"Leverage set skipped (may already be set): {e}")

    async def place_order(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        order_type: str = "Market",
        post_only: bool = False,
        price: Optional[float] = None,
        symbol: Optional[str] = None,
    ) -> dict:
        """Place order with retry + exponential backoff."""
        if not self._session:
            logger.error("Trader.setup() not called yet — cannot place order")
            return {}

        # FIX: Market + PostOnly is invalid on Bybit (retCode 10004)
        if order_type == "Market" and post_only:
            logger.warning("Market order cannot be PostOnly — ignoring post_only flag")
            post_only = False

        sym = symbol or config.symbol
        params: dict = dict(
            category="linear",
            symbol=sym,
            side=side,
            orderType=order_type,
            qty=str(qty),
            timeInForce="PostOnly" if post_only else "GTC",
            orderLinkId=str(uuid.uuid4()),
        )
        if order_type == "Limit" and price is not None:
            params["price"] = str(price)

        loop = asyncio.get_running_loop()
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await loop.run_in_executor(
                    None, lambda: self._session.place_order(**params)
                )
                if resp.get("retCode") == 0:
                    logger.info(f"Order OK [{attempt}]: {side} {qty} {sym}")
                    return resp
                else:
                    logger.warning(
                        f"Order retCode={resp.get('retCode')} msg={resp.get('retMsg')}"
                    )
            except Exception as e:
                logger.error(f"Order attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BASE * (2 ** (attempt - 1)))

        logger.error(f"Order FAILED after {MAX_RETRIES} attempts: {side} {qty} {sym}")
        return {}

    async def close_position(self, symbol: Optional[str] = None) -> None:
        """Close open position via reduceOnly market order.

        FIX: clears state fields AFTER confirmed close to prevent double-close
        on reconnect/restart. Sends critical Telegram alert if all retries fail.
        """
        if not self._session:
            return

        with state.lock:
            pos = state.open_position
            qty = state.open_qty
            sym = symbol or config.symbol

        if not pos or qty == 0:
            return

        close_side = "Sell" if pos == "long" else "Buy"
        loop = asyncio.get_running_loop()
        closed = False

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda: self._session.place_order(
                        category="linear",
                        symbol=sym,
                        side=close_side,
                        orderType="Market",
                        qty=str(qty),
                        timeInForce="GTC",
                        reduceOnly=True,
                    ),
                )
                if resp.get("retCode") == 0:
                    logger.info(f"Position closed OK: {pos} {qty} {sym}")
                    closed = True
                    break
                else:
                    logger.warning(
                        f"Close retCode={resp.get('retCode')} msg={resp.get('retMsg')}"
                    )
            except Exception as e:
                logger.error(f"Close attempt {attempt} failed: {e}")
            await asyncio.sleep(RETRY_BASE * (2 ** (attempt - 1)))

        if closed:
            # FIX: clear state AFTER confirmed close (not before)
            with state.lock:
                state.open_position = None
                state.open_qty      = 0.0
                state.open_entry    = 0.0
                state.trailing_stop = 0.0
        else:
            logger.critical(
                f"FAILED to close {pos} {qty} {sym} after {MAX_RETRIES} retries "
                "— MANUAL ACTION REQUIRED"
            )
            try:
                from .telegram_ui import send_message
                await send_message(
                    f"🚨 *CRITICAL*: Failed to close `{pos}` on `{sym}`!\n"
                    f"qty=`{qty}` — *MANUAL ACTION REQUIRED*"
                )
            except Exception:
                pass

    async def sync_position_from_exchange(self, symbol: Optional[str] = None) -> None:
        """Sync local state from exchange on startup/reconnect.

        Prevents the bot from ignoring an open position left from a previous run.
        """
        sym = symbol or config.symbol
        pos = await self.get_position(sym)
        size  = float(pos.get("size", 0))
        side  = pos.get("side", "")   # 'Buy' | 'Sell' | ''
        entry = float(pos.get("avgPrice", 0))

        with state.lock:
            if size > 0 and side:
                state.open_position = "long" if side == "Buy" else "short"
                state.open_qty      = size
                state.open_entry    = entry
                logger.warning(
                    f"Synced open position from exchange: "
                    f"{state.open_position} qty={size} entry={entry} [{sym}]"
                )
            else:
                state.open_position = None
                state.open_qty      = 0.0
                state.open_entry    = 0.0
                logger.info(f"No open position on exchange [{sym}]")

    async def get_position(self, symbol: Optional[str] = None) -> dict:
        if not self._session:
            return {}
        sym = symbol or config.symbol
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._session.get_positions(category="linear", symbol=sym),
        )
        items = resp.get("result", {}).get("list", [])
        return items[0] if items else {}

    async def get_balance(self) -> float:
        if not self._session:
            return 0.0
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._session.get_wallet_balance(
                accountType="UNIFIED", coin="USDT"
            ),
        )
        try:
            return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
        except (KeyError, IndexError):
            return 0.0


trader = Trader()
