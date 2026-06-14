"""SQLite persistence v0.4.0 — trades log + daily PnL survive restarts.

Schema:
  trades(id, ts, symbol, side, entry, exit_price, qty, pnl_usdt, pnl_pct,
         reason, signal_score, funding_rate)
  daily_pnl(date TEXT PK, symbol, realized_pnl, total_trades, win_trades)
  metrics_snapshot(id, ts, symbol, sharpe, max_dd, profit_factor, total_pnl)

Usage:
  from .persistence import db
  db.record_trade(...)    # called by risk.update_pnl()
  db.load_daily_pnl()     # called on startup to restore state
  db.daily_summary()      # called by daily_report.py
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
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            with self._conn() as conn:
                conn.executescript("""
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
        logger.info(f"SQLite DB initialised at {self._path}")

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
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        today = date.today().isoformat()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
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
                # Upsert daily_pnl
                conn.execute(
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

    def load_daily_pnl(self, symbol: str) -> tuple[float, int, int]:
        """Returns (realized_pnl, total_trades, win_trades) for today."""
        today = date.today().isoformat()
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT realized_pnl, total_trades, win_trades "
                    "FROM daily_pnl WHERE date=? AND symbol=?",
                    (today, symbol),
                ).fetchone()
        if row:
            return float(row[0]), int(row[1]), int(row[2])
        return 0.0, 0, 0

    def daily_summary(self, symbol: str, days: int = 7) -> list[dict]:
        """Return last N days of PnL for daily report."""
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
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
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO metrics_snapshot "
                    "(ts, symbol, sharpe, max_dd, profit_factor, total_pnl) "
                    "VALUES (?,?,?,?,?,?)",
                    (ts, symbol, sharpe, max_dd, profit_factor, total_pnl),
                )

    def get_all_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
        return [dict(r) for r in rows]


db = Database()
