"""L-e STRUCTURAL isolation (2026-08-06) — storage, not convention.

Before: one shared book; isolation was the rec_id convention ('-TEST-'), so a
bug in lane derivation could silently cross the streams.
After : each lane owns data/lane/<lane>/{positions,orders}.json. A write in one
lane CANNOT land in the other's file — the guarantee is physical.
"""
import json
import tempfile
import unittest
from pathlib import Path

import src.engine.paper_trader as pt

FUNNEL = "2026-08-06-001"
SCALP = "20260806-TEST-1-SPY-0dte"


class _LaneDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = pt.LANE_DIR
        pt.LANE_DIR = Path(self.tmp.name)
        for lane in pt.LANES:
            (pt.LANE_DIR / lane).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        pt.LANE_DIR = self._orig
        self.tmp.cleanup()

    def _raw(self, kind, lane):
        f = pt.lane_book_path(kind, lane)
        return json.loads(f.read_text()) if f.exists() else []


class WritesArePhysicallyPartitioned(_LaneDir):
    def test_a_write_lands_only_in_its_own_lane_file(self):
        pt._save(pt.POSITIONS_FILE, [
            {"rec_id": FUNNEL, "ticker": "AAPL", "quantity": 1},
            {"rec_id": SCALP, "ticker": "SPY", "quantity": 2}])
        f_rows = self._raw("positions", "funnel")
        s_rows = self._raw("positions", "scalp")
        self.assertEqual([r["rec_id"] for r in f_rows], [FUNNEL])
        self.assertEqual([r["rec_id"] for r in s_rows], [SCALP])
        self.assertNotIn(SCALP, json.dumps(f_rows),
                         "a scalp row must NEVER appear in the funnel file")
        self.assertNotIn(FUNNEL, json.dumps(s_rows),
                         "a funnel row must NEVER appear in the scalp file")

    def test_pending_orders_partition_too(self):
        pt._save(pt.PENDING_FILE, [
            {"rec_id": FUNNEL, "intent": "open"},
            {"rec_id": SCALP, "intent": "open"}])
        self.assertEqual(len(self._raw("orders", "funnel")), 1)
        self.assertEqual(len(self._raw("orders", "scalp")), 1)


class LanePrivateReads(_LaneDir):
    def test_a_lane_load_never_sees_the_other_lanes_rows(self):
        pt._save(pt.POSITIONS_FILE, [
            {"rec_id": FUNNEL, "ticker": "AAPL", "quantity": 1},
            {"rec_id": SCALP, "ticker": "SPY", "quantity": 2}])
        f = pt.load_positions_for_lane("funnel")
        s = pt.load_positions_for_lane("scalp")
        self.assertEqual([r["rec_id"] for r in f], [FUNNEL])
        self.assertEqual([r["rec_id"] for r in s], [SCALP])

    def test_union_view_still_available_for_account_aggregates(self):
        pt._save(pt.POSITIONS_FILE, [
            {"rec_id": FUNNEL, "ticker": "AAPL", "quantity": 1},
            {"rec_id": SCALP, "ticker": "SPY", "quantity": 2}])
        self.assertEqual(len(pt.load_positions()), 2,
                         "account-level aggregates (restated O-13) still need "
                         "the union view")

    def test_per_lane_budget_counts_only_its_own_file(self):
        pt._save(pt.POSITIONS_FILE, [
            {"rec_id": FUNNEL, "ticker": "AAPL", "quantity": 1},
            {"rec_id": FUNNEL.replace("001", "002"), "ticker": "MSFT", "quantity": 1},
            {"rec_id": SCALP, "ticker": "SPY", "quantity": 1}])
        self.assertEqual(pt.open_paper_count(lane="scalp"), 1)
        self.assertEqual(pt.open_paper_count(lane="funnel"), 2)


class SharedBooksRetired(unittest.TestCase):
    def test_startup_check_fails_loudly_if_a_shared_book_is_repopulated(self):
        with tempfile.TemporaryDirectory() as td:
            legacy = Path(td) / "paper_positions.json"
            legacy.write_text(json.dumps([{"rec_id": FUNNEL}]))
            orig = pt.LEGACY_POSITIONS
            pt.LEGACY_POSITIONS = legacy
            try:
                with self.assertRaises(RuntimeError) as cm:
                    pt.assert_shared_books_retired()
                self.assertIn("SHARED BOOK STILL IN USE", str(cm.exception))
            finally:
                pt.LEGACY_POSITIONS = orig

    def test_empty_or_absent_shared_book_is_fine(self):
        pt.assert_shared_books_retired()   # archived/absent -> no raise


if __name__ == "__main__":
    unittest.main()
