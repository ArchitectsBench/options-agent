"""B' items 3 + 6 — partial-fill booking and fresh-truth sizing.

Origin (2026-08-05 GOOGL): order requested 5, broker filled 4, book recorded 5
because the position quantity was copied from the INTENT. The phantom 5th drove
an exit that could not fill; the position was dropped from the book while
contracts remained live at the broker; a later entry stacked on the residue.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.engine.paper_trader as pt

LEGS = [{"symbol": "GOOGL260805C00380000", "action": "buy_to_open"}]


class _Book(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        b = Path(self.tmp.name)
        self._orig = (pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG)
        pt.PENDING_FILE = b / "orders.json"
        pt.POSITIONS_FILE = b / "positions.json"
        pt.FILLS_LOG = b / "fills.jsonl"

    def tearDown(self):
        pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG = self._orig
        self.tmp.cleanup()

    def _pending(self, qty=5, submitted="2026-08-05T07:30:00"):
        return {"intent": "open", "order_id": 23, "rec_id": "R-GOOGL",
                "ticker": "GOOGL", "structure": "long call", "strategy": "0dte",
                "direction": "long", "legs": LEGS, "rec_price": 1.90,
                "quantity": qty, "max_profit": 1.0, "max_loss": 1.9,
                "expiration": "2026-08-05", "submitted_at": submitted}

    def _client(self, order):
        c = MagicMock(); c.available.return_value = True
        c.get_order.return_value = order
        return c

    @staticmethod
    def _order(status, filled, price=1.89):
        return {"id": 23, "status": status, "filled_quantity": filled,
                "avg_fill_price": price,
                "leg": [{"option_symbol": LEGS[0]["symbol"], "side": "buy_to_open",
                         "avg_fill_price": price, "exec_quantity": filled}]}


class Item6FreshTruthSizing(_Book):
    def test_position_quantity_is_broker_truth_not_intent(self):
        """THE qty-5 BUG: requested 5, broker filled 4 -> book MUST say 4."""
        from datetime import datetime
        pt._save(pt.PENDING_FILE, [self._pending(
            qty=5, submitted=datetime.now().isoformat(timespec="seconds"))])
        with patch.object(pt, "PaperOrderClient",
                          return_value=self._client(self._order("filled", 4))):
            pt.poll_fills()
        pos = pt.load_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["quantity"], 4,
                         "position must be what the BROKER filled, not the request")

    def test_executed_quantity_never_invents_a_number(self):
        """No broker truth -> None, so callers cannot silently fabricate size."""
        self.assertIsNone(pt.executed_quantity({"status": "partially_filled"},
                                               {"legs": []}, None))

    def test_executed_quantity_prefers_filled_quantity(self):
        self.assertEqual(pt.executed_quantity({"filled_quantity": 3},
                                              {"legs": [{"quantity": 9}]}, 5), 3)

    def test_spread_is_only_as_filled_as_its_least_filled_leg(self):
        d = {"legs": [{"quantity": 4}, {"quantity": 2}]}
        self.assertEqual(pt.executed_quantity({}, d, 5), 2)


class Item3PartialBooking(_Book):
    def test_partial_is_booked_and_managed_with_real_quantity(self):
        pt._save(pt.PENDING_FILE, [self._pending(qty=5)])
        with patch.object(pt, "PaperOrderClient",
                          return_value=self._client(self._order("partially_filled", 1))):
            pt.poll_fills()
        pos = pt.load_positions()
        self.assertEqual(len(pos), 1, "a partial fill IS a live position")
        self.assertEqual(pos[0]["quantity"], 1)
        self.assertEqual(pos[0]["entry_price"], 1.89,
                         "stop/target must come from the ACTUAL average fill")
        self.assertTrue(pos[0]["partial"])
        self.assertFalse(pos[0]["closing"], "must enter normal management")

    def test_partial_then_more_partial_tracks_broker(self):
        from datetime import datetime
        # fresh submit: the remainder must still be inside the 60s window,
        # otherwise the timeout cancels it before the second fill can arrive
        pt._save(pt.PENDING_FILE, [self._pending(
            qty=5, submitted=datetime.now().isoformat(timespec="seconds"))])
        with patch.object(pt, "PaperOrderClient",
                          return_value=self._client(self._order("partially_filled", 1))):
            pt.poll_fills()
        with patch.object(pt, "PaperOrderClient",
                          return_value=self._client(self._order("partially_filled", 4))):
            pt.poll_fills()
        self.assertEqual(pt.load_positions()[0]["quantity"], 4)

    def test_partial_then_complete_does_not_duplicate(self):
        from datetime import datetime
        pt._save(pt.PENDING_FILE, [self._pending(
            qty=5, submitted=datetime.now().isoformat(timespec="seconds"))])
        with patch.object(pt, "PaperOrderClient",
                          return_value=self._client(self._order("partially_filled", 2))):
            pt.poll_fills()
        with patch.object(pt, "PaperOrderClient",
                          return_value=self._client(self._order("filled", 5))):
            pt.poll_fills()
        pos = pt.load_positions()
        self.assertEqual(len(pos), 1, "R4 rec_id dedup must prevent a second position")

    def test_remainder_cancelled_after_timeout(self):
        old = self._pending(qty=5, submitted="2026-08-05T00:00:00")   # long past 60s
        pt._save(pt.PENDING_FILE, [old])
        client = self._client(self._order("partially_filled", 1))
        with patch.object(pt, "PaperOrderClient", return_value=client):
            events = pt.poll_fills()
        client.cancel_order.assert_called_once_with(23)
        self.assertIn("partial_remainder_cancelled", [e["event"] for e in events])
        self.assertEqual(pt._load(pt.PENDING_FILE), [],
                         "cancelled remainder must leave pending")

    def test_remainder_kept_before_timeout(self):
        from datetime import datetime
        fresh = self._pending(qty=5, submitted=datetime.now().isoformat(timespec="seconds"))
        pt._save(pt.PENDING_FILE, [fresh])
        client = self._client(self._order("partially_filled", 1))
        with patch.object(pt, "PaperOrderClient", return_value=client):
            pt.poll_fills()
        client.cancel_order.assert_not_called()
        self.assertEqual(len(pt._load(pt.PENDING_FILE)), 1, "remainder still working")

    def test_config_key_default_is_60(self):
        self.assertEqual(pt.partial_fill_remainder_cancel_seconds(), 60)


if __name__ == "__main__":
    unittest.main()
