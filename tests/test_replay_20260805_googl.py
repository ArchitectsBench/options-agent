"""ITEM 5 — replay of the full 2026-08-05 GOOGL sequence as a regression test.

Reconstructed from logs/observation/fills.jsonl and the live broker records:

  07:30:09  partial_fill_detected  order 23 (requested 5, broker filled 1)
  ...       remainder keeps filling                     -> broker holds 4
  08:00:16  paper_fill order 23 qty=5 @1.89   <- BOOK RECORDED 5 (intent, not truth)
  08:05:09  paper_exit order 51 qty=5         <- exit sized off the PHANTOM 5th
            broker: permId 2033402227 SELL 4 status=Cancelled filled=0.0
                    permId 2033402226 BUY  0 status=Filled    filled=0.0
            -> escalate_stale_close read the STATUS WORD and concluded
               "exit_fill_during_cancel" 13 consecutive times while the
               position stayed open
  08:15:13  paper_fill order 65 qty=5         <- RE-ENTRY STACKED on the residue

Each assertion below fails on pre-batch code and passes on the batch.
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.engine.paper_trader as pt
from src.data.ibkr_orders import IbkrOrderClient
from tests.mock_ib import MockIB, MockContract, MockOrderStatus

SYM = "GOOGL260805C00380000"
LEGS = [{"symbol": SYM, "action": "buy_to_open"}]


class Replay20260805(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        b = Path(self.tmp.name)
        self._orig = (pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG)
        pt.PENDING_FILE = b / "orders.json"
        pt.POSITIONS_FILE = b / "positions.json"
        pt.FILLS_LOG = b / "fills.jsonl"
        MockIB._BROKER_BOOK.clear()

    def tearDown(self):
        pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG = self._orig
        self.tmp.cleanup()
        MockIB._BROKER_BOOK.clear()

    def _pending(self):
        return {"intent": "open", "order_id": 23, "rec_id": "20260805-TEST-2538-GOOGL",
                "ticker": "GOOGL", "structure": "long call", "strategy": "0dte",
                "direction": "long", "legs": LEGS, "rec_price": 1.90, "quantity": 5,
                "max_profit": 1.0, "max_loss": 1.9, "expiration": "2026-08-05",
                "submitted_at": datetime.now().isoformat(timespec="seconds")}

    @staticmethod
    def _order(status, filled):
        return {"id": 23, "status": status, "filled_quantity": filled,
                "avg_fill_price": 1.89,
                "leg": [{"option_symbol": SYM, "side": "buy_to_open",
                         "avg_fill_price": 1.89, "exec_quantity": filled}]}

    def _poll(self, order):
        c = MagicMock(); c.available.return_value = True
        c.get_order.return_value = order
        with patch.object(pt, "PaperOrderClient", return_value=c):
            return pt.poll_fills()

    # ── the sequence ────────────────────────────────────────────────────────
    def test_step1_partial_1_of_5_is_booked_not_stranded(self):
        pt._save(pt.PENDING_FILE, [self._pending()])
        self._poll(self._order("partially_filled", 1))
        pos = pt.load_positions()
        self.assertEqual(len(pos), 1, "the partially-filled contract was STRANDED "
                                      "unmanaged on 08-05")
        self.assertEqual(pos[0]["quantity"], 1)

    def test_step2_book_records_4_not_the_requested_5(self):
        """THE qty-5 BUG. Pre-batch this booked 5."""
        pt._save(pt.PENDING_FILE, [self._pending()])
        self._poll(self._order("partially_filled", 1))
        self._poll(self._order("filled", 4))          # broker truth: 4, not 5
        pos = pt.load_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["quantity"], 4,
                         "book must equal broker truth, never the request")

    def test_step3_phantom_filled_record_is_not_a_fill(self):
        """The exit misfire: status='Filled' with filled=0.0 executed NOTHING."""
        class _T:
            contract = MockContract(secType="OPT", conId=1, localSymbol=SYM)
            order = type("O", (), {"orderId": 0, "permId": 2033402226,
                                   "orderRef": "UNVALIDATED-PAPER-ONLY"})()
            orderStatus = MockOrderStatus(status="Filled", filled=0.0, remaining=0.0)
            fills, log = [], []
        MockIB._BROKER_BOOK[1] = _T()
        c = IbkrOrderClient(ib=MockIB())
        o = [x for x in (c.get_orders() or []) if x["id"] == 2033402226][0]
        self.assertNotEqual(o["status"], "filled",
                            "this misread drove 13 exit_fill_during_cancel loops")

    def test_step4_cancelled_with_zero_filled_is_not_a_fill(self):
        class _T:
            contract = MockContract(secType="OPT", conId=1, localSymbol=SYM)
            order = type("O", (), {"orderId": 0, "permId": 2033402227,
                                   "orderRef": "UNVALIDATED-PAPER-ONLY"})()
            orderStatus = MockOrderStatus(status="Cancelled", filled=0.0, remaining=0.0)
            fills, log = [], []
        MockIB._BROKER_BOOK[2] = _T()
        c = IbkrOrderClient(ib=MockIB())
        o = [x for x in (c.get_orders() or []) if x["id"] == 2033402227][0]
        self.assertEqual(o["status"], "canceled")

    def test_step5_book_drop_with_broker_residue_is_alarmed(self):
        """Book emptied while the broker still held 4 -> must alarm (detect-only)."""
        pt._save(pt.POSITIONS_FILE, [])
        c = MagicMock(); c.available.return_value = True
        c.get_positions.return_value = [{"symbol": SYM, "quantity": 4}]
        alarms = pt.audit_book_vs_broker(c)
        self.assertEqual([a["kind"] for a in alarms], ["broker_position_not_in_book"])
        c.cancel_order.assert_not_called()      # detect only

    def test_step6_reentry_stacking_is_visible_as_quantity_mismatch(self):
        """Re-entry booked 5 on top of a 4-contract residue -> mismatch alarm."""
        pt._save(pt.POSITIONS_FILE, [{
            "rec_id": "20260805-TEST-2547-GOOGL", "ticker": "GOOGL", "quantity": 5,
            "legs": LEGS}])
        c = MagicMock(); c.available.return_value = True
        c.get_positions.return_value = [{"symbol": SYM, "quantity": 4}]
        alarms = pt.audit_book_vs_broker(c)
        self.assertEqual([a["kind"] for a in alarms], ["quantity_mismatch"])

    def test_step7_exit_loop_is_broken(self):
        """13 retries must be impossible: the breaker halts at 3."""
        esc = Path(self.tmp.name) / "esc.json"
        orig = pt.ESCALATION_COUNTS_FILE
        pt.ESCALATION_COUNTS_FILE = esc
        try:
            counts = [pt._bump_exit_escalations("20260805-TEST-2538-GOOGL")
                      for _ in range(13)]
            over = [n for n in counts if n > pt.MAX_EXIT_ESCALATIONS]
            self.assertTrue(over, "breaker must trip well before 13 retries")
            self.assertEqual(pt.MAX_EXIT_ESCALATIONS, 3)
        finally:
            pt.ESCALATION_COUNTS_FILE = orig


if __name__ == "__main__":
    unittest.main()
