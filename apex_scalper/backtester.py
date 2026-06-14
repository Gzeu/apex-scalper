"""Backtester v0.4.0 — replay Bybit kline history through the full strategy.

Usage:
    python -m apex_scalper.backtester --symbol BTCUSDT --days 30
    python -m apex_scalper.backtester --symbol ETHUSDT --days 90 --output results.json

Downloads klines directly from Bybit REST (no account needed) and replays
every confirmed candle through indicators + signal engine + position manager
logic, producing PnL, Sharpe, MaxDD, WinRate, ProfitFactor.
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


KLINE_LIMIT = 200  # max per Bybit request


# ─────────────────────────────────────────────────────────────────────────────
# Data fetch
# ─────────────────────────────────────────────────────────────────────────────

def fetch_klines(
    symbol: str,
    interval: str = "1",
    days: int = 30,
    testnet: bool = False,
) -> list[dict]:
    """Download klines from Bybit public REST. No auth required."""
    base = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
    url  = f"{base}/v5/market/kline"
    now_ms  = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000

    candles: list[dict] = []
    end_ms = now_ms

    while True:
        params = dict(
            category="linear",
            symbol=symbol,
            interval=interval,
            start=start_ms,
            end=end_ms,
            limit=KLINE_LIMIT,
        )
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        rows = data.get("result", {}).get("list", [])
        if not rows:
            break

        # Bybit returns newest-first; each row = [ts, open, high, low, close, vol, turnover]
        for row in reversed(rows):
            ts = int(row[0])
            if ts < start_ms:
                continue
            candles.append({
                "ts":     ts,
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "volume": float(row[5]),
            })

        oldest_ts = int(rows[-1][0])
        if oldest_ts <= start_ms or len(rows) < KLINE_LIMIT:
            break
        end_ms = oldest_ts - 1
        time.sleep(0.15)  # rate limit

    candles.sort(key=lambda c: c["ts"])
    return candles


# ─────────────────────────────────────────────────────────────────────────────
# Backtest engine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BtPosition:
    side: str            # 'long' | 'short'
    qty: float
    entry: float
    entry_idx: int
    tp1_done: bool = False
    trailing_stop: float = 0.0


@dataclass
class BtResult:
    symbol: str
    days: int
    total_trades: int = 0
    win_trades:   int = 0
    total_pnl:    float = 0.0
    trades:       list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)

    @property
    def winrate(self) -> float:
        return round(self.win_trades / self.total_trades * 100, 1) if self.total_trades else 0.0

    @property
    def sharpe(self) -> float:
        returns = [t["pnl_pct"] for t in self.trades]
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
        for pnl in [t["pnl_usdt"] for t in self.trades]:
            running += pnl
            peak = max(peak, running)
            dd = peak - running
            max_dd = max(max_dd, dd)
        return round(max_dd, 4)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t["pnl_usdt"] for t in self.trades if t["pnl_usdt"] > 0)
        gross_loss   = abs(sum(t["pnl_usdt"] for t in self.trades if t["pnl_usdt"] < 0))
        return round(gross_profit / gross_loss if gross_loss > 0 else 0.0, 3)

    def summary(self) -> dict:
        return {
            "symbol":       self.symbol,
            "days":         self.days,
            "total_trades": self.total_trades,
            "win_trades":   self.win_trades,
            "winrate_pct":  self.winrate,
            "total_pnl_usdt": round(self.total_pnl, 4),
            "sharpe":       self.sharpe,
            "max_drawdown_usdt": self.max_drawdown,
            "profit_factor": self.profit_factor,
        }


def run_backtest(
    symbol: str,
    days: int = 30,
    profile: Optional[dict] = None,
    candles: Optional[list[dict]] = None,
    testnet: bool = False,
) -> BtResult:
    """Core backtest engine. Pass candles to avoid re-downloading (for optimizer)."""
    p = profile or SYMBOL_PROFILES.get(symbol, SYMBOL_PROFILES[DEFAULT_SYMBOL])

    # Params from profile
    tp1_pct         = p["tp1_pct"]
    tp2_pct         = p["tp2_pct"]
    sl_pct          = p["sl_pct"]
    trail_pct       = p["trail_pct"]
    trail_delta     = p["trail_delta"]
    max_hold        = p["max_hold_candles"]
    order_size_usdt = p["order_size_usdt"]
    leverage        = p["leverage"]
    entry_threshold = p["entry_threshold"]
    rsi_long_min    = p["rsi_long_min"]
    rsi_short_max   = p["rsi_short_max"]
    imb_long        = p["imbalance_long"]
    imb_short       = p["imbalance_short"]
    atr_min_pct     = p["atr_min_pct"]
    atr_max_pct     = p["atr_max_pct"]
    vol_zscore_min  = p["vol_zscore_min"]

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

            # Trailing stop update
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

            # TP1 scale-out (simulated: record partial PnL)
            if not pos.tp1_done and pnl_pct >= tp1_pct:
                partial_pnl = pnl_pct * (pos.qty / 2) * pos.entry
                pos.qty = round(pos.qty / 2, 3)
                pos.tp1_done = True
                equity += partial_pnl
                result.total_pnl += partial_pnl
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
                pnl_usdt = pnl_pct * pos.qty * pos.entry
                # Simulated taker fee (market close)
                fee = close * pos.qty * 0.00055
                pnl_usdt -= fee

                equity += pnl_usdt
                result.total_pnl += pnl_usdt
                result.total_trades += 1
                if pnl_usdt > 0:
                    result.win_trades += 1
                result.trades.append({
                    "idx":      i,
                    "ts":       c["ts"],
                    "side":     pos.side,
                    "entry":    pos.entry,
                    "exit":     close,
                    "pnl_usdt": round(pnl_usdt, 4),
                    "pnl_pct":  round(pnl_pct, 6),
                    "reason":   reason,
                })
                result.equity_curve.append(round(equity, 4))
                pos = None
                hold_count = 0

        # ── ENTRY ──
        if pos is None and ind.rsi_ready and ind.atr_ready:
            cross_up   = prev_fast <= prev_slow and ind.ema_fast > ind.ema_slow
            cross_down = prev_fast >= prev_slow and ind.ema_fast < ind.ema_slow

            # Score computation (mirrors strategy.py weights)
            if cross_up or cross_down:
                side = "long" if cross_up else "short"
                score = 0.0

                # EMA cross (0.25)
                score += 0.25
                # EMA50 trend (0.20)
                if side == "long" and close > ind.ema_trend:
                    score += 0.20
                elif side == "short" and close < ind.ema_trend:
                    score += 0.20
                # RSI (0.20)
                if side == "long" and rsi_long_min <= ind.rsi_value <= 70:
                    score += 0.20 * min((ind.rsi_value - rsi_long_min) / (70 - rsi_long_min), 1)
                elif side == "short" and 30 <= ind.rsi_value <= rsi_short_max:
                    score += 0.20 * min((rsi_short_max - ind.rsi_value) / (rsi_short_max - 30), 1)
                # Imbalance: not available in backtester (no OB history) → partial score
                score += 0.10   # neutral imbalance credit
                # Volume (0.10)
                if ind.vol_ready and ind.vol_zscore >= vol_zscore_min:
                    score += 0.10 * min(max(ind.vol_zscore / 2.0, 0), 1)
                # ATR gate (0.05)
                if atr_min_pct <= atr_pct <= atr_max_pct:
                    score += 0.05

                if score >= entry_threshold:
                    qty = max(round((order_size_usdt * leverage) / close, 3), 0.001)
                    pos = BtPosition(side=side, qty=qty, entry=close, entry_idx=i)
                    hold_count = 0

        prev_fast = ind.ema_fast
        prev_slow = ind.ema_slow

    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apex Scalper Backtester")
    parser.add_argument("--symbol",  default="BTCUSDT")
    parser.add_argument("--days",    type=int, default=30)
    parser.add_argument("--output",  default=None, help="Save JSON results to file")
    parser.add_argument("--testnet", action="store_true")
    args = parser.parse_args()

    result = run_backtest(args.symbol, days=args.days, testnet=args.testnet)
    s = result.summary()

    print("\n" + "=" * 50)
    print(f"  BACKTEST RESULTS — {s['symbol']} ({s['days']}d)")
    print("=" * 50)
    print(f"  Trades:        {s['total_trades']}")
    print(f"  Win Rate:      {s['winrate_pct']}%")
    print(f"  Total PnL:     {s['total_pnl_usdt']} USDT")
    print(f"  Sharpe:        {s['sharpe']}")
    print(f"  Max Drawdown:  {s['max_drawdown_usdt']} USDT")
    print(f"  Profit Factor: {s['profit_factor']}")
    print("=" * 50 + "\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump({"summary": s, "trades": result.trades}, f, indent=2)
        print(f"Results saved to {args.output}")
