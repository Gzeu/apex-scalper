"""Trade analytics v0.7.5 — signal breakdown from SQLite trades table.

Answers questions that raw PnL cannot:
  - Which exit reason (TP1/TP2/TP3/SL/TRAIL/TIMEOUT) has the worst loss rate?
  - Which signal_score bucket correlates with losing trades?
  - Which hour of the day has the worst win rate?
  - What is the longest losing streak?

All queries run on the existing trades table schema (persistence.py v0.4.1).
No schema migration required.

Usage:
  from .analytics import analytics
  breakdown = analytics.breakdown_by_reason("BTCUSDT", days=7)
  hourly    = analytics.hourly_win_rate("BTCUSDT", days=30)

Used by:
  daily_report.py — appends breakdown table to Telegram report
  telegram_ui.py  — /analytics command (optional)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any
from loguru import logger


class TradeAnalytics:
    """Signal and exit breakdown analytics on the SQLite trade log."""

    def _since_ts(self, days: int) -> int:
        """Unix timestamp in ms for N days ago."""
        dt = datetime.now(timezone.utc) - timedelta(days=days)
        return int(dt.timestamp() * 1000)

    def _conn(self):
        from .persistence import db
        return db._conn_obj

    # ------------------------------------------------------------------
    # 1. Breakdown by exit reason
    # ------------------------------------------------------------------

    def breakdown_by_reason(
        self,
        symbol: str,
        days: int = 7,
    ) -> list[dict[str, Any]]:
        """Per-reason stats: count, win%, avg_pnl_pct, total_pnl_usdt.

        Answers: 'TIMEOUT has 12% win rate vs TP3 has 88% — close faster.'
        Excludes OPEN rows (incomplete trades).
        """
        since = self._since_ts(days)
        try:
            rows = self._conn().execute(
                """
                SELECT
                    reason,
                    COUNT(*)                             AS cnt,
                    ROUND(AVG(CASE WHEN pnl_usdt > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_pct,
                    ROUND(AVG(pnl_pct) * 100, 4)         AS avg_pnl_pct,
                    ROUND(SUM(pnl_usdt), 4)              AS total_pnl_usdt
                FROM trades
                WHERE symbol = ?
                  AND ts >= ?
                  AND reason != 'OPEN'
                  AND reason IS NOT NULL
                GROUP BY reason
                ORDER BY total_pnl_usdt ASC
                """,
                (symbol, since),
            ).fetchall()
            return [
                {
                    "reason":        r["reason"],
                    "count":         r["cnt"],
                    "win_pct":       r["win_pct"],
                    "avg_pnl_pct":   r["avg_pnl_pct"],
                    "total_pnl_usdt": r["total_pnl_usdt"],
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"[analytics] breakdown_by_reason error: {e}")
            return []

    # ------------------------------------------------------------------
    # 2. Score bucket stats
    # ------------------------------------------------------------------

    def score_bucket_stats(
        self,
        symbol: str,
        days: int = 14,
        buckets: int = 5,
    ) -> list[dict[str, Any]]:
        """Divide signal_score [0..1] into N buckets and show win% per bucket.

        Answers: 'score 0.65-0.72 has 41% win rate, score 0.80+ has 71%.'
        Use this to tune ENTRY_THRESHOLD.
        """
        since = self._since_ts(days)
        step  = 1.0 / buckets
        results = []
        try:
            for i in range(buckets):
                lo = round(i * step, 4)
                hi = round((i + 1) * step, 4)
                row = self._conn().execute(
                    """
                    SELECT
                        COUNT(*) AS cnt,
                        ROUND(AVG(CASE WHEN pnl_usdt > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_pct,
                        ROUND(AVG(pnl_pct) * 100, 4) AS avg_pnl_pct,
                        ROUND(SUM(pnl_usdt), 4) AS total_pnl
                    FROM trades
                    WHERE symbol = ?
                      AND ts >= ?
                      AND signal_score >= ?
                      AND signal_score < ?
                      AND reason != 'OPEN'
                    """,
                    (symbol, since, lo, hi),
                ).fetchone()
                results.append({
                    "bucket":     f"{lo:.2f}-{hi:.2f}",
                    "count":      row["cnt"],
                    "win_pct":    row["win_pct"] or 0.0,
                    "avg_pnl_pct": row["avg_pnl_pct"] or 0.0,
                    "total_pnl":  row["total_pnl"] or 0.0,
                })
        except Exception as e:
            logger.warning(f"[analytics] score_bucket_stats error: {e}")
        return results

    # ------------------------------------------------------------------
    # 3. Hourly win rate
    # ------------------------------------------------------------------

    def hourly_win_rate(
        self,
        symbol: str,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Win rate by UTC hour (0-23).

        Answers: 'hours 02-04 UTC have 28% win rate — avoid Asian open.'
        ts column is Unix ms UTC.
        """
        since = self._since_ts(days)
        try:
            rows = self._conn().execute(
                """
                SELECT
                    CAST((ts / 1000 / 3600) % 24 AS INTEGER) AS hour_utc,
                    COUNT(*)                                   AS cnt,
                    ROUND(AVG(CASE WHEN pnl_usdt > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_pct,
                    ROUND(SUM(pnl_usdt), 4)                   AS total_pnl
                FROM trades
                WHERE symbol = ?
                  AND ts >= ?
                  AND reason != 'OPEN'
                GROUP BY hour_utc
                ORDER BY hour_utc ASC
                """,
                (symbol, since),
            ).fetchall()
            return [
                {
                    "hour_utc":  r["hour_utc"],
                    "count":     r["cnt"],
                    "win_pct":   r["win_pct"],
                    "total_pnl": r["total_pnl"],
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"[analytics] hourly_win_rate error: {e}")
            return []

    # ------------------------------------------------------------------
    # 4. Worst losing streak
    # ------------------------------------------------------------------

    def worst_losing_streak(self, symbol: str) -> dict[str, Any]:
        """Find the longest consecutive losing trade sequence.

        Returns: {streak_len, total_pnl_usdt, start_ts, end_ts}
        Answers: 'worst streak was 7 losses = -$14.20'
        """
        try:
            rows = self._conn().execute(
                """
                SELECT ts, pnl_usdt FROM trades
                WHERE symbol = ? AND reason != 'OPEN'
                ORDER BY ts ASC
                """,
                (symbol,),
            ).fetchall()

            best = {"streak_len": 0, "total_pnl_usdt": 0.0,
                    "start_ts": 0, "end_ts": 0}
            cur_len   = 0
            cur_pnl   = 0.0
            cur_start = 0

            for r in rows:
                if r["pnl_usdt"] < 0:
                    if cur_len == 0:
                        cur_start = r["ts"]
                    cur_len += 1
                    cur_pnl += r["pnl_usdt"]
                    if cur_len > best["streak_len"]:
                        best = {
                            "streak_len":    cur_len,
                            "total_pnl_usdt": round(cur_pnl, 4),
                            "start_ts":      cur_start,
                            "end_ts":        r["ts"],
                        }
                else:
                    cur_len = 0
                    cur_pnl = 0.0

            return best
        except Exception as e:
            logger.warning(f"[analytics] worst_losing_streak error: {e}")
            return {"streak_len": 0, "total_pnl_usdt": 0.0}

    # ------------------------------------------------------------------
    # 5. Telegram-ready summary (used by daily_report.py)
    # ------------------------------------------------------------------

    def telegram_breakdown(self, symbol: str, days: int = 1) -> str:
        """Return a formatted Telegram message block with signal breakdown.

        Called from daily_report.py to append to nightly report.
        """
        lines = [f"\n\U0001f4ca *Signal Breakdown* (last {days}d)\n"]

        # Exit reason breakdown
        reasons = self.breakdown_by_reason(symbol, days=days)
        if reasons:
            lines.append("*By exit reason:*")
            for r in reasons[:6]:   # max 6 rows in Telegram
                icon = "✅" if (r["total_pnl_usdt"] or 0) >= 0 else "❌"
                lines.append(
                    f"`{r['reason']:<8}` {icon} "
                    f"n={r['count']} "
                    f"win={r['win_pct']}% "
                    f"avg={r['avg_pnl_pct']:+.4f}% "
                    f"pnl={r['total_pnl_usdt']:+.3f}Ⓞ"
                )

        # Score bucket worst performer
        buckets = self.score_bucket_stats(symbol, days=max(days, 7))
        worst = min(buckets, key=lambda b: b["win_pct"]) if buckets else None
        if worst and worst["count"] >= 3:
            lines.append(
                f"\n*Worst score bucket:* `{worst['bucket']}` "
                f"win={worst['win_pct']}% n={worst['count']}"
            )

        # Worst losing streak
        streak = self.worst_losing_streak(symbol)
        if streak["streak_len"] >= 3:
            lines.append(
                f"*Worst streak:* {streak['streak_len']} losses = "
                f"`{streak['total_pnl_usdt']:+.3f}` USDT"
            )

        return "\n".join(lines) if len(lines) > 1 else ""


analytics = TradeAnalytics()
