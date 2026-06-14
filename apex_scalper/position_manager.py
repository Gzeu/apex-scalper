"""Position Manager v0.7.2 — TP1/TP2/TP3 scale-out, trailing SL, pyramid.

Changelog:
  v0.7.2 — GAP #2 fix: _confirm_order_filled now uses module-level
             _api_call_with_retry from trader.py instead of non-existent
             trader._api_call() method. Prevents AttributeError on every
             partial close confirmation poll.
  v0.7.1 — fill confirmation via order history poll (fixes false filled
             state from retCode==0 only). Kelly sizing integration.
             _prev_fast / _prev_slow dead code removed.

Scale-out:
  TP1 → close TP1_FRACTION (default 25%)
  TP2 → close TP2_FRACTION (default 25%)
  TP3 → close TP3_FRACTION (default 50%)
  All exits via Limit reduceOnly. Fallback to Market if not filled in 2s.

Trailing:
  Activates when unrealised PnL ≥ TRAIL_PCT.
  Trail delta amended on exchange via trader.amend_sl_tp().

Pyramid:
  Add to winner if score ≥ PYRAMID_SCORE_MIN AND pnl ≥ PYRAMID_PNL_MIN.
  Max MAX_PYRAMID_ADDS additions per trade.
"""
from __future__ import annotations

import asyncio
from loguru import logger

from .state import state
from .trader import trader, _api_call_with_retry   # GAP #2 FIX v0.7.2
from .risk import risk

# --------------------------------------------------------------------------- #
#  Parameters (all overrideable via /setparam or ENV)                          #
# --------------------------------------------------------------------------- #
TP1_PCT          = 0.0012
TP2_PCT          = 0.0025
TP3_PCT          = 0.0040
TP1_FRACTION     = 0.25
TP2_FRACTION     = 0.25
TP3_FRACTION     = 0.50
SL_PCT           = 0.0008
TRAIL_PCT        = 0.0015   # activate trailing when PnL ≥ 0.15%
TRAIL_DELTA      = 0.0006   # trail 0.06% behind peak
MAX_HOLD_CANDLES = 5
MAX_PYRAMID_ADDS = 2

PYRAMID_SCORE_MIN = 0.70
PYRAMID_PNL_MIN   = 0.0010
CONFIRM_POLL_INTERVAL = 0.5   # seconds between order history polls
CONFIRM_POLL_MAX      = 8     # max polls before assuming not filled


async def _confirm_order_filled(order_id: str, sym: str) -> bool:
    """Poll order history to confirm fill — never assume from retCode==0.

    GAP #2 FIX v0.7.2: uses module-level _api_call_with_retry from trader.py.
    Previous version called trader._api_call() which does not exist on the
    Trader class, causing AttributeError on every partial close.
    """
    for _ in range(CONFIRM_POLL_MAX):
        await asyncio.sleep(CONFIRM_POLL_INTERVAL)
        try:
            # GAP #2 FIX: _api_call_with_retry is module-level in trader.py
            result = await _api_call_with_retry(
                trader._client.get_order_history,
                category="linear",
                symbol=sym,
                orderId=order_id,
                limit=1,
            )
            orders = result.get("result", {}).get("list", [])
            if orders and orders[0].get("orderStatus") == "Filled":
                return True
        except Exception as e:
            logger.warning(f"[PM] confirm_fill poll error: {e}")
    return False


class PositionManager:
    def __init__(self):
        self._tp1_hit   = False
        self._tp2_hit   = False
        self._tp3_hit   = False
        self._trail_active   = False
        self._trail_peak_pnl = 0.0
        self._hold_candles   = 0
        self._pyramid_adds   = 0
        self._entry_price    = 0.0
        self._entry_side     = ""
        self._trade_id: int | None = None

    def reset(self):
        self._tp1_hit        = False
        self._tp2_hit        = False
        self._tp3_hit        = False
        self._trail_active   = False
        self._trail_peak_pnl = 0.0
        self._hold_candles   = 0
        self._pyramid_adds   = 0
        self._entry_price    = 0.0
        self._entry_side     = ""
        self._trade_id       = None

    def on_entry(self, side: str, entry_price: float, trade_id: int | None = None):
        self.reset()
        self._entry_side  = side
        self._entry_price = entry_price
        self._trade_id    = trade_id
        logger.info(f"[PM] Entry recorded: side={side} price={entry_price}")

    def _unrealised_pnl_pct(self, current_price: float) -> float:
        if self._entry_price <= 0:
            return 0.0
        if self._entry_side == "Buy":
            return (current_price - self._entry_price) / self._entry_price
        return (self._entry_price - current_price) / self._entry_price

    async def close_partial(
        self,
        fraction: float,
        label: str,
        sym: str,
        side: str,
    ) -> bool:
        """Close a fraction of open position. Returns True if confirmed filled."""
        with state.lock:
            open_qty  = state.open_qty
            last_price = state.last_price

        qty = round(open_qty * fraction, 6)
        if qty <= 0:
            logger.warning(f"[PM] {label}: qty=0, skipping close")
            return False

        close_side = "Sell" if side == "Buy" else "Buy"

        resp = await trader.place_limit_order(
            side=close_side,
            qty=qty,
            price=last_price,
            sym=sym,
            reduce_only=True,
            post_only=False,
        )
        if resp.get("retCode") != 0:
            logger.error(f"[PM] {label} limit rejected: {resp}")
            return False

        order_id = resp.get("result", {}).get("orderId", "")

        # v0.7.1 FIX: poll order history, never assume filled from retCode==0
        filled = await _confirm_order_filled(order_id, sym)

        if not filled:
            logger.warning(f"[PM] {label} limit not filled — market fallback")
            fb = await trader.place_market_order(
                side=close_side, qty=qty, sym=sym, reduce_only=True
            )
            filled = fb.get("retCode") == 0

        if filled:
            with state.lock:
                state.open_qty = max(0.0, state.open_qty - qty)
            logger.info(f"[PM] {label} filled: qty={qty} remaining={state.open_qty}")

        return filled

    async def evaluate(
        self,
        current_price: float,
        sym: str,
        side: str,
        score: float = 0.0,
    ) -> None:
        """Called every candle. Checks TP levels, trailing, timeout, pyramid."""
        if not state.open_position:
            return

        pnl_pct = self._unrealised_pnl_pct(current_price)
        self._hold_candles += 1

        # --- TP1 ---
        if not self._tp1_hit and pnl_pct >= TP1_PCT:
            ok = await self.close_partial(TP1_FRACTION, "TP1", sym, side)
            if ok:
                self._tp1_hit = True
                logger.info(f"[PM] TP1 hit @ {pnl_pct:.4%}")

        # --- TP2 ---
        elif self._tp1_hit and not self._tp2_hit and pnl_pct >= TP2_PCT:
            ok = await self.close_partial(TP2_FRACTION, "TP2", sym, side)
            if ok:
                self._tp2_hit = True
                logger.info(f"[PM] TP2 hit @ {pnl_pct:.4%}")

        # --- TP3 ---
        elif self._tp2_hit and not self._tp3_hit and pnl_pct >= TP3_PCT:
            ok = await self.close_partial(TP3_FRACTION, "TP3", sym, side)
            if ok:
                self._tp3_hit = True
                logger.info(f"[PM] TP3 hit @ {pnl_pct:.4%} — trade complete")
                with state.lock:
                    state.open_position = None
                self.reset()
                return

        # --- Trailing stop ---
        if pnl_pct >= TRAIL_PCT:
            if not self._trail_active:
                self._trail_active   = True
                self._trail_peak_pnl = pnl_pct
                logger.info(f"[PM] Trailing activated @ {pnl_pct:.4%}")
            elif pnl_pct > self._trail_peak_pnl:
                self._trail_peak_pnl = pnl_pct
                trail_sl = current_price * (
                    (1 - TRAIL_DELTA) if side == "Buy" else (1 + TRAIL_DELTA)
                )
                await trader.amend_sl_tp(sym=sym, stop_loss=trail_sl)
                logger.debug(f"[PM] Trail SL amended to {trail_sl:.4f}")
            elif pnl_pct <= self._trail_peak_pnl - TRAIL_DELTA:
                logger.info(f"[PM] Trail triggered: pnl={pnl_pct:.4%} peak={self._trail_peak_pnl:.4%}")
                await trader.close_position()
                with state.lock:
                    state.open_position = None
                self.reset()
                return

        # --- Timeout ---
        if self._hold_candles >= MAX_HOLD_CANDLES:
            logger.info(f"[PM] Timeout ({MAX_HOLD_CANDLES} candles) — closing")
            await trader.close_position()
            with state.lock:
                state.open_position = None
            self.reset()
            return

        # --- Pyramid ---
        if (
            self._pyramid_adds < MAX_PYRAMID_ADDS
            and score >= PYRAMID_SCORE_MIN
            and pnl_pct >= PYRAMID_PNL_MIN
            and self._tp1_hit
        ):
            add_qty = risk.position_size(
                state.last_price, TP1_PCT, scale=0.5
            )
            if add_qty > 0:
                resp = await trader.place_market_order(
                    side=side, qty=add_qty, sym=sym
                )
                if resp.get("retCode") == 0:
                    self._pyramid_adds += 1
                    with state.lock:
                        state.open_qty += add_qty
                    logger.info(
                        f"[PM] Pyramid add #{self._pyramid_adds}: "
                        f"qty={add_qty} pnl={pnl_pct:.4%}"
                    )


position_manager = PositionManager()
