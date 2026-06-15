"""Health & metrics HTTP endpoint v0.8.5.

Changelog:
  v0.8.5 — FIX: `type method doesn't define __round__ method`.
    perf.win_rate / sharpe / profit_factor / max_drawdown pot returna None
    sau un tip non-numeric cand nu exista trades inca. round() pe None crapa.
    Fix: float(x or 0.0) inainte de round() pe toate campurile din /metrics.
  v0.8.4 — BUG 20 FIX: last_tick_ts exista acum explicit in BotState.
  v0.7.4 — /health, /metrics, /metrics/prometheus endpoints.

Endpoints:
  GET /health     JSON: {status, uptime_s, last_tick_age_s, feed_stale, open_position}
  GET /metrics    JSON: {daily_pnl, win_rate, sharpe, trades_today, kelly_factor}
  GET /metrics/prometheus   Prometheus plain-text format
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


def _f(val, fallback: float = 0.0) -> float:
    """Conversie sigura la float — evita __round__ errors pe None/property."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return fallback


class _HealthHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
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

    def do_GET(self):
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
            last_tick_ts  = state.last_tick_ts
            open_position = state.open_position
            running       = state.running
            paused        = state.paused

        tick_age   = time.time() - last_tick_ts if last_tick_ts else float("inf")
        feed_stale = tick_age > FEED_STALE_S
        status     = "ok" if (running and not feed_stale) else "degraded"

        self._send_json({
            "status":          status,
            "uptime_s":        round(_f(uptime), 1),
            "last_tick_age_s": round(_f(tick_age), 3),
            "feed_stale":      feed_stale,
            "open_position":   open_position or "none",
            "trading_active":  running and not paused,
        }, 200 if status == "ok" else 503)

    def _handle_metrics_json(self) -> None:
        from .performance import perf
        from .risk import risk
        self._send_json({
            "daily_pnl_usdt":      round(_f(getattr(risk,  "_daily_loss",       0.0)), 4),
            "win_rate":            round(_f(getattr(perf,  "win_rate",          0.0)), 4),
            "sharpe":              round(_f(getattr(perf,  "sharpe",            0.0)), 4),
            "profit_factor":       round(_f(getattr(perf,  "profit_factor",     0.0)), 4),
            "max_drawdown":        round(_f(getattr(perf,  "max_drawdown",      0.0)), 4),
            "kelly_factor":        round(_f(getattr(risk,  "_kelly_factor",     0.0)), 4),
            "consecutive_losses":  int(getattr(risk, "_consecutive_losses", 0) or 0),
        })

    def _handle_metrics_prometheus(self) -> None:
        from .performance import perf
        from .risk import risk
        from .state import state
        with state.lock:
            tick_age = time.time() - state.last_tick_ts if state.last_tick_ts else float("inf")

        lines = [
            "# HELP apex_win_rate Trading win rate",
            "# TYPE apex_win_rate gauge",
            f"apex_win_rate {_f(getattr(perf, 'win_rate', 0.0)):.6f}",
            "# HELP apex_sharpe Sharpe ratio (Welford streaming)",
            "# TYPE apex_sharpe gauge",
            f"apex_sharpe {_f(getattr(perf, 'sharpe', 0.0)):.6f}",
            "# HELP apex_feed_tick_age_seconds Age of last OB tick",
            "# TYPE apex_feed_tick_age_seconds gauge",
            f"apex_feed_tick_age_seconds {_f(tick_age):.3f}",
            "# HELP apex_kelly_factor Current Kelly sizing factor",
            "# TYPE apex_kelly_factor gauge",
            f"apex_kelly_factor {_f(getattr(risk, '_kelly_factor', 0.0)):.6f}",
            "# HELP apex_consecutive_losses Consecutive losing trades",
            "# TYPE apex_consecutive_losses gauge",
            f"apex_consecutive_losses {int(getattr(risk, '_consecutive_losses', 0) or 0)}",
            "",
        ]
        self._send_text("\n".join(lines))


def start_health_server() -> None:
    """Start health/metrics server in a background daemon thread. Non-blocking."""
    def _run():
        server = HTTPServer(("", HEALTH_PORT), _HealthHandler)
        logger.info(
            f"Health server listening on :{HEALTH_PORT} "
            f"(GET /health, /metrics, /metrics/prometheus)"
        )
        server.serve_forever()

    t = threading.Thread(target=_run, name="health-server", daemon=True)
    t.start()
