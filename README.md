# ⚡ Apex Scalper v0.7.1

Production-grade async crypto scalping bot for **Bybit USDT Perpetual Futures** (V5 API).  
Built to compete with commercial-grade bots via institutional-level signal engineering, smart execution, and probabilistic risk management.

> **Status:** Internally consistent and feature-complete. Ready for testnet validation (5+ days) before mainnet.

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

## Regime Detection

Market classified every candle into **TRENDING / RANGING / VOLATILE / NEUTRAL**:

| Regime | ADX(14) | ATR %ile (20d) | Hurst(50) | Entry | Size |
|---|---|---|---|---|---|
| TRENDING | ≥ 25 | ≥ 40th | ≥ 0.55 | ✅ allowed | 100% |
| VOLATILE | any | ≥ 80th | any | ✅ allowed | 50% |
| NEUTRAL | 20–25 | 20–80th | 0.45–0.55 | ✅ allowed | 75% |
| RANGING | < 20 | < 20th | < 0.45 | ❌ blocked | 0% |

---

## Position Management

### 3-Level Scale-Out

| Level | BTC default | ETH default | HYPE default | Close fraction |
|---|---|---|---|---|
| TP1 | +0.12% | +0.13% | +0.20% | 25% of position |
| TP2 | +0.25% | +0.28% | +0.45% | 25% of position |
| TP3 | +0.40% | +0.45% | +0.75% | 50% remainder |

All exits via Limit `reduceOnly`. Fallback to Market if not filled in 2s.

### Other Exit Conditions
- **Stop Loss**: native exchange SL attached on every entry
- **Trailing Stop**: activates after `TRAIL_PCT`, amended on exchange in real-time via `amend_sl_tp()`
- **Timeout**: `MAX_HOLD_CANDLES` (4–5 by symbol)
- **Pyramid**: add to winners if score ≥ 0.70 AND PnL ≥ 0.10% (configurable per symbol)

---

## Execution Engine

| Feature | Detail |
|---|---|
| Entry | **Limit PostOnly** (maker 0.020%) |
| Exit | **Limit reduceOnly**, Market fallback 2s |
| Order amendment | `amend_order()` — modify SL/TP without cancel+repost |
| Rate limiting | Token bucket 10 req/s, burst 3, exponential backoff on 429 |
| Native SL/TP | Attached on every entry (exchange-side) |
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
| **Anti-manipulation** | L2 spoof / wash detection |
| **Consecutive losses** | Pause after N consecutive losses |

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
| Persistence | SQLite — trade log + Kelly state survive restarts |
| Daily report | Telegram 23:59 UTC automated summary |
| Midnight reset | Daily PnL counters at UTC 00:00:05 |
| Graceful shutdown | SIGINT/SIGTERM closes position before exit |
| Docker | Multi-stage build, non-root user, HEALTHCHECK |

---

## Telegram Commands

| Command | Action |
|---|---|
| `/start` `/stop` | Enable/disable trading |
| `/pause` `/resume` | Suspend/resume entries |
| `/status` | Price, spread, EMA, RSI, ATR, regime, book pressure |
| `/signals` | Full snapshot: all 10 indicators + book Δ + regime |
| `/regime` | Regime label, ADX, Hurst, size factor, entry allowed |
| `/pnl` | Realized PnL, daily, win rate |
| `/metrics` | Sharpe, PF, MaxDD, expectancy, Kelly trades, win streak |
| `/balance` | USDT wallet balance |
| `/close` | Force close position |
| `/watchdog` | WS feed health + last heartbeat |
| `/setparam KEY VALUE` | Live-tune any of 26 strategy parameters |

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  Bybit WebSocket (pybit thread)                                │
│   ├─ orderbook.50 → SortedDict L2 + bp.on_tick()             │
│   └─ kline.1 (confirmed) → update_indicators()               │
│        └─ run_coroutine_threadsafe → strategy.evaluate()       │
├───────────────────────────────────────────────────────────────────┤
│  Async Event Loop                                              │
│   ├─ regime_filter    → ADX + ATR pct + Hurst (every candle)  │
│   ├─ book_pressure    → cum. delta + accel + absorption        │
│   ├─ strategy         → 10-signal weighted score               │
│   ├─ position_manager → TP1/TP2/TP3 scale-out + pyramid        │
│   ├─ risk             → half-Kelly × regime_factor             │
│   ├─ trader           → PostOnly + amend + rate limiter        │
│   ├─ mtf_filter       → EMA50(15m) refresh every 5m            │
│   ├─ funding_rate     → Bybit fetch every 5m                   │
│   ├─ anti_manip       → L2 spoof detection                     │
│   ├─ persistence      → SQLite write on every trade            │
│   ├─ performance      → Sharpe/PF/DD Welford streaming         │
│   ├─ watchdog         → heartbeat + auto-restart               │
│   ├─ daily_report     → Telegram 23:59 UTC                     │
│   └─ telegram_ui      → 11 commands + /regime + /setparam(26)  │
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

# 3. Testnet minimum 5 days — watch /regime logs
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
| `ADX_TRENDING_MIN` | `25.0` | ADX threshold for TRENDING regime |
| `USE_LIMIT_ORDERS` | `true` | PostOnly entry orders |

---

## Codebase Overview

| Module | Version | Lines | Purpose |
|---|---|---|---|
| `strategy.py` | v0.7.1 | ~220 | 10-signal scoring + entry/exit logic |
| `indicators.py` | v0.7.1 | ~230 | EMA/RSI/ATR/BB/VWAP/VolZ/MACD/StochRSI |
| `position_manager.py` | v0.7.1 | ~240 | TP1/2/3 scale-out + trailing + pyramid |
| `trader.py` | v0.7.0 | ~350 | Bybit V5 REST, PostOnly, amend, rate limiter |
| `risk.py` | v0.7.0 | ~130 | Half-Kelly sizing + daily loss guard |
| `regime_filter.py` | v0.7.0 | ~145 | ADX + ATR pct + Hurst classification |
| `book_pressure.py` | v0.7.0 | ~115 | Cum delta + acceleration + absorption |
| `telegram_ui.py` | v0.7.1 | ~195 | 12 commands + live /setparam(26 params) |
| `config.py` | v0.7.1 | ~175 | 5 symbol profiles, all params complete |
| `main.py` | v0.7.1 | ~125 | inject_profile + startup + tasks |
| `walk_forward.py` | v0.7.0 | ~210 | Rolling OOS + Monte Carlo + verdict |
| `backtester.py` | v0.7.0 | ~430 | Vectorized backtest + fee modeling |
| `optimizer.py` | v0.7.0 | ~210 | Optuna Sharpe optimization |

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
