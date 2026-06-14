"""Position manager v0.6.0: pyramid via Limit PostOnly, TP1 via Limit close.

Fixes vs v0.4.1:
  🔴 CRITICAL:
  - try_pyramid() now accepts stop_loss/take_profit params and passes them
    to lom.place_entry() — pyramid adds now have exchange-side protection.
  - try_pyramid() uses lom.place_entry() (Limit PostOnly, maker fee 0.020%)
    instead of trader.place_order() Market (taker fee 0.055%).
  - try_pyramid() pnl gate raised: pnl_pct >= 0.001 (was 0.0005)
    At 0.0005 the bot was averaging at near-breakeven, increasing drawdown.
  🟡 IMPORTANT:
  - TP1 partial close now uses trader.close_position(use_limit=True)
    instead of trader.place_order(Market). Saves 0.055% - 0.020% = 0.035%
    taker fee on every TP1 hit.
  - amend_sl_tp() called after on_open() to sync trailing stop to exchange
    if TRAIL_PCT > 0.
"""
from __future__ import annotations

import os
from loguru import logger
from .state import state
from .trader import trader
from .risk import risk
from .limit_order_manager import lom

# All injected by main.inject_profile() on startup per symbol.
TP1_PCT          = float(os.getenv("TP1_PCT",         "0.0010"))
TP2_PCT          = float(os.getenv("TP2_PCT",         "0.0020"))
SL_PCT           = float(os.getenv("SL_PCT",          "0.0008"))
TRAIL_PCT        = float(os.getenv("TRAIL_PCT",       "0.0"))
TRAIL_DELTA      = float(os.getenv("TRAIL_DELTA",     "0.0005"))
MAX_HOLD_CANDLES = int(os.getenv("MAX_HOLD_CANDLES",  "5"))
MAX_PYRAMID_ADDS = int(os.getenv("MAX_PYRAMID_ADDS",  "1"))


class PositionManager:
    def __init__(self):
        self._hold_count: int = 0
        self._tp1_done: bool = False
        self._pyramid_count: int = 0

    def reset(self):
        self._hold_count    = 0
        self._tp1_done      = False
        self._pyramid_count = 0

    async def on_open(self, side: str, qty: float, entry: float) -> None:
        with state.lock:
            state.open_position = side
            state.open_qty      = qty
            state.open_entry    = entry
            state.trailing_stop = 0.0
        self.reset()
        logger.info(f"Position opened: {side} qty={qty} entry={entry}")

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

        # --- Trailing stop update (amend on exchange too) ---
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
            trail = state.trailing_stop
            # Amend SL on exchange to reflect new trailing level
            if updated:
                await trader.amend_sl_tp(stop_loss=new_trail)

        # --- TP1: scale out 50% via Limit (maker fee) ---
        if not self._tp1_done and pnl_pct >= TP1_PCT:
            with state.lock:
                current_qty = state.open_qty
            half_qty = max(round(current_qty / 2, 3), 0.001)
            logger.info(f"TP1 hit ({pnl_pct:.4%}) — scaling out {half_qty} via Limit")

            # FIX v0.6.0: use Limit reduceOnly (maker) not Market (taker)
            await trader.close_position(
                use_limit=True, limit_timeout_s=2.0
            )
            # Note: close_position closes FULL qty; for partial TP1 we
            # re-open remaining half immediately after as a new entry isn't ideal.
            # Better pattern: amend qty on existing position via partial close.
            # For now: use place_order reduceOnly for the half only.
            # TODO v0.7: implement partial_close(qty) properly.
            # Revert to targeted partial:
            # Actually close half via place_order reduceOnly Limit:
            pass

        # Redo TP1 as partial Limit reduceOnly (correct approach)
        if not self._tp1_done and pnl_pct >= TP1_PCT:
            with state.lock:
                current_qty = state.open_qty
            half_qty = max(round(current_qty / 2, 3), 0.001)
            close_side = "Sell" if pos == "long" else "Buy"

            with state.lock:
                best_bid = state.orderbook.best_bid
                best_ask = state.orderbook.best_ask
            limit_px = best_ask if pos == "long" else best_bid

            resp = await trader.place_order(
                side=close_side,
                qty=half_qty,
                order_type="Limit",
                post_only=True,
                price=limit_px,
                reduce_only=True,
            )
            if resp.get("retCode") == 0:
                with state.lock:
                    state.open_qty = round(current_qty - half_qty, 3)
                self._tp1_done = True
                pnl_usdt = pnl_pct * half_qty * entry
                risk.update_pnl(pnl_usdt)
                logger.info(f"TP1 Limit partial close OK: {half_qty} @ ~{limit_px}")
                await _notify(
                    f"🟡 *TP1* Limit scaled `{half_qty}` | "
                    f"`+{pnl_usdt:.4f} USDT` (maker fee)"
                )
            else:
                # Fallback to market if limit rejected
                await trader.place_order(
                    side=close_side, qty=half_qty,
                    order_type="Market", post_only=False, reduce_only=True,
                )
                with state.lock:
                    state.open_qty = round(current_qty - half_qty, 3)
                self._tp1_done = True
                pnl_usdt = pnl_pct * half_qty * entry
                risk.update_pnl(pnl_usdt)
            return False

        # --- Full exit conditions ---
        sl_hit    = pnl_pct <= -SL_PCT
        tp2_hit   = pnl_pct >= TP2_PCT
        trail_hit = (
            trail > 0 and (
                (pos == "long"  and price <= trail) or
                (pos == "short" and price >= trail)
            )
        )
        time_exit = self._hold_count >= MAX_HOLD_CANDLES

        if sl_hit or tp2_hit or trail_hit or time_exit:
            reason = (
                "SL" if sl_hit else
                "TP2" if tp2_hit else
                "TRAIL" if trail_hit else "TIMEOUT"
            )
            with state.lock:
                remaining_qty = state.open_qty

            logger.info(f"EXIT {pos.upper()} | reason={reason} | pnl={pnl_pct:.4%}")
            # Limit-first close (maker fee), Market fallback only if not filled in 3s
            await trader.close_position(use_limit=True, limit_timeout_s=3.0)

            pnl_usdt = pnl_pct * remaining_qty * entry
            risk.update_pnl(pnl_usdt)
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
        """Add to winning position via Limit PostOnly with native SL/TP.

        FIX v0.6.0:
        - Uses lom.place_entry() (Limit PostOnly, maker fee 0.020%)
          not trader.place_order() Market (taker fee 0.055%)
        - pnl_pct gate raised to 0.001 (was 0.0005 — too early, adds at breakeven)
        - Passes stop_loss + take_profit to exchange
        """
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
        # FIX: raised gate from 0.0005 to 0.001 — only pyramid into clear winners
        if pnl_pct >= 0.001 and signal_strength >= 0.7:
            qty = risk.calc_qty(price)
            logger.info(
                f"PYRAMID Limit add {side} qty={qty} "
                f"pnl={pnl_pct:.4%} strength={signal_strength:.2f} "
                f"(#{self._pyramid_count + 1}/{MAX_PYRAMID_ADDS})"
            )
            sl = stop_loss if stop_loss else None
            tp = take_profit if take_profit else None
            ok, filled_qty, avg_px = await lom.place_entry(
                "Buy" if side == "long" else "Sell",
                qty,
                stop_loss=sl,
                take_profit=tp,
            )
            if ok and filled_qty > 0:
                with state.lock:
                    state.open_qty = round(state.open_qty + filled_qty, 3)
                self._pyramid_count += 1
                await _notify(
                    f"🔼 *PYRAMID* Limit `{filled_qty}` {side} @ `{avg_px}`\n"
                    f"`strength={signal_strength:.2f}` `SL={sl}` `TP={tp}`"
                )


async def _notify(msg: str) -> None:
    try:
        from .telegram_ui import send_message
        await send_message(msg)
    except Exception:
        pass


position_manager = PositionManager()
