"""Pulse reporter v0.8.2 — compatibilitate pm_mod cu position_manager v1.3.3.

Changelog:
  v0.8.2 — FIX: pm_mod.TP1_PCT / SL_PCT etc. nu mai exista in v1.3.3.
    Fix: _get_pm_profile_vals() citeste din _get_profile() / _DEFAULT_* la runtime.
  v0.8.1 — BUG 10+11 fix.
  v0.7.9 — race conditions fix.
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


def _get_pm_profile_vals() -> dict:
    """Citeste TP/SL/trail din profil la runtime (pm v1.3.3 nu mai are constante module-level)."""
    try:
        import apex_scalper.position_manager as _pm
        tp1  = getattr(_pm, "_DEFAULT_TP1_PCT",   0.0030)
        tp2  = getattr(_pm, "_DEFAULT_TP2_PCT",   0.0060)
        tp3  = getattr(_pm, "_DEFAULT_TP3_PCT",   0.0100)
        sl   = getattr(_pm, "_DEFAULT_SL_PCT",    0.0020)
        tr   = getattr(_pm, "_DEFAULT_TRAIL_PCT", 0.0030)
        mh   = getattr(_pm, "_DEFAULT_MAX_HOLD",  4)
        # Incearca profil dinamic daca exista
        if hasattr(_pm, "_get_profile"):
            prof = _pm._get_profile()
            tp1 = prof.get("tp1_pct", tp1)
            tp2 = prof.get("tp2_pct", tp2)
            tp3 = prof.get("tp3_pct", tp3)
            tr  = prof.get("trail_pct", tr)
            mh  = prof.get("max_hold_candles", mh)
        if hasattr(_pm, "_dynamic_sl_pct"):
            sl = _pm._dynamic_sl_pct()
        return {"TP1_PCT": tp1, "TP2_PCT": tp2, "TP3_PCT": tp3,
                "SL_PCT": sl, "TRAIL_PCT": tr, "MAX_HOLD": mh}
    except Exception:
        return {"TP1_PCT": 0.0030, "TP2_PCT": 0.0060, "TP3_PCT": 0.0100,
                "SL_PCT": 0.0020, "TRAIL_PCT": 0.0030, "MAX_HOLD": 4}


async def build_pulse_message() -> str:
    from .state import state
    from .strategy import ind, score_snapshot, ENTRY_THRESHOLD
    from .orderbook_analytics import compute as compute_ob
    from .regime_filter import regime
    from .book_pressure import bp
    from .funding_rate import funding
    from .mtf_filter import mtf
    from .risk import risk
    from .config import config
    from .position_manager import position_manager as pm

    pv = _get_pm_profile_vals()
    TP1_PCT, TP2_PCT, TP3_PCT = pv["TP1_PCT"], pv["TP2_PCT"], pv["TP3_PCT"]
    SL_PCT, TRAIL_PCT, MAX_HOLD_CANDLES = pv["SL_PCT"], pv["TRAIL_PCT"], pv["MAX_HOLD"]

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
    score_l, score_s = await score_snapshot(price, ob)

    bot_status = "✅ ACTIV" if (running and not paused) else ("⏸ PAUZA" if paused else "🛑 OPRIT")
    feed_icon  = "✅" if feed_ok else "🔴"

    lines = [
        f"⚡ *Apex Pulse* `{config.symbol}` — `{time.strftime('%H:%M:%S UTC', time.gmtime())}`",
        f"",
        f"🧠 *BOT*: {bot_status} | uptime `{_uptime()}` | feed {feed_icon} `{tick_age}s`",
        f"💰 *PnL azi*: `{daily_pnl:+.4f} USDT` | trades `{total_trades}` | WR `{win_rate}%`",
    ]

    snap = await pm.snapshot()

    if pos and snap.entry_price > 0:
        pnl_pct  = snap.unrealised_pnl_pct(price)
        pnl_usdt = round(pnl_pct * snap.entry_price * open_qty, 4)
        pnl_icon = "⬆️" if pnl_pct >= 0 else "⬇️"
        sl_price = round(snap.entry_price * (1 - SL_PCT  if pos == "long" else 1 + SL_PCT), 6)
        tp1_p    = round(snap.entry_price * (1 + TP1_PCT if pos == "long" else 1 - TP1_PCT), 6)
        tp2_p    = round(snap.entry_price * (1 + TP2_PCT if pos == "long" else 1 - TP2_PCT), 6)
        tp3_p    = round(snap.entry_price * (1 + TP3_PCT if pos == "long" else 1 - TP3_PCT), 6)
        tp1_hit  = "✅" if snap.tp1_hit else "◻️"
        tp2_hit  = "✅" if snap.tp2_hit else "◻️"
        tp3_hit  = "✅" if snap.tp3_hit else "◻️"
        trail    = "🔴 trail ON" if snap.trail_active else ""
        lines += [
            f"",
            f"{'\U0001f7e2' if pos == 'long' else '\U0001f534'} *POZITIE {pos.upper()}*",
            f"  entry `{snap.entry_price}` \u2192 acum `{price}` {pnl_icon} `{pnl_pct*100:+.4f}%` (`{pnl_usdt:+.4f}` USDT)",
            f"  qty `{open_qty}` | hold `{snap.hold_candles}/{MAX_HOLD_CANDLES}` {'\u23f0 timeout iminent' if snap.hold_candles >= MAX_HOLD_CANDLES - 1 else ''}",
            f"  SL `{sl_price}` | TP1 {tp1_hit}`{tp1_p}` TP2 {tp2_hit}`{tp2_p}` TP3 {tp3_hit}`{tp3_p}`",
            f"  pyramid: `{snap.pyramid_adds}` {trail}",
        ]
    else:
        lines += [f"", f"\u25ab\ufe0f *Fara pozitie deschisa*"]

    rsi_str   = f"`{ind.rsi_value:.1f}`"        if ind.rsi_ready  else "`warmup`"
    atr_str   = f"`{ind.atr_value:.5f}`"        if ind.atr_ready  else "`warmup`"
    vol_str   = f"`{ind.vol_zscore:.2f}`"        if ind.vol_ready  else "`warmup`"
    macd_str  = f"`{ind.macd_histogram:+.5f}`"  if ind.macd_ready else "`warmup`"
    stoch_str = (f"`{ind.stoch_k:.1f}`/`{ind.stoch_d:.1f}`" if ind.stoch_ready else "`warmup`")
    bb_str    = (f"`{ind.bb_lower:.5f}`\u2026`{ind.bb_upper:.5f}`" if ind.bb_ready else "`warmup`")
    vwap_str  = f"`{ind.vwap:.5f}`" if ind.vwap > 0 else "`-`"

    lines += [
        f"",
        f"📊 *INDICATORI* @ `{price}`",
        f"  EMA 9/21/50: `{ind.ema_fast:.5f}` / `{ind.ema_slow:.5f}` / `{ind.ema_trend:.5f}`",
        f"  RSI(14): {rsi_str} | ATR(14): {atr_str} | Vol Z: {vol_str}",
        f"  MACD hist: {macd_str} | StochRSI %K/%D: {stoch_str}",
        f"  BB(20,2): {bb_str} | VWAP: {vwap_str}",
        f"  OB imbalance: `{ob.imbalance:.4f}` | pressure: `{ob.pressure_score:.4f}`",
    ]

    l_bar = _bar(score_l)
    s_bar = _bar(score_s)
    l_vs_thr = "✅ INTRARE" if score_l >= ENTRY_THRESHOLD else f"`{ENTRY_THRESHOLD - score_l:.3f}` lipsa"
    s_vs_thr = "✅ INTRARE" if score_s >= ENTRY_THRESHOLD else f"`{ENTRY_THRESHOLD - score_s:.3f}` lipsa"
    lines += [
        f"",
        f"🎯 *SCORE ACUM* (prag `{ENTRY_THRESHOLD}`)",
        f"  LONG:  `{score_l:.4f}` {l_bar} {l_vs_thr}",
        f"  SHORT: `{score_s:.4f}` {s_bar} {s_vs_thr}",
    ]

    regime_icon = {"TRENDING": "🟢", "RANGING": "🔴", "VOLATILE": "🟡", "NEUTRAL": "🟤"}.get(regime.label, "⚫")
    entry_ok = regime.allow_entry()
    lines += [
        f"",
        f"{regime_icon} *REGIM*: `{regime.label}` | ADX `{regime.adx:.1f}` | sz\u00d7`{regime.size_factor():.2f}`",
        f"  Entry: {'\u2705 permis' if entry_ok else '\u274c BLOCAT'} | MTF: `{'BULL' if price > mtf.ema50 else 'BEAR'}` EMA50=`{mtf.ema50:.5f}`",
    ]

    p_long  = bp.pressure_long()
    p_short = bp.pressure_short()
    bp_dir  = ("🟢 LONG" if p_long else ("🔴 SHORT" if p_short else "⚪ neutru"))
    lines += [
        f"",
        f"📖 *BOOK PRESSURE*: {bp_dir} | cum\u0394 `{bp.cum_delta:+.0f}`",
    ]

    fund_long  = "✅" if funding.can_enter_long()  else "❌ blocat"
    fund_short = "✅" if funding.can_enter_short() else "❌ blocat"
    lines += [
        f"",
        f"💸 *FUNDING*: `{funding.rate_pct}` | LONG {fund_long} | SHORT {fund_short}",
    ]

    can_open = risk.can_open()
    consec   = risk.consecutive_losses
    kelly_f  = risk.kelly_f
    lines += [
        f"",
        f"🛡 *RISK*: can\_open `{'DA' if can_open else 'NU \u274c'}` | consec losses `{consec}` | Kelly `{kelly_f:.3f}`",
    ]

    if not running or paused:
        next_action = "⏸ Bot oprit / pauza"
    elif pos and snap.entry_price > 0:
        if not snap.tp1_hit:
            next_action = f"\u23f3 Astept TP1 @ `{round(snap.entry_price*(1+TP1_PCT if pos=='long' else 1-TP1_PCT),6)}`"
        elif not snap.tp2_hit:
            next_action = f"\u23f3 Astept TP2 @ `{round(snap.entry_price*(1+TP2_PCT if pos=='long' else 1-TP2_PCT),6)}`"
        elif not snap.tp3_hit:
            next_action = "\u23f3 Astept TP3 sau trail SL"
        elif snap.hold_candles >= MAX_HOLD_CANDLES - 1:
            next_action = "\u26a0\ufe0f Timeout iminent"
        else:
            next_action = "\u23f3 Pozitie activa — monitorizez"
    elif not can_open:
        next_action = "🔴 Risk blocat"
    elif not entry_ok:
        next_action = "🔴 Regim RANGING — astept TRENDING/NEUTRAL"
    elif not mtf.ready:
        next_action = "\u23f3 Astept MTF ready"
    elif score_l >= ENTRY_THRESHOLD:
        next_action = f"🟢 LONG TRIGGER — score `{score_l:.4f}` — astept BP"
    elif score_s >= ENTRY_THRESHOLD:
        next_action = f"🔴 SHORT TRIGGER — score `{score_s:.4f}` — astept BP"
    else:
        best = max(score_l, score_s)
        next_action = f"\u23f3 Scanez — max score `{best:.4f}` (lipsesc `{ENTRY_THRESHOLD-best:.3f}`)"

    lines += [
        f"",
        f"\u27a1\ufe0f *URMATOAREA ACTIUNE*: {next_action}",
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
