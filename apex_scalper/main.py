"""Entrypoint v0.3.1.

Fixes vs v0.3.0:
- inject_profile() called on startup: per-symbol params now actually applied.
- trader.setup() called async: leverage set correctly, no sync-in-__init__ crash.
- sync_position_from_exchange() on startup: open positions survive restarts.
- _shutdown() wraps close_position() in try/except (was unguarded).
"""
from __future__ import annotations

import asyncio
import signal
import sys
from loguru import logger

from .config import config, SYMBOL_PROFILES
from .feed import start_feed
from .telegram_ui import build_app
from .watchdog import run_watchdog
from .state import state
from .trader import trader


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )
    logger.add(
        "logs/apex_scalper.log",
        rotation="10 MB",
        retention="14 days",
        level=config.log_level,
    )


def inject_profile(symbol: str) -> None:
    """Inject per-symbol optimal params into strategy / risk / position_manager.

    FIX: this was never called in v0.3.0 — all profile params were ignored.
    """
    import apex_scalper.strategy        as sm
    import apex_scalper.risk            as rm
    import apex_scalper.position_manager as pm

    p = SYMBOL_PROFILES.get(symbol, SYMBOL_PROFILES["BTCUSDT"])

    # Strategy signals
    sm.RSI_LONG_MIN    = p["rsi_long_min"]
    sm.RSI_SHORT_MAX   = p["rsi_short_max"]
    sm.IMBALANCE_LONG  = p["imbalance_long"]
    sm.IMBALANCE_SHORT = p["imbalance_short"]
    sm.VOL_ZSCORE_MIN  = p["vol_zscore_min"]
    sm.ATR_MIN_PCT     = p["atr_min_pct"]
    sm.ATR_MAX_PCT     = p["atr_max_pct"]
    sm.ENTRY_THRESHOLD = p["entry_threshold"]

    # Risk
    rm.MAX_SPREAD_BPS = p["max_spread_bps"]
    rm.MIN_BID_DEPTH  = p["min_bid_depth"]
    rm.MIN_ASK_DEPTH  = p["min_ask_depth"]

    # Position manager
    pm.TP1_PCT          = p["tp1_pct"]
    pm.TP2_PCT          = p["tp2_pct"]
    pm.SL_PCT           = p["sl_pct"]
    pm.TRAIL_PCT        = p["trail_pct"]
    pm.TRAIL_DELTA      = p["trail_delta"]
    pm.MAX_HOLD_CANDLES = p["max_hold_candles"]

    logger.info(
        f"✅ Profile injected [{symbol}]: "
        f"TP1={p['tp1_pct']:.4f} TP2={p['tp2_pct']:.4f} "
        f"SL={p['sl_pct']:.4f} lev={p['leverage']}x "
        f"threshold={p['entry_threshold']}"
    )


async def _shutdown(loop: asyncio.AbstractEventLoop, tg_app=None) -> None:
    logger.warning("🛑 Shutdown — closing position if open...")
    state.running = False
    try:
        await trader.close_position()
    except Exception as e:
        logger.error(f"Shutdown close_position error: {e}")
    if tg_app:
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            pass
    tasks = [
        t for t in asyncio.all_tasks(loop)
        if t is not asyncio.current_task()
    ]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


async def main() -> None:
    setup_logging()
    logger.info(
        f"⚡ Apex Scalper v0.3.1 | {config.symbol} | "
        f"{'TESTNET' if config.testnet else '⚠️  MAINNET'} | "
        f"lev={config.leverage}x size={config.order_size_usdt}USDT"
    )

    # 1. Inject per-symbol optimal params (FIX: was missing in v0.3.0)
    inject_profile(config.symbol)

    # 2. Async trader setup: create HTTP session + set leverage
    await trader.setup()

    # 3. Sync position state from exchange (survive restarts with open pos)
    await trader.sync_position_from_exchange()

    loop = asyncio.get_running_loop()
    tg_app = None

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.create_task(_shutdown(loop, tg_app))
        )

    # 4. Telegram bot
    if config.telegram_token:
        tg_app = build_app()
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot ready")
    else:
        logger.warning("TELEGRAM_TOKEN not set — Telegram disabled")

    # 5. Watchdog task
    asyncio.create_task(run_watchdog())

    # 6. WS feed (reconnect loop inside start_feed)
    await start_feed()


if __name__ == "__main__":
    asyncio.run(main())
