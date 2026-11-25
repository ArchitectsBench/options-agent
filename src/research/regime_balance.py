"""Regime-balance experiment: can a BEAR-defensive sleeve fix the momentum book's
2022 fragility WITHOUT overfitting?

The RESMOM+EARNMOM book is bull-regime-dependent (negative in the 2022 bear and the
2023 recovery; all edge 2024-26), so it fails the per-regime gate. Hypothesis: a
factor that is POSITIVE in the bear — even if its full-period Sharpe is negative —
can lift the combined book's WORST YEAR toward zero. That's genuine cross-regime
diversification (vs EQGROW, which faked robustness by being bull-only too).

Step 1: per-calendar-year Sharpe of every factor -> which are positive in 2022/2023?
Step 2: test base + each bear-defensive sleeve (and base + all) against the worst-year
        gate. Honest outcome either way: if the combo still loses in the bear, the
        answer is the long game (more data/features), not more factor mining.

  python src/research/regime_balance.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.research.factor_survey import build_factors, eq_risk
from src.research.factor_stack import stats
from src.research.monte_carlo import per_year_sharpe, REGIME_MIN_WORST_YEAR_SHARPE

BASE = ["RESMOM", "EARNMOM"]
BEAR_YEARS = [2022, 2023]   # the years the base book loses


def main():
    F = build_factors()
    years = sorted({y for k in F for y in per_year_sharpe(F[k])})

    # ── Step 1: per-year Sharpe of every factor ──
    print("Per-calendar-year Sharpe (looking for sleeves POSITIVE in the 2022 bear):")
    print(f"{'factor':10} " + " ".join(f"{y:>7}" for y in years) + f"  {'full':>6}")
    bear_defensive = []
    for k in F:
        by = per_year_sharpe(F[k])
        full = stats(F[k])["sharpe"]
        cells = " ".join(f"{by.get(y, float('nan')):>7.2f}" for y in years)
        bear_ok = all(by.get(y, -9) > 0 for y in BEAR_YEARS)
        tag = "  <- bear-defensive" if (bear_ok and k not in BASE) else ""
        if bear_ok and k not in BASE:
            bear_defensive.append(k)
        print(f"{k:10} {cells}  {full:>6.2f}{tag}")

    print(f"\nBear-defensive sleeves (positive in {BEAR_YEARS}): {bear_defensive or 'NONE'}")

    # ── Step 2: do they lift the combined book's worst year past the gate? ──
    print(f"\n{'book':34} {'Sharpe':>7} {'OOS':>6} {'MC5':>6} {'WorstYr':>8} {'2022':>6} {'2023':>6}  gate")

    def show(name, keys):
        d = eq_risk(F, keys)
        m = stats(d)
        by = m["by"]
        ok = (m["sharpe"] >= 1.2 and m["oos"] >= 0.8 and m["maxdd"] <= 0.2 and m["mc5"] > 0.5
              and not pd.isna(m["wy"]) and m["wy"] >= REGIME_MIN_WORST_YEAR_SHARPE)
        print(f"{name:34} {m['sharpe']:>7.2f} {m['oos']:>6.2f} {m['mc5']:>6.2f} "
              f"{m['wy']:>8.2f} {by.get(2022, float('nan')):>6.2f} {by.get(2023, float('nan')):>6.2f}"
              f"  {'PASS ✅' if ok else ''}")

    show("base (RESMOM+EARNMOM)", BASE)
    for k in bear_defensive:
        show(f"base + {k}", BASE + [k])
    if len(bear_defensive) > 1:
        show("base + ALL bear-defensive", BASE + bear_defensive)
    # also try the canonical defensive even if it failed the strict 2-year test,
    # to show the worst-year sensitivity
    for k in ["LOWBETA", "VALUE", "IVOL", "GPROF"]:
        if k in F and k not in bear_defensive:
            show(f"base + {k} (not bear-pos)", BASE + [k])


if __name__ == "__main__":
    main()
