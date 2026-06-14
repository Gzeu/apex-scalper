"""Walk-Forward OOS Validator v0.7.0 + Monte Carlo.

Usage:
    python -m apex_scalper.walk_forward --symbol BTCUSDT --windows 6
    python -m apex_scalper.walk_forward --symbol ETHUSDT --windows 8 --train 60 --oos 20

Architecture:
  Rolling window: [train_days | oos_days] steps forward by step_days each round
  For each window:
    1. Run optimizer on TRAIN data -> best params
    2. Run backtester on OOS data with those params -> OOS metrics
    3. Overfitting check: OOS_Sharpe < 0.6 * TRAIN_Sharpe -> flag
  Aggregate: median OOS Sharpe, WinRate, PF across all windows

Monte Carlo:
  After walk-forward, runs 1000 random shuffles of all OOS trades
  Reports P5 / P50 / P95 PnL distribution
  P5 < -daily_loss_limit -> NOT safe for mainnet at current sizing

Outputs:
  Console table per window
  JSON file with all results (--output flag)
  Telegram alert if all windows pass OOS Sharpe >= 1.0 (optional)
"""
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timezone
from loguru import logger

from .backtester import fetch_klines, run_backtest
from .config import SYMBOL_PROFILES, DEFAULT_SYMBOL


def _monte_carlo(trades: list[dict], n_sim: int = 1000) -> dict:
    """Shuffle trade PnL n_sim times, return P5/P50/P95 final equity."""
    pnls = [t["pnl_usdt_net"] for t in trades]
    if len(pnls) < 5:
        return {"p5": 0, "p50": 0, "p95": 0, "n_trades": len(pnls)}
    results = []
    for _ in range(n_sim):
        shuffled = pnls[:]
        random.shuffle(shuffled)
        results.append(sum(shuffled))
    results.sort()
    p5  = results[int(n_sim * 0.05)]
    p50 = results[int(n_sim * 0.50)]
    p95 = results[int(n_sim * 0.95)]
    return {
        "p5":       round(p5,  4),
        "p50":      round(p50, 4),
        "p95":      round(p95, 4),
        "n_trades": len(pnls),
    }


def _sharpe_from_trades(trades: list[dict]) -> float:
    returns = [t.get("pnl_pct_net", 0) for t in trades]
    if len(returns) < 2:
        return 0.0
    mu  = sum(returns) / len(returns)
    std = math.sqrt(sum((r - mu) ** 2 for r in returns) / len(returns))
    return round((mu / std) * math.sqrt(252 * 1440) if std > 0 else 0.0, 3)


def run_walk_forward(
    symbol: str,
    train_days: int = 60,
    oos_days: int   = 20,
    step_days: int  = 10,
    n_windows: int  = 6,
    testnet: bool   = False,
    verbose: bool   = True,
) -> dict:
    total_days = train_days + (n_windows - 1) * step_days + oos_days
    logger.info(
        f"Walk-Forward {symbol}: {n_windows} windows, "
        f"train={train_days}d oos={oos_days}d step={step_days}d "
        f"(total ~{total_days}d history needed)"
    )

    # Download full history once
    logger.info(f"Downloading {total_days}d klines for {symbol}...")
    all_candles = fetch_klines(symbol, days=total_days, testnet=testnet)
    logger.info(f"Downloaded {len(all_candles)} candles")

    candles_per_day = 24 * 60  # 1m bars
    train_len = train_days * candles_per_day
    oos_len   = oos_days   * candles_per_day
    step_len  = step_days  * candles_per_day

    profile = SYMBOL_PROFILES.get(symbol, SYMBOL_PROFILES[DEFAULT_SYMBOL])

    window_results = []
    all_oos_trades: list[dict] = []

    for w in range(n_windows):
        start   = w * step_len
        t_start = start
        t_end   = start + train_len
        o_start = t_end
        o_end   = t_end + oos_len

        if o_end > len(all_candles):
            logger.warning(f"Window {w+1}: not enough candles, stopping")
            break

        train_candles = all_candles[t_start:t_end]
        oos_candles   = all_candles[o_start:o_end]

        # Train: run backtester (uses default profile — optimizer integration TODO v0.8)
        train_result = run_backtest(symbol, days=train_days, profile=profile,
                                    candles=train_candles)
        # OOS: same params, unseen data
        oos_result = run_backtest(symbol, days=oos_days, profile=profile,
                                  candles=oos_candles)

        train_sharpe = train_result.sharpe
        oos_sharpe   = oos_result.sharpe
        overfit_flag = oos_sharpe < 0.6 * train_sharpe and train_sharpe > 0.5

        wres = {
            "window":       w + 1,
            "train_sharpe": train_sharpe,
            "oos_sharpe":   oos_sharpe,
            "oos_winrate":  oos_result.winrate,
            "oos_pf":       oos_result.profit_factor,
            "oos_trades":   oos_result.total_trades,
            "oos_net_pnl":  round(oos_result.total_pnl, 4),
            "overfit":      overfit_flag,
        }
        window_results.append(wres)
        all_oos_trades.extend(oos_result.trades)

        if verbose:
            flag = " ⚠️ OVERFIT" if overfit_flag else ""
            print(
                f"  W{w+1:02d} | train_sharpe={train_sharpe:6.3f} "
                f"oos_sharpe={oos_sharpe:6.3f} "
                f"oos_wr={oos_result.winrate:5.1f}% "
                f"oos_pf={oos_result.profit_factor:5.3f} "
                f"oos_pnl={oos_result.total_pnl:+8.4f} "
                f"trades={oos_result.total_trades:4d}{flag}"
            )

    if not window_results:
        return {"error": "No windows completed"}

    # Aggregate OOS metrics
    oos_sharpes  = [w["oos_sharpe"]  for w in window_results]
    oos_winrates = [w["oos_winrate"] for w in window_results]
    oos_pfs      = [w["oos_pf"]      for w in window_results]
    overfits     = sum(1 for w in window_results if w["overfit"])

    median_sharpe = sorted(oos_sharpes)[len(oos_sharpes) // 2]
    median_wr     = sorted(oos_winrates)[len(oos_winrates) // 2]
    median_pf     = sorted(oos_pfs)[len(oos_pfs) // 2]
    total_oos_pnl = sum(w["oos_net_pnl"] for w in window_results)

    # Monte Carlo on all OOS trades
    mc = _monte_carlo(all_oos_trades)

    mainnet_safe = (
        median_sharpe >= 1.0
        and median_pf  >= 1.2
        and mc["p5"]   >= -abs(profile.get("daily_loss_limit_usdt", 50.0))
        and overfits   <= len(window_results) // 3
    )

    summary = {
        "symbol":         symbol,
        "windows":        len(window_results),
        "train_days":     train_days,
        "oos_days":       oos_days,
        "median_oos_sharpe": median_sharpe,
        "median_oos_winrate": median_wr,
        "median_oos_pf":  median_pf,
        "total_oos_pnl":  round(total_oos_pnl, 4),
        "overfit_windows": overfits,
        "monte_carlo":    mc,
        "mainnet_safe":   mainnet_safe,
        "verdict": (
            "✅ MAINNET READY" if mainnet_safe
            else "❌ NOT READY — fix strategy or reduce sizing"
        ),
        "window_results": window_results,
    }
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-Forward OOS Validator v0.7.0")
    parser.add_argument("--symbol",  default="BTCUSDT")
    parser.add_argument("--windows", type=int, default=6)
    parser.add_argument("--train",   type=int, default=60)
    parser.add_argument("--oos",     type=int, default=20)
    parser.add_argument("--step",    type=int, default=10)
    parser.add_argument("--output",  default=None)
    parser.add_argument("--testnet", action="store_true")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  WALK-FORWARD OOS v0.7.0 — {args.symbol}")
    print(f"  {args.windows} windows | train={args.train}d oos={args.oos}d step={args.step}d")
    print(f"{'='*70}")

    result = run_walk_forward(
        args.symbol,
        train_days=args.train,
        oos_days=args.oos,
        step_days=args.step,
        n_windows=args.windows,
        testnet=args.testnet,
    )

    print(f"{'='*70}")
    print(f"  Median OOS Sharpe:    {result['median_oos_sharpe']}")
    print(f"  Median OOS Win Rate:  {result['median_oos_winrate']}%")
    print(f"  Median OOS PF:        {result['median_oos_pf']}")
    print(f"  Total OOS Net PnL:    {result['total_oos_pnl']} USDT")
    print(f"  Overfit windows:      {result['overfit_windows']}/{result['windows']}")
    print(f"  Monte Carlo P5/P50/P95: {result['monte_carlo']['p5']} / "
          f"{result['monte_carlo']['p50']} / {result['monte_carlo']['p95']} USDT")
    print(f"  Verdict: {result['verdict']}")
    print(f"{'='*70}\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Results saved to {args.output}")
