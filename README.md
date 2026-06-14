# ⚡ Apex Scalper v0.7.1

Production-grade async crypto scalping bot for **Bybit USDT Perpetual Futures** (V5 API).  
Built to compete with commercial-grade bots via institutional-level signal engineering, smart execution, and probabilistic risk management.

> **Status:** Feature-complete for top-5% retail bot territory. Awaiting live testnet validation before mainnet.

---

## Signal Engine — 10 Indicators

| Indicator | Parameters | Role |
|---|---|---|
| **Book Pressure** | Cumulative delta, 50-tick window | Primary entry trigger |
| **EMA Cross** | EMA(9) / EMA(21) | Confirmation filter |
| **EMA Trend** | EMA(50) 1m | Macro direction gate |
| **RSI** | 14, Wilder smoothing | Momentum + OB/OS penalty |
| **MACD** | 12, 26, 9 | Momentum confirmation, histogram |
| **Stochastic RSI** | 14, 3, 3 | Sensitive scalp-level momentum |
| **Bollinger Bands** | 20, 2σ | Price extension context |
| **VWAP** | Session, resets UTC midnight | Intraday bias |
| **Volume Z-Score** | 20-period rolling | Volume confirmation |
| **ATR** | 14, Wilder | Volatility gate + dynamic SL/TP |
| **Orderbook Imbalance** | L2-50 top levels | Directional pressure from book |

All signals contribute to a **weighted score [0–1]**. Entry only if score ≥ `ENTRY_THRESHOLD` (default `0.65`).

**Signal weights v0.7.1:**
```
book_pressure=0.28  rsi=0.18  imbalance=0.16  trend=0.12
ema_cross=0.10  volume=0.08  bb=0.04  vwap=0.04
```

---

## Regime Detection

Market classified every candle into **TRENDING / RANGING / VOLATILE / NEUTRAL**:

| Metric | Trending | Ranging | Volatile |
|---|---|---|---|
| ADX(14) | ≥ 25 | < 20 | any |
| ATR percentile (20d) | ≥ 40th | < 20th | ≥ 80th |
| Hurst exponent (50 bars) | ≥ 0.55 | < 0.45 | any |

- **RANGING** → entries fully blocked (eliminates EMA whipsaws in lateral markets)
- **VOLATILE** → position size halved (50%)
- **TRENDING** → full entries, full size
- **NEUTRAL** → entries allowed, 75% size

---

## Position Management

### Scale-Out (3 levels, all via Limit reduceOnly)

| Level | Default Trigger | Close Fraction |
|---|---|---|
| TP1 | +0.10% | 25% of position |
| TP2 | +0.20% | 25% of position |
| TP3 | +0.35% | 50% (remainder) |

Fallback to Market if Limit not filled in 2s.

### Other exit conditions
- **Stop Loss**: native exchange SL attached on every entry, default -0.08%
- **Trailing Stop**: activates after `TRAIL_PCT`, amends on exchange in real-time
- **Timeout**: max `MAX_HOLD_CANDLES` candles (default 5)
- **Pyramid**: add to winners if score ≥ 0.85 AND pnl ≥ 0.10%, via Limit PostOnly

---

## Execution Engine

| Feature | Detail |
|---|---|
| Entry order type | **Limit PostOnly** (maker 0.020%) |
| Exit order type | **Limit reduceOnly**, Market fallback |
| Order amendment | `amend_order()` — modify price without cancel+repost |
| Rate limiting | Token bucket 10 req/s, burst 3, exponential backoff on 429 |
| Native SL/TP | Attached on every entry (stopLoss + takeProfit params) |
| Ghost position recovery | Detects SL triggered while offline on restart |
| Position mode | OneWay enforced at startup |

---

## Risk Management

| Feature | Detail |
|---|---|
| **Kelly sizing** | Half-Kelly f\* from last 50 trades. Bounded [0.30×–1.80×] base size. Fixed until 20 trades |
| **Regime factor** | Kelly qty × regime.size_factor() (0.5 in VOLATILE, 0 in RANGING) |
| **Daily loss limit** | Auto-pause if losses > `MAX_DAILY_LOSS_USDT` |
| **Dynamic spread gate** | `base_spread_bps × (1 + ATR_SPREAD_MULT × atr_ratio)` — widens in volatility |
| **Orderbook depth** | Min bid/ask depth required before entry |
| **Funding rate filter** | Blocks entries counter to negative funding |
| **MTF filter** | EMA50 on 15m must confirm 1m direction |
| **Anti-manipulation** | Detects OB spoofing / volume wash |
| **Consecutive losses** | Pause after N consecutive losses |

---

## Validation & Backtesting

### Walk-Forward OOS Validator
```bash
python -m apex_scalper.walk_forward --symbol BTCUSDT --windows 6 --train 60 --oos 20
```
- Rolling windows: 60d train → 20d OOS, steps of 10d
- Per-window: OOS Sharpe, Win Rate, Profit Factor, Net PnL
- Overfitting detection: flags if OOS Sharpe < 0.6 × Train Sharpe
- **Monte Carlo**: 1000 shuffles of OOS trades → P5/P50/P95 PnL distribution
- **Verdict**: `✅ MAINNET READY` if median OOS Sharpe ≥ 1.0 AND P5 > -daily_loss_limit

### Optimizer
```bash
python -m apex_scalper.optimizer --symbol BTCUSDT
```
- Optuna grid search over TP/SL/threshold/RSI params
- Optimizes for **Sharpe net of real fees** (maker 0.020% in + out)

---

## Infrastructure

| Component | Detail |
|---|---|
| WebSocket feed | pybit `orderbook.50` + `kline.1` with auto-reconnect |
| Orderbook | SortedDict L2, O(log n) updates, O(1) best bid/ask |
| Watchdog | Heartbeat monitor, auto-restart (max 3/hour) |
| Persistence | SQLite trade log — PnL + Kelly state survive restarts |
| Daily report | Automated 23:59 UTC Telegram summary |
| Midnight reset | Daily PnL counters reset at UTC 00:00:05 |
| Graceful shutdown | SIGINT/SIGTERM closes open position before exit |
| Docker | Multi-stage build, non-root user, HEALTHCHECK |

---

## Telegram Commands

| Command | Action |
|---|---|
| `/start` `/stop` | Enable/disable trading |
| `/pause` `/resume` | Suspend/resume entries |
| `/status` | Price, spread, EMA, RSI, ATR, regime, book pressure |
| `/signals` | Full indicator snapshot (all 10 indicators) |
| `/pnl` | Realized PnL, daily, win rate |
| `/metrics` | Sharpe, PF, MaxDD, expectancy, Kelly factor |
| `/balance` | USDT wallet balance |
| `/close` | Force close position |
| `/watchdog` | WS feed health + last heartbeat |
| `/regime` | Current regime label + ADX + Hurst exponent |
| `/setparam KEY VALUE` | Live-tune any strategy param |

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  Bybit WebSocket (pybit thread)                                │
│   ├─ orderbook.50 → SortedDict L2 + bp.on_tick() (book Δ)   │
│   └─ kline.1 (confirmed only) → update_indicators()           │
│        └─ run_coroutine_threadsafe → strategy.evaluate()       │
├───────────────────────────────────────────────────────────────────┤
│  Async Event Loop                                              │
│   ├─ regime_filter    → ADX + ATR pct + Hurst (every candle)  │
│   ├─ book_pressure    → cumul. delta + accel + absorption      │
│   ├─ strategy         → 10-signal weighted score               │
│   ├─ position_manager → TP1/TP2/TP3 scale-out + pyramid        │
│   ├─ risk             → half-Kelly × regime_factor             │
│   ├─ trader           → PostOnly + amend + rate limiter        │
│   ├─ mtf_filter       → EMA50(15m) refresh every 5m            │
│   ├─ funding_rate     → Bybit funding fetch every 5m           │
│   ├─ anti_manip       → spoof detection                        │
│   ├─ persistence      → SQLite write on every trade            │
│   ├─ performance      → Sharpe/PF/DD Welford streaming         │
│   ├─ watchdog         → heartbeat + auto-restart               │
│   ├─ daily_report     → Telegram 23:59 UTC                     │
│   └─ telegram_ui      → 11 commands + live /setparam            │
└───────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
cp .env.example .env
# Required: BYBIT_API_KEY, BYBIT_API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# Docker (recommended)
docker compose up --build

# Or locally
pip install -r requirements.txt
python -m apex_scalper.main
```

### Recommended flow before mainnet:
```bash
# 1. Optimize params for your symbol
python -m apex_scalper.optimizer --symbol BTCUSDT

# 2. Walk-forward OOS validation (verdict: MAINNET READY / NOT READY)
python -m apex_scalper.walk_forward --symbol BTCUSDT --windows 6 --output wf_results.json

# 3. Testnet minimum 5 days
TESTNET=true python -m apex_scalper.main

# 4. Inspect regime + book pressure calibration from logs before mainnet
```

---

## Key ENV Parameters

See [.env.example](.env.example) for the full list of 50+ parameters.

| Parameter | Default | Description |
|---|---|---|
| `BYBIT_API_KEY` | required | Bybit API key |
| `BYBIT_API_SECRET` | required | Bybit API secret |
| `SYMBOL` | `BTCUSDT` | Trading pair |
| `TESTNET` | `true` | Testnet mode |
| `LEVERAGE` | `5` | Position leverage |
| `ORDER_SIZE_USDT` | `100` | Base order size |
| `ENTRY_THRESHOLD` | `0.65` | Min signal score |
| `SL_PCT` | `0.0008` | Stop loss (0.08%) |
| `TP1_PCT / TP2_PCT / TP3_PCT` | `0.0010/0.0020/0.0035` | Take profit levels |
| `TP1_FRACTION / TP2_FRACTION / TP3_FRACTION` | `0.25/0.25/0.50` | Scale-out fractions |
| `MAX_DAILY_LOSS_USDT` | `50` | Daily loss limit |
| `KELLY_FRACTION` | `0.5` | Half-Kelly multiplier |
| `ADX_TRENDING_MIN` | `25` | ADX threshold TRENDING |
| `BP_BASE_THRESHOLD` | `50000` | Book pressure delta (USDT) |
| `USE_LIMIT_ORDERS` | `true` | PostOnly on entry |

---

## Roadmap — v0.8.0

| Feature | Priority | Description |
|---|---|---|
| **ML re-scoring** | 🔴 HIGH | XGBoost re-score from 6 live features. Trains weekly on DB trades. Expected +0.5 Sharpe |
| **Multi-symbol** | 🟡 MED | Parallel loops for BTCUSDT + ETHUSDT simultaneously |
| **Auto parameter drift** | 🟡 MED | Detect live Sharpe drop vs backtest → auto re-optimize |
| **Dashboard UI** | 🟢 LOW | FastAPI + equity curve + live metrics HTML |

---

## Disclaimer

For educational purposes only. Crypto trading involves significant risk of loss. Past backtest performance does not guarantee future results. Use at your own risk.
