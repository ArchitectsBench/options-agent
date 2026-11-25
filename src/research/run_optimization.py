"""Phase 1, Step 3 — Stages 4-5: optimize EMA crossover on SPY, then Monte Carlo filter.

Usage: python src/research/run_optimization.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.research.monte_carlo import acceptance_gate, evaluate_candidate
from src.research.optimizer import optimize_ema_crossover
from src.research.strategies import ema_crossover

TICKER = "SPY"
IS_FRACTION = 0.70
N_TRIALS = 200


def fmt(x, spec=".2f"):
    return "  nan" if pd.isna(x) else f"{x:{spec}}"


def main():
    df = pd.read_parquet(ROOT / "data" / "historical" / f"{TICKER}.parquet")
    split = int(len(df) * IS_FRACTION)
    df_is, df_oos = df.iloc[:split], df.iloc[split:]
    print(f"{TICKER} daily: {len(df)} bars | in-sample: {len(df_is)} "
          f"({df_is.index.min().date()} to {df_is.index.max().date()}) | "
          f"out-of-sample: {len(df_oos)} ({df_oos.index.min().date()} to {df_oos.index.max().date()})")

    # ── Stage 4: Bayesian optimization (in-sample only) ──
    print(f"\nStage 4 — Optuna TPE, {N_TRIALS} trials, fast 5-50 / slow 10-100 (fast < slow)")
    candidates = optimize_ema_crossover(df_is, n_trials=N_TRIALS, top_n=10)
    print(f"\nTop {len(candidates)} parameter combinations (in-sample Sharpe):")
    print(f"  {'rank':<5}{'fast':>5}{'slow':>6}{'IS Sharpe':>11}")
    for i, c in enumerate(candidates, 1):
        print(f"  {i:<5}{c.params['fast_period']:>5}{c.params['slow_period']:>6}{c.is_sharpe:>11.2f}")

    # ── Stage 5: Monte Carlo overfit filter ──
    print("\nStage 5 — Monte Carlo filter (1000 bootstrap resamples, ±20% sensitivity, OOS check)")
    reports = [
        evaluate_candidate(c.params, c.is_sharpe, df_is, df_oos, df, ema_crossover)
        for c in candidates
    ]
    # Rank by MC 5th-percentile Sharpe (NaN sinks to the bottom), not raw IS Sharpe.
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
    print("  (sens = worst ±20%-perturbed Sharpe / base IS Sharpe; trades = full 2yr period)")

    # ── Stage 6 acceptance gate ──
    print("\nAcceptance gate (CLAUDE.md Stage 6):")
    any_passed = False
    for r in reports:
        results = acceptance_gate(r)
        passed = all(ok for _, ok, _ in results)
        any_passed |= passed
        params = f"({r.params['fast_period']},{r.params['slow_period']})"
        status = "PROMOTED" if passed else "rejected"
        failed = [f"{name} (got {actual})" for name, ok, actual in results if not ok]
        print(f"  {params:<14}{status}" + ("" if passed else f" — fails: {'; '.join(failed)}"))

    print(f"\n=> {'At least one candidate PASSED — eligible for promotion.' if any_passed else 'No candidate passed the acceptance bar.'}")


if __name__ == "__main__":
    main()
