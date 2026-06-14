"""SQLite persistence v0.4.1 — WAL connection + correlated trade records.

Fixes vs v0.4.0:
  FIX #3 — Persistent WAL connection replaces per-call sqlite3.connect():
    A single sqlite3.Connection is created at __init__ with WAL journal mode.
    All methods reuse self._conn_obj under self._lock.
    Eliminates 'database is locked' errors under concurrent record_trade calls
    and removes the overhead of connection setup/teardown per write.

  FIX #9 — Open trades correlated with close records:
    New method record_open_trade(): inserts with reason='OPEN', exit_price=0.
    New method close_trade_record(): updates the matching OPEN row to fill in
      exit_price, pnl_usdt, pnl_pct, and reason (TP1/TP2/TP3/SL/TRAIL/TIMEOUT).
    If no matching OPEN row exists (e.g. bot was restarted mid-trade), falls back
    to inserting a new row (backward-compatible).
    Result: one complete row per trade lifecycle, no duplicate/orphaned records.

Schema unchanged (backward-compatible). record_trade() retained for legacy callers
(e.g. SL_OFFLINE reconstruction in trader.py).
"""
from __future__ import annotations

import bisect
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
        # FIX #3: single persistent connection with WAL mode
        self._conn_obj: sqlite3.Connection = sqlite3.connect(
            self._path, check_same_thread=False
        )
        self._conn_obj.row_factory = sqlite3.Row
        self._conn_obj.execute("PRAGMA journal_mode=WAL")
        self._conn_obj.execute("PRAGMA synchronous=NORMAL")
        self._conn_obj.commit()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        """Return the persistent connection. All callers use this."""
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

    # -------------------------------------------------------------------------
    # FIX #9 — trade lifecycle: open → close as a single correlated row
    # -------------------------------------------------------------------------

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
    ) -> None:
        """Update the OPEN row with exit data. Falls back to insert if not found."""
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
                return  # success path

        # Fallback: insert a new close row (bot restarted mid-trade, no open row)
        self.record_trade(
            symbol="", side="", entry=0.0,
            exit_price=exit_price, qty=0.0,
            pnl_usdt=pnl_usdt, pnl_pct=pnl_pct, reason=reason,
        )

    # -------------------------------------------------------------------------
    # Legacy record_trade — kept for backward compat (SL_OFFLINE, partials)
    # -------------------------------------------------------------------------

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
