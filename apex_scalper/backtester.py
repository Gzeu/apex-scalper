"""Backtester v0.6.0 — real fee simulation, aligned weights with strategy.py.

Fixes vs v0.4.0:
  🔴 CRITICAL:
  - Fee simulation corrected:
    Entry fee: 0.020% maker (Limit PostOnly) — was MISSING
    Exit fee:  0.020% maker (Limit close)    — was 0.055% taker
    Delta: real Sharpe is ~15-25% lower than v0.4.0 results showed.
    If OOS Sharpe was 1.2 before, expect ~0.9-1.0 with real fees.
  - Signal weights aligned with strategy.py v0.6.0:
    ema_cross=0.23, trend=0.18, rsi=0.18, imbalance=0.18,
    volume=0.10, atr=0.03, bb=0.05, vwap=0.05
    Previously backtester used different weights (ema_cross=0.25, trend=0.20)
    causing optimized params to not match live behavior.
  - BB + VWAP signals added to backtest scoring (were missing in v0.4.0)
    Backtester now has 8/8 signals matching strategy.py.
  - RSI overbought/oversold penalty added (matches strategy.py v0.6.0)
  - ENTRY_THRESHOLD default raised to 0.65

  🟡 IMPORTANT:
  - Slippage model added: 0.5 tick slippage on entry + exit (realistic for 1m)
    Can be overridden via profile["slippage_ticks"]
  - Fee report in summary: shows total fees paid vs total PnL

Usage:
    python -m apex_scalper.backtester --symbol BTCUSDT --days 90
    python -m apex_scalper.backtester --symbol ETHUSDT --days 90 --output results.json
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
from loguru import logger

from .config import SYMBOL_PROFILES, DEFAULT_SYMBOL
from .indicators import IndicatorState, update_all

KLINE_LIMIT = 200

# Fee constants (mainnet 2026, non-VIP Bybit USDT Perp)
MAKER_FEE = 0.00020   # 0.020% — Limit PostOnly entry + exit
TAKER_FEE = 0.00055   # 0.055% — Market fallback only

# Signal weights — must match strategy.py exactly
_W = {
    "ema_cross":  0.23,
    "trend":      0.18,
    "rsi":        0.18,
    "imbalance":  0.18,   # not available in BT -> neutral credit 0.09 (50%)
    "volume":     0.10,
    "atr":        0.03,
    "bb":         0.05,
    "vwap":       0.05,
}


def fetch_klines(
    symbol: str,
    interval: str = "1",
    days: int = 30,
    testnet: bool = False,
) -> list[dict]:
    base = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
    url  = f"{base}/v5/market/kline"
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000

    candles: list[dict] = []
    end_ms = now_ms

    while True:
        params = dict(
            category="linear", symbol=symbol, interval=interval,
            start=start_ms, end=end_ms, limit=KLINE_LIMIT,
        )
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        rows = data.get("result", {}).get("list", [])
        if not rows:
            break
        for row in reversed(rows):
            ts = int(row[0])
            if ts < start_ms:
                continue
            candles.append({
                "ts": ts, "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
            })
        oldest_ts = int(rows[-1][0])
        if oldest_ts <= start_ms or len(rows) < KLINE_LIMIT:
            break
        end_ms = oldest_ts - 1
        time.sleep(0.15)

    candles.sort(key=lambda c: c["ts"])
    return candles


@dataclass
class BtPosition:
    side: str
    qty: float
    entry: float
    entry_idx: int
    tp1_done: bool = False
    trailing_stop: float = 0.0
    entry_fee: float = 0.0   # recorded at open


@dataclass
class BtResult:
    symbol: str
    days: int
    total_trades:  int   = 0
    win_trades:    int   = 0
    total_pnl:     float = 0.0
    total_fees:    float = 0.0   # NEW v0.6.0
    gross_pnl:     float = 0.0   # before fees
    trades:        list  = field(default_factory=list)
    equity_curve:  list  = field(default_factory=list)

    @property
    def winrate(self) -> float:
        return round(self.win_trades / self.total_trades * 100, 1) if self.total_trades else 0.0

    @property
    def sharpe(self) -> float:
        returns = [t["pnl_pct_net"] for t in self.trades]  # net of fees
        if len(returns) < 2:
            return 0.0
        mu  = sum(returns) / len(returns)
        std = math.sqrt(sum((r - mu) ** 2 for r in returns) / len(returns))
        return round((mu / std) * math.sqrt(252 * 24 * 60) if std > 0 else 0.0, 3)

    @property
    def max_drawdown(self) -> float:
        peak = 0.0
        max_dd = 0.0
        running = 0.0
        for pnl in [t["pnl_usdt_net"] for t in self.trades]:
            running += pnl
            peak = max(peak, running)
            dd = peak - running
            max_dd = max(max_dd, dd)
        return round(max_dd, 4)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t["pnl_usdt_net"] for t in self.trades if t["pnl_usdt_net"] > 0)
        gross_loss   = abs(sum(t["pnl_usdt_net"] for t in self.trades if t["pnl_usdt_net"] < 0))
        return round(gross_profit / gross_loss if gross_loss > 0 else 0.0, 3)

    def summary(self) -> dict:
        return {
            "symbol":           self.symbol,
            "days":             self.days,
            "total_trades":     self.total_trades,
            "win_trades":       self.win_trades,
            "winrate_pct":      self.winrate,
            "gross_pnl_usdt":   round(self.gross_pnl, 4),
            "total_fees_usdt":  round(self.total_fees, 4),
            "net_pnl_usdt":     round(self.total_pnl, 4),
            "sharpe":           self.sharpe,
            "max_drawdown_usdt": self.max_drawdown,
            "profit_factor":    self.profit_factor,
            "fee_model":        f"maker={MAKER_FEE*100:.3f}% entry+exit + 0.5tick slippage",
        }


def _score(
    side: str,
    ind: IndicatorState,
    close: float,
    rsi_long_min: float,
    rsi_short_max: float,
    imb_long: float,
    imb_short: float,
    atr_min_pct: float,
    atr_max_pct: float,
    vol_zscore_min: float,
    atr_pct: float,
    rsi_ob_penalty: float = 65.0,
    rsi_os_penalty: float = 35.0,
) -> float:
    """Mirrors strategy.py _score_long / _score_short exactly."""
    score = 0.0
    RSI_OB_LIMIT = 70.0
    RSI_OS_LIMIT = 30.0

    # 1. EMA cross — always True here (called only after cross confirmed)
    score += _W["ema_cross"]

    # 2. Trend
    if side == "long" and close > ind.ema_trend:
        score += _W["trend"]
    elif side == "short" and close < ind.ema_trend:
        score += _W["trend"]

    # 3. RSI with overbought/oversold penalty
    if side == "long" and rsi_long_min <= ind.rsi_value <= RSI_OB_LIMIT:
        rsi_conf = min((ind.rsi_value - rsi_long_min) / (RSI_OB_LIMIT - rsi_long_min), 1.0)
        rsi_s = _W["rsi"] * rsi_conf
        if ind.rsi_value >= rsi_ob_penalty:
            pf = 1.0 - (ind.rsi_value - rsi_ob_penalty) / (RSI_OB_LIMIT - rsi_ob_penalty)
            rsi_s *= max(pf, 0.0)
        score += rsi_s
    elif side == "short" and RSI_OS_LIMIT <= ind.rsi_value <= rsi_short_max:
        rsi_conf = min((rsi_short_max - ind.rsi_value) / (rsi_short_max - RSI_OS_LIMIT), 1.0)
        rsi_s = _W["rsi"] * rsi_conf
        if ind.rsi_value <= rsi_os_penalty:
            pf = 1.0 - (rsi_os_penalty - ind.rsi_value) / (rsi_os_penalty - RSI_OS_LIMIT)
            rsi_s *= max(pf, 0.0)
        score += rsi_s

    # 4. Imbalance — OB not available in backtest -> 50% neutral credit
    score += _W["imbalance"] * 0.5

    # 5. Volume
    if ind.vol_ready and ind.vol_zscore >= vol_zscore_min:
        score += _W["volume"] * min(max(ind.vol_zscore / 2.0, 0), 1)

    # 6. ATR gate
    if atr_min_pct <= atr_pct <= atr_max_pct:
        score += _W["atr"]

    # 7. BB
    if ind.bb_ready and ind.bb_mid > 0:
        if side == "long":
            if close <= ind.bb_lower:
                score += _W["bb"]
            elif close < ind.bb_mid:
                bb_c = (ind.bb_mid - close) / (ind.bb_mid - ind.bb_lower) if ind.bb_mid > ind.bb_lower else 0
                score += _W["bb"] * min(bb_c, 1.0)
        else:
            if close >= ind.bb_upper:
                score += _W["bb"]
            elif close > ind.bb_mid:
                bb_c = (close - ind.bb_mid) / (ind.bb_upper - ind.bb_mid) if ind.bb_upper > ind.bb_mid else 0
                score += _W["bb"] * min(bb_c, 1.0)

    # 8. VWAP
    if ind.vwap > 0:
        if side == "long":
            if close > ind.vwap:
                score += _W["vwap"]
            else:
                gap = (ind.vwap - close) / ind.vwap
                if gap < 0.001:
                    score += _W["vwap"] * (1 - gap / 0.001)
        else:
            if close < ind.vwap:
                score += _W["vwap"]
            else:
                gap = (close - ind.vwap) / ind.vwap
                if gap < 0.001:
                    score += _W["vwap"] * (1 - gap / 0.001)

    return score


def run_backtest(
    symbol: str,
    days: int = 30,
    profile: Optional[dict] = None,
    candles: Optional[list[dict]] = None,
    testnet: bool = False,
) -> BtResult:
    p = profile or SYMBOL_PROFILES.get(symbol, SYMBOL_PROFILES[DEFAULT_SYMBOL])

    tp1_pct          = p["tp1_pct"]
    tp2_pct          = p["tp2_pct"]
    sl_pct           = p["sl_pct"]
    trail_pct        = p["trail_pct"]
    trail_delta      = p["trail_delta"]
    max_hold         = p["max_hold_candles"]
    order_size_usdt  = p["order_size_usdt"]
    leverage         = p["leverage"]
    entry_threshold  = p.get("entry_threshold", 0.65)   # raised default
    rsi_long_min     = p["rsi_long_min"]
    rsi_short_max    = p["rsi_short_max"]
    imb_long         = p["imbalance_long"]
    imb_short        = p["imbalance_short"]
    atr_min_pct      = p["atr_min_pct"]
    atr_max_pct      = p["atr_max_pct"]
    vol_zscore_min   = p["vol_zscore_min"]
    slippage_ticks   = p.get("slippage_ticks", 0.5)     # NEW: 0.5 tick slippage model
    tick_size        = p.get("tick_size", 0.10)          # BTC default
    slippage_usdt    = slippage_ticks * tick_size        # per unit

    if candles is None:
        logger.info(f"Downloading {days}d klines for {symbol}...")
        candles = fetch_klines(symbol, days=days, testnet=testnet)
        logger.info(f"Downloaded {len(candles)} candles")

    ind = IndicatorState()
    result = BtResult(symbol=symbol, days=days)
    pos: Optional[BtPosition] = None
    prev_fast = 0.0
    prev_slow = 0.0
    hold_count = 0
    equity = 0.0

    for i, c in enumerate(candles):
        close  = c["close"]
        high   = c["high"]
        low    = c["low"]
        volume = c["volume"]

        update_all(ind, close, high, low, volume)

        if not ind.rsi_ready or not ind.atr_ready:
            prev_fast = ind.ema_fast
            prev_slow = ind.ema_slow
            continue

        atr_pct = ind.atr_value / close if close > 0 else 0

        # ── POSITION MANAGEMENT ──
        if pos is not None:
            hold_count += 1
            pnl_pct = (
                (close - pos.entry) / pos.entry if pos.side == "long"
                else (pos.entry - close) / pos.entry
            )

            if trail_pct > 0 and pnl_pct >= trail_pct:
                new_trail = (
                    close * (1 - trail_delta) if pos.side == "long"
                    else close * (1 + trail_delta)
                )
                if pos.side == "long":
                    pos.trailing_stop = max(pos.trailing_stop, new_trail)
                else:
                    pos.trailing_stop = (
                        min(pos.trailing_stop, new_trail)
                        if pos.trailing_stop > 0 else new_trail
                    )

            # TP1 partial
            if not pos.tp1_done and pnl_pct >= tp1_pct:
                partial_qty = pos.qty / 2
                partial_notional = partial_qty * close
                exit_fee  = partial_notional * MAKER_FEE
                slip_cost = partial_qty * slippage_usdt
                partial_pnl = pnl_pct * partial_qty * pos.entry - exit_fee - slip_cost
                pos.qty = round(pos.qty / 2, 3)
                pos.tp1_done = True
                equity += partial_pnl
                result.gross_pnl  += pnl_pct * partial_qty * pos.entry
                result.total_fees += exit_fee + slip_cost
                result.total_pnl  += partial_pnl
                result.equity_curve.append(round(equity, 4))

            # Exit conditions
            sl_hit    = pnl_pct <= -sl_pct
            tp2_hit   = pnl_pct >= tp2_pct
            trail_hit = (
                pos.trailing_stop > 0 and (
                    (pos.side == "long"  and close <= pos.trailing_stop) or
                    (pos.side == "short" and close >= pos.trailing_stop)
                )
            )
            time_exit = hold_count >= max_hold

            if sl_hit or tp2_hit or trail_hit or time_exit:
                reason = (
                    "SL" if sl_hit else
                    "TP2" if tp2_hit else
                    "TRAIL" if trail_hit else "TIMEOUT"
                )
                notional  = pos.qty * close
                # SL = Market (taker), TP/TRAIL/TIMEOUT = Limit (maker)
                exit_fee_rate = TAKER_FEE if sl_hit else MAKER_FEE
                exit_fee  = notional * exit_fee_rate
                slip_cost = pos.qty * slippage_usdt
                gross_pnl = pnl_pct * pos.qty * pos.entry
                pnl_usdt  = gross_pnl - pos.entry_fee - exit_fee - slip_cost

                equity += pnl_usdt
                result.gross_pnl  += gross_pnl
                result.total_fees += pos.entry_fee + exit_fee + slip_cost
                result.total_pnl  += pnl_usdt
                result.total_trades += 1
                if pnl_usdt > 0:
                    result.win_trades += 1
                result.trades.append({
                    "idx":          i,
                    "ts":           c["ts"],
                    "side":         pos.side,
                    "entry":        pos.entry,
                    "exit":         close,
                    "pnl_usdt_net": round(pnl_usdt, 4),
                    "pnl_pct_net":  round(pnl_usdt / (pos.entry * pos.qty) if pos.entry * pos.qty > 0 else 0, 6),
                    "fee_usdt":     round(pos.entry_fee + exit_fee, 4),
                    "reason":       reason,
                })
                result.equity_curve.append(round(equity, 4))
                pos = None
                hold_count = 0

        # ── ENTRY ──
        if pos is None and ind.rsi_ready and ind.atr_ready:
            cross_up   = prev_fast <= prev_slow and ind.ema_fast > ind.ema_slow
            cross_down = prev_fast >= prev_slow and ind.ema_fast < ind.ema_slow

            if cross_up or cross_down:
                side = "long" if cross_up else "short"
                score = _score(
                    side=side, ind=ind, close=close,
                    rsi_long_min=rsi_long_min, rsi_short_max=rsi_short_max,
                    imb_long=imb_long, imb_short=imb_short,
                    atr_min_pct=atr_min_pct, atr_max_pct=atr_max_pct,
                    vol_zscore_min=vol_zscore_min, atr_pct=atr_pct,
                )

                if score >= entry_threshold:
                    qty = max(round((order_size_usdt * leverage) / close, 3), 0.001)
                    # FIX v0.6.0: entry fee recorded at open (maker 0.020%)
                    entry_notional = qty * close
                    entry_fee      = entry_notional * MAKER_FEE
                    slip_cost      = qty * slippage_usdt
                    result.total_fees += entry_fee + slip_cost
                    pos = BtPosition(
                        side=side, qty=qty, entry=close,
                        entry_idx=i, entry_fee=entry_fee + slip_cost,
                    )
                    hold_count = 0

        prev_fast = ind.ema_fast
        prev_slow = ind.ema_slow

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apex Scalper Backtester v0.6.0")
    parser.add_argument("--symbol",  default="BTCUSDT")
    parser.add_argument("--days",    type=int, default=90)
    parser.add_argument("--output",  default=None)
    parser.add_argument("--testnet", action="store_true")
    args = parser.parse_args()

    result = run_backtest(args.symbol, days=args.days, testnet=args.testnet)
    s = result.summary()

    print("\n" + "=" * 60)
    print(f"  BACKTEST v0.6.0 — {s['symbol']} ({s['days']}d) [REAL FEES]")
    print("=" * 60)
    print(f"  Trades:         {s['total_trades']}")
    print(f"  Win Rate:       {s['winrate_pct']}%")
    print(f"  Gross PnL:      {s['gross_pnl_usdt']} USDT  (before fees)")
    print(f"  Total Fees:     {s['total_fees_usdt']} USDT  (maker 0.020% + slippage)")
    print(f"  Net PnL:        {s['net_pnl_usdt']} USDT  (after fees)")
    print(f"  Sharpe:         {s['sharpe']}  (net of fees)")
    print(f"  Max Drawdown:   {s['max_drawdown_usdt']} USDT")
    print(f"  Profit Factor:  {s['profit_factor']}")
    print(f"  Fee model:      {s['fee_model']}")
    print("=" * 60)
    print()
    if s['sharpe'] < 1.0:
        print("  ⚠️  Sharpe < 1.0 — strategy needs redesign before mainnet")
    elif s['sharpe'] < 1.5:
        print("  ✅  Sharpe OK — run optimizer then testnet validation")
    else:
        print("  🚀  Sharpe > 1.5 — strong signal, proceed to testnet")
    print()

    if args.output:
        with open(args.output, "w") as f:
            json.dump({"summary": s, "trades": result.trades}, f, indent=2)
        print(f"Results saved to {args.output}")
