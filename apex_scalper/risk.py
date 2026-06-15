"""Risk manager v0.9.0 — drawdown-contingent position sizing.

Changelog:
  v0.9.0 — FEATURE: drawdown-contingent sizing (recomandare r/algotrading).
    PROBLEMA: bot-ul se oprea complet la 5 consecutive losses (prea agresiv).
    Comunitatea recomanda: reduce size treptat, nu oprire totala.
    FIX: size_factor() returneaza un multiplicator bazat pe consecutive losses:
      0 losses  -> 1.00x (normal)
      1 loss    -> 1.00x (nu penalizam primul)
      2 losses  -> 0.60x (60% din order size)
      3 losses  -> 0.40x (40%)
      4 losses  -> 0.25x (25% — minim functional)
      5+ losses -> 0.00x -> can_open() returneaza False (oprire ca inainte)
    BONUS: dupa primul TP post-drawdown, size_factor revine la 1.0x automat.
    Rezultat: in loc de stop complet la 3 SL-uri consecutive, bot-ul
    continua cu 40% size, limiteaza paguba si poate recupera.
  v0.8.7 — qty_step fix in calc_qty().
  v0.8.6 — reset_daily() reseteaza consecutive_losses.
  v0.8.1 — kelly_f property.
  v0.8.0 — Kelly formula corecta.
  v0.7.1 — reset_daily fix, MAX_CONSECUTIVE_LOSSES, partial close tracking.
"""
from __future__ import annotations

import math
import os
import threading
from collections import deque
from loguru import logger

MAX_DAILY_LOSS         = float(os.getenv("MAX_DAILY_LOSS_USDT",      "50.0"))
MAX_OPEN_POSITIONS     = int(os.getenv("MAX_OPEN_POSITIONS",         "1"))
MAX_SPREAD_BPS         = float(os.getenv("MAX_SPREAD_BPS",           "5.0"))
MIN_BID_DEPTH          = float(os.getenv("MIN_BID_DEPTH",            "10000"))
MIN_ASK_DEPTH          = float(os.getenv("MIN_ASK_DEPTH",            "10000"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES",     "5"))

KELLY_FRACTION      = float(os.getenv("KELLY_FRACTION",       "0.5"))
KELLY_LOOKBACK      = int(os.getenv("KELLY_LOOKBACK",         "50"))
MIN_KELLY_TRADES    = int(os.getenv("MIN_KELLY_TRADES",       "20"))
MIN_KELLY_F         = float(os.getenv("MIN_KELLY_F",          "0.30"))
MAX_KELLY_F         = float(os.getenv("MAX_KELLY_F",          "1.80"))

# Drawdown-contingent sizing: consecutive losses -> size multiplier
# Index = numar de pierderi consecutive (0-4)
# La MAX_CONSECUTIVE_LOSSES (5+) -> can_open() returneaza False (stop total)
_DRAWDOWN_SIZE_TABLE = [
    1.00,  # 0 losses  — normal
    1.00,  # 1 loss    — primul nu conteaza, nu penalizam
    0.60,  # 2 losses  — reducem la 60%
    0.40,  # 3 losses  — reducem la 40%
    0.25,  # 4 losses  — minim functional (25%)
]


class RiskManager:
    def __init__(self):
        self._lock               = threading.Lock()
        self._daily_loss         = 0.0
        self._daily_limit        = MAX_DAILY_LOSS
        self._open_count         = 0
        self._consecutive_losses = 0
        self._trade_results: deque = deque(maxlen=KELLY_LOOKBACK)

    def can_open(self) -> bool:
        with self._lock:
            if self._daily_loss >= self._daily_limit:
                logger.warning(f"Daily loss limit hit: {self._daily_loss:.2f}/{self._daily_limit:.2f}")
                return False
            if self._open_count >= MAX_OPEN_POSITIONS:
                return False
            if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                logger.warning(
                    f"Consecutive losses={self._consecutive_losses} >= "
                    f"MAX_CONSECUTIVE_LOSSES={MAX_CONSECUTIVE_LOSSES} — paused"
                )
                return False
        return True

    def drawdown_size_factor(self) -> float:
        """Returneaza multiplicatorul de size bazat pe consecutive losses.

        v0.9.0: in loc sa oprim bot-ul la 2-3 pierderi, reducem size-ul
        treptat. Bot-ul continua sa tranzactioneze dar cu risc redus.
        Tabela _DRAWDOWN_SIZE_TABLE mapeaza consecutive_losses -> factor.
        """
        with self._lock:
            n = min(self._consecutive_losses, len(_DRAWDOWN_SIZE_TABLE) - 1)
            factor = _DRAWDOWN_SIZE_TABLE[n]
        if self._consecutive_losses > 0 and factor < 1.0:
            logger.debug(
                f"[Risk] Drawdown sizing: {self._consecutive_losses} losses "
                f"-> size_factor={factor:.2f}x"
            )
        return factor

    def on_open(self) -> None:
        with self._lock:
            self._open_count += 1

    def on_close(self, pnl_usdt: float, pnl_pct: float) -> None:
        with self._lock:
            if pnl_usdt < 0:
                self._daily_loss         += abs(pnl_usdt)
                self._consecutive_losses += 1
                logger.info(
                    f"[Risk] Loss #{self._consecutive_losses}: {pnl_usdt:.4f} USDT "
                    f"| next size_factor={_DRAWDOWN_SIZE_TABLE[min(self._consecutive_losses, len(_DRAWDOWN_SIZE_TABLE)-1)]:.2f}x"
                )
            else:
                if self._consecutive_losses > 0:
                    logger.info(
                        f"[Risk] TP dupa {self._consecutive_losses} losses — "
                        f"size_factor revine la 1.00x"
                    )
                self._consecutive_losses = 0
            self._open_count = max(0, self._open_count - 1)
            self._trade_results.append({
                "pnl_pct": pnl_pct,
                "win":     pnl_usdt > 0,
            })

    def update_pnl(self, pnl_usdt: float, pnl_pct: float = 0.0) -> None:
        self.on_close(pnl_usdt, pnl_pct)

    def _kelly_factor(self) -> float:
        """Half-Kelly sizing factor din trade history recent."""
        trades = list(self._trade_results)
        if len(trades) < MIN_KELLY_TRADES:
            return 1.0

        wins   = [t for t in trades if t["win"]]
        losses = [t for t in trades if not t["win"]]

        if not wins or not losses:
            return MIN_KELLY_F if not wins else MAX_KELLY_F

        win_rate  = len(wins)  / len(trades)
        loss_rate = len(losses) / len(trades)

        avg_win  = sum(abs(t["pnl_pct"]) for t in wins)  / len(wins)
        avg_loss = sum(abs(t["pnl_pct"]) for t in losses) / len(losses)

        if avg_win == 0:
            return MIN_KELLY_F

        f = win_rate - loss_rate * (avg_loss / avg_win)
        f = f * KELLY_FRACTION
        f = max(MIN_KELLY_F, min(MAX_KELLY_F, f))
        return f

    @property
    def kelly_f(self) -> float:
        with self._lock:
            return self._kelly_factor()

    def calc_qty(
        self,
        price: float,
        order_size_usdt: float = 0.0,
        leverage: float = 1.0,
        qty_step: float = 0.001,
        regime_factor: float = 1.0,
    ) -> float:
        """Calculeaza qty rotunjit la qty_step.

        v0.9.0: aplica drawdown_size_factor() INAINTE de Kelly.
        Ordinea: order_size * drawdown_factor * kelly_f * regime_factor
        Drawdown factor reduce size-ul in streaks de pierderi.
        """
        if price <= 0 or order_size_usdt <= 0:
            return 0.0

        with self._lock:
            kelly_f = self._kelly_factor()

        drawdown_f     = self.drawdown_size_factor()
        effective_usdt = order_size_usdt * drawdown_f * kelly_f * regime_factor
        notional       = effective_usdt * leverage
        raw_qty        = notional / price

        if qty_step > 0:
            qty = math.floor(raw_qty / qty_step) * qty_step
        else:
            qty = raw_qty

        qty = max(qty, qty_step)

        logger.debug(
            f"[Risk] sizing: base={order_size_usdt} drawdown={drawdown_f:.2f} "
            f"kelly={kelly_f:.3f} regime={regime_factor:.2f} "
            f"raw={raw_qty:.4f} qty={qty}"
        )
        return qty

    def reset_daily(self) -> None:
        with self._lock:
            self._daily_loss         = 0.0
            self._open_count         = 0
            self._consecutive_losses = 0
        logger.info("RiskManager: daily reset (loss + open_count + consecutive_losses)")

    @property
    def consecutive_losses(self) -> int:
        with self._lock:
            return self._consecutive_losses

    def reset_consecutive_losses(self) -> None:
        with self._lock:
            self._consecutive_losses = 0
        logger.info("Consecutive losses counter reset manually")


risk = RiskManager()
