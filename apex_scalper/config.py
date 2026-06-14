"""Settings loader with per-symbol optimal defaults.

Supports multi-symbol mode: set SYMBOLS=BTCUSDT,ETHUSDT,HYPEUSDT
For single symbol mode: set SYMBOL=BTCUSDT (legacy, still works)

Per-symbol profiles are tuned for Bybit USDT Perpetual Futures
based on 24h volume ranking and volatility characteristics (June 2026).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Per-symbol optimal parameter profiles
# Tuned for today's top-5 Bybit USDT Perp by 24h volume
# ---------------------------------------------------------------------------
SYMBOL_PROFILES: dict[str, dict] = {
    # 1st. BTC/USDT — $2.1B 24h vol (41.5% of total)
    # High liquidity, low spread, moderate volatility. Conservative params.
    "BTCUSDT": {
        "leverage":          5,
        "order_size_usdt":   20,
        "tp1_pct":           0.0012,   # 0.12%
        "tp2_pct":           0.0025,   # 0.25%
        "sl_pct":            0.0008,   # 0.08%
        "trail_pct":         0.0015,   # activate trail at 0.15%
        "trail_delta":       0.0005,   # 0.05% trail distance
        "max_hold_candles":  5,
        "max_spread_bps":    3.0,      # BTC spread is tight
        "min_bid_depth":     1.0,      # BTC — deep book
        "min_ask_depth":     1.0,
        "atr_min_pct":       0.00025,  # BTC moves enough
        "atr_max_pct":       0.004,
        "rsi_long_min":      52.0,
        "rsi_short_max":     48.0,
        "imbalance_long":    0.08,
        "imbalance_short":   -0.08,
        "entry_threshold":   0.60,
        "vol_zscore_min":    0.0,
    },
    # 2nd. ETH/USDT — $875M 24h vol (17.4%)
    # Very liquid, tight spreads, slightly higher volatility than BTC.
    "ETHUSDT": {
        "leverage":          7,
        "order_size_usdt":   15,
        "tp1_pct":           0.0013,
        "tp2_pct":           0.0028,
        "sl_pct":            0.0009,
        "trail_pct":         0.0018,
        "trail_delta":       0.0006,
        "max_hold_candles":  5,
        "max_spread_bps":    3.5,
        "min_bid_depth":     5.0,      # ETH — measured in ETH units
        "min_ask_depth":     5.0,
        "atr_min_pct":       0.0003,
        "atr_max_pct":       0.005,
        "rsi_long_min":      51.0,
        "rsi_short_max":     49.0,
        "imbalance_long":    0.09,
        "imbalance_short":   -0.09,
        "entry_threshold":   0.58,
        "vol_zscore_min":    0.0,
    },
    # 3rd. HYPE/USDT — $261M 24h vol (5.2%)
    # High momentum coin, higher volatility, wider spreads. Wider TP/SL.
    "HYPEUSDT": {
        "leverage":          5,
        "order_size_usdt":   10,
        "tp1_pct":           0.0020,   # 0.20% — wider due to volatility
        "tp2_pct":           0.0045,   # 0.45%
        "sl_pct":            0.0015,   # 0.15%
        "trail_pct":         0.0025,
        "trail_delta":       0.0010,
        "max_hold_candles":  4,        # Exit faster on high-vol coin
        "max_spread_bps":    8.0,      # HYPE has wider spreads
        "min_bid_depth":     50.0,     # HYPE — in HYPE units
        "min_ask_depth":     50.0,
        "atr_min_pct":       0.0006,   # Only trade when moving
        "atr_max_pct":       0.010,
        "rsi_long_min":      53.0,     # Stricter momentum confirmation
        "rsi_short_max":     47.0,
        "imbalance_long":    0.12,
        "imbalance_short":   -0.12,
        "entry_threshold":   0.65,     # Stricter for high-vol
        "vol_zscore_min":    0.3,      # Require above-avg volume
    },
    # 4th. DOGE/USDT — $134M 24h vol (2.7%)
    # Meme coin, choppy, high tick noise. Conservative, high threshold.
    "DOGEUSDT": {
        "leverage":          5,
        "order_size_usdt":   10,
        "tp1_pct":           0.0018,
        "tp2_pct":           0.0040,
        "sl_pct":            0.0012,
        "trail_pct":         0.0020,
        "trail_delta":       0.0008,
        "max_hold_candles":  4,
        "max_spread_bps":    6.0,
        "min_bid_depth":     5000.0,  # DOGE — in DOGE units
        "min_ask_depth":     5000.0,
        "atr_min_pct":       0.0004,
        "atr_max_pct":       0.008,
        "rsi_long_min":      54.0,
        "rsi_short_max":     46.0,
        "imbalance_long":    0.15,    # Need strong imbalance for DOGE
        "imbalance_short":   -0.15,
        "entry_threshold":   0.68,    # High threshold — DOGE is noisy
        "vol_zscore_min":    0.5,     # Only trade on real volume spikes
    },
    # 5th. NEAR/USDT — $102M 24h vol (2.0%)
    # Mid-cap, decent volatility, reasonable spreads.
    "NEARUSDT": {
        "leverage":          6,
        "order_size_usdt":   10,
        "tp1_pct":           0.0015,
        "tp2_pct":           0.0033,
        "sl_pct":            0.0010,
        "trail_pct":         0.0020,
        "trail_delta":       0.0007,
        "max_hold_candles":  4,
        "max_spread_bps":    7.0,
        "min_bid_depth":     200.0,
        "min_ask_depth":     200.0,
        "atr_min_pct":       0.0005,
        "atr_max_pct":       0.009,
        "rsi_long_min":      52.0,
        "rsi_short_max":     48.0,
        "imbalance_long":    0.11,
        "imbalance_short":   -0.11,
        "entry_threshold":   0.63,
        "vol_zscore_min":    0.2,
    },
}

DEFAULT_SYMBOL = "BTCUSDT"


@dataclass
class Config:
    # Bybit auth
    api_key: str = field(default_factory=lambda: os.environ["BYBIT_API_KEY"])
    api_secret: str = field(default_factory=lambda: os.environ["BYBIT_API_SECRET"])
    testnet: bool = field(default_factory=lambda: os.getenv("BYBIT_TESTNET", "true").lower() == "true")

    # Single-symbol mode (default)
    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", DEFAULT_SYMBOL))

    # Multi-symbol mode: comma-separated, e.g. BTCUSDT,ETHUSDT,HYPEUSDT
    symbols: list = field(default_factory=lambda: [
        s.strip() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()
    ])

    # Trading (defaults overridden by profile if symbol is known)
    leverage: int = field(default_factory=lambda: int(os.getenv("LEVERAGE", "5")))
    order_size_usdt: float = field(default_factory=lambda: float(os.getenv("ORDER_SIZE_USDT", "10")))
    daily_loss_limit_usdt: float = field(default_factory=lambda: float(os.getenv("DAILY_LOSS_LIMIT_USDT", "30")))

    # Telegram
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # Misc
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def profile(self, symbol: str | None = None) -> dict:
        """Return optimal param dict for a symbol. Falls back to BTCUSDT defaults."""
        sym = symbol or self.symbol
        return SYMBOL_PROFILES.get(sym, SYMBOL_PROFILES[DEFAULT_SYMBOL])

    def active_symbols(self) -> list[str]:
        """Return list of symbols to trade (multi or single)."""
        if self.symbols:
            return self.symbols
        return [self.symbol]

    @property
    def ws_public_url(self) -> str:
        return (
            "wss://stream-testnet.bybit.com/v5/public/linear" if self.testnet
            else "wss://stream.bybit.com/v5/public/linear"
        )

    @property
    def ws_private_url(self) -> str:
        return (
            "wss://stream-testnet.bybit.com/v5/private" if self.testnet
            else "wss://stream.bybit.com/v5/private"
        )

    @property
    def rest_url(self) -> str:
        return (
            "https://api-testnet.bybit.com" if self.testnet
            else "https://api.bybit.com"
        )


config = Config()
