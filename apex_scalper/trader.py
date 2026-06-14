"""Trader module v0.7.1 — rate limiter + SL-triggered-while-offline detection.

Upgrades vs v0.5.0:
  1. RateLimiter: token bucket 10 req/s, burst=3
     All place_order / amend_order / close_position go through _rate_limit()
     429 response: auto-retry after Retry-After delay
     Exponential backoff: 0.1s -> 0.2s -> 0.4s -> 0.8s (max 4 attempts)

  2. sync_position_from_exchange():
     If exchange shows NO open position BUT state has open_position set:
     -> SL was triggered while bot was offline
     -> Reconstructs PnL from entry price vs current market price
     -> Calls risk.on_close() + db.record_trade() + Telegram alert
     -> Clears state.open_position cleanly
"""
from __future__ import annotations

import asyncio
import time
import threading
from loguru import logger
from pybit.unified_trading import HTTP

from .config import config
from .state import state


class RateLimiter:
    """Token bucket: 10 tokens/s, burst=3. Thread-safe."""

    RATE    = 10.0   # tokens per second (Bybit REST limit)
    BURST   = 3      # max burst
    BACKOFF = [0.1, 0.2, 0.4, 0.8]   # seconds, exponential

    def __init__(self):
        self._tokens   = float(self.BURST)
        self._last_ref = time.monotonic()
        self._lock     = threading.Lock()

    async def acquire(self) -> None:
        """Block (async) until a token is available."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_ref
                self._tokens = min(self.BURST, self._tokens + elapsed * self.RATE)
                self._last_ref = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.RATE
            await asyncio.sleep(wait)


_limiter = RateLimiter()


async def _api_call_with_retry(fn, *args, **kwargs) -> dict:
    """Execute fn(*args, **kwargs) with rate limiting + retry on 429."""
    for attempt, backoff in enumerate(_limiter.BACKOFF + [None]):
        await _limiter.acquire()
        try:
            result = fn(*args, **kwargs)
            # Bybit returns retCode 10006 for rate limit exceeded
            if isinstance(result, dict) and result.get("retCode") in (429, 10006):
                if backoff is None:
                    logger.error("Rate limit: max retries exceeded")
                    return result
                retry_after = float(
                    result.get("retExtInfo", {}).get("retryAfter", backoff)
                )
                logger.warning(f"Rate limit hit (attempt {attempt+1}), retrying in {retry_after:.2f}s")
                await asyncio.sleep(retry_after)
                continue
            return result
        except Exception as e:
            if backoff is None:
                raise
            logger.warning(f"API call error (attempt {attempt+1}): {e}, retrying in {backoff}s")
            await asyncio.sleep(backoff)
    return {"retCode": -1, "retMsg": "max retries exceeded"}


class Trader:
    def __init__(self):
        self._client: HTTP | None = None
        self._symbol: str = ""
        self._qty_step:   float = 0.001
        self._tick_size:  float = 0.01
        self._min_qty:    float = 0.001

    async def setup(self) -> None:
        self._symbol = config.symbol
        self._client = HTTP(
            testnet=config.testnet,
            api_key=config.api_key,
            api_secret=config.api_secret,
        )
        await self._set_leverage()
        await self.set_position_mode()
        await self.get_instrument_info()
        logger.info(
            f"Trader ready: {self._symbol} "
            f"qty_step={self._qty_step} tick={self._tick_size} "
            f"min_qty={self._min_qty} "
            f"rate_limit=10req/s burst=3"
        )

    async def _set_leverage(self) -> None:
        try:
            result = await _api_call_with_retry(
                self._client.set_leverage,
                category="linear",
                symbol=self._symbol,
                buyLeverage=str(config.leverage),
                sellLeverage=str(config.leverage),
            )
            if result.get("retCode") not in (0, 110043):
                logger.warning(f"set_leverage: {result}")
            else:
                logger.info(f"Leverage set to {config.leverage}x")
        except Exception as e:
            logger.warning(f"set_leverage failed: {e}")

    async def set_position_mode(self) -> None:
        try:
            result = await _api_call_with_retry(
                self._client.switch_position_mode,
                category="linear",
                symbol=self._symbol,
                mode=0,   # 0 = OneWay
            )
            if result.get("retCode") not in (0, 110025):
                logger.warning(f"set_position_mode: {result}")
            else:
                logger.info("Position mode: OneWay ✅")
        except Exception as e:
            logger.warning(f"set_position_mode failed: {e}")

    async def get_instrument_info(self) -> None:
        try:
            result = await _api_call_with_retry(
                self._client.get_instruments_info,
                category="linear",
                symbol=self._symbol,
            )
            if result.get("retCode") == 0:
                info = result["result"]["list"][0]
                lot  = info["lotSizeFilter"]
                prc  = info["priceFilter"]
                self._qty_step  = float(lot.get("qtyStep",  self._qty_step))
                self._min_qty   = float(lot.get("minOrderQty", self._min_qty))
                self._tick_size = float(prc.get("tickSize",  self._tick_size))
                logger.info(
                    f"Instrument: qty_step={self._qty_step} "
                    f"min_qty={self._min_qty} tick={self._tick_size}"
                )
        except Exception as e:
            logger.warning(f"get_instrument_info failed: {e}")

    def fee_estimate(self, qty: float, price: float, order_type: str) -> float:
        rate = 0.00020 if order_type == "Limit" else 0.00055
        return round(qty * price * rate, 6)

    async def place_order(
        self,
        side: str,
        qty: float,
        order_type: str = "Limit",
        post_only: bool = True,
        price: float = 0.0,
        reduce_only: bool = False,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict:
        if self._client is None:
            return {"retCode": -1, "retMsg": "not initialized"}

        if order_type == "Limit" and post_only and reduce_only:
            post_only = False

        params: dict = dict(
            category="linear",
            symbol=self._symbol,
            side=side,
            orderType=order_type,
            qty=str(round(qty, 6)),
            reduceOnly=reduce_only,
        )
        if order_type == "Limit":
            params["price"]         = str(round(price, 8))
            params["timeInForce"]   = "PostOnly" if post_only else "GTC"
        if stop_loss:
            params["stopLoss"] = str(round(stop_loss, 8))
        if take_profit:
            params["takeProfit"] = str(round(take_profit, 8))

        result = await _api_call_with_retry(self._client.place_order, **params)
        if result.get("retCode") != 0:
            logger.warning(f"place_order {side} {qty}: {result.get('retMsg')}")
        return result

    async def amend_order(
        self,
        order_id: str,
        qty: float | None = None,
        price: float | None = None,
    ) -> dict:
        params: dict = dict(
            category="linear",
            symbol=self._symbol,
            orderId=order_id,
        )
        if qty    is not None: params["qty"]   = str(round(qty,   6))
        if price  is not None: params["price"] = str(round(price, 8))
        return await _api_call_with_retry(self._client.amend_order, **params)

    async def amend_sl_tp(
        self,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict:
        if self._client is None:
            return {"retCode": -1}
        params: dict = dict(
            category="linear",
            symbol=self._symbol,
            positionIdx=0,
        )
        if stop_loss:   params["stopLoss"]   = str(round(stop_loss,   8))
        if take_profit: params["takeProfit"] = str(round(take_profit, 8))
        return await _api_call_with_retry(self._client.set_trading_stop, **params)

    async def close_position(
        self,
        use_limit: bool = True,
        limit_timeout_s: float = 3.0,
    ) -> None:
        """Close full position. Limit-first (maker fee), Market fallback."""
        with state.lock:
            pos = state.open_position
            qty = state.open_qty
            best_bid = state.orderbook.best_bid
            best_ask = state.orderbook.best_ask

        if not pos or qty <= 0:
            return

        close_side = "Sell" if pos == "long" else "Buy"

        if use_limit:
            limit_px = best_ask if pos == "long" else best_bid
            resp = await self.place_order(
                side=close_side, qty=qty,
                order_type="Limit", post_only=False,
                price=limit_px, reduce_only=True,
            )
            if resp.get("retCode") == 0:
                await asyncio.sleep(limit_timeout_s)

        # Market fallback / final close
        await self.place_order(
            side=close_side, qty=qty,
            order_type="Market", post_only=False,
            reduce_only=True,
        )
        with state.lock:
            state.open_position = ""
            state.open_qty      = 0.0
            state.open_entry    = 0.0
            state.trailing_stop = 0.0

    async def sync_position_from_exchange(self) -> None:
        """Called at startup. Detect SL triggered while offline (ghost state)."""
        if self._client is None:
            return
        try:
            result = await _api_call_with_retry(
                self._client.get_positions,
                category="linear",
                symbol=self._symbol,
            )
            if result.get("retCode") != 0:
                return

            positions = result["result"].get("list", [])
            exchange_has_pos = any(
                float(p.get("size", 0)) > 0 for p in positions
            )

            with state.lock:
                bot_has_pos = bool(state.open_position)
                bot_entry   = state.open_entry
                bot_side    = state.open_position
                bot_qty     = state.open_qty
                current_px  = state.last_price

            if bot_has_pos and not exchange_has_pos:
                # SL was triggered while offline — reconstruct & clean state
                logger.warning(
                    f"Ghost position detected: bot={bot_side} qty={bot_qty} "
                    f"entry={bot_entry} — exchange has NO open position. "
                    f"SL was triggered while offline."
                )
                if current_px > 0 and bot_entry > 0:
                    pnl_pct = (
                        (current_px - bot_entry) / bot_entry if bot_side == "long"
                        else (bot_entry - current_px) / bot_entry
                    )
                    pnl_usdt = pnl_pct * bot_qty * bot_entry
                else:
                    pnl_pct  = 0.0
                    pnl_usdt = 0.0

                from .risk import risk
                from .persistence import db
                risk.on_close(pnl_usdt, pnl_pct)
                db.record_trade(
                    symbol=self._symbol,
                    side=bot_side,
                    entry=bot_entry,
                    exit_price=current_px,
                    qty=bot_qty,
                    pnl_usdt=pnl_usdt,
                    pnl_pct=pnl_pct,
                    reason="SL_OFFLINE",
                    signal_score=0.0,
                    funding_rate=0.0,
                )
                with state.lock:
                    state.open_position = ""
                    state.open_qty      = 0.0
                    state.open_entry    = 0.0
                    state.trailing_stop = 0.0

                try:
                    from .telegram_ui import send_message
                    await send_message(
                        f"⚠️ *SL triggered while offline* — ghost state cleared\n"
                        f"`{bot_side} {bot_qty} entry={bot_entry}`\n"
                        f"`estimated pnl={pnl_usdt:+.4f} USDT`"
                    )
                except Exception:
                    pass

            elif not bot_has_pos and exchange_has_pos:
                # Exchange has position but bot doesn't know — sync from exchange
                for p in positions:
                    sz = float(p.get("size", 0))
                    if sz > 0:
                        side_raw = p.get("side", "")
                        entry_px = float(p.get("avgPrice", 0))
                        bot_side = "long" if side_raw == "Buy" else "short"
                        with state.lock:
                            state.open_position = bot_side
                            state.open_qty      = sz
                            state.open_entry    = entry_px
                        logger.info(
                            f"Synced position from exchange: "
                            f"{bot_side} qty={sz} entry={entry_px}"
                        )
                        break
            else:
                logger.info("Position sync: state matches exchange ✅")

        except Exception as e:
            logger.warning(f"sync_position_from_exchange failed: {e}")


trader = Trader()
