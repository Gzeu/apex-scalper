"""Health & metrics HTTP endpoint v0.7.4.

Exposes two endpoints on port 8080 (ENV: HEALTH_PORT) in a background thread.
Zero impact on the async trading loop — runs in a separate daemon thread.

Endpoints:
  GET /health     JSON: {status, uptime_s, last_tick_age_s, feed_stale, open_position}
  GET /metrics    JSON: {daily_pnl, win_rate, sharpe, trades_today, kelly_factor}
  GET /metrics/prometheus   Prometheus plain-text format

Docker HEALTHCHECK:
  HEALTHCHECK CMD curl -f http://localhost:8080/health || exit 1

Usage:
  From main.py: from .health import start_health_server; start_health_server()
"""
from __future__ import annotations

import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from loguru import logger

HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
FEED_STALE_S = float(os.getenv("FEED_STALE_S", "2.0"))

_start_time = time.time()


class _HealthHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # silence default access logs
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        try:
            if self.path == "/health":
                self._handle_health()
            elif self.path == "/metrics":
                self._handle_metrics_json()
            elif self.path == "/metrics/prometheus":
                self._handle_metrics_prometheus()
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as e:
            logger.warning(f"[health] handler error: {e}")
            self._send_json({"error": str(e)}, 500)

    def _handle_health(self) -> None:
        from .state import state
        uptime = time.time() - _start_time
        with state.lock:
            last_tick_ts   = getattr(state, "last_tick_ts", 0.0)
            open_position  = getattr(state, "open_position", None)
            running        = getattr(state, "running", False)
            paused         = getattr(state, "paused", False)

        tick_age   = time.time() - last_tick_ts if last_tick_ts else float("inf")
        feed_stale = tick_age > FEED_STALE_S
        status     = "ok" if (running and not feed_stale) else "degraded"

        self._send_json({
            "status":          status,
            "uptime_s":        round(uptime, 1),
            "last_tick_age_s": round(tick_age, 3),
            "feed_stale":      feed_stale,
            "open_position":   open_position or "none",
            "trading_active":  running and not paused,
        }, 200 if status == "ok" else 503)

    def _handle_metrics_json(self) -> None:
        from .performance import perf
        from .risk import risk
        self._send_json({
            "daily_pnl_usdt": round(getattr(risk, "_daily_loss", 0.0), 4),
            "win_rate":       round(perf.win_rate, 4),
            "sharpe":         round(perf.sharpe, 4),
            "profit_factor":  round(perf.profit_factor, 4),
            "max_drawdown":   round(perf.max_drawdown, 4),
            "kelly_factor":   round(getattr(risk, "_kelly_factor", 0.0), 4),
            "consecutive_losses": getattr(risk, "_consecutive_losses", 0),
        })

    def _handle_metrics_prometheus(self) -> None:
        from .performance import perf
        from .risk import risk
        from .state import state
        with state.lock:
            tick_age = time.time() - getattr(state, "last_tick_ts", 0.0)

        lines = [
            "# HELP apex_win_rate Trading win rate",
            "# TYPE apex_win_rate gauge",
            f"apex_win_rate {perf.win_rate:.6f}",
            "# HELP apex_sharpe Sharpe ratio (Welford streaming)",
            "# TYPE apex_sharpe gauge",
            f"apex_sharpe {perf.sharpe:.6f}",
            "# HELP apex_feed_tick_age_seconds Age of last OB tick",
            "# TYPE apex_feed_tick_age_seconds gauge",
            f"apex_feed_tick_age_seconds {tick_age:.3f}",
            "# HELP apex_kelly_factor Current Kelly sizing factor",
            "# TYPE apex_kelly_factor gauge",
            f"apex_kelly_factor {getattr(risk, '_kelly_factor', 0.0):.6f}",
            "# HELP apex_consecutive_losses Consecutive losing trades",
            "# TYPE apex_consecutive_losses gauge",
            f"apex_consecutive_losses {getattr(risk, '_consecutive_losses', 0)}",
            "",
        ]
        self._send_text("\n".join(lines))


def start_health_server() -> None:
    """Start health/metrics server in a background daemon thread.

    Call once from main.py at startup. Non-blocking.
    """
    def _run():
        server = HTTPServer(("", HEALTH_PORT), _HealthHandler)
        logger.info(
            f"Health server listening on :{HEALTH_PORT} "
            f"(GET /health, /metrics, /metrics/prometheus)"
        )
        server.serve_forever()

    t = threading.Thread(target=_run, name="health-server", daemon=True)
    t.start()
