"""Tests for the z-signal entry overlay + execution-quality telemetry (2026-07-06).

Run: .venv/bin/python -m unittest tests.test_execution_overlay -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.engine.smile import price_z_scores, rank_candidates  # noqa: E402


def ladder(mids: dict[float, float]):
    return [{"strike": k, "mid": m} for k, m in mids.items()]


class SmileFitTest(unittest.TestCase):
    def test_flags_the_planted_outlier(self):
        # smooth exponential-decay ladder with one strike marked 15% rich
        mids = {k: round(20.0 * (0.82 ** i), 2) for i, k in enumerate(range(90, 112, 2))}
        mids[100] = round(mids[100] * 1.15, 2)  # rich
        z = price_z_scores(ladder(mids))
        self.assertTrue(z, "fit should succeed on 11 strikes")
        self.assertEqual(max(z, key=z.get), 100.0)  # richest strike found
        self.assertGreater(z[100.0], 1.0)

    def test_thin_ladder_returns_empty(self):
        self.assertEqual(price_z_scores(ladder({100: 5.0, 105: 3.0})), {})

    def test_bad_mids_skipped(self):
        rows = ladder({k: 5.0 for k in range(90, 104, 2)})
        rows[0]["mid"] = None
        self.assertIsInstance(price_z_scores(rows), dict)  # never raises


class RankTest(unittest.TestCase):
    def _cands(self):
        # sorted best-first by delta closeness; z will prefer strike 102 (cheap)
        return [
            {"strike": 100.0, "mid": 5.00, "_delta_dist": 0.01},
            {"strike": 102.0, "mid": 4.20, "_delta_dist": 0.03},
            {"strike": 104.0, "mid": 3.60, "_delta_dist": 0.09},  # outside band
        ]

    def test_buy_prefers_cheap_within_band(self):
        z = {100.0: 0.8, 102.0: -1.4, 104.0: -2.5}
        chosen, t = rank_candidates(self._cands(), z, "buy", delta_tolerance=0.05)
        self.assertEqual(chosen["strike"], 102.0)  # cheap and in-band
        self.assertTrue(t["applied"])
        self.assertEqual(t["default_strike"], 100.0)

    def test_sell_prefers_rich(self):
        z = {100.0: 0.8, 102.0: -1.4, 104.0: 2.5}
        chosen, _ = rank_candidates(self._cands(), z, "sell", delta_tolerance=0.05)
        self.assertEqual(chosen["strike"], 100.0)  # richest in-band

    def test_no_fit_keeps_default(self):
        chosen, t = rank_candidates(self._cands(), {}, "buy")
        self.assertEqual(chosen["strike"], 100.0)
        self.assertFalse(t["applied"])


class MidAtFillTest(unittest.TestCase):
    def test_capture_telemetry(self):
        import src.engine.paper_trader as pt
        legs = [{"symbol": "A", "action": "buy_to_open"},
                {"symbol": "B", "action": "sell_to_open"}]
        client = MagicMock()
        client.quotes.return_value = {"A": {"bid": 5.0, "ask": 5.2},
                                      "B": {"bid": 1.5, "ask": 1.7}}
        with patch("src.data.tradier.get_client", return_value=client):
            out = pt._mid_at_fill(legs)
        self.assertEqual(out["net_mid"], 3.5)       # 5.1 - 1.6
        self.assertEqual(out["spread_total"], 0.4)

    def test_missing_quote_returns_none(self):
        import src.engine.paper_trader as pt
        client = MagicMock()
        client.quotes.return_value = {"A": {"bid": 5.0, "ask": None}}
        with patch("src.data.tradier.get_client", return_value=client):
            self.assertIsNone(pt._mid_at_fill([{"symbol": "A", "action": "buy_to_open"}]))


class ExecutionQualityScriptTest(unittest.TestCase):
    def test_evaluate_runs_on_real_ledger(self):
        sys.path.insert(0, str(ROOT))
        from scripts.execution_quality import evaluate
        out = evaluate()
        self.assertIn("fill_rate", out)
        self.assertIn("capture_vs_mid_at_fill", out)


if __name__ == "__main__":
    unittest.main()
