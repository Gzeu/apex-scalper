"""Optuna optimizer v0.4.0 — grid search TP/SL/threshold per symbol.

Usage:
    pip install optuna
    python -m apex_scalper.optimizer --symbol BTCUSDT --days 60 --trials 200
    python -m apex_scalper.optimizer --symbol ETHUSDT --days 90 --trials 300 --metric sharpe

Optimizes over: tp1_pct, tp2_pct, sl_pct, trail_pct, trail_delta,
                entry_threshold, rsi_long_min, rsi_short_max

Objective: maximize Sharpe (default) or total PnL or profit_factor.
Best params are saved to optimizer_results/{symbol}_best.json and can be
pasted directly into SYMBOL_PROFILES in config.py.
"""
from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy

from loguru import logger

from .config import SYMBOL_PROFILES, DEFAULT_SYMBOL
from .backtester import fetch_klines, run_backtest


def optimize(
    symbol: str,
    days: int = 60,
    n_trials: int = 200,
    metric: str = "sharpe",    # 'sharpe' | 'pnl' | 'profit_factor'
    testnet: bool = False,
) -> dict:
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        raise ImportError(
            "optuna not installed. Run: pip install optuna"
        )

    base_profile = deepcopy(
        SYMBOL_PROFILES.get(symbol, SYMBOL_PROFILES[DEFAULT_SYMBOL])
    )

    # Download once, reuse across all trials
    logger.info(f"Downloading {days}d klines for {symbol} (used by all trials)...")
    candles = fetch_klines(symbol, days=days, testnet=testnet)
    logger.info(f"Downloaded {len(candles)} candles. Starting {n_trials} trials...")

    def objective(trial: "optuna.Trial") -> float:  # type: ignore
        profile = deepcopy(base_profile)

        # Search space
        profile["tp1_pct"]         = trial.suggest_float("tp1_pct",         0.0006, 0.003,  step=0.0001)
        profile["tp2_pct"]         = trial.suggest_float("tp2_pct",         0.0015, 0.006,  step=0.0001)
        profile["sl_pct"]          = trial.suggest_float("sl_pct",          0.0005, 0.002,  step=0.0001)
        profile["trail_pct"]       = trial.suggest_float("trail_pct",       0.0,    0.004,  step=0.0005)
        profile["trail_delta"]     = trial.suggest_float("trail_delta",     0.0002, 0.002,  step=0.0001)
        profile["entry_threshold"] = trial.suggest_float("entry_threshold", 0.45,   0.80,   step=0.05)
        profile["rsi_long_min"]    = trial.suggest_float("rsi_long_min",    48.0,   58.0,   step=1.0)
        profile["rsi_short_max"]   = trial.suggest_float("rsi_short_max",   42.0,   52.0,   step=1.0)
        profile["max_hold_candles"]= trial.suggest_int("max_hold_candles",  3, 10)

        # Constraint: tp2 > tp1, sl < tp1
        if profile["tp2_pct"] <= profile["tp1_pct"]:
            return -999.0
        if profile["sl_pct"] >= profile["tp1_pct"]:
            return -999.0

        result = run_backtest(symbol, days=days, profile=profile, candles=candles)

        # Penalize low trade count (overfitting guard)
        if result.total_trades < 5:
            return -999.0

        if metric == "sharpe":
            return result.sharpe
        elif metric == "pnl":
            return result.total_pnl
        elif metric == "profit_factor":
            return result.profit_factor
        else:
            return result.sharpe

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_params
    best_value = study.best_value
    logger.info(f"Best {metric}={best_value:.4f} | params={best}")

    # Merge best params into full profile for easy copy-paste
    merged = deepcopy(base_profile)
    merged.update(best)

    output = {
        "symbol": symbol,
        "days": days,
        "n_trials": n_trials,
        "metric": metric,
        "best_value": best_value,
        "best_params": best,
        "full_profile": merged,
    }

    os.makedirs("optimizer_results", exist_ok=True)
    out_path = f"optimizer_results/{symbol}_best.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"Best profile saved to {out_path}")

    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apex Scalper Optimizer (Optuna)")
    parser.add_argument("--symbol",  default="BTCUSDT")
    parser.add_argument("--days",    type=int, default=60)
    parser.add_argument("--trials",  type=int, default=200)
    parser.add_argument("--metric",  default="sharpe", choices=["sharpe", "pnl", "profit_factor"])
    parser.add_argument("--testnet", action="store_true")
    args = parser.parse_args()

    result = optimize(
        symbol=args.symbol,
        days=args.days,
        n_trials=args.trials,
        metric=args.metric,
        testnet=args.testnet,
    )

    print("\n" + "=" * 55)
    print(f"  OPTIMIZER RESULTS — {result['symbol']} ({result['metric']})")
    print("=" * 55)
    print(f"  Best {result['metric']}: {result['best_value']:.4f}")
    print(f"  Best params:")
    for k, v in result["best_params"].items():
        print(f"    {k:<22} = {v}")
    print("=" * 55)
    print(f"\n  Full profile saved to: optimizer_results/{result['symbol']}_best.json")
    print("  Paste 'full_profile' into SYMBOL_PROFILES in config.py\n")
