"""Scalp forward tracker tests (scripts/scalp_forward_check.py).

Pins the evidence-window integrity that bit us on freeze day (2026-07-09): the
window filter compared timestamp STRINGS, so a UTC entry ('...14:25Z' = 07:25
PDT) read as later than the '-07:00' freeze stamp and 9 pre-freeze trades
leaked into frozen evidence. These tests redirect all state to a tmpdir
(lesson L6) and assert tz-aware filtering, naive-stamp localization, tag
scoping, and the fallback-structure split.

Run: .venv/bin/python -m unittest tests.test_scalp_forward_check -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scalp_forward_check as sfc  # noqa: E402

FREEZE = "2026-07-09T12:18:00-07:00"


def _exit(rec_id, entered, pnl, ts="2026-07-09T21:00:00Z"):
    return {"event": "paper_exit", "rec_id": rec_id, "ticker": rec_id.split("-")[-2],
            "ts": ts, "pnl_total": pnl,
            "round_trip": {"entry_filled_at": entered}}


class _LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_root = sfc.ROOT
        sfc.ROOT = Path(self._tmp.name)
        (sfc.ROOT / "logs" / "observation").mkdir(parents=True)
        self.criteria = {
            "status": "FROZEN", "window_start": FREEZE,
            "scope": {"ledger": "logs/observation/fills.jsonl",
                      "rec_id_tag": "-TEST-", "claude_approved_only": False,
                      "include_fallback": True},
        }

    def tearDown(self):
        sfc.ROOT = self._orig_root
        self._tmp.cleanup()

    def _write_ledger(self, events):
        p = sfc.ROOT / "logs" / "observation" / "fills.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in events) + "\n")


class ScalpWindowTest(_LedgerTestBase):
    def test_pre_freeze_utc_entry_excluded(self):
        """THE freeze-day bug: 14:25Z == 07:25 PDT, which is BEFORE the 12:18
        PDT freeze — string comparison wrongly included it."""
        self._write_ledger([
            _exit("20260709-TEST-1-SPY-debit_spread", "2026-07-09T14:25:00Z", 100.0),
            _exit("20260709-TEST-2-QQQ-debit_spread", "2026-07-09T20:30:00Z", 50.0),
        ])
        got, _ = sfc.qualifying(self.criteria)
        self.assertEqual([t["rec_id"] for t in got], ["20260709-TEST-2-QQQ-debit_spread"])

    def test_naive_timestamp_localized_not_dropped(self):
        """A tz-naive local stamp must be treated as America/Los_Angeles."""
        self._write_ledger([
            _exit("20260709-TEST-3-TLT-0dte_directional", "2026-07-09T11:00:00", 10.0),
            _exit("20260709-TEST-4-TLT-0dte_directional", "2026-07-09T13:00:00", 20.0),
        ])
        got, _ = sfc.qualifying(self.criteria)
        self.assertEqual([t["rec_id"] for t in got], ["20260709-TEST-4-TLT-0dte_directional"])

    def test_funnel_trades_never_qualify(self):
        self._write_ledger([
            _exit("2026-07-10-001", "2026-07-10T20:00:00Z", 500.0),
            _exit("20260710-TEST-5-SPY-debit_spread", "2026-07-10T20:00:00Z", 5.0),
        ])
        got, _ = sfc.qualifying(self.criteria)
        self.assertEqual(len(got), 1)
        self.assertIn("-TEST-", got[0]["rec_id"])

    def test_fallback_structure_split(self):
        """Fallback entries (mode EXECUTION-TEST-FALLBACK in observation logs)
        are bucketed 'fallback', not by their strategy suffix."""
        obs = sfc.ROOT / "logs" / "observation" / "2026-07-10.jsonl"
        obs.write_text(json.dumps({"paper_execution_tests": [
            {"rec_id": "20260710-TEST-6-SPY-debit_spread",
             "mode": "EXECUTION-TEST-FALLBACK"}]}) + "\n")
        self._write_ledger([
            _exit("20260710-TEST-6-SPY-debit_spread", "2026-07-10T20:00:00Z", 7.0),
            _exit("20260710-TEST-7-NVDA-0dte_directional", "2026-07-10T20:00:00Z", 9.0),
        ])
        rows, _ = sfc.qualifying(self.criteria)
        got = {t["rec_id"]: t["structure"] for t in rows}
        self.assertEqual(got["20260710-TEST-6-SPY-debit_spread"], "fallback")
        self.assertEqual(got["20260710-TEST-7-NVDA-0dte_directional"], "0dte_directional")


class ArtifactFilterTest(_LedgerTestBase):
    """2026-07-11 amendment: sandbox fills outside the contemporaneous quoted
    band (+ detection-lag tolerance) are artifacts, not evidence; entries with
    no fill telemetry are unverifiable and equally excluded (fail-closed)."""

    FILT = {"enabled": True, "tolerance_abs": 0.10,
            "tolerance_frac_of_mid": 0.25, "unverifiable_policy": "exclude"}

    def setUp(self):
        super().setUp()
        self.criteria["artifact_fill_filter"] = dict(self.FILT)

    @staticmethod
    def _fill(rec_id, fill_price, mid, spread=0.10):
        return {"event": "paper_fill", "rec_id": rec_id, "fill_price": fill_price,
                "mid_at_fill": mid, "spread_at_fill": spread}

    def test_fantasy_fill_excluded_and_reported(self):
        """The motivating case: NVDA 'filled' at 1.88 against a 4.075 mid."""
        self._write_ledger([
            self._fill("20260712-TEST-8-NVDA-0dte_directional", 1.88, 4.075, 0.15),
            _exit("20260712-TEST-8-NVDA-0dte_directional", "2026-07-12T20:00:00Z", 1060.0),
            self._fill("20260712-TEST-9-SPY-debit_spread", 2.90, 2.955, 0.05),
            _exit("20260712-TEST-9-SPY-debit_spread", "2026-07-12T20:00:00Z", 87.0),
        ])
        got, excluded = sfc.qualifying(self.criteria)
        self.assertEqual([t["rec_id"] for t in got], ["20260712-TEST-9-SPY-debit_spread"])
        self.assertEqual([r["rec_id"] for r in excluded["artifact"]],
                         ["20260712-TEST-8-NVDA-0dte_directional"])

    def test_detection_lag_drift_kept(self):
        """A fill 20% off mid is within the 25%-of-mid tolerance — quote drift
        between broker fill and detection, not an artifact."""
        self._write_ledger([
            self._fill("20260712-TEST-10-QQQ-debit_spread", 1.20, 1.00, 0.04),
            _exit("20260712-TEST-10-QQQ-debit_spread", "2026-07-12T20:00:00Z", 30.0),
        ])
        got, excluded = sfc.qualifying(self.criteria)
        self.assertEqual(len(got), 1)
        self.assertEqual(excluded["artifact"], [])

    def test_missing_telemetry_unverifiable(self):
        self._write_ledger([
            _exit("20260712-TEST-11-TLT-debit_spread", "2026-07-12T20:00:00Z", 40.0),
        ])
        got, excluded = sfc.qualifying(self.criteria)
        self.assertEqual(got, [])
        self.assertEqual([r["rec_id"] for r in excluded["unverifiable"]],
                         ["20260712-TEST-11-TLT-debit_spread"])

    def test_filter_absent_means_no_filtering(self):
        """Legacy criteria without the amendment block behave as before."""
        del self.criteria["artifact_fill_filter"]
        self._write_ledger([
            _exit("20260712-TEST-12-IWM-debit_spread", "2026-07-12T20:00:00Z", 5.0),
        ])
        got, excluded = sfc.qualifying(self.criteria)
        self.assertEqual(len(got), 1)
        # 2026-08-05: production declares FOUR exclusion buckets since f75b33a
        # (market-truth scoring added pending_enrichment / no_market_data). This
        # assertion still expected the old two-bucket shape — a stale test, not a
        # masked defect: the legacy-criteria path still excludes NOTHING, which is
        # what this test actually exists to prove.
        # SCALP-v2 (2026-09-12) adds the wrong_fill_mode bucket: entries filled
        # under another mode are reported, never blended.
        self.assertEqual(excluded, {"artifact": [], "unverifiable": [], "wrong_fill_mode": [], "outside_window": [],
                                    "pending_enrichment": [], "no_market_data": []})


if __name__ == "__main__":
    unittest.main()
