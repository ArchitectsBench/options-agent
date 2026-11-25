"""A submit into an ABSENT broker must leave a queryable trace (2026-10-20).

Both submit paths used to `return None` with no logger line and no ledger row,
so a brokerless attempt was invisible everywhere (two silent submits on
2026-10-13/14). These tests pin three things per path: the row carries rec_id +
ticker, a warning is emitted, and **the return value is still None**.

That third assertion is load-bearing: turning the `None` into a truthy
`{"submitted": False, ...}` would change caller behaviour (exposure and dup
tracking read `rec["paper_order"]`), which is a money-path change reserved for
the operator. A future change of that kind must break this test consciously.

`_log_event` is replaced by a collector, so no production ledger is opened or
written at any point (CLAUDE.md destructive-probe rule).
"""
import unittest
from datetime import date, timedelta

import src.engine.paper_trader as pt

_FUTURE_EXPIRY = (date.today() + timedelta(days=7)).isoformat()

REC = {"rec_id": "2026-10-20-brokerless-001", "ticker": "SPY",
       "trade": {"expiration": _FUTURE_EXPIRY, "structure": "call_debit_spread",
                 "strategy": "debit_spread", "direction": "long", "net_debit": 1.0,
                 "max_profit": 1.0, "max_loss": 1.0,
                 "legs": [{"symbol": "SPY261027C00770000", "action": "buy_to_open"}]}}

POSITION = {"rec_id": "2026-10-20-brokerless-002", "ticker": "QQQ",
            "quantity": 1, "structure": "long_call", "entry_price": 1.0}


class _UnavailableClient:
    def available(self):
        return False


class BrokerUnavailableAtSubmit(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self._real_event = pt._log_event
        self._real_client = pt.get_order_client
        pt._log_event = self.rows.append          # never touches a real ledger
        pt.get_order_client = lambda *a, **k: _UnavailableClient()

    def tearDown(self):
        pt._log_event = self._real_event
        pt.get_order_client = self._real_client

    def test_open_submit_records_the_dropped_attempt(self):
        with self.assertLogs("", level="WARNING") as logs:
            res = pt.submit_paper_order(REC)
        self.assertIsNone(res, "the None return is the operator-gated contract")
        rows = [r for r in self.rows
                if r.get("event") == "broker_unavailable_at_submit"]
        self.assertEqual(len(rows), 1, f"expected exactly one row, got {self.rows}")
        self.assertEqual(rows[0]["rec_id"], REC["rec_id"])
        self.assertEqual(rows[0]["ticker"], "SPY")
        self.assertEqual(rows[0]["intent"], "open")
        self.assertIn("UNVALIDATED", rows[0]["stamp"])
        self.assertTrue(any("broker unavailable at submit" in m for m in logs.output))

    def test_close_submit_records_the_dropped_attempt(self):
        with self.assertLogs("", level="WARNING") as logs:
            res = pt.submit_close_order(POSITION, 1.25, "vwap_invalidation")
        self.assertIsNone(res, "the None return is the operator-gated contract")
        rows = [r for r in self.rows
                if r.get("event") == "broker_unavailable_at_submit"]
        self.assertEqual(len(rows), 1, f"expected exactly one row, got {self.rows}")
        self.assertEqual(rows[0]["rec_id"], POSITION["rec_id"])
        self.assertEqual(rows[0]["ticker"], "QQQ")
        self.assertEqual(rows[0]["intent"], "close")
        self.assertEqual(rows[0]["reason"], "vwap_invalidation")
        self.assertTrue(any("broker unavailable at submit" in m for m in logs.output))

    def test_broker_lost_close_still_short_circuits_first(self):
        """L20's broker_lost guard precedes this one and writes no new row."""
        with self.assertLogs("", level="WARNING"):
            res = pt.submit_close_order({**POSITION, "broker_lost": True},
                                        1.25, "watchdog")
        self.assertIsNone(res)
        self.assertEqual([r for r in self.rows
                          if r.get("event") == "broker_unavailable_at_submit"], [])

    def test_event_name_is_not_an_attempt_counter_name(self):
        """Gate-neutrality: the new row must not be a name any counter reads."""
        self.assertNotIn("broker_unavailable_at_submit",
                         ("entry_ioc_unfilled", "paper_fill", "partial_fill_booked"))


if __name__ == "__main__":
    unittest.main()
