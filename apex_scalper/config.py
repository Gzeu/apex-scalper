"""Settings loader with per-symbol optimal defaults.

v0.7.1: All profiles updated with tp3_pct, TP1/2/3 fractions, MAX_PYRAMID_ADDS,
        bp_base_threshold, regime thresholds, RSI penalty levels.
        inject_profile() in main.py now uses all these fields.

v0.4.1: Added wall_ratio and wall_distance_ticks per symbol
        for anti_manipulation.py parametrization.

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
# ---------------------------------------------------------------------------
SYMBOL_PROFILES: dict[str, dict] = {
    # 1st. BTC/USDT — $2.1B 24h vol (41.5% of total)
    "BTCUSDT": {
        "leverage":             5,
        "order_size_usdt":      20,
        "daily_loss_limit_usdt": 50.0,
        # TP scale-out (3 levels)
        "tp1_pct":              0.0012,
        "tp2_pct":              0.0025,
        "tp3_pct":              0.0040,
        "tp1_fraction":         0.25,
        "tp2_fraction":         0.25,
        "tp3_fraction":         0.50,
        "sl_pct":               0.0008,
        "trail_pct":            0.0015,
        "trail_delta":          0.0005,
        "max_hold_candles":     5,
        "max_pyramid_adds":     1,
        # Spread / depth
        "max_spread_bps":       3.0,
        "base_spread_bps":      3.0,
        "atr_spread_mult":      2.0,
        "atr_baseline":         0.001,
        "min_bid_depth":        1.0,
        "min_ask_depth":        1.0,
        # ATR gate
        "atr_min_pct":          0.00025,
        "atr_max_pct":          0.004,
        # Signal thresholds
        "rsi_long_min":         52.0,
        "rsi_short_max":        48.0,
        "rsi_ob_penalty":       65.0,
        "rsi_os_penalty":       35.0,
        "imbalance_long":       0.08,
        "imbalance_short":      -0.08,
        "entry_threshold":      0.60,
        "vol_zscore_min":       0.0,
        # Book pressure
        "bp_base_threshold":    50_000.0,
        "bp_absorption_ratio":  3.0,
        # Regime filter
        "adx_trending_min":     25.0,
        "adx_ranging_max":      20.0,
        "atr_volatile_pct":     80.0,
        "atr_ranging_pct":      20.0,
        "hurst_trend_min":      0.55,
        "hurst_range_max":      0.45,
        # Anti-manipulation
        "wall_ratio":           8.0,
        "wall_distance_ticks":  5,
    },
    # 2nd. ETH/USDT — $875M 24h vol
    "ETHUSDT": {
        "leverage":             7,
        "order_size_usdt":      15,
        "daily_loss_limit_usdt": 40.0,
        "tp1_pct":              0.0013,
        "tp2_pct":              0.0028,
        "tp3_pct":              0.0045,
        "tp1_fraction":         0.25,
        "tp2_fraction":         0.25,
        "tp3_fraction":         0.50,
        "sl_pct":               0.0009,
        "trail_pct":            0.0018,
        "trail_delta":          0.0006,
        "max_hold_candles":     5,
        "max_pyramid_adds":     1,
        "max_spread_bps":       3.5,
        "base_spread_bps":      3.5,
        "atr_spread_mult":      2.0,
        "atr_baseline":         0.001,
        "min_bid_depth":        5.0,
        "min_ask_depth":        5.0,
        "atr_min_pct":          0.0003,
        "atr_max_pct":          0.005,
        "rsi_long_min":         51.0,
        "rsi_short_max":        49.0,
        "rsi_ob_penalty":       65.0,
        "rsi_os_penalty":       35.0,
        "imbalance_long":       0.09,
        "imbalance_short":      -0.09,
        "entry_threshold":      0.58,
        "vol_zscore_min":       0.0,
        "bp_base_threshold":    20_000.0,
        "bp_absorption_ratio":  3.0,
        "adx_trending_min":     25.0,
        "adx_ranging_max":      20.0,
        "atr_volatile_pct":     80.0,
        "atr_ranging_pct":      20.0,
        "hurst_trend_min":      0.55,
        "hurst_range_max":      0.45,
        "wall_ratio":           7.0,
        "wall_distance_ticks":  4,
    },
    # 3rd. HYPE/USDT — $261M 24h vol
    "HYPEUSDT": {
        "leverage":             5,
        "order_size_usdt":      10,
        "daily_loss_limit_usdt": 25.0,
        "tp1_pct":              0.0020,
        "tp2_pct":              0.0045,
        "tp3_pct":              0.0075,
        "tp1_fraction":         0.30,
        "tp2_fraction":         0.30,
        "tp3_fraction":         0.40,
        "sl_pct":               0.0015,
        "trail_pct":            0.0025,
        "trail_delta":          0.0010,
        "max_hold_candles":     4,
        "max_pyramid_adds":     0,
        "max_spread_bps":       8.0,
        "base_spread_bps":      8.0,
        "atr_spread_mult":      3.0,
        "atr_baseline":         0.002,
        "min_bid_depth":        50.0,
        "min_ask_depth":        50.0,
        "atr_min_pct":          0.0006,
        "atr_max_pct":          0.010,
        "rsi_long_min":         53.0,
        "rsi_short_max":        47.0,
        "rsi_ob_penalty":       68.0,
        "rsi_os_penalty":       32.0,
        "imbalance_long":       0.12,
        "imbalance_short":      -0.12,
        "entry_threshold":      0.65,
        "vol_zscore_min":       0.3,
        "bp_base_threshold":    8_000.0,
        "bp_absorption_ratio":  2.5,
        "adx_trending_min":     22.0,
        "adx_ranging_max":      18.0,
        "atr_volatile_pct":     75.0,
        "atr_ranging_pct":      25.0,
        "hurst_trend_min":      0.55,
        "hurst_range_max":      0.45,
        "wall_ratio":           5.0,
        "wall_distance_ticks":  3,
    },
    # 4th. DOGE/USDT — $134M 24h vol
    "DOGEUSDT": {
        "leverage":             5,
        "order_size_usdt":      10,
        "daily_loss_limit_usdt": 25.0,
        "tp1_pct":              0.0018,
        "tp2_pct":              0.0040,
        "tp3_pct":              0.0065,
        "tp1_fraction":         0.25,
        "tp2_fraction":         0.25,
        "tp3_fraction":         0.50,
        "sl_pct":               0.0012,
        "trail_pct":            0.0020,
        "trail_delta":          0.0008,
        "max_hold_candles":     4,
        "max_pyramid_adds":     0,
        "max_spread_bps":       6.0,
        "base_spread_bps":      6.0,
        "atr_spread_mult":      2.5,
        "atr_baseline":         0.0015,
        "min_bid_depth":        5000.0,
        "min_ask_depth":        5000.0,
        "atr_min_pct":          0.0004,
        "atr_max_pct":          0.008,
        "rsi_long_min":         54.0,
        "rsi_short_max":        46.0,
        "rsi_ob_penalty":       68.0,
        "rsi_os_penalty":       32.0,
        "imbalance_long":       0.15,
        "imbalance_short":      -0.15,
        "entry_threshold":      0.68,
        "vol_zscore_min":       0.5,
        "bp_base_threshold":    5_000.0,
        "bp_absorption_ratio":  2.0,
        "adx_trending_min":     20.0,
        "adx_ranging_max":      15.0,
        "atr_volatile_pct":     75.0,
        "atr_ranging_pct":      25.0,
        "hurst_trend_min":      0.55,
        "hurst_range_max":      0.45,
        "wall_ratio":           4.0,
        "wall_distance_ticks":  3,
    },
    # 5th. NEAR/USDT — $102M 24h vol
    "NEARUSDT": {
        "leverage":             6,
        "order_size_usdt":      10,
        "daily_loss_limit_usdt": 20.0,
        "tp1_pct":              0.0015,
        "tp2_pct":              0.0033,
        "tp3_pct":              0.0055,
        "tp1_fraction":         0.25,
        "tp2_fraction":         0.25,
        "tp3_fraction":         0.50,
        "sl_pct":               0.0010,
        "trail_pct":            0.0020,
        "trail_delta":          0.0007,
        "max_hold_candles":     4,
        "max_pyramid_adds":     1,
        "max_spread_bps":       7.0,
        "base_spread_bps":      7.0,
        "atr_spread_mult":      2.5,
        "atr_baseline":         0.0015,
        "min_bid_depth":        200.0,
        "min_ask_depth":        200.0,
        "atr_min_pct":          0.0005,
        "atr_max_pct":          0.009,
        "rsi_long_min":         52.0,
        "rsi_short_max":        48.0,
        "rsi_ob_penalty":       66.0,
        "rsi_os_penalty":       34.0,
        "imbalance_long":       0.11,
        "imbalance_short":      -0.11,
        "entry_threshold":      0.63,
        "vol_zscore_min":       0.2,
        "bp_base_threshold":    3_000.0,
        "bp_absorption_ratio":  2.0,
        "adx_trending_min":     22.0,
        "adx_ranging_max":      17.0,
        "atr_volatile_pct":     78.0,
        "atr_ranging_pct":      22.0,
        "hurst_trend_min":      0.55,
        "hurst_range_max":      0.45,
        "wall_ratio":           5.0,
        "wall_distance_ticks":  3,
    },
}

DEFAULT_SYMBOL = "BTCUSDT"


@dataclass
class Config:
    api_key:    str  = field(default_factory=lambda: os.environ["BYBIT_API_KEY"])
    api_secret: str  = field(default_factory=lambda: os.environ["BYBIT_API_SECRET"])
    testnet:    bool = field(default_factory=lambda: os.getenv("BYBIT_TESTNET", "true").lower() == "true")
    symbol:     str  = field(default_factory=lambda: os.getenv("SYMBOL", DEFAULT_SYMBOL))
    symbols:    list = field(default_factory=lambda: [
        s.strip() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()
    ])
    leverage:          int   = field(default_factory=lambda: int(os.getenv("LEVERAGE", "5")))
    order_size_usdt:   float = field(default_factory=lambda: float(os.getenv("ORDER_SIZE_USDT", "10")))
    daily_loss_limit_usdt: float = field(default_factory=lambda: float(os.getenv("DAILY_LOSS_LIMIT_USDT", "30")))
    telegram_token:  str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def profile(self, symbol: str | None = None) -> dict:
        sym = symbol or self.symbol
        return SYMBOL_PROFILES.get(sym, SYMBOL_PROFILES[DEFAULT_SYMBOL])

    def active_symbols(self) -> list[str]:
        return self.symbols if self.symbols else [self.symbol]

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
