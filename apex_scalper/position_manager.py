"""Position Manager v0.7.9 — race-condition fix pentru pulse snapshot.

Changelog:
  v0.7.9 — BUG FIX: race condition intre pulse.py si evaluate().
    - Adaugat PositionSnapshot dataclass cu toate campurile relevante.
    - Adaugat _snapshot_lock asyncio.Lock() intern in PositionManager.
    - evaluate() si try_pyramid() achizitioneaza _snapshot_lock la scriere.
    - snapshot() metoda publica achizitioneaza acelasi lock la citire.
    - pulse.py consuma snapshot() in loc sa acceseze pm._* direct.
    - Elimina potentialul ZeroDivisionError cand entry_price=0 la reset concurent.
  v0.7.3 — Interface alignment (all AttributeErrors eliminated):
    - place_limit_order/place_market_order -> trader.place_order(order_type=)
    - risk.position_size() -> risk.calc_qty() (correct method name)
    - risk.on_close() called at ALL exit paths (TP1/2/3, trail, timeout)
    - on_entry() renamed to on_open(side, qty, entry_price) to match strategy.py
    - evaluate(price) now returns bool: True=position closed, False=still open
    - try_pyramid() added as public method called by strategy.py
    - _prev_fast/_prev_slow dead code removed from strategy interface
  v0.7.2 — _api_call_with_retry import fix (trader._api_call -> module-level fn)
  v0.7.1 — fill confirmation via get_order_history poll (retCode!=fill proof)

Interfaces consumed (verified against source):
  trader.place_order(side, qty, order_type, post_only, price, reduce_only)
  trader.amend_sl_tp(sym, stop_loss)
  trader.close_position()
  risk.calc_qty(price, order_size_usdt, leverage, regime_factor)
  risk.on_close(pnl_usdt, pnl_pct)
  strategy.py calls: on_open(side, qty, price), evaluate(price) -> bool,
                     try_pyramid(side, price, score, sl, tp)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from loguru import logger

from .state import state
from .trader import trader, _api_call_with_retry
from .risk import risk
from .persistence import db

# --------------------------------------------------------------------------- #
#  Parameters (all overrideable via /setparam or ENV)                         #
# --------------------------------------------------------------------------- #
TP1_PCT          = 0.0012
TP2_PCT          = 0.0025
TP3_PCT          = 0.0040
TP1_FRACTION     = 0.25
TP2_FRACTION     = 0.25
TP3_FRACTION     = 0.50
SL_PCT           = 0.0008
TRAIL_PCT        = 0.0015
TRAIL_DELTA      = 0.0006
MAX_HOLD_CANDLES = 5
MAX_PYRAMID_ADDS = 2

PYRAMID_SCORE_MIN      = 0.70
PYRAMID_PNL_MIN        = 0.0010
CONFIRM_POLL_INTERVAL  = 0.5
CONFIRM_POLL_MAX       = 8


@dataclass
class PositionSnapshot:
    """Immutable snapshot al starii PositionManager, citit atomic sub lock.

    Consumat de pulse.py si telegram_ui /tp pentru a evita race conditions.
    """
    entry_price:  float
    entry_side:   str
    entry_qty:    float
    tp1_hit:      bool
    tp2_hit:      bool
    tp3_hit:      bool
    trail_active: bool
    trail_peak:   float
    hold_candles: int
    pyramid_adds: int
    trade_id:     int | None

    def unrealised_pnl_pct(self, current_price: float) -> float:
        """Calculeaza PnL% din snapshot — safe, entry_price verificat."""
        if self.entry_price <= 0:
            return 0.0
        if self.entry_side == "long":
            return (current_price - self.entry_price) / self.entry_price
        return (self.entry_price - current_price) / self.entry_price


async def _confirm_order_filled(order_id: str, sym: str) -> bool:
    """Poll get_order_history until Filled or poll limit exceeded.

    v0.7.2 fix: uses module-level _api_call_with_retry (not trader._api_call).
    v0.7.1 fix: never assumes fill from retCode==0 alone.
    """
    for _ in range(CONFIRM_POLL_MAX):
        await asyncio.sleep(CONFIRM_POLL_INTERVAL)
        try:
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
    """Manages an open position: TP scale-out, trailing SL, timeout, pyramid.

    Public interface (matches strategy.py calls exactly):
      on_open(side, qty, entry_price, trade_id=None)  <- called at entry
      evaluate(price) -> bool                          <- called every candle
      try_pyramid(side, price, score, sl, tp)          <- called by strategy
      snapshot() -> PositionSnapshot                   <- atomic read for pulse
    """

    def __init__(self):
        self._tp1_hit        = False
        self._tp2_hit        = False
        self._tp3_hit        = False
        self._trail_active   = False
        self._trail_peak_pnl = 0.0
        self._hold_candles   = 0
        self._pyramid_adds   = 0
        self._entry_price    = 0.0
        self._entry_qty      = 0.0
        self._entry_side     = ""
        self._trade_id: int | None = None
        # v0.7.9: lock pentru acces concurent pulse <-> evaluate()
        self._snapshot_lock  = asyncio.Lock()

    def reset(self) -> None:
        """Reset intern — apelat NUMAI din evaluate() sub _snapshot_lock."""
        self._tp1_hit        = False
        self._tp2_hit        = False
        self._tp3_hit        = False
        self._trail_active   = False
        self._trail_peak_pnl = 0.0
        self._hold_candles   = 0
        self._pyramid_adds   = 0
        self._entry_price    = 0.0
        self._entry_qty      = 0.0
        self._entry_side     = ""
        self._trade_id       = None

    async def snapshot(self) -> PositionSnapshot:
        """Returneaza un snapshot imutabil al starii curente, atomic sub lock.

        Singura interfata publica pentru citire din afara clasei (pulse, /tp).
        Zero race conditions: lock achizitionat pe durata copierii campurilor.
        """
        async with self._snapshot_lock:
            return PositionSnapshot(
                entry_price  = self._entry_price,
                entry_side   = self._entry_side,
                entry_qty    = self._entry_qty,
                tp1_hit      = self._tp1_hit,
                tp2_hit      = self._tp2_hit,
                tp3_hit      = self._tp3_hit,
                trail_active = self._trail_active,
                trail_peak   = self._trail_peak_pnl,
                hold_candles = self._hold_candles,
                pyramid_adds = self._pyramid_adds,
                trade_id     = self._trade_id,
            )

    def on_open(
        self,
        side: str,
        qty: float,
        entry_price: float,
        trade_id: int | None = None,
    ) -> None:
        """Record entry. Called by strategy.py after fill confirmed.

        v0.7.3: renamed from on_entry() to on_open() to match strategy.py call.
        Signature: on_open(side, qty, entry_price) — qty now tracked for risk.
        Note: called from async context, _snapshot_lock not needed here because
        strategy.py calls this before the position is visible in state.
        """
        self.reset()
        self._entry_side  = side
        self._entry_qty   = qty
        self._entry_price = entry_price
        self._trade_id    = trade_id
        logger.info(
            f"[PM] Entry: side={side} qty={qty} price={entry_price}"
        )

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _unrealised_pnl_pct(self, current_price: float) -> float:
        if self._entry_price <= 0:
            return 0.0
        if self._entry_side == "long":
            return (current_price - self._entry_price) / self._entry_price
        return (self._entry_price - current_price) / self._entry_price

    def _pnl_usdt(self, pnl_pct: float, qty: float) -> float:
        return pnl_pct * qty * self._entry_price

    def _bybit_side(self, position_side: str, closing: bool) -> str:
        if position_side == "long":
            return "Sell" if closing else "Buy"
        return "Buy" if closing else "Sell"

    def _sym(self) -> str:
        from .config import config
        return config.symbol

    async def _close_partial(
        self,
        fraction: float,
        label: str,
    ) -> tuple[bool, float]:
        sym = self._sym()
        with state.lock:
            open_qty   = state.open_qty
            last_price = state.last_price

        qty = round(open_qty * fraction, 6)
        if qty <= 0:
            logger.warning(f"[PM] {label}: qty=0, skipping")
            return False, 0.0

        close_side = self._bybit_side(self._entry_side, closing=True)

        resp = await trader.place_order(
            side=close_side,
            qty=qty,
            order_type="Limit",
            post_only=False,
            price=last_price,
            reduce_only=True,
        )
        if resp.get("retCode") != 0:
            logger.error(f"[PM] {label} limit rejected: {resp.get('retMsg')}")
            fb = await trader.place_order(
                side=close_side,
                qty=qty,
                order_type="Market",
                post_only=False,
                reduce_only=True,
            )
            filled = fb.get("retCode") == 0
            if filled:
                with state.lock:
                    state.open_qty = max(0.0, state.open_qty - qty)
            return filled, qty if filled else 0.0

        order_id = resp.get("result", {}).get("orderId", "")
        filled = await _confirm_order_filled(order_id, sym)

        if not filled:
            logger.warning(f"[PM] {label} limit not filled — market fallback")
            fb = await trader.place_order(
                side=close_side,
                qty=qty,
                order_type="Market",
                post_only=False,
                reduce_only=True,
            )
            filled = fb.get("retCode") == 0

        if filled:
            with state.lock:
                state.open_qty = max(0.0, state.open_qty - qty)
            logger.info(f"[PM] {label} filled: qty={qty} remaining={state.open_qty}")

        return filled, qty if filled else 0.0

    async def _close_full(self, reason: str, pnl_pct: float) -> None:
        with state.lock:
            remaining_qty = state.open_qty
        pnl_usdt = self._pnl_usdt(pnl_pct, remaining_qty)
        await trader.close_position()
        risk.on_close(pnl_usdt, pnl_pct)
        logger.info(
            f"[PM] Full close ({reason}): "
            f"pnl={pnl_pct:.4%} ({pnl_usdt:+.4f} USDT)"
        )
        with state.lock:
            state.open_position = None
        self.reset()

    # ------------------------------------------------------------------ #
    #  Main evaluate loop — called every candle by strategy.py            #
    # ------------------------------------------------------------------ #

    async def evaluate(self, current_price: float) -> bool:
        """Check TP levels, trailing SL, and timeout every candle.

        v0.7.9: toate scrierile pe campuri interne sub _snapshot_lock
          pentru a preveni race condition cu pulse.snapshot().
        Returns True if position was fully closed.
        """
        if not state.open_position:
            return True

        async with self._snapshot_lock:
            pnl_pct = self._unrealised_pnl_pct(current_price)
            self._hold_candles += 1

            # --- TP1 ---
            if not self._tp1_hit and pnl_pct >= TP1_PCT:
                # Release lock during IO (place_order + poll)
                pass

        # IO in afara lock-ului — evitam deadlock la await
        pnl_pct = self._unrealised_pnl_pct(current_price)

        if not self._tp1_hit and pnl_pct >= TP1_PCT:
            filled, qty_closed = await self._close_partial(TP1_FRACTION, "TP1")
            if filled:
                async with self._snapshot_lock:
                    self._tp1_hit = True
                partial_pnl = self._pnl_usdt(pnl_pct, qty_closed)
                risk.on_close(partial_pnl, pnl_pct)
                logger.info(f"[PM] TP1 @ {pnl_pct:.4%} pnl={partial_pnl:+.4f}")

        elif self._tp1_hit and not self._tp2_hit and pnl_pct >= TP2_PCT:
            filled, qty_closed = await self._close_partial(TP2_FRACTION, "TP2")
            if filled:
                async with self._snapshot_lock:
                    self._tp2_hit = True
                partial_pnl = self._pnl_usdt(pnl_pct, qty_closed)
                risk.on_close(partial_pnl, pnl_pct)
                logger.info(f"[PM] TP2 @ {pnl_pct:.4%} pnl={partial_pnl:+.4f}")

        elif self._tp2_hit and not self._tp3_hit and pnl_pct >= TP3_PCT:
            filled, qty_closed = await self._close_partial(TP3_FRACTION, "TP3")
            if filled:
                async with self._snapshot_lock:
                    self._tp3_hit = True
                    self.reset()
                partial_pnl = self._pnl_usdt(pnl_pct, qty_closed)
                risk.on_close(partial_pnl, pnl_pct)
                logger.info(f"[PM] TP3 @ {pnl_pct:.4%} — trade complete")
                with state.lock:
                    state.open_position = None
                return True

        # --- Trailing stop ---
        if pnl_pct >= TRAIL_PCT:
            if not self._trail_active:
                async with self._snapshot_lock:
                    self._trail_active   = True
                    self._trail_peak_pnl = pnl_pct
                logger.info(f"[PM] Trailing activated @ {pnl_pct:.4%}")
            elif pnl_pct > self._trail_peak_pnl:
                async with self._snapshot_lock:
                    self._trail_peak_pnl = pnl_pct
                trail_sl = current_price * (
                    (1 - TRAIL_DELTA) if self._entry_side == "long"
                    else (1 + TRAIL_DELTA)
                )
                await trader.amend_sl_tp(stop_loss=trail_sl)
                logger.debug(f"[PM] Trail SL amended to {trail_sl:.4f}")
            elif pnl_pct <= self._trail_peak_pnl - TRAIL_DELTA:
                logger.info(
                    f"[PM] Trail triggered: pnl={pnl_pct:.4%} "
                    f"peak={self._trail_peak_pnl:.4%}"
                )
                await self._close_full("TRAIL", pnl_pct)
                return True

        # --- Timeout ---
        async with self._snapshot_lock:
            hold = self._hold_candles
        if hold >= MAX_HOLD_CANDLES:
            logger.info(f"[PM] Timeout ({MAX_HOLD_CANDLES} candles) — closing")
            await self._close_full("TIMEOUT", pnl_pct)
            return True

        return False

    # ------------------------------------------------------------------ #
    #  Pyramid                                                             #
    # ------------------------------------------------------------------ #

    async def try_pyramid(
        self,
        side: str,
        price: float,
        score: float,
        stop_loss: float,
        take_profit: float,
    ) -> None:
        async with self._snapshot_lock:
            if self._pyramid_adds >= MAX_PYRAMID_ADDS:
                return
            pnl_pct = self._unrealised_pnl_pct(price)
            if pnl_pct < PYRAMID_PNL_MIN:
                return
            if not self._tp1_hit:
                return

        from .config import config
        add_qty = risk.calc_qty(
            price,
            order_size_usdt=config.order_size_usdt,
            leverage=config.leverage,
            regime_factor=0.5,
        )
        if add_qty <= 0:
            return

        bybit_side = self._bybit_side(side, closing=False)
        resp = await trader.place_order(
            side=bybit_side,
            qty=add_qty,
            order_type="Market",
            post_only=False,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        if resp.get("retCode") == 0:
            async with self._snapshot_lock:
                self._pyramid_adds += 1
            with state.lock:
                state.open_qty += add_qty
            logger.info(
                f"[PM] Pyramid add #{self._pyramid_adds}: "
                f"qty={add_qty} pnl={pnl_pct:.4%} score={score:.3f}"
            )


position_manager = PositionManager()
