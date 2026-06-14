# ⚡ Apex Scalper v0.7.2

Production-grade async crypto scalping bot for **Bybit USDT Perpetual Futures** (V5 API).  
Built to compete with commercial-grade bots via institutional-level signal engineering, smart execution, and probabilistic risk management.

> **Status:** All critical bugs and integration gaps closed. Ready for testnet validation (5+ days) before mainnet.

---

## Bug Fix History

### v0.7.2 — Gap Closure (June 2026)
| ID | File | Issue | Fix |
|---|---|---|---|
| GAP #1 | `feed.py` | `bp.on_tick()` passed scalar totals → book_pressure Check B (deep wall) disabled | Pass `list[(price, size)]` level data; activates granular spoof detection |
| GAP #2 | `position_manager.py` | `trader._api_call()` does not exist → `AttributeError` on every partial close | Import and use module-level `_api_call_with_retry` from `trader.py` |
| GAP #3 | `telegram_ui.py` | `/resume` did not reset consecutive loss counter → permanent entry block after N losses | `/resume` now calls `risk.reset_consecutive_losses()` |

### v0.7.1 — Critical Fixes (June 2026)
| ID | File | Issue | Fix |
|---|---|---|---|
| FIX #1 | `position_manager.py` | `retCode==0` assumed fill → false state, wrong qty tracking | Poll `get_order_history` to confirm fill |
| FIX #2 | `limit_order_manager.py` | `_market_fallback()` missing `stop_loss`/`take_profit` → no native SL on fallback | Pass all params through all 3 callsites |
| FIX #3 | `persistence.py` | New connection per `record_trade()` → `database is locked` under load | Single persistent connection + WAL mode |
| FIX #4 | `regime_filter.py` | ADX used simple average, not Wilder smoothing → diverges from TradingView | Implemented Wilder EMA smoothing |
| FIX #5 | `book_pressure.py` | Absorption used avg total vol → spoof wall at depth passes undetected | Two-check granular system: near-touch (Check A) + deep wall ratio (Check B) |
| FIX #6 | `anti_manipulation.py` | `_signals` singleton mutated without lock → race condition | `threading.Lock()` with atomic snapshot writes |
| FIX #7 | `limit_order_manager.py` | `get_instrument_info()` REST call on every entry | Lazy-cache `tick_size` after first call |
| FIX #8 | `regime_filter.py` | `sorted()` on 28,800-element deque per candle → 15ms/candle | `bisect.insort` on parallel sorted list → O(log n) |
| FIX #9 | `persistence.py` | OPEN trade record never updated → duplicate records on restart | `record_open_trade()` + `close_trade_record(trade_id)` correlated pair |

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

All signals contribute to a **weighted score [0–1]**. Entry only if score ≥ `ENTRY_THRESHOLD` (default `0.60–0.68` by symbol).  
ATR(14) Wilder is used as a volatility gate and for SL/TP sizing (not in score).

---

## Book Pressure — Absorption Detection v0.7.1

Two independent checks block entry when absorption is detected:

| Check | Logic | Detects |
|---|---|---|
| **Check A — Near-touch wall** | `avg_near_ask / avg_near_bid > 3.0×` (levels 0–2) | Genuine resistance at touch — real orders that will be hit |
| **Check B — Deep wall ratio** | `avg_deep_ask / avg_near_ask > 5.0×` (levels 3–9) | Spoof pattern: thin near-touch + huge wall at depth |

Both checks use a 5-tick rolling window to smooth single-tick noise. Either check alone is sufficient to block.  
Feed passes `list[(price, size)]` level data (not scalar totals) since v0.7.2, activating full granular detection.

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

All exits via Limit `reduceOnly`. Fallback to Market if not filled after poll confirmation.  
Fill confirmed via `get_order_history` poll — never assumed from `retCode==0` (v0.7.1 fix).

### Other Exit Conditions
- **Stop Loss**: native exchange SL attached on every entry (including market fallback — v0.7.1 fix)
- **Trailing Stop**: activates after `TRAIL_PCT`, amended on exchange in real-time via `amend_sl_tp()`
- **Timeout**: `MAX_HOLD_CANDLES` (4–5 by symbol)
- **Pyramid**: add to winners if score ≥ 0.70 AND PnL ≥ 0.10% (configurable per symbol)

---

## Execution Engine

| Feature | Detail |
|---|---|
| Entry | **Limit PostOnly** (maker 0.020%) |
| Exit | **Limit reduceOnly**, Market fallback with fill confirmation |
| Order amendment | `amend_order()` — modify SL/TP without cancel+repost |
| Rate limiting | Token bucket 10 req/s, burst 3, exponential backoff on 429 |
| Native SL/TP | Attached on every entry including market fallback (v0.7.1 fix) |
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
| **Funding rate filter** | Blocks entries counter to negative funding |
| **MTF filter** | EMA50(15m) must confirm 1m direction |
| **Anti-manipulation** | L2 spoof / wash detection (thread-safe singleton v0.7.1 fix) |
| **Consecutive losses** | Pause after N losses. `/resume` resets counter (v0.7.2 fix). |

---

## Per-Symbol Profiles

| Symbol | 24h Vol | Lev | Size USDT | Entry Thr | TP3 | SL | BP Thr |
|---|---|---|---|---|---|---|---|
| BTCUSDT | $2.1B | 5x | $20 | 0.60 | 0.40% | 0.08% | 50,000 |
| ETHUSDT | $875M | 7x | $15 | 0.58 | 0.45% | 0.09% | 20,000 |
| HYPEUSDT | $261M | 5x | $10 | 0.65 | 0.75% | 0.15% | 8,000 |
| DOGEUSDT | $134M | 5x | $10 | 0.68 | 0.65% | 0.12% | 5,000 |
| NEARUSDT | $102M | 6x | $10 | 0.63 | 0.55% | 0.10% | 3,000 |

Profiles are tuned for Bybit Perpetual Futures (June 2026 volume data). All parameters override-able via `/setparam`.

---

## Validation & Backtesting

### Walk-Forward OOS Validator
```bash
python -m apex_scalper.walk_forward --symbol BTCUSDT --windows 6 --train 60 --oos 20
```
- Rolling windows: 60d train → 20d OOS, steps of 10d
- Per-window metrics: OOS Sharpe, Win Rate, Profit Factor, Net PnL
- Overfitting detection: flags if OOS Sharpe < 0.6 × Train Sharpe
- **Monte Carlo**: 1000 shuffles → P5/P50/P95 PnL distribution
- **Verdict**: `✅ MAINNET READY` if median OOS Sharpe ≥ 1.0 AND P5 > -daily\_loss\_limit

### Optimizer
```bash
python -m apex_scalper.optimizer --symbol BTCUSDT
```
Optuna grid search over TP/SL/threshold params, optimizing for **Sharpe net of real fees**.

---

## Infrastructure

| Component | Detail |
|---|---|
| WebSocket | pybit `orderbook.50` + `kline.1`, auto-reconnect |
| Orderbook | SortedDict L2, O(log n) updates |
| Watchdog | Heartbeat monitor, auto-restart (max 3/hour) |
| Persistence | SQLite WAL — correlated open/close records, survive restarts (v0.7.1 fix) |
| Daily report | Telegram 23:59 UTC automated summary |
| Midnight reset | Daily PnL counters at UTC 00:00:05 |
| Graceful shutdown | SIGINT/SIGTERM closes position before exit |
| Docker | Multi-stage build, non-root user, HEALTHCHECK |

---

## Telegram Commands

| Command | Action |
|---|---|
| `/start` `/stop` | Enable/disable trading |
| `/pause` | Suspend new entries |
| `/resume` | Resume entries, reset daily PnL + consecutive loss counter |
| `/status` | Price, spread, EMA, RSI, ATR, regime, book pressure |
| `/signals` | Full snapshot: all 10 indicators + book Δ + regime |
| `/regime` | Regime label, ADX, Hurst, size factor, entry allowed |
| `/pnl` | Realized PnL, daily, win rate |
| `/metrics` | Sharpe, PF, MaxDD, expectancy, Kelly trades, win streak |
| `/balance` | USDT wallet balance |
| `/close` | Force close position |
| `/watchdog` | WS feed health + last heartbeat |
| `/setparam KEY VALUE` | Live-tune any of 25 strategy parameters |

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  Bybit WebSocket (pybit thread)                                   │
│   ├─ orderbook.50 → SortedDict L2 + bp.on_tick(levels)  v0.7.2   │
│   └─ kline.1 (confirmed) → update_indicators()                   │
│        └─ run_coroutine_threadsafe → strategy.evaluate()          │
├───────────────────────────────────────────────────────────────────┤
│  Async Event Loop                                                 │
│   ├─ regime_filter    → ADX Wilder + ATR pct + Hurst              │
│   ├─ book_pressure    → cum.delta + accel + 2-check absorption    │
│   ├─ strategy         → 10-signal weighted score                  │
│   ├─ position_manager → TP1/2/3 + fill-poll + trail + pyramid     │
│   ├─ risk             → half-Kelly × regime_factor                │
│   ├─ trader           → PostOnly + amend + rate limiter           │
│   ├─ mtf_filter       → EMA50(15m) refresh every 5m               │
│   ├─ funding_rate     → Bybit fetch every 5m                      │
│   ├─ anti_manip       → thread-safe spoof detection               │
│   ├─ persistence      → SQLite WAL, correlated records            │
│   ├─ performance      → Sharpe/PF/DD Welford streaming            │
│   ├─ watchdog         → heartbeat + auto-restart                  │
│   ├─ daily_report     → Telegram 23:59 UTC                        │
│   └─ telegram_ui      → 12 commands + /setparam(25)               │
└───────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
cp .env.example .env
# Fill in: BYBIT_API_KEY, BYBIT_API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# Docker (recommended for production)
docker compose up --build

# Or locally
pip install -r requirements.txt
python -m apex_scalper.main
```

### Recommended flow before mainnet:
```bash
# 1. Optimize params
python -m apex_scalper.optimizer --symbol BTCUSDT

# 2. Walk-forward OOS (must return MAINNET READY)
python -m apex_scalper.walk_forward --symbol BTCUSDT --windows 6

# 3. Testnet minimum 5 days — watch /regime and /signals logs
BYBIT_TESTNET=true python -m apex_scalper.main

# 4. Calibrate BP_BASE_THRESHOLD from /signals bp.cum_delta range
# 5. Mainnet: BYBIT_TESTNET=false
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
| `ENTRY_THRESHOLD` | `0.65` | Min signal score (overridden by profile) |
| `TP1_PCT/TP2_PCT/TP3_PCT` | `0.0012/0.0025/0.0040` | Take profit levels |
| `TP1/2/3_FRACTION` | `0.25/0.25/0.50` | Scale-out fractions |
| `SL_PCT` | `0.0008` | Stop loss |
| `MAX_DAILY_LOSS_USDT` | `50` | Daily loss limit |
| `KELLY_FRACTION` | `0.5` | Half-Kelly multiplier |
| `BP_BASE_THRESHOLD` | `50000` | Book pressure delta threshold (calibrate from testnet) |
| `ABSORPTION_NEAR_LEVELS` | `3` | Levels considered near-touch for absorption Check A |
| `DEEP_WALL_MULT` | `5.0` | Deep wall vs near-touch ratio threshold for Check B |
| `ADX_TRENDING_MIN` | `25.0` | ADX threshold for TRENDING regime |
| `USE_LIMIT_ORDERS` | `true` | PostOnly entry orders |

---

## Codebase Overview

| Module | Version | Purpose |
|---|---|---|
| `main.py` | v0.7.1 | inject_profile + startup + async tasks |
| `config.py` | v0.7.1 | 5 symbol profiles, all 50+ env params |
| `state.py` | v0.7.0 | Shared mutable state + threading.Lock |
| `feed.py` | v0.7.2 | WebSocket OB + kline, level-granular bp.on_tick |
| `trader.py` | v0.7.0 | Bybit V5 REST, PostOnly, amend, rate limiter |
| `strategy.py` | v0.7.2 | 10-signal weighted scoring + entry/exit logic |
| `position_manager.py` | v0.7.2 | TP1/2/3 fill-poll + trailing SL + pyramid |
| `limit_order_manager.py` | v0.5.1 | PostOnly Limit + amend + SL/TP on fallback |
| `risk.py` | v0.7.1 | Half-Kelly sizing + daily loss + consec losses |
| `book_pressure.py` | v0.7.1 | Cum delta + accel + 2-check granular absorption |
| `regime_filter.py` | v0.7.1 | ADX Wilder + ATR percentile (bisect) + Hurst |
| `anti_manipulation.py` | v0.4.2 | Thread-safe spoof/wash detection |
| `indicators.py` | v0.7.1 | EMA/RSI/ATR/BB/VWAP/VolZ/MACD/StochRSI |
| `orderbook_analytics.py` | v0.7.0 | OB imbalance + pressure score for strategy |
| `persistence.py` | v0.4.1 | SQLite WAL, correlated open/close records |
| `telegram_ui.py` | v0.7.2 | 12 commands + /setparam(25) + /resume fix |
| `funding_rate.py` | v0.7.0 | Funding rate gate (fetch every 5m) |
| `mtf_filter.py` | v0.7.0 | Multi-timeframe EMA50(15m) confirmation |
| `watchdog.py` | v0.7.0 | Heartbeat + auto-restart (max 3/hour) |
| `performance.py` | v0.7.0 | Sharpe/PF/MaxDD Welford streaming |
| `daily_report.py` | v0.7.0 | Telegram 23:59 UTC automated summary |
| `backtester.py` | v0.7.0 | Vectorized backtest + fee modeling |
| `optimizer.py` | v0.7.0 | Optuna Sharpe optimization |
| `walk_forward.py` | v0.7.0 | Rolling OOS + Monte Carlo + verdict |

---

## Roadmap — v0.8.0

| Feature | Priority | Description |
|---|---|---|
| **ML re-scoring** | 🔴 HIGH | XGBoost re-score on 6 live features. Weekly retraining on SQLite trade log. Expected +0.5 Sharpe. |
| **Multi-symbol** | 🟡 MED | Parallel loops BTCUSDT + ETHUSDT simultaneously |
| **Auto param drift** | 🟡 MED | Detect live Sharpe drop vs backtest → trigger re-optimize |
| **Dashboard UI** | 🟢 LOW | FastAPI + equity curve + live heatmap |

---

## Disclaimer

For educational purposes only. Crypto trading involves significant risk of loss. Past performance does not guarantee future results. Use at your own risk.
