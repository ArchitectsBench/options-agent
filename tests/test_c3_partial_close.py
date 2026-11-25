"""C-3 (2026-08-28, operator-approved) — a partial fill on a CLOSE order must
book the executed portion and keep managing the remainder.

Origin (IWM 20260827-SCALP-3652, twice): close 746 filled 1/7, close 863 filled
5/6. The R5 recheck treated `filled_quantity > 0` as a FULL fill, the cancel had
already killed the unfilled remainder, and the branch parked the position on
"poll_fills will book the exit" — which never books a cancelled-after-partial
close. 6 spreads ran unmanaged 23h through the 15:45 flatten. The loop breaker
never fired because the R5 branch returned without bumping the counter.

These tests drive the REAL escalation and polling paths against a broker stub
returning partial fills, per the C-3 verification spec:
  (a) the remainder is re-armed for a fresh close,
  (b) the escalation counter advances (loop breaker can fire),
  (c) the filled portion books a paper_exit at executed quantity.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.engine.paper_trader as pt

SYM = "IWM260831C00299000"
LEGS = [{"symbol": SYM, "action": "buy_to_open"}]
REC = "R-IWM-C3"


def _close_order(status, filled, requested=7, price=1.11, order_id=746):
    """Broker view of a close order (sell_to_close a long call)."""
    return {"id": order_id, "status": status, "filled_quantity": filled,
            "avg_fill_price": price, "side": "sell_to_close",
            "leg": [{"option_symbol": SYM, "side": "sell_to_close",
                     "avg_fill_price": price, "exec_quantity": filled}]}


class _Book(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        b = Path(self.tmp.name)
        self._orig = (pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG,
                      pt.ESCALATION_COUNTS_FILE)
        pt.PENDING_FILE = b / "orders.json"
        pt.POSITIONS_FILE = b / "positions.json"
        pt.FILLS_LOG = b / "fills.jsonl"
        pt.ESCALATION_COUNTS_FILE = b / "esc.json"

    def tearDown(self):
        (pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG,
         pt.ESCALATION_COUNTS_FILE) = self._orig
        self.tmp.cleanup()

    def _position(self, qty=7, closing=True):
        return {"rec_id": REC, "ticker": "IWM", "structure": "long call",
                "strategy": "debit_spread", "direction": "long", "legs": LEGS,
                "entry_price": 1.26, "quantity": qty,
                "mark_high": 1.36, "mark_low": 1.10,
                "entry_order_id": 740, "closing": closing,
                "max_profit": None, "max_loss": 1.26, "expiration": "2026-08-31"}

    def _pending_close(self, qty=7, order_id=746, age_s=600):
        return {"intent": "close", "order_id": order_id, "rec_id": REC,
                "ticker": "IWM", "quantity": qty, "structure": "long call",
                "entry_price": 1.26, "close_limit": 1.11,
                "close_reason": "invalidation (lost VWAP)",
                "submitted_at": (datetime.now() - timedelta(seconds=age_s)
                                 ).isoformat(timespec="seconds")}

    def _exits(self):
        if not pt.FILLS_LOG.exists():
            return []
        return [json.loads(l) for l in pt.FILLS_LOG.read_text().splitlines()
                if '"paper_exit"' in l]


class R5PartialDuringCancel(_Book):
    """escalate_stale_close: the wedge path itself."""

    def _run(self, filled, requested=7):
        pos = self._position(qty=requested)
        pt._save(pt.POSITIONS_FILE, [pos])
        pt._save(pt.PENDING_FILE, [self._pending_close(qty=requested)])
        client = MagicMock()
        client.available.return_value = True
        client.get_order.return_value = _close_order(
            "partially_filled", filled, requested)
        quotes = {SYM: {"bid": 1.10, "ask": 1.14}}
        with patch.object(pt, "get_order_client", return_value=client):
            out = pt.escalate_stale_close(pos, quotes, datetime(2026, 8, 28, 10, 0))
        return out, client

    def test_partial_books_exit_at_executed_quantity(self):
        """(c) 1 of 7 filled -> paper_exit for exactly 1 contract."""
        out, _ = self._run(filled=1)
        self.assertEqual(out["event"], "exit_partial_during_cancel")
        exits = self._exits()
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["quantity"], 1)
        self.assertTrue(exits[0]["partial_close"])
        self.assertEqual(exits[0]["remaining_quantity"], 6)
        # sell_to_close at 1.11 vs entry 1.26 -> -$15/contract
        self.assertAlmostEqual(exits[0]["pnl_total"], -15.0, places=2)

    def test_partial_rearms_remainder_and_drops_dead_order(self):
        """(a) the cancelled remainder must return to exit management."""
        self._run(filled=1)
        pos = pt.load_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["quantity"], 6,
                         "book must hold what the broker still holds")
        self.assertFalse(pos[0]["closing"],
                         "remainder must be re-armed for a fresh close")
        self.assertEqual(pt._load(pt.PENDING_FILE), [],
                         "the dead close order must not pend forever")

    def test_partial_bumps_escalation_counter(self):
        """(b) THE 23h wedge: the old branch never counted, so the loop
        breaker built for exactly this could not fire."""
        self._run(filled=1)
        counts = json.loads(pt.ESCALATION_COUNTS_FILE.read_text())
        self.assertEqual(counts[REC], 1)

    def test_repeated_partial_wedge_pages(self):
        """(b) past MAX_EXIT_ESCALATIONS the wedge must alarm, not loop."""
        for _ in range(pt.MAX_EXIT_ESCALATIONS):
            pt._bump_exit_escalations(REC)
        with patch("src.engine.notify.send") as send:
            self._run(filled=1)
            self.assertTrue(send.called, "repeated partial closes must page")

    def test_full_fill_during_cancel_still_defers_to_poll_fills(self):
        """A true full fill keeps the R5 contract: no resubmit, poll books."""
        out, _ = self._run(filled=7)
        self.assertEqual(out["event"], "exit_fill_during_cancel")
        self.assertEqual(self._exits(), [], "R5 full-fill path does not book")
        self.assertEqual(len(pt._load(pt.PENDING_FILE)), 1,
                         "pending record stays for poll_fills to book")

    def test_phantom_zero_fill_is_still_not_a_fill(self):
        """ITEM 2 regression: 'filled' with filled_quantity=0 escalates."""
        pos = self._position(qty=7)
        pt._save(pt.POSITIONS_FILE, [pos])
        pt._save(pt.PENDING_FILE, [self._pending_close(qty=7)])
        client = MagicMock(); client.available.return_value = True
        # first GET keeps the order alive; recheck reports the phantom
        client.get_order.side_effect = [
            _close_order("partially_filled", 0),
            _close_order("filled", 0),
        ]
        client.place_option_order.return_value = {"order": {"id": 900}}
        quotes = {SYM: {"bid": 1.10, "ask": 1.14}}
        with patch.object(pt, "get_order_client", return_value=client):
            out = pt.escalate_stale_close(pos, quotes, datetime(2026, 8, 28, 10, 0))
        self.assertNotIn(out["event"],
                         ("exit_fill_during_cancel", "exit_partial_during_cancel"),
                         "a status word with zero executed is not a fill")


class PollFillsPartialClose(_Book):
    """poll_fills: booking close executions the escalation path didn't."""

    def _poll(self, order, position=None, pending=None):
        if position is not None:
            pt._save(pt.POSITIONS_FILE, [position])
        else:
            pt._save(pt.POSITIONS_FILE, [])
        pt._save(pt.PENDING_FILE, [pending or self._pending_close()])
        client = MagicMock(); client.available.return_value = True
        client.get_order.return_value = order
        with patch.object(pt, "get_order_client", return_value=client):
            return pt.poll_fills()

    def test_live_partial_close_books_increment_and_stays_pending(self):
        self._poll(_close_order("partially_filled", 5),
                   position=self._position(qty=7),
                   pending=self._pending_close(qty=7))
        exits = self._exits()
        self.assertEqual([e["quantity"] for e in exits], [5])
        self.assertEqual(pt.load_positions()[0]["quantity"], 2)
        self.assertEqual(len(pt._load(pt.PENDING_FILE)), 1,
                         "live partial keeps pending — the chase continues")

    def test_partial_booking_is_idempotent_across_polls(self):
        """Second poll with the same broker view books NOTHING new (R4)."""
        pos = self._position(qty=7)
        self._poll(_close_order("partially_filled", 5), position=pos,
                   pending=self._pending_close(qty=7))
        # poll again with the book as the first poll left it
        client = MagicMock(); client.available.return_value = True
        client.get_order.return_value = _close_order("partially_filled", 5)
        with patch.object(pt, "get_order_client", return_value=client):
            pt.poll_fills()
        self.assertEqual(len(self._exits()), 1, "no double booking")
        self.assertEqual(pt.load_positions()[0]["quantity"], 2)

    def test_dead_partial_record_is_dropped_once_position_gone(self):
        """Cancelled-after-partial reads 'partially_filled' forever; once
        everything executed is booked and the position is gone, the record
        must leave pending instead of alerting forever."""
        self._poll(_close_order("partially_filled", 7), position=None)
        self.assertEqual(pt._load(pt.PENDING_FILE), [])

    def test_cancelled_after_partial_books_then_rearms(self):
        """If the adapter ever reports the honest status, the canceled branch
        must book the executed portion before re-arming."""
        self._poll(_close_order("canceled", 2), position=self._position(qty=7),
                   pending=self._pending_close(qty=7))
        exits = self._exits()
        self.assertEqual([e["quantity"] for e in exits], [2])
        pos = pt.load_positions()
        self.assertEqual(pos[0]["quantity"], 5)
        self.assertFalse(pos[0]["closing"])
        self.assertEqual(pt._load(pt.PENDING_FILE), [])

    def test_filled_close_books_only_the_unbooked_tail(self):
        """Partial booked 5 earlier (position already reduced to 2); the order
        then completes filled=7 -> book 2 more, not 7 again."""
        self._poll(_close_order("filled", 7), position=self._position(qty=2),
                   pending=self._pending_close(qty=7))
        exits = self._exits()
        self.assertEqual([e["quantity"] for e in exits], [2])
        self.assertEqual(pt.load_positions(), [], "fully closed")
        self.assertEqual(pt._load(pt.PENDING_FILE), [])

    def test_plain_full_close_unchanged(self):
        """The ordinary path books the full quantity exactly once."""
        self._poll(_close_order("filled", 7), position=self._position(qty=7),
                   pending=self._pending_close(qty=7))
        exits = self._exits()
        self.assertEqual([e["quantity"] for e in exits], [7])
        self.assertFalse(exits[0]["partial_close"])
        self.assertEqual(pt.load_positions(), [])
        # full close resets the escalation counter (documented, now wired)
        pt._bump_exit_escalations(REC)
        self.assertEqual(json.loads(pt.ESCALATION_COUNTS_FILE.read_text())[REC], 1)


if __name__ == "__main__":
    unittest.main()
