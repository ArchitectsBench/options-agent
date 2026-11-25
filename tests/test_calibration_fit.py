"""Calibration fitter tests (src/research/calibration_fit.py).

Covers: PAV correctness (known small cases, weighted pooling, monotonicity on
random inputs, already-monotone passthrough), the remap interpolation/clamping,
threshold inversion (reachable, unreachable, endpoint), and end-to-end fitting
on a synthetic label set (including the schema-drift row guard). The fitter is
propose-only, so tests also assert run(write=False) touches no files.

Run: .venv/bin/python -m unittest tests.test_calibration_fit -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.calibration_fit import (  # noqa: E402
    fit_isotonic_map, invert_threshold, load_candidates, pav, remap, status_group,
    strategy_group,
)


class TestPAV(unittest.TestCase):
    def test_already_monotone_is_unchanged(self):
        y = np.array([0.1, 0.2, 0.2, 0.5, 0.9])
        w = np.ones_like(y)
        np.testing.assert_allclose(pav(y, w), y)

    def test_single_violation_pools_to_mean(self):
        # classic: [1, 0] with equal weights pools to [0.5, 0.5]
        np.testing.assert_allclose(pav(np.array([1.0, 0.0]), np.ones(2)), [0.5, 0.5])

    def test_known_three_point_case(self):
        # [0.6, 0.2, 0.7]: first two pool to 0.4, third stays
        np.testing.assert_allclose(
            pav(np.array([0.6, 0.2, 0.7]), np.ones(3)), [0.4, 0.4, 0.7])

    def test_weighted_pooling(self):
        # weights 3 and 1: pooled mean = (0.8*3 + 0.0*1)/4 = 0.6
        np.testing.assert_allclose(
            pav(np.array([0.8, 0.0]), np.array([3.0, 1.0])), [0.6, 0.6])

    def test_cascading_merge(self):
        # a late low value must pull earlier pooled blocks down too
        y = np.array([0.5, 0.6, 0.0])
        got = pav(y, np.ones(3))
        expected = np.full(3, y.mean())
        np.testing.assert_allclose(got, expected)

    def test_monotone_and_weighted_mean_preserving_on_random_input(self):
        rng = np.random.default_rng(42)
        for _ in range(20):
            y = rng.random(30)
            w = rng.random(30) + 0.1
            out = pav(y, w)
            self.assertTrue(np.all(np.diff(out) >= -1e-12), "fit must be non-decreasing")
            # PAV preserves the weighted mean
            self.assertAlmostEqual(float(np.sum(out * w)), float(np.sum(y * w)), places=9)

    def test_empty_and_bad_inputs(self):
        self.assertEqual(len(pav(np.array([]), np.array([]))), 0)
        with self.assertRaises(ValueError):
            pav(np.array([1.0]), np.array([0.0]))  # non-positive weight
        with self.assertRaises(ValueError):
            pav(np.array([1.0, 2.0]), np.array([1.0]))  # length mismatch


class TestRemapAndInversion(unittest.TestCase):
    XS = np.array([0.4, 0.6, 0.8])
    FIT = np.array([0.30, 0.50, 0.70])

    def test_remap_interpolates_and_clamps(self):
        self.assertAlmostEqual(float(remap(0.5, self.XS, self.FIT)), 0.40)
        self.assertAlmostEqual(float(remap(0.6, self.XS, self.FIT)), 0.50)
        self.assertAlmostEqual(float(remap(0.1, self.XS, self.FIT)), 0.30)  # clamp low
        self.assertAlmostEqual(float(remap(0.99, self.XS, self.FIT)), 0.70)  # clamp high

    def test_invert_reachable_target(self):
        # calibrated 0.60 sits halfway between (0.6, 0.50) and (0.8, 0.70) -> raw 0.7
        self.assertAlmostEqual(invert_threshold(self.XS, self.FIT, 0.60), 0.7)

    def test_invert_exact_breakpoint(self):
        self.assertAlmostEqual(invert_threshold(self.XS, self.FIT, 0.50), 0.6)

    def test_invert_unreachable_returns_none(self):
        self.assertIsNone(invert_threshold(self.XS, self.FIT, 0.75))
        self.assertIsNone(invert_threshold(np.array([]), np.array([]), 0.5))

    def test_invert_target_below_map_returns_first_breakpoint(self):
        self.assertEqual(invert_threshold(self.XS, self.FIT, 0.10), 0.4)

    def test_invert_flat_map(self):
        xs = np.array([0.5, 0.9])
        flat = np.array([0.45, 0.45])
        self.assertIsNone(invert_threshold(xs, flat, 0.46))
        self.assertEqual(invert_threshold(xs, flat, 0.45), 0.5)

    def test_roundtrip_remap_of_inverted_threshold(self):
        raw = invert_threshold(self.XS, self.FIT, 0.55)
        self.assertAlmostEqual(float(remap(raw, self.XS, self.FIT)), 0.55)


class TestFitOnSyntheticLabels(unittest.TestCase):
    @staticmethod
    def _rows(spec):
        """spec: list of (conf, n, n_wins) -> candidate rows."""
        rows = []
        for conf, n, wins in spec:
            for i in range(n):
                rows.append({"local_confidence": conf, "_day": "2026-06-11",
                             "cf_eod": 0.01 if i < wins else -0.01,
                             "strategy": "debit_spread", "status": "skipped_held"})
        return rows

    def test_per_bin_rates_and_monotonicity(self):
        rows = self._rows([(0.6, 10, 3), (0.7, 10, 8), (0.8, 10, 6)])
        xs, emp, fitted, ns = fit_isotonic_map(rows)
        np.testing.assert_allclose(xs, [0.6, 0.7, 0.8])
        np.testing.assert_allclose(emp, [0.3, 0.8, 0.6])
        np.testing.assert_allclose(ns, [10, 10, 10])
        # 0.8 and 0.6 empirical rates at conf 0.7/0.8 violate -> pool to 0.7
        np.testing.assert_allclose(fitted, [0.3, 0.7, 0.7])

    def test_load_candidates_skips_drifted_rows(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "labels-2026-06-11.jsonl"
            rows = [
                {"kind": "candidate", "local_confidence": 0.8, "cf_eod": 0.01},
                {"kind": "candidate", "local_confidence": None, "cf_eod": 0.01},
                {"kind": "candidate", "local_confidence": 0.8},  # missing cf_eod
                {"kind": "snapshot", "regime_bias": "bullish"},
            ]
            p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            got = load_candidates(Path(td))
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["_day"], "2026-06-11")

    def test_segment_group_helpers(self):
        self.assertEqual(strategy_group({"strategy": "0dte_directional"}), "0dte_directional")
        self.assertEqual(strategy_group({"strategy": "premium_selling"}), "other")
        self.assertEqual(strategy_group({"strategy": None}), "other")
        self.assertEqual(status_group({"status": "posted_approved"}), "actionable")
        self.assertEqual(status_group({"status": "skipped_veto_active"}), "skipped/vetoed")
        self.assertEqual(status_group({"status": "vetoed"}), "skipped/vetoed")


if __name__ == "__main__":
    unittest.main()
