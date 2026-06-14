"""Entrypoint v0.7.8.

New in v0.7.8 vs v0.7.7:
  - log_sink.py: structured JSON logs in logs/apex_structured.jsonl
    Zero dependente noi. Parsabil cu jq din terminal sau Grafana Loki.
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
from .persistence import db
from .mtf_filter import mtf, run_mtf_refresh_loop
from .funding_rate import run_funding_refresh_loop
from .daily_report import run_daily_report_loop
from .anti_manipulation import inject_wall_params
from .regime_filter import regime
from .book_pressure import bp
from .pulse import run_pulse_loop
from .health import start_health_server
from .log_sink import setup_json_sink


def setup_logging() -> None:
    logger.remove()
    # Sink 1: stderr (color, human readable)
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )
    # Sink 2: text file rotativ
    logger.add(
        "logs/apex_scalper.log",
        rotation="10 MB",
        retention="14 days",
        level=config.log_level,
    )
    # Sink 3: JSON structurat (parsabil jq / Grafana Loki)
    setup_json_sink()


def inject_profile(symbol: str) -> None:
    import apex_scalper.strategy         as sm
    import apex_scalper.risk             as rm
    import apex_scalper.position_manager as pm
    import apex_scalper.regime_filter    as rf
    import apex_scalper.book_pressure    as bpm

    p = SYMBOL_PROFILES.get(symbol, SYMBOL_PROFILES["BTCUSDT"])

    sm.RSI_LONG_MIN     = p["rsi_long_min"]
    sm.RSI_SHORT_MAX    = p["rsi_short_max"]
    sm.IMBALANCE_LONG   = p["imbalance_long"]
    sm.IMBALANCE_SHORT  = p["imbalance_short"]
    sm.VOL_ZSCORE_MIN   = p["vol_zscore_min"]
    sm.ATR_MIN_PCT      = p["atr_min_pct"]
    sm.ATR_MAX_PCT      = p["atr_max_pct"]
    sm.ENTRY_THRESHOLD  = p.get("entry_threshold", 0.65)
    sm.RSI_OB_PENALTY   = p.get("rsi_ob_penalty", 65.0)
    sm.RSI_OS_PENALTY   = p.get("rsi_os_penalty", 35.0)
    sm.BASE_SPREAD_BPS  = p.get("base_spread_bps", 3.0)
    sm.ATR_SPREAD_MULT  = p.get("atr_spread_mult", 2.0)
    sm.ATR_BASELINE     = p.get("atr_baseline",    0.001)

    rm.MAX_SPREAD_BPS   = p["max_spread_bps"]
    rm.MIN_BID_DEPTH    = p["min_bid_depth"]
    rm.MIN_ASK_DEPTH    = p["min_ask_depth"]
    rm.MAX_DAILY_LOSS   = p.get("daily_loss_limit_usdt", 50.0)

    pm.TP1_PCT          = p["tp1_pct"]
    pm.TP2_PCT          = p["tp2_pct"]
    pm.TP3_PCT          = p.get("tp3_pct",       0.0035)
    pm.SL_PCT           = p["sl_pct"]
    pm.TRAIL_PCT        = p["trail_pct"]
    pm.TRAIL_DELTA      = p["trail_delta"]
    pm.MAX_HOLD_CANDLES = p["max_hold_candles"]
    pm.MAX_PYRAMID_ADDS = p.get("max_pyramid_adds", 1)
    pm.TP1_FRACTION     = p.get("tp1_fraction",    0.25)
    pm.TP2_FRACTION     = p.get("tp2_fraction",    0.25)
    pm.TP3_FRACTION     = p.get("tp3_fraction",    0.50)

    rf.ADX_TRENDING_MIN = p.get("adx_trending_min", 25.0)
    rf.ADX_RANGING_MAX  = p.get("adx_ranging_max",  20.0)
    rf.ATR_VOLATILE_PCT = p.get("atr_volatile_pct", 80.0)
    rf.ATR_RANGING_PCT  = p.get("atr_ranging_pct",  20.0)
    rf.HURST_TREND_MIN  = p.get("hurst_trend_min",  0.55)
    rf.HURST_RANGE_MAX  = p.get("hurst_range_max",  0.45)

    bpm.BASE_THRESHOLD   = p.get("bp_base_threshold",   50_000.0)
    bpm.ABSORPTION_RATIO = p.get("bp_absorption_ratio", 3.0)

    inject_wall_params(
        wall_ratio=p.get("wall_ratio", 8.0),
        wall_distance_ticks=p.get("wall_distance_ticks", 5),
    )

    logger.info(
        f"\u2705 Profile injected [{symbol}]: "
        f"TP1={p['tp1_pct']:.4f}({p.get('tp1_fraction',0.25):.0%}) "
        f"TP2={p['tp2_pct']:.4f}({p.get('tp2_fraction',0.25):.0%}) "
        f"TP3={p.get('tp3_pct',0.0035):.4f}({p.get('tp3_fraction',0.50):.0%}) "
        f"SL={p['sl_pct']:.4f} lev={p['leverage']}x "
        f"threshold={p.get('entry_threshold', 0.65)} "
        f"adx_min={p.get('adx_trending_min', 25)} "
        f"bp_thr={p.get('bp_base_threshold', 50000):.0f} "
        f"pyramid_max={p.get('max_pyramid_adds', 1)}"
    )


async def _midnight_reset_loop() -> None:
    from datetime import datetime, timezone, timedelta
    while True:
        now    = datetime.now(timezone.utc)
        target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        await asyncio.sleep((target - now).total_seconds())
        from .risk import risk as r
        r.reset_daily()
        with state.lock:
            state.daily_pnl = 0.0
        logger.info("Daily PnL counters reset at UTC midnight")


async def _shutdown(loop: asyncio.AbstractEventLoop, tg_app=None) -> None:
    logger.warning("🛑 Shutdown — closing position if open...")
    state.running = False
    try:
        await trader.close_position(use_limit=False)
    except Exception as e:
        logger.error(f"Shutdown close_position error: {e}")
    if tg_app:
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            pass
    tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


async def main() -> None:
    setup_logging()
    env_label = "TESTNET" if config.testnet else "⚠️  MAINNET"
    logger.info(
        f"⚡ Apex Scalper v0.7.8 | {config.symbol} | "
        f"{env_label} | lev={config.leverage}x size={config.order_size_usdt}USDT"
    )

    inject_profile(config.symbol)
    await trader.setup()
    await trader.sync_position_from_exchange()

    daily_pnl, total_trades, win_trades = db.load_daily_pnl(config.symbol)
    with state.lock:
        state.daily_pnl    = daily_pnl
        state.total_trades = total_trades
        state.win_trades   = win_trades
    if total_trades > 0:
        logger.info(
            f"Restored from DB: daily_pnl={daily_pnl:.4f} USDT "
            f"trades={total_trades} wr={round(win_trades/total_trades*100,1)}%"
        )

    logger.info("Fetching MTF EMA50(15m) before starting feed...")
    await mtf.refresh(config.symbol)
    if mtf.ready:
        logger.info(f"MTF ready: EMA50(15m)={mtf.ema50:.4f}")
    else:
        logger.warning("MTF fetch failed — entries BLOCKED until first successful refresh.")

    start_health_server()

    loop   = asyncio.get_running_loop()
    tg_app = None

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(loop, tg_app)))

    if config.telegram_token:
        tg_app = build_app()
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot ready")
    else:
        logger.warning("TELEGRAM_TOKEN not set — Telegram disabled")

    asyncio.create_task(run_watchdog())
    asyncio.create_task(run_mtf_refresh_loop(config.symbol))
    asyncio.create_task(run_funding_refresh_loop(config.symbol))
    asyncio.create_task(run_daily_report_loop(config.symbol))
    asyncio.create_task(_midnight_reset_loop())
    asyncio.create_task(run_pulse_loop(config.symbol))
    logger.info(
        "Background tasks: watchdog | MTF | funding | daily_report | "
        "midnight_reset | pulse (1min) ✅"
    )

    with state.lock:
        state.running = True
    logger.info(
        f"state.running = True — strategy v0.7.8 active\n"
        f"  JSON logs:   logs/apex_structured.jsonl (jq parsabil)\n"
        f"  Pulse:       fiecare {__import__('os').getenv('PULSE_INTERVAL_S', '60')}s pe Telegram\n"
        f"  Health:      http://localhost:8080/health\n"
        f"  Scale-out:   TP1={pm_info()} | Kelly active after 20 trades"
    )

    await start_feed()


def pm_info() -> str:
    try:
        import apex_scalper.position_manager as pm
        return (
            f"{pm.TP1_PCT:.4f}({pm.TP1_FRACTION:.0%}) "
            f"TP2={pm.TP2_PCT:.4f}({pm.TP2_FRACTION:.0%}) "
            f"TP3={pm.TP3_PCT:.4f}({pm.TP3_FRACTION:.0%})"
        )
    except Exception:
        return "?"


if __name__ == "__main__":
    asyncio.run(main())
