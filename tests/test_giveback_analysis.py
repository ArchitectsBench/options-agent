"""Give-back analysis tests (scripts/giveback_analysis.py) — evidence-integrity
math for the L13 exit fix. Hand-computed expectations on a tmp ledger (L6):
capture ratio, winners-turned counting, the counterfactual lock, and the
fix-readiness pooling (test+funnel paths pooled; funnel tracked separately).

Run: .venv/bin/python -m unittest tests.test_giveback_analysis -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import giveback_analysis as ga  # noqa: E402


def _exit(rec_id, entry, mfe, exit_cr, qty=1, reason="vwap_invalidation", marks=None):
    return {"event": "paper_exit", "rec_id": rec_id, "ticker": "X",
            "ts": "2026-07-09T20:00:00Z", "exit_reason": reason,
            "round_trip": {"entry_net_debit": entry, "mfe_value": mfe,
                           "exit_net_credit": exit_cr, "quantity": qty,
                           "mark_series": marks}}


class GivebackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = ga.FILLS
        ga.FILLS = Path(self._tmp.name) / "fills.jsonl"

    def tearDown(self):
        ga.FILLS = self._orig
        self._tmp.cleanup()

    def _write(self, events):
        ga.FILLS.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    def test_capture_ratio_and_winner_turned(self):
        # SLV-shaped: entry 1.00, peak 2.00 (+1.00), exit 0.50 (-0.50), qty 1
        # peak profit $100, realized -$50 -> capture -0.5, giveback $150
        self._write([_exit("2026-07-09-001", 1.00, 2.00, 0.50)])
        rep = ga.analyze(ga._rows())["vwap_invalidation"]
        self.assertEqual(rep["n_reached_profit"], 1)
        self.assertEqual(rep["n_winners_turned_flat_or_red"], 1)
        self.assertAlmostEqual(rep["aggregate_capture_ratio"], -0.5)
        self.assertEqual(rep["total_giveback_usd"], 150)

    def test_full_capture_is_ratio_one(self):
        # exit at the peak: entry 1.00, mfe 1.40, exit 1.40 -> capture 1.0
        self._write([_exit("2026-07-09-002", 1.00, 1.40, 1.40, reason="target_50")])
        rep = ga.analyze(ga._rows())["target_50"]
        self.assertAlmostEqual(rep["aggregate_capture_ratio"], 1.0)
        self.assertEqual(rep["n_winners_turned_flat_or_red"], 0)

    def test_counterfactual_lock_math(self):
        # entry 1.00, peak reached 1.30 (+0.30 = 30% of debit), exited -0.20.
        # Lock at 20% of debit (trigger +0.20): peak reached it -> locked +$20.
        # Lock at 50%: never reached -> keeps actual -$20.
        self._write([_exit("2026-07-09-003", 1.00, 1.30, 0.80)])
        cf = ga.counterfactual_target_sweep(ga._rows())
        self.assertEqual(cf["baseline_actual_usd"], -20)
        self.assertEqual(cf["levels"]["20pct_of_debit"]["policy_pnl_usd"], 20)
        self.assertEqual(cf["levels"]["50pct_of_debit"]["policy_pnl_usd"], -20)

    def test_fix_readiness_pools_test_and_funnel(self):
        marks = [["t1", 1.0], ["t2", 1.2], ["t3", 0.9]]
        self._write([
            _exit("2026-07-09-004", 1.0, 1.2, 0.9, marks=marks),            # funnel, path
            _exit("20260709-TEST-9-SPY-debit_spread", 1.0, 1.2, 0.9, marks=marks),  # test, path
            _exit("20260709-TEST-10-QQQ-debit_spread", 1.0, 1.2, 0.9),      # test, NO path
        ])
        fr = ga.fix_readiness(ga._rows())
        self.assertEqual(fr["ordered_path_positions_pooled"], 2)   # test+funnel pooled
        self.assertEqual(fr["ordered_path_positions_funnel"], 1)   # funnel tracked separately
        self.assertFalse(fr["ready_to_tune"])


if __name__ == "__main__":
    unittest.main()
