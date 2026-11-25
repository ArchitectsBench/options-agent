"""Stage 5/6 Monte-Carlo + acceptance-gate tests (src/research/monte_carlo.py).

All expected values are hand-computed:
  * per_year / worst_year Sharpe on a two-year series with a known mean/std;
  * bootstrap determinism and sign;
  * perturbed_param_sets enumerates the exact ±20% variants;
  * parameter_sensitivity returns worst/base retention;
  * acceptance_gate PASSES a clean report and FAILS one that violates each of
    the robustness gates (worst-year, OOS-vs-IS ratio, drawdown, trade count,
    NaN headline) — the overfit-rejection behaviour of acceptance criterion #3.

Run: .venv/bin/python -m unittest tests.test_monte_carlo -v
"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.monte_carlo import (  # noqa: E402
    MonteCarloReport, acceptance_gate, bootstrap_sharpe, parameter_sensitivity,
    per_year_sharpe, perturbed_param_sets, worst_year_sharpe,
)


class TestPerYearSharpe(unittest.TestCase):
    def _two_year_series(self):
        idx = pd.to_datetime([
            "2020-01-02", "2020-01-03", "2020-01-06",   # 2020: +mean
            "2021-01-04", "2021-01-05", "2021-01-06"])  # 2021: -mean
        return pd.Series([0.01, 0.02, 0.03, -0.01, -0.02, -0.03], index=idx)

    def test_per_year_sharpe_known_values(self):
        # each year: mean 0.02, std(ddof=1) 0.01 -> annualised(pp=1) Sharpe ±2.0
        by = per_year_sharpe(self._two_year_series(), periods_per_year=1, min_sessions=3)
        self.assertAlmostEqual(by[2020], 2.0, places=9)
        self.assertAlmostEqual(by[2021], -2.0, places=9)

    def test_worst_year_returns_minimum(self):
        worst, by = worst_year_sharpe(self._two_year_series(), periods_per_year=1, min_sessions=3)
        self.assertAlmostEqual(worst, -2.0, places=9)
        self.assertEqual(set(by), {2020, 2021})

    def test_short_year_omitted_fails_closed(self):
        r = pd.Series([0.01, 0.02], index=pd.to_datetime(["2020-01-02", "2020-01-03"]))
        worst, by = worst_year_sharpe(r, periods_per_year=1, min_sessions=3)
        self.assertTrue(math.isnan(worst))
        self.assertEqual(by, {})

    def test_non_datetime_index_yields_empty(self):
        self.assertEqual(per_year_sharpe(pd.Series([0.01, 0.02, 0.03]), min_sessions=1), {})


class TestBootstrapSharpe(unittest.TestCase):
    def test_too_few_trades_is_nan(self):
        med, p05 = bootstrap_sharpe(np.array([0.1, 0.2]), trades_per_year=252)
        self.assertTrue(math.isnan(med) and math.isnan(p05))

    def test_positive_mean_and_deterministic(self):
        arr = np.array([0.05, 0.02, 0.03, 0.04, 0.06, 0.01])
        med, p05 = bootstrap_sharpe(arr, trades_per_year=252, n_resamples=500, seed=7)
        self.assertGreater(med, 0.0)
        self.assertLessEqual(p05, med)
        med2, _ = bootstrap_sharpe(arr, trades_per_year=252, n_resamples=500, seed=7)
        self.assertEqual(med, med2)  # same seed -> identical


class TestPerturbation(unittest.TestCase):
    def test_perturbed_param_sets_enumerates_numeric_only(self):
        out = perturbed_param_sets({"fast": 10, "slow": 20, "flag": True, "name": "x"})
        self.assertEqual([(n, v) for n, v, _ in out],
                         [("fast", 8), ("fast", 12), ("slow", 16), ("slow", 24)])

    def test_sensitivity_worst_over_base(self):
        score, detail = parameter_sensitivity(
            2.0, {"fast_period": 5, "slow_period": 20}, lambda p: 1.0)
        self.assertAlmostEqual(score, 0.5)     # worst 1.0 / base 2.0
        self.assertEqual(len(detail), 4)       # fast±, slow±, all satisfy fast<slow

    def test_sensitivity_nan_when_base_nonpositive(self):
        score, _ = parameter_sensitivity(
            0.0, {"fast_period": 5, "slow_period": 20}, lambda p: 1.0)
        self.assertTrue(math.isnan(score))


def _report(**over):
    """A report that PASSES every gate unless overridden."""
    base = dict(params={"a": 1}, is_sharpe=1.0, oos_sharpe=0.8, mc_median_sharpe=0.5,
                mc_p05_sharpe=0.2, sensitivity_score=0.9, sensitivity_detail=[],
                full_sharpe=1.0, full_max_dd=-0.10, full_trades=150,
                worst_year_sharpe=0.3, regime_by_year={2022: 0.3, 2023: 0.5})
    base.update(over)
    return MonteCarloReport(**base)


def _gate(report, keyword):
    return next(ok for c, ok, _ in acceptance_gate(report) if keyword in c)


class TestAcceptanceGate(unittest.TestCase):
    def test_clean_report_passes_all(self):
        self.assertTrue(all(ok for _, ok, _ in acceptance_gate(_report())))

    def test_negative_worst_year_fails(self):
        r = _report(worst_year_sharpe=-0.1, regime_by_year={2022: -0.1, 2023: 0.5})
        self.assertFalse(_gate(r, "Worst-year"))
        self.assertFalse(all(ok for _, ok, _ in acceptance_gate(r)))

    def test_oos_below_ratio_fails_even_if_above_floor(self):
        r = _report(is_sharpe=2.0, oos_sharpe=0.7)   # 0.7 >= 0.6 floor, but < 0.6*2.0
        self.assertTrue(_gate(r, "OOS Sharpe"))
        self.assertFalse(_gate(r, "within 60%"))

    def test_drawdown_over_limit_fails(self):
        self.assertFalse(_gate(_report(full_max_dd=-0.25), "Max drawdown"))

    def test_too_few_trades_fails(self):
        self.assertFalse(_gate(_report(full_trades=50), "100 trades"))

    def test_nan_headline_sharpe_fails(self):
        self.assertFalse(_gate(_report(full_sharpe=float("nan")), "full period"))


if __name__ == "__main__":
    unittest.main()
