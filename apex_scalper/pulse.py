"""Pulse reporter v0.7.9 — fix race conditions Bug1 + Bug5.

Changelog:
  v0.7.9:
    Bug 1 fix: inlocuit toate accesele pm._* cu snap = await pm.snapshot()
      snap e PositionSnapshot imutabil citit atomic sub _snapshot_lock.
      Elimina race condition si potentialul ZeroDivisionError.
    Bug 5 fix: inlocuit _score_long(ind, ob, price) cu
      score_l, score_s = await score_snapshot(price, ob)
      Scorurile sunt acum calculate din snapshot consistent al ind sub _ind_lock.
  v0.7.7:
    - Loop 1 minut cu snapshot complet pe Telegram
    - Toggle /pulse on|off

ENV:
  PULSE_INTERVAL_S  (default 60)
  PULSE_ENABLED     (default true)
"""
from __future__ import annotations

import asyncio
import os
import time
from loguru import logger

PULSE_INTERVAL_S = int(os.getenv("PULSE_INTERVAL_S", "60"))
PULSE_ENABLED    = os.getenv("PULSE_ENABLED", "true").lower() == "true"

_start_time   = time.time()
_pulse_active = PULSE_ENABLED


def set_pulse_active(val: bool) -> None:
    global _pulse_active
    _pulse_active = val


def is_pulse_active() -> bool:
    return _pulse_active


def _uptime() -> str:
    s = int(time.time() - _start_time)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _bar(value: float, max_val: float = 1.0, width: int = 10) -> str:
    filled = int(round(min(value / max(max_val, 1e-9), 1.0) * width))
    return "█" * filled + "░" * (width - filled)


async def build_pulse_message() -> str:
    """Build the full pulse message from live data.

    v0.7.9 fixes:
      - pm fields: via await pm.snapshot() (atomic, no race condition)
      - scores: via await score_snapshot(price, ob) (consistent ind read)
    """
    from .state import state
    from .strategy import ind, score_snapshot, ENTRY_THRESHOLD
    from .orderbook_analytics import compute as compute_ob
    from .regime_filter import regime
    from .book_pressure import bp
    from .funding_rate import funding
    from .mtf_filter import mtf
    from .risk import risk
    from .config import config
    from .position_manager import (
        position_manager as pm,
        TP1_PCT, TP2_PCT, TP3_PCT, SL_PCT,
        TRAIL_PCT, MAX_HOLD_CANDLES,
    )

    with state.lock:
        price        = state.last_price
        pos          = state.open_position
        open_qty     = state.open_qty
        running      = state.running
        paused       = state.paused
        last_tick_ts = getattr(state, "last_tick_ts", 0.0)
        daily_pnl    = getattr(state, "daily_pnl", 0.0)
        total_trades = getattr(state, "total_trades", 0)
        win_trades   = getattr(state, "win_trades", 0)

    tick_age = round(time.time() - last_tick_ts, 2) if last_tick_ts else 99.9
    feed_ok  = tick_age < 2.0
    win_rate = round(win_trades / total_trades * 100, 1) if total_trades > 0 else 0.0

    ob = compute_ob()

    # Bug 5 fix: scoruri calculate atomic sub _ind_lock
    score_l, score_s = await score_snapshot(price, ob)

    bot_status = "\u2705 ACTIV" if (running and not paused) else ("\u23f8 PAUZA" if paused else "\ud83d\uded1 OPRIT")
    feed_icon  = "\u2705" if feed_ok else "\U0001f534"

    lines = [
        f"\u26a1 *Apex Pulse* `{config.symbol}` \u2014 `{time.strftime('%H:%M:%S UTC', time.gmtime())}`",
        f"",
        f"\U0001f9e0 *BOT*: {bot_status} | uptime `{_uptime()}` | feed {feed_icon} `{tick_age}s`",
        f"\U0001f4b0 *PnL azi*: `{daily_pnl:+.4f} USDT` | trades `{total_trades}` | WR `{win_rate}%`",
    ]

    # Bug 1 fix: snapshot atomic al PositionManager
    snap = await pm.snapshot()

    if pos and snap.entry_price > 0:
        pnl_pct  = snap.unrealised_pnl_pct(price)
        pnl_usdt = round(pnl_pct * snap.entry_price * open_qty, 4)
        pnl_icon = "\u2b06\ufe0f" if pnl_pct >= 0 else "\u2b07\ufe0f"
        sl_price = round(snap.entry_price * (1 - SL_PCT  if pos == "long" else 1 + SL_PCT), 2)
        tp1_p    = round(snap.entry_price * (1 + TP1_PCT if pos == "long" else 1 - TP1_PCT), 2)
        tp2_p    = round(snap.entry_price * (1 + TP2_PCT if pos == "long" else 1 - TP2_PCT), 2)
        tp3_p    = round(snap.entry_price * (1 + TP3_PCT if pos == "long" else 1 - TP3_PCT), 2)
        tp1_hit  = "\u2705" if snap.tp1_hit else "\u25fb\ufe0f"
        tp2_hit  = "\u2705" if snap.tp2_hit else "\u25fb\ufe0f"
        tp3_hit  = "\u2705" if snap.tp3_hit else "\u25fb\ufe0f"
        trail    = "\U0001f534 trail ON" if snap.trail_active else ""

        lines += [
            f"",
            f"{'\U0001f7e2' if pos == 'long' else '\U0001f534'} *POZITIE {pos.upper()}*",
            f"  entry `{snap.entry_price}` \u2192 acum `{price}` {pnl_icon} `{pnl_pct*100:+.4f}%` (`{pnl_usdt:+.4f}` USDT)",
            f"  qty `{open_qty}` | hold `{snap.hold_candles}/{MAX_HOLD_CANDLES}` candle {'\u23f0 timeout iminent' if snap.hold_candles >= MAX_HOLD_CANDLES - 1 else ''}",
            f"  SL `{sl_price}` | TP1 {tp1_hit}`{tp1_p}` TP2 {tp2_hit}`{tp2_p}` TP3 {tp3_hit}`{tp3_p}`",
            f"  pyramid adds: `{snap.pyramid_adds}` {trail}",
        ]
    else:
        lines += [
            f"",
            f"\u25ab\ufe0f *Fara pozitie deschisa*",
        ]

    # Indicatori (cititi direct din ind — ok pt afisare, nu calcul scoruri)
    rsi_str   = f"`{ind.rsi_value:.1f}`"    if ind.rsi_ready  else "`warmup`"
    atr_str   = f"`{ind.atr_value:.4f}`"   if ind.atr_ready  else "`warmup`"
    vol_str   = f"`{ind.vol_zscore:.2f}`"  if ind.vol_ready  else "`warmup`"
    macd_str  = f"`{ind.macd_histogram:+.5f}`" if ind.macd_ready else "`warmup`"
    stoch_str = (f"`{ind.stoch_k:.1f}`/`{ind.stoch_d:.1f}`" if ind.stoch_ready else "`warmup`")
    bb_str    = (f"`{ind.bb_lower:.1f}`\u2026`{ind.bb_upper:.1f}`" if ind.bb_ready else "`warmup`")
    vwap_str  = f"`{ind.vwap:.2f}`" if ind.vwap > 0 else "`-`"

    lines += [
        f"",
        f"\U0001f4ca *INDICATORI* @ `{price}`",
        f"  EMA 9/21/50: `{ind.ema_fast:.2f}` / `{ind.ema_slow:.2f}` / `{ind.ema_trend:.2f}`",
        f"  RSI(14): {rsi_str} | ATR(14): {atr_str} | Vol Z: {vol_str}",
        f"  MACD hist: {macd_str} | StochRSI %K/%D: {stoch_str}",
        f"  BB(20,2): {bb_str} | VWAP: {vwap_str}",
        f"  OB imbalance: `{ob.imbalance:.4f}` | pressure: `{ob.pressure_score:.4f}`",
    ]

    # Scoruri
    l_bar    = _bar(score_l)
    s_bar    = _bar(score_s)
    l_vs_thr = "\u2705 INTRARE" if score_l >= ENTRY_THRESHOLD else f"`{ENTRY_THRESHOLD - score_l:.3f}` lipsa"
    s_vs_thr = "\u2705 INTRARE" if score_s >= ENTRY_THRESHOLD else f"`{ENTRY_THRESHOLD - score_s:.3f}` lipsa"

    lines += [
        f"",
        f"\U0001f3af *SCORE ACUM*  (prag `{ENTRY_THRESHOLD}`)",
        f"  LONG:  `{score_l:.4f}` {l_bar} {l_vs_thr}",
        f"  SHORT: `{score_s:.4f}` {s_bar} {s_vs_thr}",
    ]

    # Regim
    regime_icon = {
        "TRENDING": "\U0001f7e2", "RANGING": "\U0001f534",
        "VOLATILE": "\U0001f7e1", "NEUTRAL": "\U0001f7e4",
    }.get(regime.label, "\u26ab")
    entry_ok = regime.allow_entry()

    lines += [
        f"",
        f"{regime_icon} *REGIM*: `{regime.label}` | ADX `{regime.adx:.1f}` | sz\u00d7`{regime.size_factor():.2f}`",
        f"  Entry: {'\u2705 permis' if entry_ok else '\u274c BLOCAT \u2014 RANGING'} | MTF: `{'BULL' if price > mtf.ema50 else 'BEAR'}` EMA50(15m)=`{mtf.ema50:.2f}`",
    ]

    # Book pressure
    p_long  = bp.pressure_long()
    p_short = bp.pressure_short()
    bp_dir  = ("\U0001f7e2 LONG" if p_long else ("\U0001f534 SHORT" if p_short else "\u26aa neutru"))

    lines += [
        f"",
        f"\U0001f4d6 *BOOK PRESSURE*: {bp_dir}",
        f"  cum\u0394 `{bp.cum_delta:+.0f}` | thr `\u00b1{bp._threshold():.0f}`",
    ]

    # Funding
    fund_long  = "\u2705" if funding.can_enter_long()  else "\u274c blocat"
    fund_short = "\u2705" if funding.can_enter_short() else "\u274c blocat"

    lines += [
        f"",
        f"\U0001f4b8 *FUNDING*: `{funding.rate_pct}` | LONG {fund_long} | SHORT {fund_short}",
    ]

    # Risk
    can_open = risk.can_open()
    consec   = getattr(risk, "_consecutive_losses", 0)
    kelly_f  = getattr(risk, "_kelly_factor", 0.5)

    lines += [
        f"",
        f"\U0001f6e1 *RISK*: can\_open `{'DA' if can_open else 'NU \u274c'}` | consec losses `{consec}` | Kelly `{kelly_f:.3f}`",
    ]

    # Urmatoarea actiune — foloseste snap (atomic)
    if not running or paused:
        next_action = "\u23f8 Bot oprit / pauza \u2014 fara tranzactii"
    elif pos and snap.entry_price > 0:
        if not snap.tp1_hit:
            next_action = f"\u23f3 Astept TP1 @ `{round(snap.entry_price * (1 + TP1_PCT if pos == 'long' else 1 - TP1_PCT), 2)}`"
        elif not snap.tp2_hit:
            next_action = f"\u23f3 Astept TP2 @ `{round(snap.entry_price * (1 + TP2_PCT if pos == 'long' else 1 - TP2_PCT), 2)}`"
        elif not snap.tp3_hit:
            next_action = f"\u23f3 Astept TP3 sau trail SL"
        elif snap.hold_candles >= MAX_HOLD_CANDLES - 1:
            next_action = "\u26a0\ufe0f Timeout iminent \u2014 inchide la urmatoarea lumanare"
        else:
            next_action = "\u23f3 Pozitie activa \u2014 monitorizez"
    elif not can_open:
        next_action = "\U0001f534 Risk blocat (daily limit / consecutive losses)"
    elif not entry_ok:
        next_action = "\U0001f534 Regim RANGING \u2014 astept TRENDING/NEUTRAL"
    elif not mtf.ready:
        next_action = "\u23f3 Astept MTF ready (EMA50 15m)"
    elif score_l >= ENTRY_THRESHOLD:
        next_action = f"\U0001f7e2 LONG TRIGGER \u2014 score `{score_l:.4f}` >= `{ENTRY_THRESHOLD}` \u2014 astept confirmare BP"
    elif score_s >= ENTRY_THRESHOLD:
        next_action = f"\U0001f534 SHORT TRIGGER \u2014 score `{score_s:.4f}` >= `{ENTRY_THRESHOLD}` \u2014 astept confirmare BP"
    else:
        best = max(score_l, score_s)
        next_action = f"\u23f3 Scanez \u2014 score max `{best:.4f}` (lipsesc `{ENTRY_THRESHOLD - best:.3f}` pana la prag)"

    lines += [
        f"",
        f"\u27a1\ufe0f *URMATOAREA ACTIUNE*:",
        f"  {next_action}",
        f"",
        f"_pulse interval: {PULSE_INTERVAL_S}s | /pulse off dezactiveaza_",
    ]

    return "\n".join(lines)


async def run_pulse_loop(symbol: str | None = None) -> None:
    global _start_time
    _start_time = time.time()
    logger.info(f"Pulse loop pornit (interval={PULSE_INTERVAL_S}s, enabled={_pulse_active})")

    while True:
        await asyncio.sleep(PULSE_INTERVAL_S)
        if not _pulse_active:
            continue
        try:
            msg = await build_pulse_message()
            from .telegram_ui import send_message
            await send_message(msg)
        except Exception as e:
            logger.warning(f"[pulse] eroare: {e}")
