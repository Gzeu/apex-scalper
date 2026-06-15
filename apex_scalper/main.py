"""Entrypoint v1.0.3 — polish: startup/shutdown Telegram alerts.

Changelog:
  v1.0.3 — Telegram startup banner la pornire cu versiune, symbol, mode,
    config summary. Shutdown alert cu motiv. Daily midnight reset notificare.
  v0.9.8 — warmup_indicators() la startup.
  v0.9.4 — graceful shutdown cu confirmare.
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
from .strategy import set_main_loop
from .indicator_warmup import warmup_indicators

VERSION = "1.0.3"
SHUTDOWN_CLOSE_TIMEOUT = 15.0


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
    sm.RSI_OB_PENALTY   = p.get("rsi_ob_penalty",  65.0)
    sm.RSI_OS_PENALTY   = p.get("rsi_os_penalty",  35.0)
    sm.BASE_SPREAD_BPS  = p.get("base_spread_bps",  3.0)
    sm.ATR_SPREAD_MULT  = p.get("atr_spread_mult",  2.0)
    sm.ATR_BASELINE     = p.get("atr_baseline",     0.001)

    rm.MAX_SPREAD_BPS   = p["max_spread_bps"]
    rm.MIN_BID_DEPTH    = p["min_bid_depth"]
    rm.MIN_ASK_DEPTH    = p["min_ask_depth"]
    rm.MAX_DAILY_LOSS   = p.get("daily_loss_limit_usdt", 50.0)

    pm.TP1_PCT          = p["tp1_pct"]
    pm.TP2_PCT          = p["tp2_pct"]
    pm.TP3_PCT          = p.get("tp3_pct",         0.0035)
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
        f"pyramid_max={p.get('max_pyramid_adds', 1)}"
    )


async def _midnight_reset_loop() -> None:
    from datetime import datetime, timezone, timedelta
    from .telegram_ui import send_message
    while True:
        now    = datetime.now(timezone.utc)
        target = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0
        )
        await asyncio.sleep((target - now).total_seconds())

        from .risk import risk as r
        r.reset_daily()
        with state.lock:
            prev_pnl    = state.daily_pnl
            prev_trades = state.total_trades
            prev_wins   = state.win_trades
            state.daily_pnl    = 0.0
            state.total_trades = 0
            state.win_trades   = 0

        wr = round(prev_wins / prev_trades * 100, 1) if prev_trades > 0 else 0.0
        pnl_icon = "\U0001f7e2" if prev_pnl >= 0 else "\U0001f534"
        logger.info("Daily counters reset at UTC midnight")

        try:
            await send_message(
                f"{pnl_icon} *Daily Reset — UTC midnight*\n"
                f"PnL ieri: `{prev_pnl:+.4f} USDT`\n"
                f"Trades: `{prev_trades}` | WR: `{wr}%`\n"
                f"_Contoarele au fost resetate pentru ziua noua._"
            )
        except Exception:
            pass

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, db.run_maintenance)
        except Exception as e:
            logger.warning(f"DB maintenance failed: {e}")


async def _shutdown(loop: asyncio.AbstractEventLoop, tg_app=None) -> None:
    logger.warning("\U0001f6d1 Shutdown initiata — astept inchiderea pozitiei...")
    state.running = False

    with state.lock:
        has_position = bool(state.open_position)
        pos_side     = state.open_position
        pos_qty      = state.open_qty
        pos_entry    = state.open_entry

    if has_position:
        logger.warning(
            f"Shutdown: pozitie activa — "
            f"{pos_side} qty={pos_qty} entry={pos_entry} — inchidere..."
        )
        try:
            closed = await asyncio.wait_for(
                trader.close_position(use_limit=False),
                timeout=SHUTDOWN_CLOSE_TIMEOUT,
            )
            if closed:
                logger.info("Shutdown: pozitia inchisa \u2705")
                try:
                    from .telegram_ui import send_message
                    await send_message(
                        f"\U0001f6d1 *Apex Scalper OPRIT*\n"
                        f"Pozitie {pos_side} qty={pos_qty} @ {pos_entry} inchisa la shutdown."
                    )
                except Exception:
                    pass
            else:
                _alert_shutdown_failure(pos_side, pos_qty, pos_entry)
        except asyncio.TimeoutError:
            logger.critical(f"Shutdown: TIMEOUT {SHUTDOWN_CLOSE_TIMEOUT}s")
            _alert_shutdown_failure(pos_side, pos_qty, pos_entry, timeout=True)
        except Exception as e:
            logger.error(f"Shutdown close_position error: {e}")
            _alert_shutdown_failure(pos_side, pos_qty, pos_entry)
    else:
        logger.info("Shutdown: fara pozitie activa — oprire curata.")
        try:
            from .telegram_ui import send_message
            await send_message("\U0001f6d1 *Apex Scalper OPRIT* — fara pozitie activa.")
        except Exception:
            pass

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


def _alert_shutdown_failure(
    side: str, qty: float, entry: float, timeout: bool = False
) -> None:
    reason = f"TIMEOUT {SHUTDOWN_CLOSE_TIMEOUT}s" if timeout else "close_position ESUAT"
    msg = (
        f"\U0001f6a8 *CRITIC — Shutdown incomplet!*\n"
        f"Reason: `{reason}`\n"
        f"Pozitie: `{side} qty={qty} entry={entry}`\n"
        f"Verifica manual pe Bybit!"
    )
    logger.critical(msg)
    try:
        from .telegram_ui import send_message
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(send_message(msg))
    except Exception:
        pass


async def main() -> None:
    setup_logging()
    config.validate()

    logger.info(
        f"\u26a1 Apex Scalper v{VERSION} | {config.symbol} | "
        f"{'TESTNET' if config.testnet else chr(9888)+' MAINNET'} | "
        f"lev={config.leverage}x size={config.order_size_usdt}USDT"
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

    logger.info("Fetching MTF EMA50(15m)...")
    await mtf.refresh(config.symbol)
    if mtf.ready:
        logger.info(f"MTF ready: EMA50(15m)={mtf.ema50:.4f}")
    else:
        logger.warning("MTF fetch failed — entries BLOCKED until first refresh.")

    await warmup_indicators(config.symbol)

    start_health_server()

    loop   = asyncio.get_running_loop()
    set_main_loop(loop)
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

    with state.lock:
        state.running = True

    import apex_scalper.position_manager as pm
    balance = 0.0
    try:
        balance = await trader.get_balance()
    except Exception:
        pass

    startup_msg = (
        f"\u26a1 *Apex Scalper v{VERSION} PORNIT*\n"
        f"Symbol: `{config.symbol}` | "
        f"{'\u26a0\ufe0f MAINNET' if not config.testnet else 'TESTNET'}\n"
        f"Leverage: `{config.leverage}x` | Size: `{config.order_size_usdt} USDT`\n"
        f"Balance: `{balance:.2f} USDT`\n"
        f"MTF EMA50: `{mtf.ema50:.2f}` ({'\u2705 ready' if mtf.ready else '\u274c not ready'})\n"
        f"Entry threshold: `{pm.TP1_PCT:.4f}` TP1 / `{pm.SL_PCT:.4f}` SL\n"
        f"Daily loss limit: `{pm.SL_PCT * config.order_size_usdt * config.leverage:.2f} USDT` max\n"
        f"_Scrie /menu pentru control._"
    )
    try:
        from .telegram_ui import send_message
        await send_message(startup_msg)
    except Exception:
        pass

    logger.info(
        f"state.running = True — Apex Scalper v{VERSION} active\n"
        f"  JSON logs:   logs/apex_structured.jsonl\n"
        f"  Health:      http://localhost:8080/health\n"
        f"  Pulse:       fiecare {__import__('os').getenv('PULSE_INTERVAL_S', '60')}s"
    )

    await start_feed()


if __name__ == "__main__":
    asyncio.run(main())
