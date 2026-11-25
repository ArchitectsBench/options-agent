"""Cockpit API feed smoke tests (2026-07-04).

The feed builder runs against the REAL repo state (read-only), so these
assert shape and honesty invariants rather than specific values.

Run: .venv/bin/python -m unittest tests.test_api_feed -v
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.feed import build_feed  # noqa: E402

REQUIRED_KEYS = {
    "simulated", "agent", "mode", "marketOpen", "cycleSeconds", "regime",
    "claude", "forwardClock", "today", "positions", "noPositionsReason",
    "cycleId", "recs", "noRecsReason", "confirmSeconds", "logs", "dataAge",
    "killEngaged", "perf", "health", "reports", "build",
}


class FeedSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed = build_feed()

    def test_contract_keys_present(self):
        self.assertTrue(REQUIRED_KEYS.issubset(self.feed.keys()),
                        REQUIRED_KEYS - set(self.feed.keys()))

    def test_real_feed_is_not_simulated(self):
        self.assertFalse(self.feed["simulated"])

    def test_mode_is_observation(self):
        # live mode is impossible today; the feed must reflect that
        self.assertEqual(self.feed["mode"], "observation")

    def test_forward_clock_shape(self):
        fc = self.feed["forwardClock"]
        self.assertEqual(len(fc["rings"]), 2)
        self.assertLessEqual(fc["rings"][0]["done"], fc["rings"][0]["goal"])
        self.assertIn(fc["gate"]["label"], ("LOCKED", "READY"))

    def test_today_funnel_blended_split(self):
        t = self.feed["today"]
        # funnel is a subset of blended — never larger
        self.assertLessEqual(t["funnel"]["roundTrips"], t["blended"]["roundTrips"])

    def test_positions_match_book(self):
        import json
        # L-e (2026-08-06): the shared book is RETIRED; lanes own their files.
        book = []
        for _lane in ("funnel", "scalp"):
            _f = ROOT / "data" / "lane" / _lane / "positions.json"
            if _f.exists():
                book.extend(json.loads(_f.read_text() or "[]"))
        self.assertEqual(len(self.feed["positions"]), len(book))

    def test_json_serializable(self):
        import json
        json.dumps(self.feed)

    def test_feed_build_stamp(self):
        """O-24: the payload must say what code THIS process is running.

        The cockpit served Jul-24 code for 17 days across a lane repoint and a
        rec_id rename, reporting confident wrong numbers with nothing to reveal
        it. Staleness must be readable, not inferred from bad output.
        """
        b = self.feed["build"]
        self.assertTrue(b["sha"], "build sha must not be empty")
        # startedAt is THIS process's start; parseable or the stamp is useless
        from datetime import datetime
        datetime.fromisoformat(b["startedAt"])

    def test_positions_read_lane_books_not_retired_shared_book(self):
        """REGRESSION (2026-08-10): positions_section() read the RETIRED shared
        book (data/paper_positions.json). It is now empty, so the cockpit showed
        an empty panel against a live book — and test_positions_match_book above
        compared 0 == 0 outside market hours and passed vacuously for four days.

        This asserts with SYNTHETIC positions so it has teeth whether or not the
        real book happens to be occupied when the suite runs.
        """
        from unittest.mock import patch
        from src.api import feed as feed_mod
        fake = [{"rec_id": "20260810-SCALP-1-XYZ-0dte_directional", "ticker": "XYZ",
                 "quantity": 3, "entry_price": 1.0, "last_value": 1.5,
                 "mark_high": 1.5, "mark_low": 1.0, "direction": "long",
                 "structure": "0DTE long call", "legs": [], "max_loss": 1.0,
                 "opened_at": "2026-08-10T07:00:00"}]
        with patch("src.engine.paper_trader.load_positions", return_value=fake):
            rows, unreal = feed_mod.positions_section({})
        self.assertEqual(len(rows), 1, "positions must come from the lane books")
        self.assertEqual(rows[0]["ticker"], "XYZ")
        # 0.50 x 100 x 3 contracts
        self.assertAlmostEqual(unreal, 150.0, places=2)

    def test_scalp_is_reported_as_its_own_stream(self):
        """Standing rule: SCALP is the formal frozen-gate candidate and must be
        reported as its own stream, not appear only folded into 'blended'."""
        t = self.feed["today"]
        self.assertIn("scalp", t)
        self.assertLessEqual(t["scalp"]["roundTrips"], t["blended"]["roundTrips"])
        # the two lanes must partition blended — no trade counted twice or lost
        self.assertEqual(t["funnel"]["roundTrips"] + t["scalp"]["roundTrips"],
                         t["blended"]["roundTrips"])


if __name__ == "__main__":
    unittest.main()
