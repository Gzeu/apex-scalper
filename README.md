# ⚡ Apex Scalper v0.3.0

Production-grade async scalping bot for **Bybit USDT Perpetual Futures** (V5 API).
Built to outperform off-the-shelf bots via a **multi-signal engine**, **partial TP**, **live Telegram tuning**, and **real-time analytics**.

## Features

### Signal Engine (7 confirmation filters)
| Signal | Purpose |
|---|---|
| EMA(9/21) cross | Primary entry trigger |
| EMA(50) trend filter | Only trade in trend direction |
| RSI(14) Wilder | Momentum confirmation, avoid OB/OS |
| Orderbook imbalance | Directional pressure from L2 book |
| Volume Z-Score | Skip low-volume false breakouts |
| ATR(14) volatility gate | Avoid entries in extreme volatility |
| Bollinger Band(20,2) | Context: avoid chasing extended moves |

All signals contribute a **weighted score** (0–1). Entry only if total ≥ `ENTRY_THRESHOLD` (default 0.60).

### Position Management
- **Partial TP**: scale out 50% at TP1, close rest at TP2
- **Pyramid entry**: add to winners if score ≥ 0.85
- **Trailing stop**: activates after `TRAIL_PCT` in profit
- **Timeout exit**: max `MAX_HOLD_CANDLES` candles held

### Risk Management
- Daily loss limit (auto-pause)
- Spread filter (max bps configurable)
- Orderbook depth filter (liquidity check)
- Session time filter (skip low-liquidity hours UTC)
- Exponential backoff retry on all orders (3 attempts)

### Infrastructure
- **SortedDict** L2 orderbook (O(log n) updates, O(1) best bid/ask)
- **threading.Lock** for cross-thread WS safety
- **Watchdog** task: detects dead WS feed, alerts via Telegram
- **Graceful shutdown**: SIGINT/SIGTERM closes position before exit
- **Performance tracker**: Sharpe, Profit Factor, Max Drawdown, Win Streak

### Telegram Commands
| Command | Action |
|---|---|
| `/start` `/stop` | Enable/disable trading |
| `/pause` `/resume` | Suspend/resume entries |
| `/status` | Price, spread, EMA, RSI, ATR, imbalance |
| `/signals` | Full indicator snapshot |
| `/pnl` | Realized PnL, daily, winrate |
| `/metrics` | Sharpe, PF, MaxDD, expectancy, streaks |
| `/balance` | USDT wallet balance |
| `/close` | Force close position |
| `/watchdog` | WS feed health |
| `/setparam KEY VALUE` | Live-tune any strategy param |

## Quick Start
```bash
cp .env.example .env
# Fill BYBIT_API_KEY, BYBIT_API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
docker compose up --build
```

Or locally:
```bash
pip install -r requirements.txt
python -m apex_scalper.main
```

## Architecture
```
┌──────────────────────────────────────────────────────────────┐
│ Bybit WS (pybit thread)                               │
│  ├─ orderbook.50 → threading.Lock → SortedDict L2     │
│  └─ kline.1 (confirmed only) → indicators update      │
│       └─ run_coroutine_threadsafe → strategy.evaluate()│
├──────────────────────────────────────────────────────────────┤
│ Async Event Loop (strategy + trader + telegram)       │
│  ├─ strategy.evaluate() → score_long/short (7 signals) │
│  ├─ position_manager  → partial TP1/TP2, pyramid       │
│  ├─ risk_manager      → spread/depth/session/daily     │
│  ├─ performance       → Sharpe, DD, PF (Welford)       │
│  ├─ watchdog          → heartbeat monitor               │
│  └─ telegram_ui       → 12 commands, live /setparam     │
└──────────────────────────────────────────────────────────────┘
```

## All ENV Params
See [.env.example](.env.example) for the full list of 30+ configurable parameters.

## Disclaimer
For educational purposes. Trading crypto involves significant risk. Use at your own risk.
