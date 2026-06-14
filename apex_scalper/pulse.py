"""Pulse reporter v0.7.7 — mesaj Telegram complet la fiecare minut.

Trimmite la fiecare PULSE_INTERVAL_S secunde (default 60) un snapshot
complet cu date reale din toate modulele active:

  │ STARE BOT    — running/paused, uptime, feed latency
  │ POZITIE      — side, entry, qty, PnL live, SL/TP, hold candles
  │ INDICATORI   — toti 10 + score LONG/SHORT calculat acum
  │ REGIM        — TRENDING/RANGING/VOLATILE/NEUTRAL + ADX + Hurst
  │ BOOK PRESS   — cum_delta, pressure dir, absorption status
  │ FUNDING      — rata, directia blocata
  │ MTF          — EMA50(15m), bias, ready
  │ RISK         — daily PnL, consec losses, Kelly, can_open
  │ NEXT ACTION  — ce ar face botul daca ar evalua acum

ENV:
  PULSE_INTERVAL_S  (default 60)    — interval in secunde
  PULSE_ENABLED     (default true)  — false dezactiveaza loop-ul
"""
from __future__ import annotations

import asyncio
import os
import time
from loguru import logger

PULSE_INTERVAL_S = int(os.getenv("PULSE_INTERVAL_S", "60"))
PULSE_ENABLED    = os.getenv("PULSE_ENABLED", "true").lower() == "true"

_start_time  = time.time()
_pulse_active = PULSE_ENABLED   # toggled by /pulse on/off


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
    """ASCII progress bar for scores."""
    filled = int(round(min(value / max(max_val, 1e-9), 1.0) * width))
    return "█" * filled + "░" * (width - filled)


async def build_pulse_message() -> str:
    """Build the full pulse message from live data."""
    from .state import state
    from .strategy import ind, _score_long, _score_short, ENTRY_THRESHOLD
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

    tick_age   = round(time.time() - last_tick_ts, 2) if last_tick_ts else 99.9
    feed_ok    = tick_age < 2.0
    win_rate   = round(win_trades / total_trades * 100, 1) if total_trades > 0 else 0.0

    ob         = compute_ob()
    score_l    = _score_long(ind, ob, price)
    score_s    = _score_short(ind, ob, price)

    # ── Stare bot ──────────────────────────────────────────────────
    bot_status = "✅ ACTIV" if (running and not paused) else ("⏸ PAUZA" if paused else "🛑 OPRIT")
    feed_icon  = "✅" if feed_ok else "🔴"

    lines = [
        f"⚡ *Apex Pulse* `{config.symbol}` — `{time.strftime('%H:%M:%S UTC', time.gmtime())}`",
        f"",
        f"🧠 *BOT*: {bot_status} | uptime `{_uptime()}` | feed {feed_icon} `{tick_age}s`",
        f"💰 *PnL azi*: `{daily_pnl:+.4f} USDT` | trades `{total_trades}` | WR `{win_rate}%`",
    ]

    # ── Pozitie deschisa ───────────────────────────────────────────
    if pos and pm._entry_price > 0:
        entry_p  = pm._entry_price
        hold_c   = pm._hold_candles
        tp1_hit  = "✅" if pm._tp1_hit else "◻️"
        tp2_hit  = "✅" if pm._tp2_hit else "◻️"
        tp3_hit  = "✅" if pm._tp3_hit else "◻️"
        trail    = "🔴 trail ON" if pm._trail_active else ""
        pnl_pct  = pm._unrealised_pnl_pct(price) if entry_p > 0 else 0.0
        pnl_usdt = round(pnl_pct * entry_p * open_qty, 4)
        pnl_icon = "⬆️" if pnl_pct >= 0 else "⬇️"
        sl_price = round(entry_p * (1 - SL_PCT if pos == "long" else 1 + SL_PCT), 2)
        tp1_p    = round(entry_p * (1 + TP1_PCT if pos == "long" else 1 - TP1_PCT), 2)
        tp2_p    = round(entry_p * (1 + TP2_PCT if pos == "long" else 1 - TP2_PCT), 2)
        tp3_p    = round(entry_p * (1 + TP3_PCT if pos == "long" else 1 - TP3_PCT), 2)
        pyr_adds = pm._pyramid_adds

        lines += [
            f"",
            f"{'\U0001f7e2' if pos == 'long' else '\U0001f534'} *POZITIE {pos.upper()}*",
            f"  entry `{entry_p}` → acum `{price}` {pnl_icon} `{pnl_pct*100:+.4f}%` (`{pnl_usdt:+.4f}` USDT)",
            f"  qty `{open_qty}` | hold `{hold_c}/{MAX_HOLD_CANDLES}` candle {'\u23f0 timeout iminent' if hold_c >= MAX_HOLD_CANDLES - 1 else ''}",
            f"  SL `{sl_price}` | TP1 {tp1_hit}`{tp1_p}` TP2 {tp2_hit}`{tp2_p}` TP3 {tp3_hit}`{tp3_p}`",
            f"  pyramid adds: `{pyr_adds}` {trail}",
        ]
    else:
        lines += [
            f"",
            f"▫️ *Fara pozitie deschisa*",
        ]

    # ── Indicatori ───────────────────────────────────────────────────────
    rsi_str   = f"`{ind.rsi_value:.1f}`" if ind.rsi_ready   else "`warmup`"
    atr_str   = f"`{ind.atr_value:.4f}`" if ind.atr_ready   else "`warmup`"
    vol_str   = f"`{ind.vol_zscore:.2f}`" if ind.vol_ready  else "`warmup`"
    macd_str  = f"`{ind.macd_histogram:+.5f}`" if ind.macd_ready  else "`warmup`"
    stoch_str = f"`{ind.stoch_k:.1f}`/`{ind.stoch_d:.1f}`" if ind.stoch_ready else "`warmup`"
    bb_str    = (f"`{ind.bb_lower:.1f}`…`{ind.bb_upper:.1f}`" if ind.bb_ready else "`warmup`")
    vwap_str  = f"`{ind.vwap:.2f}`" if ind.vwap > 0 else "`-`"

    lines += [
        f"",
        f"📊 *INDICATORI* @ `{price}`",
        f"  EMA 9/21/50: `{ind.ema_fast:.2f}` / `{ind.ema_slow:.2f}` / `{ind.ema_trend:.2f}`",
        f"  RSI(14): {rsi_str} | ATR(14): {atr_str} | Vol Z: {vol_str}",
        f"  MACD hist: {macd_str} | StochRSI %K/%D: {stoch_str}",
        f"  BB(20,2): {bb_str} | VWAP: {vwap_str}",
        f"  OB imbalance: `{ob.imbalance:.4f}` | pressure: `{ob.pressure_score:.4f}`",
    ]

    # ── Scoruri ───────────────────────────────────────────────────────────
    l_bar    = _bar(score_l, 1.0)
    s_bar    = _bar(score_s, 1.0)
    l_vs_thr = "✅ INTRARE" if score_l >= ENTRY_THRESHOLD else f"`{ENTRY_THRESHOLD - score_l:.3f}` lipsa"
    s_vs_thr = "✅ INTRARE" if score_s >= ENTRY_THRESHOLD else f"`{ENTRY_THRESHOLD - score_s:.3f}` lipsa"

    lines += [
        f"",
        f"🎯 *SCORE ACUM*  (prag `{ENTRY_THRESHOLD}`)",
        f"  LONG:  `{score_l:.4f}` {l_bar} {l_vs_thr}",
        f"  SHORT: `{score_s:.4f}` {s_bar} {s_vs_thr}",
    ]

    # ── Regim ────────────────────────────────────────────────────────────────
    regime_icon = {
        "TRENDING": "🟢", "RANGING": "🔴",
        "VOLATILE": "🟡", "NEUTRAL": "🟤",
    }.get(regime.label, "⚫")
    entry_ok = regime.allow_entry()

    lines += [
        f"",
        f"{regime_icon} *REGIM*: `{regime.label}` | ADX `{regime.adx:.1f}` | sz×`{regime.size_factor():.2f}`",
        f"  Entry: {'\u2705 permis' if entry_ok else '\u274c BLOCAT — RANGING'} | MTF: `{'BULL' if price > mtf.ema50 else 'BEAR'}` EMA50(15m)=`{mtf.ema50:.2f}`",
    ]

    # ── Book pressure ────────────────────────────────────────────────
    p_long  = bp.pressure_long()
    p_short = bp.pressure_short()
    bp_dir  = ("🟢 LONG" if p_long else ("🔴 SHORT" if p_short else "⚪ neutru"))

    lines += [
        f"",
        f"📖 *BOOK PRESSURE*: {bp_dir}",
        f"  cumΔ `{bp.cum_delta:+.0f}` | thr `±{bp._threshold():.0f}`",
    ]

    # ── Funding ──────────────────────────────────────────────────────────
    fund_long  = "✅" if funding.can_enter_long()  else "❌ blocat"
    fund_short = "✅" if funding.can_enter_short() else "❌ blocat"

    lines += [
        f"",
        f"💸 *FUNDING*: `{funding.rate_pct}` | LONG {fund_long} | SHORT {fund_short}",
    ]

    # ── Risk ──────────────────────────────────────────────────────────────
    can_open  = risk.can_open()
    consec    = getattr(risk, "_consecutive_losses", 0)
    kelly_f   = getattr(risk, "_kelly_factor", 0.5)

    lines += [
        f"",
        f"🛡 *RISK*: can\_open `{'DA' if can_open else 'NU ❌'}` | consec losses `{consec}` | Kelly `{kelly_f:.3f}`",
    ]

    # ── Urmatoarea actiune ───────────────────────────────────────────
    if not running or paused:
        next_action = "⏸ Bot oprit / pauza — fara tranzactii"
    elif pos:
        hold_c = pm._hold_candles
        if not pm._tp1_hit:
            next_action = f"⏳ Astept TP1 @ `{round(pm._entry_price * (1 + TP1_PCT if pos == 'long' else 1 - TP1_PCT), 2)}`"
        elif not pm._tp2_hit:
            next_action = f"⏳ Astept TP2 @ `{round(pm._entry_price * (1 + TP2_PCT if pos == 'long' else 1 - TP2_PCT), 2)}`"
        elif not pm._tp3_hit:
            next_action = f"⏳ Astept TP3 sau trail SL"
        elif hold_c >= MAX_HOLD_CANDLES - 1:
            next_action = "⚠️ Timeout iminent — inchide la urmatoarea lumanare"
        else:
            next_action = "⏳ Pozitie activa — monitorizez"
    elif not can_open:
        next_action = "🔴 Risk blocat (daily limit / consecutive losses)"
    elif not entry_ok:
        next_action = "🔴 Regim RANGING — astept TRENDING/NEUTRAL"
    elif not mtf.ready:
        next_action = "⏳ Astept MTF ready (EMA50 15m)"
    elif score_l >= ENTRY_THRESHOLD:
        next_action = f"🟢 LONG TRIGGER — score `{score_l:.4f}` >= `{ENTRY_THRESHOLD}` — astept confirmare BP"
    elif score_s >= ENTRY_THRESHOLD:
        next_action = f"🔴 SHORT TRIGGER — score `{score_s:.4f}` >= `{ENTRY_THRESHOLD}` — astept confirmare BP"
    else:
        best = max(score_l, score_s)
        next_action = f"⏳ Scanez — score max `{best:.4f}` (lipsesc `{ENTRY_THRESHOLD - best:.3f}` pana la prag)"

    lines += [
        f"",
        f"➡️ *URMATOAREA ACTIUNE*:",
        f"  {next_action}",
        f"",
        f"_pulse interval: {PULSE_INTERVAL_S}s | /pulse off dezactiveaza_",
    ]

    return "\n".join(lines)


async def run_pulse_loop(symbol: str | None = None) -> None:
    """Background task: trimite pulse la fiecare PULSE_INTERVAL_S secunde."""
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
