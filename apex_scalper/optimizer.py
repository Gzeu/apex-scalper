"""Optuna optimizer v0.4.1 — walk-forward validation (70% train / 30% OOS).

Walk-forward methodology:
  1. Download full candle history (e.g. 90 days)
  2. Split: train=first 63 days (70%), OOS=last 27 days (30%)
  3. Each Optuna trial optimizes on TRAIN set
  4. Best params are evaluated on OOS set
  5. Accept only if OOS Sharpe > OOS_MIN_SHARPE and OOS trades >= OOS_MIN_TRADES
  6. Report both train and OOS metrics side-by-side

This prevents overfitting to a specific 60-day window.

Usage:
    pip install optuna
    python -m apex_scalper.optimizer --symbol BTCUSDT --days 90 --trials 300
    python -m apex_scalper.optimizer --symbol ETHUSDT --days 120 --trials 500 --metric sharpe

Best params saved to optimizer_results/{symbol}_best.json
"""
from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy

from loguru import logger

from .config import SYMBOL_PROFILES, DEFAULT_SYMBOL
from .backtester import fetch_klines, run_backtest

OOS_SPLIT        = 0.30          # 30% of data = out-of-sample
OOS_MIN_SHARPE   = 0.5           # reject params if OOS Sharpe < this
OOS_MIN_TRADES   = 5             # reject if OOS produces fewer trades


def optimize(
    symbol: str,
    days: int = 90,
    n_trials: int = 300,
    metric: str = "sharpe",
    testnet: bool = False,
) -> dict:
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        raise ImportError("optuna not installed. Run: pip install optuna")

    base_profile = deepcopy(
        SYMBOL_PROFILES.get(symbol, SYMBOL_PROFILES[DEFAULT_SYMBOL])
    )

    # Download once, split into train / OOS
    logger.info(f"Downloading {days}d klines for {symbol}...")
    all_candles = fetch_klines(symbol, days=days, testnet=testnet)
    total = len(all_candles)
    split_idx = int(total * (1 - OOS_SPLIT))
    train_candles = all_candles[:split_idx]
    oos_candles   = all_candles[split_idx:]

    train_days = round(days * (1 - OOS_SPLIT))
    oos_days   = days - train_days
    logger.info(
        f"Walk-forward split: {len(train_candles)} candles train ({train_days}d) | "
        f"{len(oos_candles)} candles OOS ({oos_days}d)"
    )
    logger.info(f"Starting {n_trials} trials (metric={metric})...")

    def objective(trial: "optuna.Trial") -> float:  # type: ignore
        profile = deepcopy(base_profile)

        # Search space
        profile["tp1_pct"]          = trial.suggest_float("tp1_pct",         0.0006, 0.003,  step=0.0001)
        profile["tp2_pct"]          = trial.suggest_float("tp2_pct",         0.0015, 0.006,  step=0.0001)
        profile["sl_pct"]           = trial.suggest_float("sl_pct",          0.0005, 0.002,  step=0.0001)
        profile["trail_pct"]        = trial.suggest_float("trail_pct",       0.0,    0.004,  step=0.0005)
        profile["trail_delta"]      = trial.suggest_float("trail_delta",     0.0002, 0.002,  step=0.0001)
        profile["entry_threshold"]  = trial.suggest_float("entry_threshold", 0.45,   0.80,   step=0.05)
        profile["rsi_long_min"]     = trial.suggest_float("rsi_long_min",    48.0,   58.0,   step=1.0)
        profile["rsi_short_max"]    = trial.suggest_float("rsi_short_max",   42.0,   52.0,   step=1.0)
        profile["max_hold_candles"] = trial.suggest_int("max_hold_candles",  3, 10)

        # Hard constraints
        if profile["tp2_pct"] <= profile["tp1_pct"]:
            return -999.0
        if profile["sl_pct"] >= profile["tp1_pct"]:
            return -999.0

        # Evaluate on TRAIN set
        train_result = run_backtest(
            symbol, profile=profile, candles=train_candles
        )
        if train_result.total_trades < 5:
            return -999.0

        if metric == "sharpe":
            return train_result.sharpe
        elif metric == "pnl":
            return train_result.total_pnl
        elif metric == "profit_factor":
            return train_result.profit_factor
        return train_result.sharpe

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best_params = study.best_params
    best_train_value = study.best_value

    # ── Evaluate best params on OOS (out-of-sample) ──
    oos_profile = deepcopy(base_profile)
    oos_profile.update(best_params)
    oos_result = run_backtest(symbol, profile=oos_profile, candles=oos_candles)
    train_result = run_backtest(symbol, profile=oos_profile, candles=train_candles)

    oos_sharpe = oos_result.sharpe
    oos_trades = oos_result.total_trades
    oos_accepted = oos_sharpe >= OOS_MIN_SHARPE and oos_trades >= OOS_MIN_TRADES

    logger.info(
        f"{'✅' if oos_accepted else '❌'} Walk-forward result:\n"
        f"  TRAIN ({train_days}d): sharpe={train_result.sharpe:.3f} "
        f"pnl={train_result.total_pnl:.2f} trades={train_result.total_trades}\n"
        f"  OOS   ({oos_days}d):   sharpe={oos_sharpe:.3f} "
        f"pnl={oos_result.total_pnl:.2f} trades={oos_trades}\n"
        f"  OOS accepted: {oos_accepted} "
        f"(min_sharpe={OOS_MIN_SHARPE} min_trades={OOS_MIN_TRADES})"
    )

    if not oos_accepted:
        logger.warning(
            "OOS validation FAILED — params overfit to training window. "
            "Try more days (--days 120+) or fewer trials."
        )

    merged = deepcopy(base_profile)
    merged.update(best_params)

    output = {
        "symbol":            symbol,
        "days":              days,
        "train_days":        train_days,
        "oos_days":          oos_days,
        "n_trials":          n_trials,
        "metric":            metric,
        "oos_accepted":      oos_accepted,
        "train_metrics": {
            "sharpe":         train_result.sharpe,
            "total_pnl":      round(train_result.total_pnl, 4),
            "total_trades":   train_result.total_trades,
            "winrate_pct":    train_result.winrate,
            "profit_factor":  train_result.profit_factor,
            "max_drawdown":   train_result.max_drawdown,
        },
        "oos_metrics": {
            "sharpe":         oos_result.sharpe,
            "total_pnl":      round(oos_result.total_pnl, 4),
            "total_trades":   oos_result.total_trades,
            "winrate_pct":    oos_result.winrate,
            "profit_factor":  oos_result.profit_factor,
            "max_drawdown":   oos_result.max_drawdown,
        },
        "best_params":   best_params,
        "full_profile":  merged,
    }

    os.makedirs("optimizer_results", exist_ok=True)
    out_path = f"optimizer_results/{symbol}_best.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"Results saved to {out_path}")

    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apex Scalper Optimizer — walk-forward")
    parser.add_argument("--symbol",  default="BTCUSDT")
    parser.add_argument("--days",    type=int, default=90,
                        help="Total days to download. 70%% train, 30%% OOS.")
    parser.add_argument("--trials",  type=int, default=300)
    parser.add_argument("--metric",  default="sharpe",
                        choices=["sharpe", "pnl", "profit_factor"])
    parser.add_argument("--testnet", action="store_true")
    args = parser.parse_args()

    result = optimize(
        symbol=args.symbol,
        days=args.days,
        n_trials=args.trials,
        metric=args.metric,
        testnet=args.testnet,
    )

    accepted = "✅ ACCEPTED" if result["oos_accepted"] else "❌ REJECTED (overfit)"
    print("\n" + "=" * 60)
    print(f"  OPTIMIZER — {result['symbol']} | {result['metric']} | {accepted}")
    print("=" * 60)
    print(f"  TRAIN ({result['train_days']}d):")
    for k, v in result["train_metrics"].items():
        print(f"    {k:<20} = {v}")
    print(f"  OOS ({result['oos_days']}d):")
    for k, v in result["oos_metrics"].items():
        print(f"    {k:<20} = {v}")
    print(f"  Best params:")
    for k, v in result["best_params"].items():
        print(f"    {k:<22} = {v}")
    print("=" * 60)
    if not result["oos_accepted"]:
        print("  ⚠️  OOS validation FAILED — do NOT use these params live!")
        print("  Try: --days 120 --trials 500")
    else:
        print(f"  ✅ Paste full_profile into config.py → SYMBOL_PROFILES['{result['symbol']}']")
    print()
