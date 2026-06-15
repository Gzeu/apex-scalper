"""Position Manager v0.9.4 — Improvement #2: pyramid margin check.

Changelog:
  v0.9.4 — Improvement #2: try_pyramid() verifica margin disponibil inainte
    de a plasa al doilea ordin.
    Vechi: MAX_PYRAMID_ADDS limita numarul de adaugiri dar nu verifica daca
    exista margin suficient. La 100x leverage, un al doilea ordin de 20 USDT
    necesita cel putin 20 USDT margin liber; daca nu exista, exchange-ul
    respinge ordinul cu 'insufficient balance' sau mai rau: margin call instant.
    Nou: inainte de place_order(), get_balance() e apelat.
    Daca balance < order_size_usdt * PYRAMID_MARGIN_BUFFER (1.5x safety),
    pyramid-ul e sarit cu log WARNING si mesaj Telegram.
  v0.8.6 — BUG 27 FIX: Software SL in evaluate().
  v0.8.6 — BUG 28 FIX: _close_full() reseteaza explicit state.open_qty.
  v0.8.0 — BUG 1/2/5 FIX.
  v0.7.9 — race-condition fix (PositionSnapshot + lock).
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
#  Parameters                                                                  #
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
PYRAMID_MARGIN_BUFFER  = 1.5   # safety factor: balance trebuie sa fie >= cost * 1.5
CONFIRM_POLL_INTERVAL  = 0.5
CONFIRM_POLL_MAX       = 8


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

    def reset(self) -> None:
        self._reset_fields()

    async def snapshot(self) -> PositionSnapshot:
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

    async def on_open(
        self,
        side: str,
        qty: float,
        entry_price: float,
        trade_id: int | None = None,
    ) -> None:
        async with self._snapshot_lock:
            self._reset_fields()
            self._entry_side  = side
            self._entry_qty   = qty
            self._entry_price = entry_price
            self._trade_id    = trade_id
        logger.info(f"[PM] Entry: side={side} qty={qty} price={entry_price}")

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

    async def _close_partial(self, fraction: float, label: str) -> tuple[bool, float]:
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
            state.open_qty      = 0.0
            state.open_entry    = 0.0
            state.trailing_stop = 0.0
        async with self._snapshot_lock:
            self._reset_fields()

    async def evaluate(self, current_price: float) -> bool:
        if not state.open_position:
            return True

        async with self._snapshot_lock:
            self._hold_candles += 1
            hold = self._hold_candles
            entry_price_now = self._entry_price

        pnl_pct = self._unrealised_pnl_pct(current_price)

        # Software SL
        if pnl_pct <= -SL_PCT:
            logger.warning(
                f"[PM] Software SL triggered: pnl={pnl_pct:.4%} <= -{SL_PCT:.4%}"
            )
            await self._close_full("SL_SOFTWARE", pnl_pct)
            return True

        # TP1
        if not self._tp1_hit and pnl_pct >= TP1_PCT:
            filled, qty_closed = await self._close_partial(TP1_FRACTION, "TP1")
            if filled:
                async with self._snapshot_lock:
                    self._tp1_hit = True
                partial_pnl = self._pnl_usdt(pnl_pct, qty_closed, entry_price_now)
                risk.on_close(partial_pnl, pnl_pct)
                logger.info(f"[PM] TP1 @ {pnl_pct:.4%} pnl={partial_pnl:+.4f}")

        elif self._tp1_hit and not self._tp2_hit and pnl_pct >= TP2_PCT:
            filled, qty_closed = await self._close_partial(TP2_FRACTION, "TP2")
            if filled:
                async with self._snapshot_lock:
                    self._tp2_hit = True
                partial_pnl = self._pnl_usdt(pnl_pct, qty_closed, entry_price_now)
                risk.on_close(partial_pnl, pnl_pct)
                logger.info(f"[PM] TP2 @ {pnl_pct:.4%} pnl={partial_pnl:+.4f}")

        elif self._tp2_hit and not self._tp3_hit and pnl_pct >= TP3_PCT:
            filled, qty_closed = await self._close_partial(TP3_FRACTION, "TP3")
            if filled:
                saved_entry = self._entry_price
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
                return True

        # Trailing stop
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

        # Timeout
        if hold >= MAX_HOLD_CANDLES:
            logger.info(f"[PM] Timeout ({MAX_HOLD_CANDLES} candles) — closing")
            await self._close_full("TIMEOUT", pnl_pct)
            return True

        return False

    async def try_pyramid(
        self,
        side: str,
        price: float,
        score: float,
        stop_loss: float,
        take_profit: float,
    ) -> None:
        """Adauga la pozitia curenta (pyramid) cu verificare margin.

        Improvement #2: inainte de place_order(), verifica ca balance disponibil
        este suficient pentru costul ordinului de pyramid (cu safety buffer 1.5x).
        La 100x leverage, fiecare ordin de 20 USDT necesita 0.2 USDT margin +
        fees, dar in practica exchange-ul poate face margin call instant daca
        contul e aproape de limita. Buffer-ul de 1.5x acoperit si volatilitatea.

        Daca balance insuficient: log WARNING + alert Telegram + pyramid sarit.
        """
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

        # --- Improvement #2: margin check ---
        required_margin = config.order_size_usdt * PYRAMID_MARGIN_BUFFER
        available_balance = await trader.get_balance()

        if available_balance < required_margin:
            msg = (
                f"\u26a0\ufe0f *Pyramid sarit — margin insuficient*\n"
                f"Disponibil: `{available_balance:.2f} USDT`\n"
                f"Necesar (cu buffer 1.5x): `{required_margin:.2f} USDT`\n"
                f"Order size: `{config.order_size_usdt} USDT` @ {config.leverage}x"
            )
            logger.warning(
                f"[PM] Pyramid skipped — insufficient margin: "
                f"balance={available_balance:.2f} USDT < required={required_margin:.2f} USDT"
            )
            try:
                from .telegram_ui import send_message
                await send_message(msg)
            except Exception:
                pass
            return
        # --- end margin check ---

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
                f"qty={add_qty} pnl={pnl_pct:.4%} score={score:.3f} "
                f"balance_before={available_balance:.2f} USDT"
            )


position_manager = PositionManager()
