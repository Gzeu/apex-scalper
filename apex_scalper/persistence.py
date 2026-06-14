"""SQLite persistence v0.8.2 — Bug 14+15 fix: fallback complet in close_trade_record().

Changelog:
  v0.8.2 — BUG 14 FIX: close_trade_record() fallback scria symbol='', side='', entry=0
    -> rand corupt in DB -> analytics/daily_report primeau date murdare.
    Fix: close_trade_record() accepta symbol, side, entry, qty optionali
    si le foloseste in fallback-ul record_trade() cu valorile corecte.

    BUG 15 FIX: fallback nu actualiza daily_pnl pentru simbolul corect
    (symbol='' -> load_daily_pnl(symbol) nu gasea randul).
    Fix: symbol corect transmis in record_trade() din fallback.

  v0.4.1 — WAL connection + correlated trade records (FIX #3 + #9).
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone, date
from typing import Optional
from loguru import logger

DB_PATH = "data/apex_scalper.db"


class Database:
    def __init__(self, path: str = DB_PATH):
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._conn_obj: sqlite3.Connection = sqlite3.connect(
            self._path, check_same_thread=False
        )
        self._conn_obj.row_factory = sqlite3.Row
        self._conn_obj.execute("PRAGMA journal_mode=WAL")
        self._conn_obj.execute("PRAGMA synchronous=NORMAL")
        self._conn_obj.commit()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        return self._conn_obj

    def _init_schema(self) -> None:
        with self._lock:
            self._conn_obj.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           INTEGER NOT NULL,
                    symbol       TEXT NOT NULL,
                    side         TEXT NOT NULL,
                    entry        REAL NOT NULL,
                    exit_price   REAL NOT NULL,
                    qty          REAL NOT NULL,
                    pnl_usdt     REAL NOT NULL,
                    pnl_pct      REAL NOT NULL,
                    reason       TEXT,
                    signal_score REAL,
                    funding_rate REAL
                );
                CREATE TABLE IF NOT EXISTS daily_pnl (
                    date         TEXT PRIMARY KEY,
                    symbol       TEXT NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    total_trades INTEGER NOT NULL DEFAULT 0,
                    win_trades   INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS metrics_snapshot (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           INTEGER NOT NULL,
                    symbol       TEXT NOT NULL,
                    sharpe       REAL,
                    max_dd       REAL,
                    profit_factor REAL,
                    total_pnl    REAL
                );
            """)
            self._conn_obj.commit()
        logger.info(f"SQLite DB initialised (WAL) at {self._path}")

    def record_open_trade(
        self,
        symbol: str,
        side: str,
        entry: float,
        qty: float,
        signal_score: float = 0.0,
        funding_rate: float = 0.0,
    ) -> int:
        """Insert an OPEN trade row. Returns the row id for later update."""
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        with self._lock:
            cur = self._conn_obj.execute(
                """
                INSERT INTO trades
                (ts, symbol, side, entry, exit_price, qty, pnl_usdt,
                 pnl_pct, reason, signal_score, funding_rate)
                VALUES (?,?,?,?,0,?,0,0,'OPEN',?,?)
                """,
                (ts, symbol, side, entry, qty, signal_score, funding_rate),
            )
            self._conn_obj.commit()
            return cur.lastrowid or 0

    def close_trade_record(
        self,
        trade_id: int,
        exit_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        reason: str,
        # BUG 14+15 FIX: parametri optionali pentru fallback complet
        symbol: str = "",
        side: str = "",
        entry: float = 0.0,
        qty: float = 0.0,
    ) -> None:
        """Update the OPEN row with exit data.

        v0.8.2 BUG 14+15 FIX:
          Fallback foloseste acum symbol/side/entry/qty corecte in loc de
          valori goale, astfel incat daily_pnl e actualizat corect pentru
          simbolul tranzactionat si randul din DB e complet.
        """
        if trade_id and trade_id > 0:
            with self._lock:
                rows_affected = self._conn_obj.execute(
                    """
                    UPDATE trades
                    SET exit_price=?, pnl_usdt=?, pnl_pct=?, reason=?
                    WHERE id=? AND reason='OPEN'
                    """,
                    (exit_price, round(pnl_usdt, 6), round(pnl_pct, 8), reason, trade_id),
                ).rowcount
                self._conn_obj.commit()
            if rows_affected:
                # Actualizeaza si daily_pnl (UPDATE nu o face automat)
                self._update_daily_pnl(symbol, pnl_usdt)
                return

        # Fallback: insert complet (bot restartat mid-trade sau trade_id invalid)
        # BUG 14+15 FIX: transmitem simbolul si datele corecte
        self.record_trade(
            symbol=symbol,
            side=side,
            entry=entry,
            exit_price=exit_price,
            qty=qty,
            pnl_usdt=pnl_usdt,
            pnl_pct=pnl_pct,
            reason=reason,
        )

    def _update_daily_pnl(self, symbol: str, pnl_usdt: float) -> None:
        """Actualizeaza daily_pnl table dupa un UPDATE pe trades (nu INSERT).

        record_trade() face asta automat la INSERT, dar close_trade_record()
        face UPDATE pe randul existent -> daily_pnl nu era actualizat.
        """
        if not symbol:
            return
        today = date.today().isoformat()
        with self._lock:
            self._conn_obj.execute(
                """
                INSERT INTO daily_pnl (date, symbol, realized_pnl, total_trades, win_trades)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(date) DO UPDATE SET
                    realized_pnl  = realized_pnl  + excluded.realized_pnl,
                    total_trades  = total_trades  + 1,
                    win_trades    = win_trades    + excluded.win_trades
                """,
                (today, symbol, round(pnl_usdt, 6), 1 if pnl_usdt > 0 else 0),
            )
            self._conn_obj.commit()

    def record_trade(
        self,
        symbol: str,
        side: str,
        entry: float,
        exit_price: float,
        qty: float,
        pnl_usdt: float,
        pnl_pct: float,
        reason: str = "",
        signal_score: float = 0.0,
        funding_rate: float = 0.0,
    ) -> None:
        ts    = int(datetime.now(timezone.utc).timestamp() * 1000)
        today = date.today().isoformat()
        with self._lock:
            self._conn_obj.execute(
                """
                INSERT INTO trades
                (ts, symbol, side, entry, exit_price, qty, pnl_usdt,
                 pnl_pct, reason, signal_score, funding_rate)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (ts, symbol, side, entry, exit_price, qty,
                 round(pnl_usdt, 6), round(pnl_pct, 8),
                 reason, signal_score, funding_rate),
            )
            self._conn_obj.execute(
                """
                INSERT INTO daily_pnl (date, symbol, realized_pnl, total_trades, win_trades)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(date) DO UPDATE SET
                    realized_pnl  = realized_pnl  + excluded.realized_pnl,
                    total_trades  = total_trades  + 1,
                    win_trades    = win_trades    + excluded.win_trades
                """,
                (today, symbol, round(pnl_usdt, 6), 1 if pnl_usdt > 0 else 0),
            )
            self._conn_obj.commit()

    def load_daily_pnl(self, symbol: str) -> tuple[float, int, int]:
        today = date.today().isoformat()
        with self._lock:
            row = self._conn_obj.execute(
                "SELECT realized_pnl, total_trades, win_trades "
                "FROM daily_pnl WHERE date=? AND symbol=?",
                (today, symbol),
            ).fetchone()
        if row:
            return float(row[0]), int(row[1]), int(row[2])
        return 0.0, 0, 0

    def daily_summary(self, symbol: str, days: int = 7) -> list[dict]:
        with self._lock:
            rows = self._conn_obj.execute(
                "SELECT date, realized_pnl, total_trades, win_trades "
                "FROM daily_pnl WHERE symbol=? "
                "ORDER BY date DESC LIMIT ?",
                (symbol, days),
            ).fetchall()
        return [{"date": r[0], "pnl": r[1], "trades": r[2], "wins": r[3]} for r in rows]

    def save_metrics_snapshot(
        self,
        symbol: str,
        sharpe: float,
        max_dd: float,
        profit_factor: float,
        total_pnl: float,
    ) -> None:
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        with self._lock:
            self._conn_obj.execute(
                "INSERT INTO metrics_snapshot "
                "(ts, symbol, sharpe, max_dd, profit_factor, total_pnl) "
                "VALUES (?,?,?,?,?,?)",
                (ts, symbol, sharpe, max_dd, profit_factor, total_pnl),
            )
            self._conn_obj.commit()

    def get_all_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn_obj.execute(
                "SELECT * FROM trades WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        return [dict(r) for r in rows]


db = Database()
