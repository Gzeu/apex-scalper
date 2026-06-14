"""Load and validate all settings from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Bybit
    api_key: str = field(default_factory=lambda: os.environ["BYBIT_API_KEY"])
    api_secret: str = field(default_factory=lambda: os.environ["BYBIT_API_SECRET"])
    testnet: bool = field(default_factory=lambda: os.getenv("BYBIT_TESTNET", "true").lower() == "true")

    # Trading
    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", "BTCUSDT"))
    leverage: int = field(default_factory=lambda: int(os.getenv("LEVERAGE", "5")))
    order_size_usdt: float = field(default_factory=lambda: float(os.getenv("ORDER_SIZE_USDT", "10")))
    max_positions: int = field(default_factory=lambda: int(os.getenv("MAX_POSITIONS", "1")))
    daily_loss_limit_usdt: float = field(default_factory=lambda: float(os.getenv("DAILY_LOSS_LIMIT_USDT", "30")))

    # Telegram
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # Misc
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    @property
    def ws_public_url(self) -> str:
        if self.testnet:
            return "wss://stream-testnet.bybit.com/v5/public/linear"
        return "wss://stream.bybit.com/v5/public/linear"

    @property
    def ws_private_url(self) -> str:
        if self.testnet:
            return "wss://stream-testnet.bybit.com/v5/private"
        return "wss://stream.bybit.com/v5/private"

    @property
    def rest_url(self) -> str:
        if self.testnet:
            return "https://api-testnet.bybit.com"
        return "https://api.bybit.com"


config = Config()
