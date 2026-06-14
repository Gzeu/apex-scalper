"""Order execution via Bybit V5 REST — v0.5.0 mainnet-ready.

Key improvements over v0.4.1:
  1. amend_order()          — /v5/order/amend: modify price/qty without cancel+repost
                               Saves 1 round-trip + avoids losing queue position
  2. attach_sl_tp()         — native Bybit SL/TP on entry order (stopLoss/takeProfit)
                               Exchange-side stops = survive connectivity loss
  3. close_position()       — tries Limit reduceOnly first, Market only as last resort
                               On mainnet: saves 0.055% taker fee on close
  4. get_instrument_info()  — fetch qtyStep, tickSize, minQty per symbol
  5. fee_estimate()         — returns exact fee cost for an order before placing
  6. set_position_mode()    — enforce OneWay (hedge=off) at startup
  7. amend_sl_tp()          — modify SL/TP on existing position (trailing via REST)
  8. All Market orders in place_order() emit fee warning

Bybit USDT Perp fee schedule (2026, non-VIP):
  Maker (PostOnly Limit): +0.020%   <- we pay this
  Taker (Market):          0.055%   <- we AVOID this
  Delta per trade:         0.035%   <- saved per entry + per exit with limits
  On 10 trades/day x $200 notional: saves $0.14/day = $51/year per $200
  On mainnet $1000 notional / trade: saves $700/year vs pure market

VIP rebate (if volume qualifies):
  VIP1+: maker fee = 0.000% -> 0% on entry+exit = pure edge
  VIP4+: maker REBATE = -0.015% -> exchange PAYS YOU per fill
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
RETRY_BASE  = 0.5

# Cached per-symbol instrument info (populated at startup)
_instrument_cache: dict[str, dict] = {}


class Trader:
    def __init__(self):
        self._session: Optional[HTTP] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Setup
    # ─────────────────────────────────────────────────────────────────────────

    async def setup(self) -> None:
        """Async init: create session, set leverage, enforce OneWay mode."""
        self._session = HTTP(
            testnet=config.testnet,
            api_key=config.api_key,
            api_secret=config.api_secret,
        )
        # Enforce OneWay (non-hedge) mode — required for reduceOnly to work correctly
        await self.set_position_mode(config.symbol)
        await self._set_leverage(config.symbol, config.leverage)
        # Pre-fetch instrument info (tickSize, qtyStep, minQty)
        await self.get_instrument_info(config.symbol)
        self._log_fee_schedule()

    def _log_fee_schedule(self) -> None:
        sym = config.symbol
        info = _instrument_cache.get(sym, {})
        tick = info.get("tickSize", "?")
        step = info.get("qtyStep", "?")
        minq = info.get("minQty", "?")
        logger.info(
            f"Bybit fee schedule [{sym}]: "
            f"Maker=+0.020% | Taker=0.055% | Delta=0.035%/trade\n"
            f"  tickSize={tick} | qtyStep={step} | minQty={minq}\n"
            f"  Strategy: PostOnly Limit entry + Limit reduceOnly exit "
            f"-> target 0% taker fee\n"
            f"  VIP bonus: VIP1=0.00% maker | VIP4=-0.015% rebate"
        )

    async def set_position_mode(self, symbol: str) -> None:
        """Enforce OneWay mode (hedge=off). Required for reduceOnly orders."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._session.switch_position_mode(
                    category="linear",
                    symbol=symbol,
                    mode=0,   # 0 = OneWay (MergedSingle), 3 = Hedge
                ),
            )
            logger.info(f"Position mode: OneWay (mode=0) [{symbol}]")
        except Exception as e:
            # Often raises if already in OneWay mode (retCode 110025)
            logger.debug(f"set_position_mode: {e} (may already be OneWay)")

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
            logger.info(f"Leverage set: {leverage}x [{symbol}]")
        except Exception as e:
            logger.debug(f"Leverage set skipped (may already be set): {e}")

    async def get_instrument_info(self, symbol: str) -> dict:
        """Fetch tickSize, qtyStep, minQty from Bybit. Cached after first call."""
        if symbol in _instrument_cache:
            return _instrument_cache[symbol]
        if not self._session:
            return {}
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: self._session.get_instruments_info(
                    category="linear", symbol=symbol
                ),
            )
            items = resp.get("result", {}).get("list", [])
            if items:
                lot  = items[0].get("lotSizeFilter", {})
                price_f = items[0].get("priceFilter", {})
                info = {
                    "minQty":   float(lot.get("minOrderQty", 0.001)),
                    "qtyStep":  float(lot.get("qtyStep", 0.001)),
                    "tickSize": float(price_f.get("tickSize", 0.01)),
                    "minPrice": float(price_f.get("minPrice", 0)),
                }
                _instrument_cache[symbol] = info
                logger.info(
                    f"Instrument [{symbol}]: minQty={info['minQty']} "
                    f"qtyStep={info['qtyStep']} tickSize={info['tickSize']}"
                )
                return info
        except Exception as e:
            logger.warning(f"get_instrument_info error: {e}")
        return {}

    def round_qty(self, qty: float, symbol: str) -> float:
        """Round qty to symbol's qtyStep."""
        info = _instrument_cache.get(symbol, {})
        step = info.get("qtyStep", 0.001)
        if step <= 0:
            return qty
        import math
        return math.floor(qty / step) * step

    def round_price(self, price: float, symbol: str) -> float:
        """Round price to symbol's tickSize."""
        info = _instrument_cache.get(symbol, {})
        tick = info.get("tickSize", 0.01)
        if tick <= 0:
            return price
        import math
        return round(math.floor(price / tick) * tick, 10)

    def fee_estimate(self, notional_usdt: float, order_type: str = "Limit") -> dict:
        """Return fee cost and type for a given order."""
        if order_type == "Limit":
            fee_pct = 0.00020   # 0.020% maker
            label   = "Maker (PostOnly)"
        else:
            fee_pct = 0.00055   # 0.055% taker
            label   = "Taker (Market)"
        fee_usdt = notional_usdt * fee_pct
        return {
            "type":      label,
            "fee_pct":   fee_pct,
            "fee_usdt":  round(fee_usdt, 6),
            "saved_vs_market": round(notional_usdt * (0.00055 - fee_pct), 6),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Order placement
    # ─────────────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        side: Literal["Buy", "Sell"],
        qty: float,
        order_type: str = "Limit",    # Default changed to Limit in v0.5.0
        post_only: bool = True,        # Default PostOnly
        price: Optional[float] = None,
        symbol: Optional[str] = None,
        reduce_only: bool = False,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        sl_trigger_by: str = "MarkPrice",
        tp_trigger_by: str = "MarkPrice",
    ) -> dict:
        """Place order. Defaults to Limit PostOnly (maker fee).

        Market orders emit a fee warning and should only be used as fallback.
        Native SL/TP attached at entry via stopLoss/takeProfit params.
        """
        if not self._session:
            logger.error("Trader.setup() not called")
            return {}

        # Guard: Market + PostOnly is invalid on Bybit
        if order_type == "Market" and post_only:
            logger.debug("Market order cannot be PostOnly — clearing flag")
            post_only = False

        # Fee warning on Market
        if order_type == "Market":
            sym_for_fee = symbol or config.symbol
            with state.lock:
                price_ref = state.last_price
            notional = qty * price_ref if price_ref > 0 else 0
            fee = self.fee_estimate(notional, "Market")
            logger.warning(
                f"💸 MARKET ORDER — taker fee={fee['fee_pct']*100:.3f}% "
                f"cost={fee['fee_usdt']:.4f} USDT "
                f"(vs Limit: +{fee['saved_vs_market']:.4f} USDT wasted)"
            )

        sym = symbol or config.symbol
        qty = self.round_qty(qty, sym)
        if qty <= 0:
            logger.error(f"Qty rounded to 0 for {sym} — order skipped")
            return {}

        params: dict = dict(
            category="linear",
            symbol=sym,
            side=side,
            orderType=order_type,
            qty=str(qty),
            timeInForce="PostOnly" if post_only else ("IOC" if order_type == "Market" else "GTC"),
            orderLinkId=str(uuid.uuid4()),
        )

        if order_type == "Limit" and price is not None:
            params["price"] = str(self.round_price(price, sym))
        if reduce_only:
            params["reduceOnly"] = True
        if stop_loss is not None:
            params["stopLoss"]    = str(self.round_price(stop_loss, sym))
            params["slTriggerBy"] = sl_trigger_by
        if take_profit is not None:
            params["takeProfit"]  = str(self.round_price(take_profit, sym))
            params["tpTriggerBy"] = tp_trigger_by

        loop = asyncio.get_running_loop()
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await loop.run_in_executor(
                    None, lambda: self._session.place_order(**params)
                )
                if resp.get("retCode") == 0:
                    otype_label = f"{order_type}{'[PostOnly]' if post_only else ''}"
                    fee_type    = "Maker" if post_only else "Taker"
                    logger.info(
                        f"Order OK [{attempt}]: {side} {qty} {sym} "
                        f"type={otype_label} fee={fee_type}"
                        + (f" @ {params.get('price', '?')}" if order_type == "Limit" else "")
                        + (f" SL={stop_loss}" if stop_loss else "")
                        + (f" TP={take_profit}" if take_profit else "")
                    )
                    return resp
                else:
                    code = resp.get("retCode")
                    msg  = resp.get("retMsg", "")
                    logger.warning(f"Order retCode={code} msg={msg} [{attempt}/{MAX_RETRIES}]")
                    # PostOnly rejected (price crossed market) — don't retry with same price
                    if code == 10004 or "PostOnly" in msg:
                        logger.warning("PostOnly rejected — price stale, skip retry")
                        return resp
            except Exception as e:
                logger.error(f"Order attempt {attempt}/{MAX_RETRIES} exception: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BASE * (2 ** (attempt - 1)))

        logger.error(f"Order FAILED after {MAX_RETRIES} attempts: {side} {qty} {sym}")
        return {}

    async def amend_order(
        self,
        order_id: str,
        symbol: Optional[str] = None,
        price: Optional[float] = None,
        qty: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        """Amend existing order via /v5/order/amend.

        Faster than cancel+repost: preserves queue position, single round-trip.
        Works on unfilled and partially-filled orders.
        """
        if not self._session:
            return {}
        sym = symbol or config.symbol
        params: dict = dict(category="linear", symbol=sym, orderId=order_id)
        if price is not None:
            params["price"] = str(self.round_price(price, sym))
        if qty is not None:
            params["qty"] = str(self.round_qty(qty, sym))
        if stop_loss is not None:
            params["stopLoss"] = str(self.round_price(stop_loss, sym))
        if take_profit is not None:
            params["takeProfit"] = str(self.round_price(take_profit, sym))

        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None, lambda: self._session.amend_order(**params)
            )
            if resp.get("retCode") == 0:
                logger.debug(f"amend_order OK: {order_id} {params}")
            else:
                logger.warning(
                    f"amend_order failed: retCode={resp.get('retCode')} "
                    f"msg={resp.get('retMsg')}"
                )
            return resp
        except Exception as e:
            logger.warning(f"amend_order exception: {e}")
            return {}

    async def amend_sl_tp(
        self,
        symbol: Optional[str] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        position_idx: int = 0,   # 0 = OneWay mode
    ) -> dict:
        """Amend SL/TP on existing open position (not on a pending order).

        Uses /v5/position/set-tpsl endpoint.
        Ideal for trailing stop updates without closing/reopening the position.
        """
        if not self._session:
            return {}
        sym = symbol or config.symbol
        params: dict = dict(
            category="linear",
            symbol=sym,
            positionIdx=position_idx,
            tpslMode="Full",
        )
        if stop_loss is not None:
            params["stopLoss"]    = str(self.round_price(stop_loss, sym))
            params["slTriggerBy"] = "MarkPrice"
        if take_profit is not None:
            params["takeProfit"]  = str(self.round_price(take_profit, sym))
            params["tpTriggerBy"] = "MarkPrice"

        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None, lambda: self._session.set_trading_stop(**params)
            )
            if resp.get("retCode") == 0:
                logger.debug(f"amend_sl_tp OK [{sym}]: SL={stop_loss} TP={take_profit}")
            else:
                logger.warning(
                    f"amend_sl_tp failed: {resp.get('retCode')} {resp.get('retMsg')}"
                )
            return resp
        except Exception as e:
            logger.warning(f"amend_sl_tp exception: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────────
    # Position management
    # ─────────────────────────────────────────────────────────────────────────

    async def close_position(
        self,
        symbol: Optional[str] = None,
        use_limit: bool = True,
        limit_timeout_s: float = 3.0,
    ) -> None:
        """Close open position.

        v0.5.0: tries Limit reduceOnly first (0.020% maker fee),
        falls back to Market (0.055% taker) only after limit_timeout_s.
        Saves 0.035% per close on mainnet.

        For emergency close (e.g. shutdown signal), set use_limit=False.
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
        loop       = asyncio.get_running_loop()
        closed     = False

        # ── ATTEMPT 1: Limit reduceOnly (maker fee = 0.020%) ──
        if use_limit:
            with state.lock:
                best_bid = state.orderbook.best_bid
                best_ask = state.orderbook.best_ask

            # Close LONG: sell at best_ask (join ask side = maker)
            # Close SHORT: buy at best_bid (join bid side = maker)
            limit_px = best_ask if pos == "long" else best_bid
            if limit_px:
                limit_px = self.round_price(limit_px, sym)
                resp = await self.place_order(
                    side=close_side,
                    qty=qty,
                    order_type="Limit",
                    post_only=True,
                    price=limit_px,
                    symbol=sym,
                    reduce_only=True,
                )
                if resp.get("retCode") == 0:
                    order_id = resp.get("result", {}).get("orderId", "")
                    # Wait for fill
                    import time
                    start = time.monotonic()
                    while time.monotonic() - start < limit_timeout_s:
                        await asyncio.sleep(0.3)
                        try:
                            r = await loop.run_in_executor(
                                None,
                                lambda: self._session.get_order_history(
                                    category="linear", symbol=sym, orderId=order_id
                                ),
                            )
                            items = r.get("result", {}).get("list", [])
                            if items:
                                st = items[0].get("orderStatus", "")
                                if st == "Filled":
                                    logger.info(
                                        f"Position closed via Limit (maker fee 0.020%) "
                                        f"[{pos} {qty} {sym}]"
                                    )
                                    closed = True
                                    break
                        except Exception:
                            pass

                    if not closed:
                        # Cancel the unfilled limit order before market fallback
                        try:
                            await loop.run_in_executor(
                                None,
                                lambda: self._session.cancel_order(
                                    category="linear", symbol=sym, orderId=order_id
                                ),
                            )
                        except Exception:
                            pass

        # ── ATTEMPT 2: Market reduceOnly (taker fee = 0.055%) ──
        if not closed:
            if use_limit:
                logger.warning(
                    f"Limit close not filled in {limit_timeout_s}s — "
                    f"falling back to Market (taker fee 0.055%)"
                )
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
                            timeInForce="IOC",
                            reduceOnly=True,
                        ),
                    )
                    if resp.get("retCode") == 0:
                        logger.info(
                            f"Position closed via Market [attempt {attempt}] "
                            f"(taker fee 0.055%) [{pos} {qty} {sym}]"
                        )
                        closed = True
                        break
                    else:
                        logger.warning(
                            f"Market close retCode={resp.get('retCode')} "
                            f"msg={resp.get('retMsg')}"
                        )
                except Exception as e:
                    logger.error(f"Market close attempt {attempt} failed: {e}")
                await asyncio.sleep(RETRY_BASE * (2 ** (attempt - 1)))

        if closed:
            with state.lock:
                state.open_position = None
                state.open_qty      = 0.0
                state.open_entry    = 0.0
                state.trailing_stop = 0.0
        else:
            logger.critical(
                f"🚨 FAILED to close {pos} {qty} {sym} after all attempts — MANUAL ACTION REQUIRED"
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
        sym = symbol or config.symbol
        pos = await self.get_position(sym)
        size  = float(pos.get("size", 0))
        side  = pos.get("side", "")
        entry = float(pos.get("avgPrice", 0))

        with state.lock:
            if size > 0 and side:
                state.open_position = "long" if side == "Buy" else "short"
                state.open_qty      = size
                state.open_entry    = entry
                logger.warning(
                    f"Synced position from exchange: "
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
