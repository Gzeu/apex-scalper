# Apex Scalper

Bot de scalping automat pentru Bybit USDT Perpetual Futures.
Strategie bazata pe EMA, RSI, ATR, book pressure si MTF bias.

## Pornire rapida

### 1. Configurare `.env`

```bash
cp .env.example .env
# Editeaza .env cu cheile tale API
```

Variabile obligatorii:

```env
BYBIT_API_KEY=your_api_key
BYBIT_API_SECRET=your_api_secret
BYBIT_TESTNET=false          # true pentru testnet
SYMBOL=BTCUSDT
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Variabile optionale:

```env
LEVERAGE=5
ORDER_SIZE_USDT=20
DAILY_LOSS_LIMIT_USDT=50
LOG_LEVEL=INFO
PULSE_INTERVAL_S=60
FEED_STALE_S=30
```

### 2. Pornire cu Python direct

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Optiunea A: modul
python -m apex_scalper

# Optiunea B: wrapper script
python run_bot.py
```

### 3. Pornire cu Docker

```bash
docker compose up -d
docker compose logs -f
```

### 4. Health check

```bash
curl http://localhost:8080/health
```

## Comenzi Telegram

| Comanda | Descriere |
|---|---|
| `/start` | Status complet la pornire + avertizare feed stale |
| `/status` | Status curent: pozitie, PnL, balance |
| `/balance` | Balanta USDT disponibila |
| `/tp` | Niveluri TP/SL curente |
| `/stop` | Opreste bot-ul graceful |
| `/daily` | Raport zilnic |
| `/watchdog` | Status watchdog + circuit breaker |
| `/risk` | Daily loss, consecutive losses, Kelly factor, open count |
| `/history [N]` | Ultimele N trade-uri inchise cu entry/PnL/motiv (max 30) |
| `/menu` | Meniu inline cu toate comenzile |

## Simboluri suportate

| Symbol | Leverage recomandat | Order size |
|---|---|---|
| BTCUSDT | 5x | 20 USDT |
| ETHUSDT | 7x | 15 USDT |
| HYPEUSDT | 5x | 10 USDT |
| DOGEUSDT | 5x | 10 USDT |
| NEARUSDT | 6x | 10 USDT |

Pentru multi-symbol: `SYMBOLS=BTCUSDT,ETHUSDT` in `.env`

## Arhitectura feed

> **v1.0.4**: Feed-ul WebSocket foloseste `websockets` nativ async in loc de
> `pybit.unified_trading.WebSocket`. Toata comunicarea ruleaza pe acelasi
> event loop asyncio — fara thread-switching, fara race conditions la startup.
>
> O singura conexiune multiplexata pentru `orderbook.50` + `kline.1`.
> Reconnect automat la 3s dupa orice eroare. Ping Bybit la 20s.

## Structura proiect

```
apex_scalper/
├── main.py              # Entrypoint + startup banner + midnight reset
├── config.py            # Configuratie + profiles per symbol
├── strategy.py          # Logica semnal (RSI, EMA, book pressure, 8 gate-uri)
├── position_manager.py  # TP scale-out, trailing SL, pyramid
├── trader.py            # Bybit REST API wrapper
├── circuit_breaker.py   # Protectie erori exchange
├── risk.py              # Sizing Kelly + daily loss limit
├── feed.py              # WebSocket nativ async (orderbook + kline)
├── mtf_filter.py        # Multi-timeframe EMA50(15m) bias
├── regime_filter.py     # ADX + Hurst regime detection
├── book_pressure.py     # Bid/ask imbalance
├── anti_manipulation.py # Wall detection
├── persistence.py       # SQLite (trades, daily PnL, metrics, get_last_trades)
├── telegram_ui.py       # Telegram bot comenzi + notify_open/tp/sl/close
├── watchdog.py          # Auto-restart la blocaj
├── pulse.py             # Raport periodic Telegram
├── health.py            # HTTP health endpoint
├── indicator_warmup.py  # 60 candle-uri historice la startup
└── state.py             # State global shared
```

## Changelog

| Versiune | Descriere |
|---|---|
| **v1.0.4** | Feed rescris cu `websockets` nativ async — fix candle silentios |
| **v1.0.3** | Startup banner, midnight reset cu stats, `/risk`, `/history`, shutdown alert |
| **v1.0.2** | notify_tp/sl/close in position_manager |
| **v1.0.1** | FEED_STALE_S race condition fix, evaluate pe tick live |
| **v1.0.0** | Debug logging la fiecare gate blocat |
| v0.9.6 | `get_last_trades()` in persistence pentru `/history` |
| v0.9.5 | SQLite auto-cleanup + vacuum, docker-compose |
| v0.9.4 | Pyramid margin check, graceful shutdown |
| v0.9.3 | Config fail-fast validate() |
| v0.9.2 | Circuit breaker, close_position retry |
| v0.9.1 | `__main__.py`, run_bot.py fix |
| v0.9.0 | Watchdog restart fix (Bug 39) |
| v0.8.x | 39 bug fixes |
