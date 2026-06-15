"""SQLite persistence v0.9.6 — adaugat get_last_trades() pentru /history.

Changelog:
  v0.9.6 — adaugat get_last_trades(symbol, limit) pentru comanda /history
    din telegram_ui. Returneaza ultimele N trade-uri inchise (reason!='OPEN'),
    ordonate descrescator dupa timestamp, cu camp closed_at derivat din ts.
  v0.9.5 — Improvement #6: auto-cleanup + VACUUM programat.
  v0.8.2 — BUG 14+15 FIX: close_trade_record() fallback complet.
  v0.4.1 — WAL + correlated trade records.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone, date, timedelta
from loguru import logger

DB_PATH = "data/apex_scalper.db"

TRADES_RETENTION_DAYS  = 90   # sterge trades mai vechi de N zile
METRICS_RETENTION_DAYS = 30   # sterge metrics_snapshot mai vechi de N zile


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
        # Maintenance la fiecare startup
        self.run_maintenance(startup=True)

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
                CREATE TABLE IF NOT EXISTS db_maintenance_log (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           INTEGER NOT NULL,
                    trades_deleted   INTEGER NOT NULL DEFAULT 0,
                    metrics_deleted  INTEGER NOT NULL DEFAULT 0,
                    vacuum_run       INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_trades_ts     ON trades (ts);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);
                CREATE INDEX IF NOT EXISTS idx_metrics_ts    ON metrics_snapshot (ts);
            """)
            self._conn_obj.commit()
        logger.info(f"SQLite DB initialised (WAL) at {self._path}")

    # ------------------------------------------------------------------ #
    #  Maintenance                                                         #
    # ------------------------------------------------------------------ #

    def run_maintenance(self, startup: bool = False) -> None:
        """Sterge inregistrari vechi si ruleaza VACUUM.

        Improvement #6: apelat la startup si o data pe zi din midnight_reset.
        Logheaza cate randuri au fost sterse si daca VACUUM a rulat.
        La startup: VACUUM incremental (10.000 pagini max) pentru a nu bloca.
        La midnight: VACUUM complet pentru curatare maxima.
        """
        trades_deleted  = self._cleanup_old_records()
        vacuum_run      = self._run_vacuum(incremental=startup)
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        with self._lock:
            self._conn_obj.execute(
                "INSERT INTO db_maintenance_log "
                "(ts, trades_deleted, metrics_deleted, vacuum_run) VALUES (?,?,?,?)",
                (ts, trades_deleted[0], trades_deleted[1], int(vacuum_run)),
            )
            self._conn_obj.commit()
        label = "startup" if startup else "midnight"
        logger.info(
            f"DB maintenance ({label}): "
            f"trades_deleted={trades_deleted[0]} "
            f"metrics_deleted={trades_deleted[1]} "
            f"vacuum={'yes' if vacuum_run else 'no'}"
        )

    def _cleanup_old_records(self) -> tuple[int, int]:
        cutoff_trades  = int(
            (datetime.now(timezone.utc) - timedelta(days=TRADES_RETENTION_DAYS))
            .timestamp() * 1000
        )
        cutoff_metrics = int(
            (datetime.now(timezone.utc) - timedelta(days=METRICS_RETENTION_DAYS))
            .timestamp() * 1000
        )
        with self._lock:
            trades_del = self._conn_obj.execute(
                "DELETE FROM trades WHERE ts < ? AND reason != 'OPEN'",
                (cutoff_trades,)
            ).rowcount
            metrics_del = self._conn_obj.execute(
                "DELETE FROM metrics_snapshot WHERE ts < ?",
                (cutoff_metrics,)
            ).rowcount
            self._conn_obj.commit()
        return trades_del, metrics_del

    def _run_vacuum(self, incremental: bool = False) -> bool:
        """Ruleaza VACUUM pentru a elibera spatiu dupa stergeri.

        incremental=True: VACUUM INCREMENTAL (10k pagini) — rapid, sigur la startup.
        incremental=False: VACUUM complet — mai lent, rulat noaptea la midnight.
        VACUUM nu poate rula in interiorul unei tranzactii active -> fara lock.
        """
        try:
            if incremental:
                self._conn_obj.execute("PRAGMA incremental_vacuum(10000)")
            else:
                # VACUUM complet necesita autocommit (nu in tranzactie)
                old_isolation = self._conn_obj.isolation_level
                self._conn_obj.isolation_level = None
                self._conn_obj.execute("VACUUM")
                self._conn_obj.isolation_level = old_isolation
            return True
        except Exception as e:
            logger.warning(f"DB vacuum failed: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Public interface                                                    #
    # ------------------------------------------------------------------ #

    def record_open_trade(
        self,
        symbol: str,
        side: str,
        entry: float,
        qty: float,
        signal_score: float = 0.0,
        funding_rate: float = 0.0,
    ) -> int:
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
        symbol: str = "",
        side: str = "",
        entry: float = 0.0,
        qty: float = 0.0,
    ) -> None:
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
                self._update_daily_pnl(symbol, pnl_usdt)
                return

        self.record_trade(
            symbol=symbol, side=side, entry=entry,
            exit_price=exit_price, qty=qty,
            pnl_usdt=pnl_usdt, pnl_pct=pnl_pct, reason=reason,
        )

    def _update_daily_pnl(self, symbol: str, pnl_usdt: float) -> None:
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

    def get_last_trades(self, symbol: str, limit: int = 10) -> list[dict]:
        """Returneaza ultimele N trade-uri inchise pentru comanda /history.

        Exclude inregistrarile cu reason='OPEN' (pozitii inca deschise).
        Adauga campul 'closed_at' derivat din timestamp-ul UTC al inregistrarii.
        """
        limit = min(limit, 30)
        with self._lock:
            rows = self._conn_obj.execute(
                """
                SELECT * FROM trades
                WHERE symbol=? AND reason != 'OPEN'
                ORDER BY ts DESC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Derivam closed_at din ts (milliseconds UTC)
            try:
                d["closed_at"] = datetime.fromtimestamp(
                    d["ts"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                d["closed_at"] = "—"
            result.append(d)
        return result


db = Database()
