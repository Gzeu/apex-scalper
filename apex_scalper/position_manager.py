"""Position Manager v1.3.3 — on_open primeste sl_price din strategy (SL unic).

Changelog:
  v1.3.3 —
    FIX #2: on_open() primeste sl_price: float | None ca parametru optional.
      Daca sl_price e furnizat (din strategy._enter), il foloseste direct.
      Daca nu e furnizat (restart/sync), il calculeaza dinamic ca fallback.
      Elimina dubla suprasciere SL (strategy calcula unul, on_open alt unul).
    FIX #7: try_pyramid() apeleaza amend_sl_tp() cu noul SL dupa adaugare,
      recalculat din entry_price original (nu din price curent) pentru
      a mentine protectia corecta la qty total.
  v1.3.2 — on_open() seteaza SL pe exchange la entry.
  v1.3.1 — pyramid qty_step fix + close_partial floor.
  v1.3.0 — SL + Trailing dinamic pe ATR (50x safe).
  v1.2.1 — BUG CRITIC: _close_full() verifica retur + state guard.
  v1.2.0 — Breakeven SL dupa TP1, timeout smart exit.
  v1.1.0 — TP/SL/Trail citite din profil per symbol.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from loguru import logger

from .state import state
from .trader import trader, _api_call_with_retry
from .risk import risk
from .persistence import db

# --------------------------------------------------------------------------- #
#  Parametri fallback
# --------------------------------------------------------------------------- #
_DEFAULT_TP1_PCT       = 0.0030
_DEFAULT_TP2_PCT       = 0.0060
_DEFAULT_TP3_PCT       = 0.0100
_DEFAULT_TP1_FRACTION  = 0.40
_DEFAULT_TP2_FRACTION  = 0.30
_DEFAULT_TP3_FRACTION  = 0.30
_DEFAULT_SL_PCT        = 0.0020
_DEFAULT_TRAIL_PCT     = 0.0030
_DEFAULT_TRAIL_DELTA   = 0.0010
_DEFAULT_MAX_HOLD      = 4
_DEFAULT_MAX_PYRAMID   = 0

# SL dinamic ATR - limite de siguranta
_SL_ATR_MULT       = 1.5
_SL_PCT_MIN        = 0.0015
_SL_PCT_MAX        = 0.0040

# Trailing dinamic ATR - limite de siguranta
_TRAIL_ATR_MULT    = 1.0
_TRAIL_DELTA_MIN   = 0.0008
_TRAIL_DELTA_MAX   = 0.0050

# Comision Bybit taker dus-intors
ROUND_TRIP_FEE  = 0.00055 * 2   # 0.0011
_TIMEOUT_GRACE  = 2

PYRAMID_SCORE_MIN     = 0.70
PYRAMID_PNL_MIN       = 0.0010
PYRAMID_MARGIN_BUFFER = 1.5
CONFIRM_POLL_INTERVAL = 0.5
CONFIRM_POLL_MAX      = 8

MAX_HOLD_CANDLES = _DEFAULT_MAX_HOLD


def _get_profile() -> dict:
    try:
        from .config import config
        return config.profile(config.symbol)
    except Exception:
        return {}


def _p(key: str, default):
    return _get_profile().get(key, default)


def _get_current_atr_pct() -> float:
    try:
        from .strategy import ind
        if ind.atr_ready and ind.atr_value > 0 and ind.last_price > 0:
            return ind.atr_value / ind.last_price
    except Exception:
        pass
    return 0.0


def _dynamic_sl_pct() -> float:
    atr_pct = _get_current_atr_pct()
    if atr_pct <= 0:
        return _DEFAULT_SL_PCT
    sl = atr_pct * _SL_ATR_MULT
    return max(_SL_PCT_MIN, min(_SL_PCT_MAX, sl))


def _dynamic_trail_delta() -> float:
    atr_pct = _get_current_atr_pct()
    if atr_pct <= 0:
        return _DEFAULT_TRAIL_DELTA
    delta = atr_pct * _TRAIL_ATR_MULT
    return max(_TRAIL_DELTA_MIN, min(_TRAIL_DELTA_MAX, delta))


def _floor_to_qty_step(raw_qty: float) -> float:
    """Rotunjeste qty la floor de qty_step al instrumentului."""
    qty_step = trader._qty_step
    if qty_step <= 0:
        return raw_qty
    qty = math.floor(raw_qty / qty_step) * qty_step
    return max(qty, qty_step)


def _tg_notify(coro) -> None:
    try:
        asyncio.ensure_future(coro)
    except Exception as e:
        logger.debug(f"[PM] tg_notify schedule error: {e}")


@dataclass
class PositionSnapshot:
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
    breakeven_set: bool

    def unrealised_pnl_pct(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        if self.entry_side == "long":
            return (current_price - self.entry_price) / self.entry_price
        return (self.entry_price - current_price) / self.entry_price


async def _confirm_order_filled(order_id: str, sym: str) -> bool:
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
        self._breakeven_set  = False
        self._snapshot_lock  = asyncio.Lock()

    def _reset_fields(self) -> None:
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
        self._breakeven_set  = False

    def reset(self) -> None:
        self._reset_fields()

    async def snapshot(self) -> PositionSnapshot:
        async with self._snapshot_lock:
            return PositionSnapshot(
                entry_price   = self._entry_price,
                entry_side    = self._entry_side,
                entry_qty     = self._entry_qty,
                tp1_hit       = self._tp1_hit,
                tp2_hit       = self._tp2_hit,
                tp3_hit       = self._tp3_hit,
                trail_active  = self._trail_active,
                trail_peak    = self._trail_peak_pnl,
                hold_candles  = self._hold_candles,
                pyramid_adds  = self._pyramid_adds,
                trade_id      = self._trade_id,
                breakeven_set = self._breakeven_set,
            )

    async def on_open(
        self,
        side: str,
        qty: float,
        entry_price: float,
        trade_id: int | None = None,
        sl_price: float | None = None,  # FIX #2: SL gata calculat din strategy
    ) -> None:
        prof    = _get_profile()
        atr_pct = _get_current_atr_pct()

        async with self._snapshot_lock:
            self._reset_fields()
            self._entry_side  = side
            self._entry_qty   = qty
            self._entry_price = entry_price
            self._trade_id    = trade_id

        # FIX #2: folosim sl_price din strategy daca e furnizat
        # altfel calcul dinamic ca fallback (ex: restart/sync)
        if sl_price is None:
            sl_pct   = _dynamic_sl_pct()
            sl_price = entry_price * (1.0 - sl_pct if side == "long" else 1.0 + sl_pct)
            logger.debug(f"[PM] sl_price calculat dinamic (fallback): {sl_price:.6f}")
        else:
            sl_pct = abs(entry_price - sl_price) / entry_price if entry_price > 0 else _dynamic_sl_pct()

        logger.info(
            f"[PM] Entry: side={side} qty={qty} avg_price={entry_price} "
            f"| sl={sl_price:.6f} ({sl_pct:.4%}) "
            f"| ATR={atr_pct:.4%} | lev={prof.get('leverage', '?')}x"
        )

        # SL pe exchange — protectie crash bot
        try:
            resp = await trader.amend_sl_tp(stop_loss=sl_price)
            if resp and resp.get("retCode") == 0:
                logger.info(f"[PM] SL exchange confirmat: {sl_price:.6f}")
            else:
                logger.warning(
                    f"[PM] SL exchange ESUAT: {resp} "
                    f"| sl_price={sl_price:.6f} — only software SL active!"
                )
        except Exception as e:
            logger.warning(f"[PM] SL exchange exceptie: {e} — only software SL active!")

    def _unrealised_pnl_pct(self, current_price: float) -> float:
        if self._entry_price <= 0:
            return 0.0
        if self._entry_side == "long":
            return (current_price - self._entry_price) / self._entry_price
        return (self._entry_price - current_price) / self._entry_price

    def _pnl_usdt(self, pnl_pct: float, qty: float, entry_price: float | None = None) -> float:
        ep = entry_price if entry_price is not None else self._entry_price
        return pnl_pct * qty * ep

    def _bybit_side(self, position_side: str, closing: bool) -> str:
        if position_side == "long":
            return "Sell" if closing else "Buy"
        return "Buy" if closing else "Sell"

    def _sym(self) -> str:
        from .config import config
        return config.symbol

    async def _set_breakeven_sl(self) -> None:
        if self._breakeven_set or self._entry_price <= 0:
            return
        if self._entry_side == "long":
            be_price = self._entry_price * (1.0 + ROUND_TRIP_FEE)
        else:
            be_price = self._entry_price * (1.0 - ROUND_TRIP_FEE)
        if be_price <= 0:
            logger.warning(f"[PM] Breakeven SL invalid: {be_price} — skip")
            return
        try:
            resp = await trader.amend_sl_tp(stop_loss=be_price)
            if resp and resp.get("retCode") == 0:
                async with self._snapshot_lock:
                    self._breakeven_set = True
                logger.info(
                    f"[PM] Breakeven SL setat: {be_price:.6f} "
                    f"(entry={self._entry_price:.6f} fee={ROUND_TRIP_FEE:.4%})"
                )
            else:
                logger.warning(f"[PM] Breakeven SL amend esuat: {resp}")
        except Exception as e:
            logger.warning(f"[PM] Breakeven SL exceptie: {e}")

    async def _close_partial(self, fraction: float, label: str) -> tuple[bool, float]:
        sym = self._sym()
        with state.lock:
            open_qty   = state.open_qty
            last_price = state.last_price

        raw_qty = open_qty * fraction
        qty     = _floor_to_qty_step(raw_qty)

        if qty <= 0:
            logger.warning(f"[PM] {label}: qty=0, skipping")
            return False, 0.0

        close_side = self._bybit_side(self._entry_side, closing=True)

        resp = await trader.place_order(
            side=close_side, qty=qty,
            order_type="Limit", post_only=False,
            price=last_price, reduce_only=True,
        )
        if resp.get("retCode") != 0:
            logger.error(f"[PM] {label} limit rejected: {resp.get('retMsg')}")
            fb = await trader.place_order(
                side=close_side, qty=qty,
                order_type="Market", post_only=False,
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
                side=close_side, qty=qty,
                order_type="Market", post_only=False,
                reduce_only=True,
            )
            filled = fb.get("retCode") == 0

        if filled:
            with state.lock:
                state.open_qty = max(0.0, state.open_qty - qty)
            logger.info(f"[PM] {label} filled: qty={qty} remaining={state.open_qty}")

        return filled, qty if filled else 0.0

    async def _close_full(self, reason: str, pnl_pct: float) -> bool:
        with state.lock:
            remaining_qty = state.open_qty
        side     = self._entry_side
        pnl_usdt = self._pnl_usdt(pnl_pct, remaining_qty)

        closed = await trader.close_position()

        if not closed:
            logger.critical(
                f"[PM] _close_full({reason}) ESUAT — pozitia RAMANE deschisa pe exchange! "
                f"pnl={pnl_pct:.4%} ({pnl_usdt:+.4f} USDT) — bot OPRIT din siguranta"
            )
            try:
                from .telegram_ui import send_message
                _tg_notify(send_message(
                    f"\U0001f6a8 *CRITIC: Close ESUAT* ({reason})\n"
                    f"`{side} qty={remaining_qty} pnl={pnl_usdt:+.4f} USDT`\n"
                    f"Pozitia poate fi INCA DESCHISA. Verifica manual!"
                ))
            except Exception:
                pass
            with state.lock:
                state.paused = True
            return False

        risk.on_close(pnl_usdt, pnl_pct)
        logger.info(f"[PM] Full close ({reason}): pnl={pnl_pct:.4%} ({pnl_usdt:+.4f} USDT)")
        with state.lock:
            state.open_position = None
            state.open_qty      = 0.0
            state.open_entry    = 0.0
            state.trailing_stop = 0.0
        async with self._snapshot_lock:
            self._reset_fields()

        try:
            from .telegram_ui import notify_sl, notify_close
            if reason in ("SL_SOFTWARE", "SL_EXCHANGE"):
                _tg_notify(notify_sl(side, remaining_qty, pnl_usdt))
            else:
                _tg_notify(notify_close(side, remaining_qty, pnl_usdt, reason))
        except Exception as e:
            logger.debug(f"[PM] tg import error: {e}")

        return True

    async def evaluate(self, current_price: float) -> bool:
        if not state.open_position:
            return True

        tp1_pct      = _p("tp1_pct",         _DEFAULT_TP1_PCT)
        tp2_pct      = _p("tp2_pct",         _DEFAULT_TP2_PCT)
        tp3_pct      = _p("tp3_pct",         _DEFAULT_TP3_PCT)
        tp1_fraction = _p("tp1_fraction",     _DEFAULT_TP1_FRACTION)
        tp2_fraction = _p("tp2_fraction",     _DEFAULT_TP2_FRACTION)
        tp3_fraction = _p("tp3_fraction",     _DEFAULT_TP3_FRACTION)
        trail_pct    = _p("trail_pct",        _DEFAULT_TRAIL_PCT)
        max_hold     = _p("max_hold_candles", _DEFAULT_MAX_HOLD)

        sl_pct      = _dynamic_sl_pct()
        trail_delta = _dynamic_trail_delta()

        async with self._snapshot_lock:
            self._hold_candles += 1
            hold            = self._hold_candles
            entry_price_now = self._entry_price
            be_already_set  = self._breakeven_set

        pnl_pct = self._unrealised_pnl_pct(current_price)

        # --- Software SL dinamic pe ATR ---
        if not be_already_set and pnl_pct <= -sl_pct:
            atr_pct = _get_current_atr_pct()
            logger.warning(
                f"[PM] Software SL dinamic: pnl={pnl_pct:.4%} <= -{sl_pct:.4%} "
                f"(ATR={atr_pct:.4%} x{_SL_ATR_MULT})"
            )
            return await self._close_full("SL_SOFTWARE", pnl_pct)

        # --- TP1 ---
        if not self._tp1_hit and pnl_pct >= tp1_pct:
            filled, qty_closed = await self._close_partial(tp1_fraction, "TP1")
            if filled:
                async with self._snapshot_lock:
                    self._tp1_hit = True
                partial_pnl = self._pnl_usdt(pnl_pct, qty_closed, entry_price_now)
                risk.on_close(partial_pnl, pnl_pct)
                logger.info(f"[PM] TP1 @ {pnl_pct:.4%} pnl={partial_pnl:+.4f}")
                await self._set_breakeven_sl()
                try:
                    from .telegram_ui import notify_tp
                    _tg_notify(notify_tp(self._entry_side, 1, qty_closed, partial_pnl))
                except Exception as e:
                    logger.debug(f"[PM] tg notify_tp1 error: {e}")

        # --- TP2 ---
        elif self._tp1_hit and not self._tp2_hit and pnl_pct >= tp2_pct:
            filled, qty_closed = await self._close_partial(tp2_fraction, "TP2")
            if filled:
                async with self._snapshot_lock:
                    self._tp2_hit = True
                partial_pnl = self._pnl_usdt(pnl_pct, qty_closed, entry_price_now)
                risk.on_close(partial_pnl, pnl_pct)
                logger.info(f"[PM] TP2 @ {pnl_pct:.4%} pnl={partial_pnl:+.4f}")
                try:
                    from .telegram_ui import notify_tp
                    _tg_notify(notify_tp(self._entry_side, 2, qty_closed, partial_pnl))
                except Exception as e:
                    logger.debug(f"[PM] tg notify_tp2 error: {e}")

        # --- TP3 ---
        elif self._tp2_hit and not self._tp3_hit and pnl_pct >= tp3_pct:
            filled, qty_closed = await self._close_partial(tp3_fraction, "TP3")
            if filled:
                saved_entry = self._entry_price
                saved_side  = self._entry_side
                partial_pnl = self._pnl_usdt(pnl_pct, qty_closed, saved_entry)
                async with self._snapshot_lock:
                    self._tp3_hit = True
                    self._reset_fields()
                risk.on_close(partial_pnl, pnl_pct)
                logger.info(f"[PM] TP3 @ {pnl_pct:.4%} pnl={partial_pnl:+.4f} — trade complete")
                with state.lock:
                    state.open_position = None
                    state.open_qty      = 0.0
                    state.open_entry    = 0.0
                    state.trailing_stop = 0.0
                try:
                    from .telegram_ui import notify_tp
                    _tg_notify(notify_tp(saved_side, 3, qty_closed, partial_pnl))
                except Exception as e:
                    logger.debug(f"[PM] tg notify_tp3 error: {e}")
                return True

        tp1_now = self._tp1_hit
        be_now  = self._breakeven_set

        # --- Trailing stop dinamic pe ATR ---
        if pnl_pct >= trail_pct:
            if not self._trail_active:
                async with self._snapshot_lock:
                    self._trail_active   = True
                    self._trail_peak_pnl = pnl_pct
                logger.info(
                    f"[PM] Trailing activat @ {pnl_pct:.4%} "
                    f"| trail_delta={trail_delta:.4%} (ATR-based)"
                )
            elif pnl_pct > self._trail_peak_pnl:
                async with self._snapshot_lock:
                    self._trail_peak_pnl = pnl_pct
                trail_sl = current_price * (
                    (1.0 - trail_delta) if self._entry_side == "long"
                    else (1.0 + trail_delta)
                )
                await trader.amend_sl_tp(stop_loss=trail_sl)
                logger.debug(f"[PM] Trail SL -> {trail_sl:.6f} (delta={trail_delta:.4%})")
            elif pnl_pct <= self._trail_peak_pnl - trail_delta:
                logger.info(
                    f"[PM] Trail triggered: pnl={pnl_pct:.4%} "
                    f"peak={self._trail_peak_pnl:.4%} delta={trail_delta:.4%}"
                )
                return await self._close_full("TRAIL", pnl_pct)

        # --- Timeout smart ---
        if hold >= max_hold:
            if not tp1_now and pnl_pct < 0:
                if hold < max_hold + _TIMEOUT_GRACE:
                    logger.debug(
                        f"[PM] Timeout grace: hold={hold}/{max_hold + _TIMEOUT_GRACE} "
                        f"pnl={pnl_pct:.4%}"
                    )
                    return False
                else:
                    logger.info(f"[PM] Timeout FORTAT ({hold} candle-uri) pnl={pnl_pct:.4%}")
                    return await self._close_full("TIMEOUT_FORCED", pnl_pct)
            else:
                label = "TIMEOUT_PROFIT" if pnl_pct >= 0 else "TIMEOUT_BE"
                logger.info(f"[PM] {label}: hold={hold} pnl={pnl_pct:.4%}")
                return await self._close_full(label, pnl_pct)

        return False

    async def try_pyramid(
        self,
        side: str,
        price: float,
        score: float,
        stop_loss: float,
        take_profit: float,
    ) -> None:
        """Adauga la pozitie existenta (pyramid).

        FIX #7 v1.3.3: dupa adaugare, apeleaza amend_sl_tp() cu SL recalculat
        din entry_price original. Inainte SL pe exchange ramanea la entry
        initial, neactualizat pentru qty total.
        """
        max_pyramid = _p("max_pyramid_adds", _DEFAULT_MAX_PYRAMID)
        async with self._snapshot_lock:
            if self._pyramid_adds >= max_pyramid:
                return
            pnl_pct = self._unrealised_pnl_pct(price)
            if pnl_pct < PYRAMID_PNL_MIN:
                return
            if not self._tp1_hit:
                return
            original_entry = self._entry_price
            original_side  = self._entry_side

        from .config import config
        add_qty = risk.calc_qty(
            price,
            order_size_usdt = config.order_size_usdt,
            leverage        = config.leverage,
            qty_step        = trader._qty_step,
            regime_factor   = 0.5,
        )
        if add_qty <= 0:
            return

        required_margin   = config.order_size_usdt * PYRAMID_MARGIN_BUFFER
        available_balance = await trader.get_balance()

        if available_balance < required_margin:
            logger.warning(
                f"[PM] Pyramid skipped — balance={available_balance:.2f} "
                f"< required={required_margin:.2f} USDT"
            )
            return

        bybit_side = self._bybit_side(side, closing=False)
        resp = await trader.place_order(
            side=bybit_side, qty=add_qty,
            order_type="Market", post_only=False,
            stop_loss=stop_loss, take_profit=take_profit,
        )
        if resp.get("retCode") == 0:
            async with self._snapshot_lock:
                self._pyramid_adds += 1
            with state.lock:
                state.open_qty += add_qty
            logger.info(
                f"[PM] Pyramid add #{self._pyramid_adds}: "
                f"qty={add_qty} qty_step={trader._qty_step} "
                f"pnl={pnl_pct:.4%} score={score:.3f}"
            )

            # FIX #7: actualizeaza SL pe exchange dupa adaugare
            # recalculat din entry_price original pentru consistenta
            sl_pct = _dynamic_sl_pct()
            if original_side == "long":
                new_sl = original_entry * (1.0 - sl_pct)
            else:
                new_sl = original_entry * (1.0 + sl_pct)
            try:
                sl_resp = await trader.amend_sl_tp(stop_loss=new_sl)
                if sl_resp and sl_resp.get("retCode") == 0:
                    logger.info(
                        f"[PM] Pyramid SL actualizat: {new_sl:.6f} "
                        f"({sl_pct:.4%} din entry={original_entry:.6f})"
                    )
                else:
                    logger.warning(f"[PM] Pyramid SL amend esuat: {sl_resp}")
            except Exception as e:
                logger.warning(f"[PM] Pyramid SL exceptie: {e}")


position_manager = PositionManager()
