"""O-13 lane-isolation invariants (audited 2026-08-06).

Pins what IS isolated so it cannot regress, and DOCUMENTS the connections that
remain (each on the OBJECTIVES.md confirmation list because severing them is a
money-path change). A test that quietly asserted the broken behaviour as correct
would be worse than no test — the docstrings name the gap explicitly.
"""
import json
import tempfile
import unittest
from pathlib import Path

import src.engine.paper_trader as pt

FUNNEL = "2026-08-06-001"
SCALP = "20260806-TEST-1-SPY-0dte"


class _Book(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        b = Path(self.tmp.name)
        self._o = (pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG)
        pt.PENDING_FILE = b / "o.json"; pt.POSITIONS_FILE = b / "p.json"
        pt.FILLS_LOG = b / "f.jsonl"
        # risk.py holds its OWN FILLS_LOG reference — patch it too or the
        # breaker reads the real production log instead of the fixture.
        import src.engine.risk as _risk
        self._risk = _risk
        self._orisk = _risk.FILLS_LOG
        _risk.FILLS_LOG = pt.FILLS_LOG

    def tearDown(self):
        pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG = self._o
        self._risk.FILLS_LOG = self._orisk
        self.tmp.cleanup()


class LaneIdentity(unittest.TestCase):
    def test_lane_of_separates_the_two_streams(self):
        self.assertEqual(pt.lane_of(SCALP), "scalp")
        self.assertEqual(pt.lane_of(FUNNEL), "funnel")


class ExposureIsLaneScoped(_Book):
    """L-a — PASSING isolation: one lane's open position must not block the
    other lane from entering the same ticker."""

    def test_scalp_position_does_not_block_funnel_entry(self):
        pt._save(pt.POSITIONS_FILE, [{
            "rec_id": SCALP, "ticker": "SPY", "strategy": "0dte_directional",
            "direction": "long", "structure": "long_call", "quantity": 1,
            "legs": [{"symbol": "SPY260810C00770000", "action": "buy_to_open"}]}])
        if not pt._lane_isolation_on():
            self.skipTest("lane_isolation disabled in config")
        blocked = pt.has_open_exposure("SPY", "0dte_directional", "long", lane="funnel")
        self.assertIsNone(blocked, "a SCALP position must not block a FUNNEL entry")

    def test_same_lane_still_blocks_itself(self):
        pt._save(pt.POSITIONS_FILE, [{
            "rec_id": SCALP, "ticker": "SPY", "strategy": "0dte_directional",
            "direction": "long", "structure": "long_call", "quantity": 1,
            "legs": [{"symbol": "SPY260810C00770000", "action": "buy_to_open"}]}])
        if not pt._lane_isolation_on():
            self.skipTest("lane_isolation disabled in config")
        self.assertIsNotNone(
            pt.has_open_exposure("SPY", "0dte_directional", "long", lane="scalp"),
            "within a lane, duplicate exposure must still be refused")


def _exit(rec, pnl):
    import datetime as _dt
    pt._log_event({"ts": _dt.datetime.now().isoformat(timespec="seconds"),
                   "event": "paper_exit", "rec_id": rec, "ticker": "SPY",
                   "pnl_total": pnl})


class LbTwoTierBreaker(_Book):
    """L-b SEVERED into two tiers (2026-08-06): a lane trips its OWN limit and
    halts only itself; the ACCOUNT cap halts both. Account risk intentionally
    aggregates across lanes (restated O-13)."""

    RCFG = {"account_size": 50000, "max_daily_loss_pct": 3.0,
            "max_daily_loss_pct_per_lane": {"funnel": 2.0, "scalp": 2.0}}

    def test_scalp_trip_leaves_funnel_entries_live(self):
        from src.engine.risk import circuit_breaker
        _exit(SCALP, -1100.0)                       # > 2% of 50k, < 3% account
        self.assertTrue(circuit_breaker(self.RCFG, lane="scalp")["tripped"],
                        "SCALP must trip its own lane limit")
        self.assertFalse(circuit_breaker(self.RCFG, lane="funnel")["tripped"],
                         "FUNNEL must keep trading when SCALP trips")

    def test_account_cap_stops_both_lanes(self):
        from src.engine.risk import circuit_breaker
        _exit(SCALP, -900.0); _exit(FUNNEL, -700.0)  # -1600 total > 3% of 50k
        for lane in ("scalp", "funnel"):
            r = circuit_breaker(self.RCFG, lane=lane)
            self.assertTrue(r["tripped"], f"{lane} must be halted by the account cap")
            self.assertEqual(r["scope"], "account")

    def test_lane_pnl_is_partitioned(self):
        from src.engine.risk import realized_pnl_today, realized_pnl_today_by_lane
        _exit(SCALP, -100.0); _exit(FUNNEL, -50.0)
        self.assertEqual(realized_pnl_today_by_lane("scalp"), -100.0)
        self.assertEqual(realized_pnl_today_by_lane("funnel"), -50.0)
        self.assertEqual(realized_pnl_today(), -150.0, "account tier still aggregates")


class LdPerLaneConcurrency(_Book):
    """L-d SEVERED: funnel positions never consume SCALP's budget."""

    def test_funnel_positions_do_not_consume_scalp_budget(self):
        pt._save(pt.POSITIONS_FILE, [
            {"rec_id": FUNNEL, "ticker": "AAPL", "quantity": 1, "legs": []},
            {"rec_id": FUNNEL.replace("001", "002"), "ticker": "MSFT", "quantity": 1, "legs": []},
            {"rec_id": SCALP, "ticker": "SPY", "quantity": 1, "legs": []}])
        self.assertEqual(pt.open_paper_count(lane="scalp"), 1,
                         "SCALP budget must see ONLY scalp positions")
        self.assertEqual(pt.open_paper_count(lane="funnel"), 2,
                         "FUNNEL budget must see ONLY funnel positions")
        self.assertEqual(pt.open_paper_count(), 3, "pooled total still available")

    def test_each_lane_has_its_own_cap(self):
        self.assertEqual(pt.max_open_for_lane("scalp"), 7)
        self.assertEqual(pt.max_open_for_lane("funnel"), 3)


class LcAccountAggregatesSpanLanesByDesign(_Book):
    """L-c RESTATED (kept by decision): execution state is lane-isolated, but
    ACCOUNT-LEVEL RISK AGGREGATES intentionally span lanes as READ-ONLY
    aggregations — the $2000 per-underlying book cap and the account daily-loss
    cap. This pins the restated form so the intent cannot drift."""

    def test_underlying_cap_spans_both_lanes_by_design(self):
        pt._save(pt.POSITIONS_FILE, [
            {"rec_id": FUNNEL, "ticker": "SPY", "quantity": 1, "max_loss": 500,
             "legs": [{"symbol": "SPY260810C00770000", "action": "buy_to_open"}]},
            {"rec_id": SCALP, "ticker": "SPY", "quantity": 1, "max_loss": 500,
             "legs": [{"symbol": "SPY260810C00770000", "action": "buy_to_open"}]}])
        risk = pt.book_underlying_risk_usd("SPY")
        self.assertGreater(risk, 500,
                           "the underlying cap INTENTIONALLY aggregates both "
                           "lanes (restated O-13) — it is a read-only account "
                           "risk aggregate, not shared execution state")


if __name__ == "__main__":
    unittest.main()
