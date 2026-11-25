"""Phase 1, Step 4b — full Stage 2-5 pipeline on SPY 1-hour bars, session-flat.

Usage: python src/research/run_pipeline_1h.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.research.backtester import backtest
from src.research.monte_carlo import acceptance_gate, evaluate_candidate
from src.research.optimizer import optimize_ema_crossover
from src.research.rule_validator import validate_strategy
from src.research.strategies import ema_crossover

TICKER = "SPY"
IS_FRACTION = 0.70
N_TRIALS = 200
BT_KWARGS = {"flat_at_session_end": True}


def fmt(x, spec=".2f"):
    return "  nan" if pd.isna(x) else f"{x:{spec}}"


def main():
    spec = yaml.safe_load((ROOT / "config" / "strategy_rules.yaml").read_text())["ema_crossover_1h"]
    df = pd.read_parquet(ROOT / "data" / "historical" / f"{TICKER}_1h.parquet")
    n_sessions = pd.DatetimeIndex(df.index).normalize().nunique()
    print(f"{TICKER} 1h: {len(df)} bars across {n_sessions} sessions "
          f"({df.index.min()} to {df.index.max()})")

    # ── Stage 2: rule validation (baseline 9/21 parameterization) ──
    report = validate_strategy("ema_crossover_1h", spec, ema_crossover(9, 21), df)
    print("\n" + report.summary())
    if not report.passed:
        print("\nRejected at Stage 2 — stopping.")
        sys.exit(1)

    # ── In-sample / out-of-sample split on session boundaries ──
    split = int(len(df) * IS_FRACTION)
    df_is, df_oos = df.iloc[:split], df.iloc[split:]
    print(f"\nIn-sample: {len(df_is)} bars ({df_is.index.min().date()} to {df_is.index.max().date()}) | "
          f"OOS (untouched until Stage 5c): {len(df_oos)} bars "
          f"({df_oos.index.min().date()} to {df_oos.index.max().date()})")

    # ── Stage 3: baseline backtest (9/21) for reference ──
    base = backtest(df_is, *ema_crossover(9, 21)(df_is), **BT_KWARGS)
    print(f"\nStage 3 — baseline EMA 9/21 in-sample: Sharpe {fmt(base.sharpe)}, "
          f"{base.n_trades} trades, win rate {base.win_rate:.1%}, "
          f"PF {fmt(base.profit_factor)}, maxDD {base.max_drawdown:.1%}")

    # ── Stage 4: Optuna optimization (in-sample only) ──
    print(f"\nStage 4 — Optuna TPE, {N_TRIALS} trials, fast 5-50 / slow 10-100 (fast < slow)")
    candidates = optimize_ema_crossover(df_is, n_trials=N_TRIALS, top_n=10, backtest_kwargs=BT_KWARGS)
    print(f"\nTop {len(candidates)} parameter combinations (in-sample Sharpe):")
    print(f"  {'rank':<5}{'fast':>5}{'slow':>6}{'IS Sharpe':>11}")
    for i, c in enumerate(candidates, 1):
        print(f"  {i:<5}{c.params['fast_period']:>5}{c.params['slow_period']:>6}{c.is_sharpe:>11.2f}")

    # ── Stage 5: Monte Carlo overfit filter ──
    print("\nStage 5 — Monte Carlo filter (1000 bootstrap resamples, ±20% sensitivity, OOS check)")
    reports = [
        evaluate_candidate(c.params, c.is_sharpe, df_is, df_oos, df, ema_crossover,
                           backtest_kwargs=BT_KWARGS)
        for c in candidates
    ]
    reports.sort(key=lambda r: -np.inf if pd.isna(r.mc_p05_sharpe) else r.mc_p05_sharpe, reverse=True)

    header = (f"  {'rank':<5}{'params':<14}{'IS Shp':>8}{'OOS Shp':>9}"
              f"{'MC med':>8}{'MC 5pct':>9}{'sens':>7}{'trades':>8}")
    print("\n" + header)
    print("  " + "-" * (len(header) - 2))
    for i, r in enumerate(reports, 1):
        params = f"({r.params['fast_period']},{r.params['slow_period']})"
        print(f"  {i:<5}{params:<14}{fmt(r.is_sharpe):>8}{fmt(r.oos_sharpe):>9}"
              f"{fmt(r.mc_median_sharpe):>8}{fmt(r.mc_p05_sharpe):>9}"
              f"{fmt(r.sensitivity_score):>7}{r.full_trades:>8}")
    print("  (sens = worst ±20%-perturbed Sharpe / base IS Sharpe; trades = full period)")

    # ── Stage 6 acceptance gate ──
    print("\nAcceptance gate (CLAUDE.md Stage 6):")
    any_passed = False
    for r in reports:
        results = acceptance_gate(r)
        passed = all(ok for _, ok, _ in results)
        any_passed |= passed
        params = f"({r.params['fast_period']},{r.params['slow_period']})"
        failed = [f"{name} (got {actual})" for name, ok, actual in results if not ok]
        print(f"  {params:<14}{'PROMOTED' if passed else 'rejected'}"
              + ("" if passed else f" — fails: {'; '.join(failed)}"))

    print(f"\n=> {'At least one candidate PASSED the acceptance bar.' if any_passed else 'No candidate passed the acceptance bar.'}")


if __name__ == "__main__":
    main()
