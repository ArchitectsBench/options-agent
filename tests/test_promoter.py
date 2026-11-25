"""Stage 6 promoter tests (src/research/promoter.py).

The promoter is the ONLY writer of config/validated_strategies.yaml — the live
agent's single source of truth — so these tests REDIRECT that file to a temp
path (never touch the real one) and assert its hard guard:
  * promote() REFUSES (raises) a report that fails any acceptance criterion and
    writes nothing;
  * a passing report is written with params frozen and readable back;
  * re-promoting the same id replaces rather than duplicates.

Run: .venv/bin/python -m unittest tests.test_promoter -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research import promoter  # noqa: E402
from src.research.monte_carlo import MonteCarloReport  # noqa: E402


def _report(**over):
    base = dict(params={"fast_period": 5, "slow_period": 20}, is_sharpe=1.0,
                oos_sharpe=0.8, mc_median_sharpe=0.5, mc_p05_sharpe=0.2,
                sensitivity_score=0.9, sensitivity_detail=[], full_sharpe=1.0,
                full_max_dd=-0.10, full_trades=150, worst_year_sharpe=0.3,
                regime_by_year={2022: 0.3, 2023: 0.5})
    base.update(over)
    return MonteCarloReport(**base)


class TestPromoter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = promoter.VALIDATED_FILE
        promoter.VALIDATED_FILE = Path(self._tmp.name) / "validated_strategies.yaml"

    def tearDown(self):
        promoter.VALIDATED_FILE = self._orig
        self._tmp.cleanup()

    def test_failing_report_refused_and_nothing_written(self):
        bad = _report(worst_year_sharpe=-0.5, regime_by_year={2022: -0.5})
        with self.assertRaises(ValueError):
            promoter.promote("trend", "SPY", bad, run_id="r1")
        self.assertFalse(promoter.VALIDATED_FILE.exists())
        self.assertEqual(promoter.load_validated_strategies(), [])

    def test_passing_report_written_with_frozen_params(self):
        entry = promoter.promote("trend", "SPY", _report(), run_id="r1",
                                 data_window="2020-2026")
        self.assertEqual(entry["id"], "trend:SPY")
        self.assertEqual(entry["status"], "validated")
        # params carried through unchanged (numerically) and frozen
        self.assertEqual(entry["params"]["fast_period"], 5)
        self.assertEqual(entry["params"]["slow_period"], 20)
        loaded = promoter.load_validated_strategies()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "trend:SPY")
        self.assertEqual(loaded[0]["metrics"]["trades"], 150)

    def test_repromotion_replaces_not_duplicates(self):
        promoter.promote("trend", "SPY", _report(), run_id="r1")
        promoter.promote("trend", "SPY", _report(full_sharpe=1.2), run_id="r2")
        loaded = promoter.load_validated_strategies()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["run_id"], "r2")

    def test_build_entry_carries_gate_and_metrics(self):
        entry = promoter.build_entry("trend", "QQQ", _report(), run_id="r3")
        self.assertEqual(entry["ticker"], "QQQ")
        self.assertTrue(all(g["passed"] for g in entry["gate"]))
        self.assertIsInstance(entry["metrics"]["trades"], int)


if __name__ == "__main__":
    unittest.main()
