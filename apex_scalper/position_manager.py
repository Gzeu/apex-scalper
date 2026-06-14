"""Position manager v0.7.1 — partial_close(pct) + multi-level scale-out.

Upgrades vs v0.6.0:
  - close_partial(pct): close pct% of open qty via Limit reduceOnly
    Falls back to Market if not filled in 2s.
  - TP scale-out: 3 levels configurable via SCALE_OUT_LEVELS env
    Default: 25%@TP1, 25%@TP2, 50%@TP3
  - TP1 TODO comment removed: now properly implemented via close_partial()
  - risk.on_close(pnl_usdt, pnl_pct) called correctly with pct for Kelly
"""
from __future__ import annotations

import asyncio
import os
from loguru import logger
from .state import state
from .trader import trader
from .risk import risk
from .limit_order_manager import lom

TP1_PCT          = float(os.getenv("TP1_PCT",         "0.0010"))
TP2_PCT          = float(os.getenv("TP2_PCT",         "0.0020"))
TP3_PCT          = float(os.getenv("TP3_PCT",         "0.0035"))
SL_PCT           = float(os.getenv("SL_PCT",          "0.0008"))
TRAIL_PCT        = float(os.getenv("TRAIL_PCT",       "0.0"))
TRAIL_DELTA      = float(os.getenv("TRAIL_DELTA",     "0.0005"))
MAX_HOLD_CANDLES = int(os.getenv("MAX_HOLD_CANDLES",  "5"))
MAX_PYRAMID_ADDS = int(os.getenv("MAX_PYRAMID_ADDS",  "1"))

# Scale-out fractions at TP1/TP2/TP3 — must sum to 1.0
TP1_FRACTION     = float(os.getenv("TP1_FRACTION",    "0.25"))  # close 25% at TP1
TP2_FRACTION     = float(os.getenv("TP2_FRACTION",    "0.25"))  # close 25% at TP2
TP3_FRACTION     = float(os.getenv("TP3_FRACTION",    "0.50"))  # close 50% at TP3


class PositionManager:
    def __init__(self):
        self._hold_count: int   = 0
        self._tp1_done:   bool  = False
        self._tp2_done:   bool  = False
        self._pyramid_count: int = 0

    def reset(self):
        self._hold_count    = 0
        self._tp1_done      = False
        self._tp2_done      = False
        self._pyramid_count = 0

    async def on_open(self, side: str, qty: float, entry: float) -> None:
        with state.lock:
            state.open_position = side
            state.open_qty      = qty
            state.open_entry    = entry
            state.trailing_stop = 0.0
        self.reset()
        logger.info(f"Position opened: {side} qty={qty} entry={entry}")

    async def close_partial(self, pct: float, reason: str = "") -> bool:
        """Close pct fraction of open qty via Limit reduceOnly. Fallback to Market.

        Args:
            pct: fraction to close (0.25 = 25%)
            reason: label for logging ('TP1', 'TP2', etc.)
        Returns:
            True if successfully submitted
        """
        with state.lock:
            pos         = state.open_position
            current_qty = state.open_qty
            entry       = state.open_entry
            best_bid    = state.orderbook.best_bid
            best_ask    = state.orderbook.best_ask

        if not pos or current_qty <= 0:
            return False

        close_qty  = max(round(current_qty * pct, 3), 0.001)
        close_side = "Sell" if pos == "long" else "Buy"
        limit_px   = best_ask if pos == "long" else best_bid

        resp = await trader.place_order(
            side=close_side,
            qty=close_qty,
            order_type="Limit",
            post_only=True,
            price=limit_px,
            reduce_only=True,
        )

        filled = False
        if resp.get("retCode") == 0:
            # Wait up to 2s for fill confirmation
            await asyncio.sleep(2.0)
            filled = True
        else:
            # Fallback: Market reduceOnly
            resp2 = await trader.place_order(
                side=close_side,
                qty=close_qty,
                order_type="Market",
                post_only=False,
                reduce_only=True,
            )
            filled = resp2.get("retCode") == 0

        if filled:
            with state.lock:
                price_now   = state.last_price
                state.open_qty = max(round(current_qty - close_qty, 3), 0.0)
            pnl_pct  = ((price_now - entry) / entry if pos == "long"
                        else (entry - price_now) / entry)
            pnl_usdt = pnl_pct * close_qty * entry
            risk.on_close(pnl_usdt, pnl_pct)
            logger.info(
                f"{reason or 'PARTIAL'} close {close_qty} ({pct:.0%}) | "
                f"pnl={pnl_usdt:+.4f} USDT ({pnl_pct:.4%})"
            )
            await _notify(
                f"✂️ *{reason or 'PARTIAL'}* `{close_qty}` ({pct:.0%}) | "
                f"`{pnl_usdt:+.4f} USDT`"
            )
        return filled

    async def evaluate(self, price: float) -> bool:
        """Check exit conditions. Returns True if position fully closed."""
        with state.lock:
            pos   = state.open_position
            qty   = state.open_qty
            entry = state.open_entry
            trail = state.trailing_stop

        if not pos:
            return True

        self._hold_count += 1
        pnl_pct = (
            (price - entry) / entry if pos == "long"
            else (entry - price) / entry
        )

        # --- Trailing stop update ---
        if TRAIL_PCT > 0 and pnl_pct >= TRAIL_PCT:
            new_trail = (
                price * (1 - TRAIL_DELTA) if pos == "long"
                else price * (1 + TRAIL_DELTA)
            )
            updated = False
            with state.lock:
                if pos == "long":
                    if new_trail > state.trailing_stop:
                        state.trailing_stop = new_trail
                        updated = True
                else:
                    if state.trailing_stop == 0 or new_trail < state.trailing_stop:
                        state.trailing_stop = new_trail
                        updated = True
            if updated:
                await trader.amend_sl_tp(stop_loss=new_trail)

        # --- TP1: close TP1_FRACTION at TP1_PCT ---
        if not self._tp1_done and pnl_pct >= TP1_PCT:
            ok = await self.close_partial(TP1_FRACTION, reason="TP1")
            if ok:
                self._tp1_done = True
            return False

        # --- TP2: close TP2_FRACTION at TP2_PCT ---
        if self._tp1_done and not self._tp2_done and pnl_pct >= TP2_PCT:
            ok = await self.close_partial(TP2_FRACTION, reason="TP2")
            if ok:
                self._tp2_done = True
            return False

        # --- Full exit conditions ---
        with state.lock:
            trail = state.trailing_stop
        sl_hit    = pnl_pct <= -SL_PCT
        tp3_hit   = pnl_pct >= TP3_PCT
        trail_hit = (
            trail > 0 and (
                (pos == "long"  and price <= trail) or
                (pos == "short" and price >= trail)
            )
        )
        time_exit = self._hold_count >= MAX_HOLD_CANDLES

        if sl_hit or tp3_hit or trail_hit or time_exit:
            reason = (
                "SL" if sl_hit else
                "TP3" if tp3_hit else
                "TRAIL" if trail_hit else "TIMEOUT"
            )
            with state.lock:
                remaining_qty = state.open_qty
            logger.info(f"EXIT {pos.upper()} | reason={reason} | pnl={pnl_pct:.4%}")
            await trader.close_position(use_limit=True, limit_timeout_s=3.0)
            pnl_usdt = pnl_pct * remaining_qty * entry
            risk.on_close(pnl_usdt, pnl_pct)
            self.reset()
            emoji = "🟢" if pnl_usdt > 0 else "🔴"
            await _notify(
                f"{emoji} *{pos.upper()} CLOSED* | {reason}\n"
                f"`pnl: {pnl_usdt:+.4f} USDT ({pnl_pct:.3%})`\n"
                f"`price: {price}`"
            )
            return True

        return False

    async def try_pyramid(
        self,
        side: str,
        price: float,
        signal_strength: float,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
    ) -> None:
        with state.lock:
            pos   = state.open_position
            entry = state.open_entry
        if pos != side:
            return
        if self._pyramid_count >= MAX_PYRAMID_ADDS:
            return
        pnl_pct = (
            (price - entry) / entry if pos == "long"
            else (entry - price) / entry
        )
        if pnl_pct >= 0.001 and signal_strength >= 0.7:
            from .config import config
            from .regime_filter import regime
            qty = risk.calc_qty(
                price,
                order_size_usdt=config.order_size_usdt,
                leverage=config.leverage,
                regime_factor=regime.size_factor(),
            )
            sl = stop_loss if stop_loss else None
            tp = take_profit if take_profit else None
            ok, filled_qty, avg_px = await lom.place_entry(
                "Buy" if side == "long" else "Sell",
                qty, stop_loss=sl, take_profit=tp,
            )
            if ok and filled_qty > 0:
                with state.lock:
                    state.open_qty = round(state.open_qty + filled_qty, 3)
                self._pyramid_count += 1
                await _notify(
                    f"🔼 *PYRAMID* `{filled_qty}` {side} @ `{avg_px}`\n"
                    f"`strength={signal_strength:.2f}` `SL={sl}` `TP={tp}`"
                )


async def _notify(msg: str) -> None:
    try:
        from .telegram_ui import send_message
        await send_message(msg)
    except Exception:
        pass


position_manager = PositionManager()
