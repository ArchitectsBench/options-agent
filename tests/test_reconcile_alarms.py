"""B' items 4 + 7 — detect-only reconcile alarms, and foreign-ignore as a test.

Item 7 replaces an artifact that could not be captured live: the "foreign order
ignored" log line required a running engine, and the engine was stopped before
the out-of-band closes were placed on 2026-08-05. A test is stronger anyway —
repeatable, and it asserts behaviour rather than a log string.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.engine.paper_trader as pt

SYM = "GOOGL260805C00380000"


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

    def _client(self, broker_positions):
        c = MagicMock(); c.available.return_value = True
        c.get_positions.return_value = broker_positions
        return c

    def _book(self, qty):
        pt._save(pt.POSITIONS_FILE, [{
            "rec_id": "R1", "ticker": "GOOGL", "quantity": qty,
            "legs": [{"symbol": SYM, "action": "buy_to_open"}]}])


class Item4ReconcileAlarms(_Book):
    def test_broker_position_with_no_book_entry_alarms(self):
        """(a) the 08-05 unmanaged orphan."""
        pt._save(pt.POSITIONS_FILE, [])
        alarms = pt.audit_book_vs_broker(self._client([{"symbol": SYM, "quantity": 4}]))
        self.assertEqual([a["kind"] for a in alarms], ["broker_position_not_in_book"])

    def test_book_position_with_no_broker_position_alarms(self):
        """(b) the phantom the engine tried to exit forever."""
        self._book(5)
        alarms = pt.audit_book_vs_broker(self._client([]))
        self.assertEqual([a["kind"] for a in alarms], ["book_position_not_at_broker"])

    def test_quantity_mismatch_alarms(self):
        """(c) THE qty-5 BUG: book 5, broker 4."""
        self._book(5)
        alarms = pt.audit_book_vs_broker(self._client([{"symbol": SYM, "quantity": 4}]))
        self.assertEqual([a["kind"] for a in alarms], ["quantity_mismatch"])
        self.assertEqual(alarms[0]["book_quantity"], 5)
        self.assertEqual(alarms[0]["broker_quantity"], 4)

    def test_clean_book_is_silent(self):
        self._book(4)
        self.assertEqual(pt.audit_book_vs_broker(
            self._client([{"symbol": SYM, "quantity": 4}])), [])

    def test_detect_only_never_closes(self):
        self._book(5)
        c = self._client([{"symbol": SYM, "quantity": 4}])
        pt.audit_book_vs_broker(c)
        c.cancel_order.assert_not_called()
        c.place_option_order.assert_not_called()
        c.place_spread_order.assert_not_called()

    def test_failed_read_is_not_treated_as_empty(self):
        """R2: None means unknown — must not alarm every position as missing."""
        self._book(4)
        c = MagicMock(); c.available.return_value = True
        c.get_positions.return_value = None
        self.assertEqual(pt.audit_book_vs_broker(c), [])


class Item7ForeignIgnore(_Book):
    def test_untagged_order_is_ignored_never_cancelled(self):
        """The out-of-band closes placed on 08-05 carried NO orderRef. Reconcile
        must log them once and leave them alone."""
        cancelled = []

        class _C:
            def available(self): return True
            def get_orders(self):
                return [{"id": 900, "status": "open", "symbol": "GOOGL", "tag": ""}]
            def get_positions(self): return []
            def cancel_order(self, oid): cancelled.append(oid)

        with patch.object(pt, "get_order_client", lambda: _C()), \
             patch.object(pt, "resolve_intents", lambda c: []), \
             patch.object(pt, "_load", lambda p: []):
            events = pt.reconcile_with_broker()

        self.assertEqual(cancelled, [], "a foreign order must never be cancelled")
        self.assertEqual([e for e in events
                          if e.get("event") == "reconciled_orphan_order"], [])


if __name__ == "__main__":
    unittest.main()
