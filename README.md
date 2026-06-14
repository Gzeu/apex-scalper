# ⚡ Apex Scalper v0.7.8

Production-grade async crypto scalping bot for **Bybit USDT Perpetual Futures** (V5 API).  
Built to compete with commercial-grade bots via institutional-level signal engineering, smart execution, and probabilistic risk management.

> **Status:** Testnet-running. All critical bugs closed (v0.7.2–v0.7.8). Pulse + structured JSON logs active.

---

## Changelog

### v0.7.8 — Structured JSON Logs (June 2026)
| File | Change |
|---|---|
| `log_sink.py` | New: Loguru JSON sink → `logs/apex_structured.jsonl`. Auto-extracts 18 structured fields (price, score, regime, rsi, side, pnl, cum_delta, etc.) + event classifier (ENTRY_LONG, TP1_HIT, SL_HIT, API_ERROR, …). Rotation 50MB, 30 files. Zero new dependencies. |
| `main.py` | `setup_logging()` adds JSON sink as 3rd Loguru sink alongside stderr + text file. |
| `scripts/jq_tail.sh` | Terminal helper: `tail -f` + `jq` filters for entries, exits, pnl, scores, errors, regime. |

### v0.7.7 — Pulse Loop + New Telegram Commands (June 2026)
| File | Change |
|---|---|
| `pulse.py` | New: 1-minute Telegram snapshot — bot state, open position PnL, all 10 indicators, LONG/SHORT scores with ASCII bar, regime, book pressure, funding, risk, next action. Toggle `/pulse on\|off`. |
| `telegram_ui.py` | **FIX**: `/pause` was calling `cmd_resume` (copy-paste bug). NEW: `/analytics`, `/tp`, `/funding`, `/pulse on\|off`. |
| `main.py` | Wires `run_pulse_loop` + `start_health_server` as background tasks. |

### v0.7.6 — Feed Restart + Funding Fix (June 2026)
| File | Change |
|---|---|
| `watchdog.py` | **FIX**: `feed_restart_needed()` was missing → `ImportError` every 10s → WS reconnect storm. Added `feed_restart_needed()` + `record_kline()` + `_last_kline_ts`. |
| `funding_rate.py` | **FIX**: `trader._session` → `trader._client` (same class of bug as v0.7.5). Funding gate was silently disabled. |

### v0.7.5 — MTF AttributeError Fix (June 2026)
| File | Change |
|---|---|
| `mtf_filter.py` | **FIX**: `trader._session` → `trader._client`. Crashed with `AttributeError` at startup before first candle. |

### v0.7.4 — Health Server (June 2026)
| File | Change |
|---|---|
| `health.py` | New: HTTP server port 8080. `/health` (JSON), `/metrics` (JSON), `/metrics/prometheus` (Prometheus text format). Background thread, non-blocking. |

### v0.7.3 — Analytics Module (June 2026)
| File | Change |
|---|---|
| `analytics.py` | New: trade breakdown by exit reason, signal score buckets, hourly PnL, best/worst streak. Used by `/analytics` and `daily_report`. |

### v0.7.2 — Gap Closure (June 2026)
| ID | File | Issue | Fix |
|---|---|---|---|
| GAP #1 | `feed.py` | `bp.on_tick()` passed scalar totals → absorption Check B disabled | Pass `list[(price, size)]` level data |
| GAP #2 | `position_manager.py` | `trader._api_call()` does not exist → `AttributeError` on every partial close | Use module-level `_api_call_with_retry` |
| GAP #3 | `telegram_ui.py` | `/resume` did not reset consecutive loss counter → permanent block | `/resume` calls `risk.reset_consecutive_losses()` |

### v0.7.1 — Critical Fixes (June 2026)
| ID | File | Issue | Fix |
|---|---|---|---|
| FIX #1 | `position_manager.py` | `retCode==0` assumed fill → false state | Poll `get_order_history` to confirm fill |
| FIX #2 | `limit_order_manager.py` | `_market_fallback()` missing SL/TP → no native SL on fallback | Pass all params through |
| FIX #3 | `persistence.py` | New connection per write → `database is locked` | Single persistent connection + WAL mode |
| FIX #4 | `regime_filter.py` | ADX used simple average → diverges from TradingView | Wilder EMA smoothing |
| FIX #5 | `book_pressure.py` | Absorption used avg total vol → spoof wall passes | Two-check granular system (Check A + B) |
| FIX #6 | `anti_manipulation.py` | `_signals` mutated without lock → race condition | `threading.Lock()` with atomic snapshot |
| FIX #7 | `limit_order_manager.py` | `get_instrument_info()` REST on every entry | Lazy-cache `tick_size` |
| FIX #8 | `regime_filter.py` | `sorted()` on 28800-element deque per candle → 15ms/candle | `bisect.insort` → O(log n) |
| FIX #9 | `persistence.py` | OPEN trade never updated → duplicates on restart | Correlated `open_trade` / `close_trade_record` pair |

---

## Signal Engine — 10 Indicators

| Indicator | Parameters | Role | Weight |
|---|---|---|---|
| **Book Pressure** | Cum. delta, 50-tick window | Primary entry trigger | 0.24 |
| **RSI** | 14, Wilder smoothing | Momentum confirmation | 0.16 |
| **OB Imbalance** | L2-50 top levels | Directional book pressure | 0.14 |
| **EMA Trend** | EMA(50) 1m | Macro direction gate | 0.12 |
| **EMA Cross** | EMA(9)/EMA(21) | Confirmation filter | 0.10 |
| **Volume Z-Score** | 20-period rolling | Volume confirmation | 0.08 |
| **MACD** | 12, 26, 9 | Histogram direction bonus | 0.04 |
| **Stochastic RSI** | 14, 3, 3 | %K/%D crossover bonus | 0.04 |
| **Bollinger Bands** | 20, 2σ | Price extension context | 0.04 |
| **VWAP** | Session, resets UTC midnight | Intraday bias | 0.04 |

All signals produce **continuous values [0–1]** (v0.7.2 fix — no more binary weights).  
Entry only if score ≥ `ENTRY_THRESHOLD` (default `0.60–0.68` by symbol). ATR(14) Wilder is a volatility gate only (not in score).

---

## Regime Detection

Market classified every candle into **TRENDING / RANGING / VOLATILE / NEUTRAL**:

| Regime | ADX(14) | ATR %ile (20d) | Hurst(50) | Entry | Size |
|---|---|---|---|---|---|
| TRENDING | ≥ 25 | ≥ 40th | ≥ 0.55 | ✅ allowed | 100% |
| VOLATILE | any | ≥ 80th | any | ✅ allowed | 50% |
| NEUTRAL | 20–25 | 20–80th | 0.45–0.55 | ✅ allowed | 75% |
| RANGING | < 20 | < 20th | < 0.45 | ❌ blocked | 0% |

ADX uses **Wilder smoothing** (v0.7.1 fix) — aligned with TradingView to ±0.3 after warmup.

---

## Position Management

### 3-Level Scale-Out

| Level | BTC default | ETH default | HYPE default | Close fraction |
|---|---|---|---|---|
| TP1 | +0.12% | +0.13% | +0.20% | 25% of position |
| TP2 | +0.25% | +0.28% | +0.45% | 25% of position |
| TP3 | +0.40% | +0.45% | +0.75% | 50% remainder |

- **Stop Loss**: native exchange SL attached on every entry (including market fallback)
- **Trailing Stop**: activates after `TRAIL_PCT`, amended on exchange via `amend_sl_tp()`
- **Timeout**: `MAX_HOLD_CANDLES` (4–5 by symbol)
- **Pyramid**: add to winners if score ≥ 0.85 AND position already open

---

## Execution Engine

| Feature | Detail |
|---|---|
| Entry | **Limit PostOnly** (maker 0.020%) |
| Exit | **Limit reduceOnly**, Market fallback with fill confirmation |
| Order amendment | `amend_order()` — modify SL/TP without cancel+repost |
| Rate limiting | Token bucket 10 req/s, burst 3, exponential backoff on 429 |
| Native SL/TP | Attached on every entry including market fallback |
| Ghost recovery | Detects SL triggered offline, re-syncs on restart |
| Position mode | OneWay enforced at startup |

---

## Risk Management

| Feature | Detail |
|---|---|
| **Kelly sizing** | Half-Kelly f\* from last 50 trades. Bounded [0.30×–1.80×]. Fixed until 20 trades. |
| **Regime factor** | Kelly qty × regime.size\_factor() (0 RANGING, 0.5 VOLATILE, 0.75 NEUTRAL, 1.0 TRENDING) |
| **Daily loss limit** | Auto-pause when losses > `daily_loss_limit_usdt` (per symbol profile) |
| **Dynamic spread gate** | `base_spread_bps × (1 + atr_spread_mult × atr_ratio)` |
| **Depth gate** | Min bid/ask depth (USDT) required before entry |
| **Funding rate filter** | Blocks entries counter to negative funding (v0.7.6 fix: _client) |
| **MTF filter** | EMA50(15m) must confirm 1m direction (v0.7.5 fix: _client) |
| **Anti-manipulation** | L2 spoof / wash detection (thread-safe singleton) |
| **Consecutive losses** | Pause after N losses. `/resume` resets counter. |

---

## Per-Symbol Profiles

| Symbol | 24h Vol | Lev | Size USDT | Entry Thr | TP3 | SL | BP Thr |
|---|---|---|---|---|---|---|---|
| BTCUSDT | $2.1B | 5x | $20 | 0.60 | 0.40% | 0.08% | 50,000 |
| ETHUSDT | $875M | 7x | $15 | 0.58 | 0.45% | 0.09% | 20,000 |
| HYPEUSDT | $261M | 5x | $10 | 0.65 | 0.75% | 0.15% | 8,000 |
| DOGEUSDT | $134M | 5x | $10 | 0.68 | 0.65% | 0.12% | 5,000 |
| NEARUSDT | $102M | 6x | $10 | 0.63 | 0.55% | 0.10% | 3,000 |

---

## Observability

### Telegram Commands (v0.7.7+)

| Command | Action |
|---|---|
| `/start` `/stop` | Enable/disable trading |
| `/pause` | Suspend new entries (v0.7.7 fix: was calling resume) |
| `/resume` | Resume + reset daily PnL + consecutive loss counter |
| `/status` | Price, spread, EMA, RSI, ATR, regime, book pressure |
| `/signals` | Full snapshot: all 10 indicators + book Δ + regime |
| `/regime` | Regime label, ADX, Hurst, size factor, entry allowed |
| `/tp` | **NEW** — TP1/2/3 hit status, trail active, hold candles, live PnL |
| `/pnl` | Realized PnL, daily, win rate |
| `/metrics` | Sharpe, PF, MaxDD, expectancy, Kelly trades, win streak |
| `/balance` | USDT wallet balance |
| `/close` | Force close position |
| `/watchdog` | WS feed health + last heartbeat |
| `/funding` | **NEW** — Funding rate, blocked directions, time to next payment |
| `/analytics` | **NEW** — Trade breakdown by reason / score bucket / streak (7d) |
| `/setparam KEY VALUE` | Live-tune any of 25 strategy parameters |
| `/pulse on\|off` | **NEW** — Toggle 1-minute auto-report |

### Pulse Report (every 60s)

Sent automatically every minute via Telegram (configurable via `PULSE_INTERVAL_S`):
- Bot state, uptime, feed latency
- Open position: entry, live PnL%, SL/TP levels, hold candles
- All 10 indicators with values
- LONG/SHORT score + ASCII progress bar vs threshold
- Regime, MTF bias, book pressure direction
- Funding gate status, risk gates, next action prediction

### Structured JSON Logs (v0.7.8+)

Every log line is also written as JSON to `logs/apex_structured.jsonl`:

```json
{"time":"2026-06-14T22:07:12.341Z","level":"INFO","event":"ENTRY_LONG",
 "symbol":"BTCUSDT","price":104230.0,"score":0.712,"regime":"TRENDING",
 "rsi":58.4,"side":"long","qty":0.001,"sl":103666.0,"tp":104594.0,
 "cum_delta":87420,"imbalance":0.182,"macd_hist":0.00182}
```

```bash
# Install jq once
sudo apt install jq -y

# Live stream with filters
./scripts/jq_tail.sh             # all logs
./scripts/jq_tail.sh entries     # ENTRY_LONG / ENTRY_SHORT only
./scripts/jq_tail.sh exits       # TP1/2/3/SL/TIMEOUT
./scripts/jq_tail.sh pnl         # trades with pnl field
./scripts/jq_tail.sh errors      # WARNING + ERROR
./scripts/jq_tail.sh scores      # entry scores + regime + rsi
./scripts/jq_tail.sh regime      # regime changes
```

### Health Endpoints (port 8080)

```bash
curl http://localhost:8080/health          # JSON status
curl http://localhost:8080/metrics         # JSON performance
curl http://localhost:8080/metrics/prometheus  # Prometheus scrape
```

---

## Infrastructure

| Component | Detail |
|---|---|
| WebSocket | pybit `orderbook.50` + `kline.1`, auto-reconnect |
| Orderbook | SortedDict L2, O(log n) updates |
| Watchdog | Heartbeat + `feed_restart_needed()` + auto-restart max 3/hour (v0.7.6) |
| Persistence | SQLite WAL — correlated open/close records |
| Pulse loop | 1-min Telegram snapshot (v0.7.7) |
| JSON logs | `logs/apex_structured.jsonl` — jq / Loki compatible (v0.7.8) |
| Health server | HTTP port 8080 — /health /metrics /metrics/prometheus (v0.7.4) |
| Daily report | Telegram 23:59 UTC automated summary |
| Midnight reset | Daily PnL counters at UTC 00:00:05 |
| Graceful shutdown | SIGINT/SIGTERM closes position before exit |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Bybit WebSocket (pybit thread)                                     │
│   ├─ orderbook.50 → SortedDict L2 + bp.on_tick(levels)  v0.7.2     │
│   └─ kline.1 (confirmed) → update_indicators()                     │
│        └─ run_coroutine_threadsafe → strategy.evaluate()            │
├─────────────────────────────────────────────────────────────────────┤
│  Async Event Loop                                                   │
│   ├─ regime_filter    → ADX Wilder + ATR pct + Hurst                │
│   ├─ book_pressure    → cum.delta + accel + 2-check absorption      │
│   ├─ strategy         → 10-signal weighted score (continuous v0.7.2)│
│   ├─ position_manager → TP1/2/3 + fill-poll + trail + pyramid       │
│   ├─ risk             → half-Kelly × regime_factor                  │
│   ├─ trader           → PostOnly + amend + rate limiter             │
│   ├─ mtf_filter       → EMA50(15m) refresh every 60s  (v0.7.5 fix) │
│   ├─ funding_rate     → Bybit fetch every 60s          (v0.7.6 fix) │
│   ├─ anti_manip       → thread-safe spoof detection                 │
│   ├─ persistence      → SQLite WAL, correlated records              │
│   ├─ performance      → Sharpe/PF/DD Welford streaming              │
│   ├─ watchdog         → heartbeat + feed_restart + auto-restart     │
│   ├─ pulse            → 1-min Telegram snapshot       (v0.7.7 new)  │
│   ├─ health           → HTTP :8080 /health /metrics   (v0.7.4 new)  │
│   ├─ log_sink         → JSON structured logs           (v0.7.8 new) │
│   ├─ daily_report     → Telegram 23:59 UTC                          │
│   └─ telegram_ui      → 17 commands + /setparam(25)  (v0.7.7 new)  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
cp .env.example .env
# Fill in: BYBIT_API_KEY, BYBIT_API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# Docker (recommended)
docker compose up --build

# Or locally
pip install -r requirements.txt
python -m apex_scalper.main
```

### Recommended flow before mainnet

```bash
# 1. Optimize params
python -m apex_scalper.optimizer --symbol BTCUSDT

# 2. Walk-forward OOS (must return MAINNET READY)
python -m apex_scalper.walk_forward --symbol BTCUSDT --windows 6

# 3. Testnet minimum 5 days
BYBIT_TESTNET=true python -m apex_scalper.main

# 4. Monitor live from terminal
./scripts/jq_tail.sh entries     # watch for entries
./scripts/jq_tail.sh pnl         # watch pnl per trade

# 5. Calibrate BP_BASE_THRESHOLD from /signals bp.cum_delta range
# 6. Mainnet: BYBIT_TESTNET=false
```

---

## Key ENV Parameters

See [.env.example](.env.example) for all 50+ parameters with comments.

| Parameter | Default | Description |
|---|---|---|
| `BYBIT_API_KEY` | required | Bybit API key |
| `BYBIT_API_SECRET` | required | Bybit API secret |
| `BYBIT_TESTNET` | `true` | Testnet mode |
| `SYMBOL` | `BTCUSDT` | Trading pair |
| `LEVERAGE` | `5` | Position leverage |
| `ORDER_SIZE_USDT` | `20` | Base order size |
| `ENTRY_THRESHOLD` | `0.65` | Min signal score |
| `TP1_PCT/TP2_PCT/TP3_PCT` | `0.0012/0.0025/0.0040` | Take profit levels |
| `TP1/2/3_FRACTION` | `0.25/0.25/0.50` | Scale-out fractions |
| `SL_PCT` | `0.0008` | Stop loss |
| `MAX_DAILY_LOSS_USDT` | `50` | Daily loss limit |
| `KELLY_FRACTION` | `0.5` | Half-Kelly multiplier |
| `BP_BASE_THRESHOLD` | `50000` | Book pressure delta threshold |
| `PULSE_INTERVAL_S` | `60` | Telegram pulse interval (seconds) |
| `PULSE_ENABLED` | `true` | Enable 1-min pulse on startup |
| `HEARTBEAT_TIMEOUT` | `120` | Watchdog restart threshold (seconds) |
| `USE_LIMIT_ORDERS` | `true` | PostOnly entry orders |

---

## Codebase Overview

| Module | Version | Purpose |
|---|---|---|
| `main.py` | v0.7.8 | Startup, inject_profile, 7 background tasks |
| `config.py` | v0.7.1 | 5 symbol profiles, all 50+ env params |
| `state.py` | v0.7.0 | Shared mutable state + threading.Lock |
| `feed.py` | v0.7.2 | WebSocket OB + kline, level-granular bp.on_tick |
| `trader.py` | v0.7.0 | Bybit V5 REST, PostOnly, amend, rate limiter |
| `strategy.py` | v0.7.2 | 10-signal continuous scoring + entry/exit |
| `position_manager.py` | v0.7.2 | TP1/2/3 fill-poll + trailing SL + pyramid |
| `limit_order_manager.py` | v0.5.1 | PostOnly + amend + SL/TP on fallback |
| `risk.py` | v0.7.1 | Half-Kelly sizing + daily loss + consec losses |
| `book_pressure.py` | v0.7.1 | Cum delta + accel + 2-check absorption |
| `regime_filter.py` | v0.7.1 | ADX Wilder + ATR percentile (bisect) + Hurst |
| `anti_manipulation.py` | v0.4.2 | Thread-safe spoof/wash detection |
| `indicators.py` | v0.7.1 | EMA/RSI/ATR/BB/VWAP/VolZ/MACD/StochRSI |
| `orderbook_analytics.py` | v0.7.0 | OB imbalance + pressure score |
| `persistence.py` | v0.4.1 | SQLite WAL, correlated open/close records |
| `telegram_ui.py` | v0.7.7 | 17 commands + /setparam(25) + /pause fix |
| `pulse.py` | v0.7.7 | 1-min Telegram snapshot loop |
| `log_sink.py` | v0.7.8 | Structured JSON logs → apex_structured.jsonl |
| `health.py` | v0.7.4 | HTTP :8080 /health /metrics /prometheus |
| `analytics.py` | v0.7.3 | Trade breakdown by reason/score/streak |
| `funding_rate.py` | v0.7.6 | Funding rate gate (trader._client fix) |
| `mtf_filter.py` | v0.7.5 | Multi-timeframe EMA50(15m) (trader._client fix) |
| `watchdog.py` | v0.7.6 | Heartbeat + feed_restart_needed + auto-restart |
| `performance.py` | v0.7.0 | Sharpe/PF/MaxDD Welford streaming |
| `daily_report.py` | v0.7.0 | Telegram 23:59 UTC automated summary |
| `backtester.py` | v0.7.0 | Vectorized backtest + fee modeling |
| `optimizer.py` | v0.7.0 | Optuna Sharpe optimization |
| `walk_forward.py` | v0.7.0 | Rolling OOS + Monte Carlo + verdict |

---

## Roadmap — v0.8.0

| Feature | Priority | Description |
|---|---|---|
| **Grafana dashboard** | 🔴 HIGH | Docker Compose: Loki + Promtail + Grafana reading `apex_structured.jsonl`. Equity curve, regime heatmap, score distribution. |
| **CSV export zilnic** | 🔴 HIGH | Auto-export `logs/trades_YYYY-MM-DD.csv` la midnight din SQLite. Trimis pe Telegram ca fisier. |
| **ML re-scoring** | 🟡 MED | XGBoost re-score on 6 live features. Weekly retraining on SQLite trade log. Expected +0.5 Sharpe. |
| **Multi-symbol** | 🟡 MED | Parallel loops BTCUSDT + ETHUSDT simultaneously |
| **Auto param drift** | 🟢 LOW | Detect live Sharpe drop vs backtest → trigger re-optimize |

---

## Disclaimer

For educational purposes only. Crypto trading involves significant risk of loss. Past performance does not guarantee future results. Use at your own risk.
