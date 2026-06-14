# ⚡ Apex Scalper

High-performance async scalping bot for **Bybit USDT Perpetual Futures** (V5 API).

## Features
- ⚡ Full async WebSocket feed (orderbook L2 + trades + klines)
- 📊 Scalping strategy: EMA cross + momentum + spread filter
- 🛡️ Risk manager: dynamic position sizing, max drawdown, daily loss limit
- 📱 Telegram bot UI (start/stop/status/PnL)
- 🧪 Testnet / Mainnet toggle
- 🐳 Docker + Docker Compose ready

## Quick Start
```bash
cp .env.example .env
# Fill in BYBIT_API_KEY, BYBIT_API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
docker compose up --build
```

Or without Docker:
```bash
pip install -r requirements.txt
python -m apex_scalper.main
```

## Project Structure
```
apex_scalper/
  main.py          # Entrypoint
  config.py        # Settings from env
  feed.py          # Bybit WS public feed (orderbook, trades, klines)
  trader.py        # Private WS + order execution
  strategy.py      # Scalping signal logic
  risk.py          # Position sizing, drawdown guard
  telegram_ui.py   # Telegram bot commands
  state.py         # Shared in-memory state
tests/
docker-compose.yml
Dockerfile
requirements.txt
.env.example
```

## Configuration
All config lives in `.env`:

| Variable | Default | Description |
|---|---|---|
| `BYBIT_API_KEY` | — | Bybit V5 API key |
| `BYBIT_API_SECRET` | — | Bybit V5 API secret |
| `BYBIT_TESTNET` | `true` | Use testnet WS/REST |
| `SYMBOL` | `BTCUSDT` | Perpetual futures pair |
| `LEVERAGE` | `5` | Futures leverage |
| `ORDER_SIZE_USDT` | `10` | Base order size in USDT |
| `MAX_POSITIONS` | `1` | Max concurrent open positions |
| `DAILY_LOSS_LIMIT_USDT` | `30` | Stop trading after this loss |
| `TELEGRAM_TOKEN` | — | BotFather token |
| `TELEGRAM_CHAT_ID` | — | Your chat ID |

## Disclaimer
For educational purposes. Use at your own risk.
