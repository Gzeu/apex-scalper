"""Structured JSON log sink v0.7.9 — flush explicit dupa fiecare scriere.

Changelog:
  v0.7.9 — BUG FIX: flush() explicit dupa fiecare write().
    Previne pierderea ultimelor loguri la crash (OOM, SIGKILL).
    buffering=1 (line-buffered) era deja setat dar pe unele sisteme
    (Python 3.11+, anumite OS) line-buffering e ignorat pe fisiere binare
    sau daca stdout e redirectat. flush() explicit garanteaza scriere imediata.
  v0.7.8 — initial structured JSON sink

Fiecare linie este un obiect JSON complet parsabil:
  {"time": "...", "level": "INFO", "event": "ENTRY_LONG", "symbol": "BTCUSDT",
   "price": 104230.0, "score": 0.712, "regime": "TRENDING", ...}

Rotatie: 50 MB | Retentie: 30 fisiere

Folosire din terminal (necesita jq):
  ./scripts/jq_tail.sh             # toate logurile
  ./scripts/jq_tail.sh entries     # ENTRY_LONG / ENTRY_SHORT
  ./scripts/jq_tail.sh exits       # TP1/2/3/SL/TIMEOUT
  ./scripts/jq_tail.sh pnl         # linii cu camp pnl
  ./scripts/jq_tail.sh errors      # WARNING + ERROR
  ./scripts/jq_tail.sh scores      # entry scores
  ./scripts/jq_tail.sh regime      # schimbari regim
"""
from __future__ import annotations

import json
import os
import re
from datetime import timezone
from pathlib import Path
from typing import Any

from loguru import logger

_LOG_DIR  = Path("logs")
_LOG_FILE = _LOG_DIR / "apex_structured.jsonl"
_MAX_BYTES  = 50 * 1024 * 1024
_KEEP_FILES = 30

# ── Pattern matchers ────────────────────────────────────────────────────────
_RE_PRICE      = re.compile(r'price[=:\s]+([0-9]+\.?[0-9]*)', re.I)
_RE_SCORE      = re.compile(r'score[=:\s]+([0-9]+\.[0-9]+)', re.I)
_RE_REGIME     = re.compile(r'regime[=:\s]+([A-Z]+)', re.I)
_RE_RSI        = re.compile(r'rsi[=:\s]+([0-9]+\.?[0-9]*)', re.I)
_RE_SIDE       = re.compile(r'\b(long|short|buy|sell)\b', re.I)
_RE_QTY        = re.compile(r'qty[=:\s]+([0-9]+\.?[0-9]+)', re.I)
_RE_PNL        = re.compile(r'pnl[=:\s]+([+-]?[0-9]+\.?[0-9]*)', re.I)
_RE_ATR        = re.compile(r'atr[=:\s]+([0-9]+\.?[0-9]+)', re.I)
_RE_ADX        = re.compile(r'adx[=:\s]+([0-9]+\.?[0-9]+)', re.I)
_RE_DELTA      = re.compile(r'(?:cum_?delta|delta)[=:\s]+([+-]?[0-9]+)', re.I)
_RE_IMBALANCE  = re.compile(r'imb(?:alance)?[=:\s]+([+-]?[0-9]+\.[0-9]+)', re.I)
_RE_MACD       = re.compile(r'macd_?h(?:ist)?[=:\s]+([+-]?[0-9]+\.[0-9]+)', re.I)
_RE_STOCH      = re.compile(r'stoch_?k[=:\s]+([0-9]+\.?[0-9]+)', re.I)
_RE_VOLZ       = re.compile(r'vol_?z(?:score)?[=:\s]+([+-]?[0-9]+\.?[0-9]+)', re.I)
_RE_SL         = re.compile(r'\bsl[=:\s]+([0-9]+\.?[0-9]*)', re.I)
_RE_TP         = re.compile(r'\btp[=:\s]+([0-9]+\.?[0-9]*)', re.I)
_RE_FUNDING    = re.compile(r'funding[_\s]?rate[=:\s]+([+-]?[0-9]+\.?[0-9]+)', re.I)
_RE_HOLD       = re.compile(r'hold[=:\s]+([0-9]+)', re.I)

_EVENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'LONG bp score|ENTRY.*LONG|\bLONG\b.*score',  re.I), "ENTRY_LONG"),
    (re.compile(r'SHORT bp score|ENTRY.*SHORT|\bSHORT\b.*score', re.I), "ENTRY_SHORT"),
    (re.compile(r'TP1.*hit|hit.*TP1|tp1_hit',                  re.I), "TP1_HIT"),
    (re.compile(r'TP2.*hit|hit.*TP2|tp2_hit',                  re.I), "TP2_HIT"),
    (re.compile(r'TP3.*hit|hit.*TP3|tp3_hit',                  re.I), "TP3_HIT"),
    (re.compile(r'stop.?loss.*hit|SL.*hit|hit.*SL|stoploss',   re.I), "SL_HIT"),
    (re.compile(r'trail.*stop|trailing.*activated',            re.I), "TRAIL_ACTIVATED"),
    (re.compile(r'pyramid.*add|adding.*pyramid',               re.I), "PYRAMID_ADD"),
    (re.compile(r'position.*clos|clos.*position|close exec',   re.I), "POSITION_CLOSED"),
    (re.compile(r'timeout.*clos|max.*hold.*candle',            re.I), "TIMEOUT_CLOSE"),
    (re.compile(r'watchdog.*restart|restart.*watchdog',        re.I), "WATCHDOG_RESTART"),
    (re.compile(r'feed.*stale|stale.*feed|WS.*DEAD',           re.I), "FEED_STALE"),
    (re.compile(r'manipulation.*detect|large.*wall',           re.I), "MANIPULATION_DETECT"),
    (re.compile(r'daily.*loss.*limit|MAX_DAILY_LOSS',          re.I), "DAILY_LOSS_LIMIT"),
    (re.compile(r'consecutive.*loss|MAX_CONSECUTIVE',          re.I), "CONSECUTIVE_LOSSES"),
    (re.compile(r'MTF.*EMA50|EMA50.*15m',                      re.I), "MTF_REFRESH"),
    (re.compile(r'funding.*rate.*\[',                          re.I), "FUNDING_UPDATE"),
    (re.compile(r'regime.*updated|ADX.*TRENDING|ADX.*RANGING', re.I), "REGIME_CHANGE"),
    (re.compile(r'API.*error|api call error',                   re.I), "API_ERROR"),
    (re.compile(r'WS subscribed|websocket.*listen',            re.I), "WS_CONNECTED"),
    (re.compile(r'bot.*started|state\.running.*True',          re.I), "BOT_START"),
    (re.compile(r'shutdown|bot.*stop',                         re.I), "BOT_STOP"),
]


def _classify_event(msg: str) -> str:
    for pattern, event_name in _EVENT_PATTERNS:
        if pattern.search(msg):
            return event_name
    return "LOG"


def _extract(pattern: re.Pattern, text: str, cast=float) -> Any | None:
    m = pattern.search(text)
    if m:
        try:
            return cast(m.group(1))
        except (ValueError, IndexError):
            return None
    return None


def _build_record(record: dict) -> dict:
    msg = record["message"]
    ts  = record["time"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    entry: dict[str, Any] = {
        "time":    ts,
        "level":   record["level"].name,
        "event":   _classify_event(msg),
        "symbol":  os.getenv("SYMBOL", "BTCUSDT"),
        "message": msg,
    }

    price = _extract(_RE_PRICE,     msg); price and entry.update({"price": price})
    score = _extract(_RE_SCORE,     msg); score and entry.update({"score": score})
    m_reg = _RE_REGIME.search(msg);     m_reg and entry.update({"regime": m_reg.group(1).upper()})
    rsi   = _extract(_RE_RSI,       msg); rsi   and entry.update({"rsi": rsi})
    m_side= _RE_SIDE.search(msg);       m_side and entry.update({"side": m_side.group(1).lower()})
    qty   = _extract(_RE_QTY,       msg); qty   and entry.update({"qty": qty})
    pnl   = _extract(_RE_PNL,       msg)
    if pnl is not None:                  entry["pnl"] = pnl
    atr   = _extract(_RE_ATR,       msg); atr   and entry.update({"atr": atr})
    adx   = _extract(_RE_ADX,       msg); adx   and entry.update({"adx": adx})
    delta = _extract(_RE_DELTA,     msg, int)
    if delta is not None:                entry["cum_delta"] = delta
    imb   = _extract(_RE_IMBALANCE, msg)
    if imb is not None:                  entry["imbalance"] = imb
    macd  = _extract(_RE_MACD,      msg)
    if macd is not None:                 entry["macd_hist"] = macd
    stoch = _extract(_RE_STOCH,     msg); stoch and entry.update({"stoch_k": stoch})
    volz  = _extract(_RE_VOLZ,      msg)
    if volz is not None:                 entry["vol_zscore"] = volz
    sl    = _extract(_RE_SL,        msg); sl    and entry.update({"sl": sl})
    tp    = _extract(_RE_TP,        msg); tp    and entry.update({"tp": tp})
    fund  = _extract(_RE_FUNDING,   msg)
    if fund is not None:                 entry["funding_rate"] = fund
    hold  = _extract(_RE_HOLD,      msg, int)
    if hold is not None:                 entry["hold_candles"] = hold

    return entry


class _JsonSink:
    """Loguru-compatible sink cu rotatie manuala si flush explicit.

    v0.7.9 fix: self._fh.flush() dupa fiecare write() — previne pierderea
    logurilor la crash (OOM, SIGKILL, power loss).
    """

    def __init__(self) -> None:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(_LOG_FILE, "a", buffering=1, encoding="utf-8")
        self._bytes_written = _LOG_FILE.stat().st_size if _LOG_FILE.exists() else 0

    def write(self, message) -> None:
        record = message.record
        try:
            entry = _build_record(record)
            line  = json.dumps(entry, ensure_ascii=False) + "\n"
        except Exception as e:
            line = json.dumps({"time": "", "level": "ERROR",
                               "event": "SINK_ERROR", "message": str(e)}) + "\n"
        self._fh.write(line)
        self._fh.flush()   # v0.7.9 fix: garanteaza scriere imediata la crash
        self._bytes_written += len(line.encode())
        if self._bytes_written >= _MAX_BYTES:
            self._rotate()

    def _rotate(self) -> None:
        self._fh.close()
        for i in range(_KEEP_FILES - 1, 0, -1):
            src = _LOG_DIR / f"apex_structured.{i}.jsonl"
            dst = _LOG_DIR / f"apex_structured.{i+1}.jsonl"
            if src.exists():
                src.rename(dst)
        _LOG_FILE.rename(_LOG_DIR / "apex_structured.1.jsonl")
        self._fh = open(_LOG_FILE, "a", buffering=1, encoding="utf-8")
        self._bytes_written = 0

    def __del__(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


_sink_instance: _JsonSink | None = None


def setup_json_sink() -> None:
    global _sink_instance
    _sink_instance = _JsonSink()
    logger.add(
        _sink_instance.write,
        level="DEBUG",
        format="{message}",
        colorize=False,
        backtrace=False,
        diagnose=False,
    )
    logger.info(f"JSON structured log sink activ: {_LOG_FILE} (rotatie la {_MAX_BYTES//1024//1024}MB, flush explicit)")
