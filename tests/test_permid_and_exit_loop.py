"""Part B' items 1+2 — permId identity and the exit-loop misread.

Both regressions are taken from the REAL broker records observed on
2026-08-05, when the engine looped `exit_fill_during_cancel` 13 times on GOOGL
while the position stayed open:

    permId=2033402227 SELL qty=4.0 status=Cancelled filled=0.0
    permId=2033402226 BUY  qty=0.0 status=Filled    filled=0.0   <- phantom
    id=0 ...                                                     <- collides

These tests fail on the old code and pass on the new.
"""
import unittest
from unittest.mock import patch

from src.data.ibkr_orders import IbkrOrderClient
from tests.mock_ib import MockIB, MockContract, MockOrderStatus


def _rec(order_id, perm_id, status, filled, remaining=0.0, ref="UNVALIDATED-PAPER-ONLY"):
    class _T:
        contract = MockContract(secType="OPT", conId=1, localSymbol="GOOGL 260805C00380000")
        order = type("O", (), {"orderId": order_id, "permId": perm_id, "orderRef": ref})()
        orderStatus = MockOrderStatus(status=status, filled=filled, remaining=remaining)
        fills, log = [], []
    return _T()


class PermIdIdentityTest(unittest.TestCase):
    def setUp(self):
        MockIB._BROKER_BOOK.clear(); MockIB.fail_order_query = False

    def tearDown(self):
        MockIB._BROKER_BOOK.clear()

    def test_two_completed_orders_do_not_collide_on_zero(self):
        """THE 08-05 BUG: reqCompletedOrders returns orderId=0 for every
        completed record; keying by it collapsed them into one phantom order."""
        MockIB._BROKER_BOOK[1] = _rec(0, 2033402227, "Cancelled", 0.0)
        MockIB._BROKER_BOOK[2] = _rec(0, 2033402226, "Filled", 3.0)

        c = IbkrOrderClient(ib=MockIB())
        orders = c.get_orders()
        self.assertIsNotNone(orders)
        ids = [o["id"] for o in orders]
        self.assertEqual(len(ids), len(set(ids)), f"ids collided: {ids}")
        self.assertEqual(set(ids), {2033402227, 2033402226},
                         "completed orders must be keyed by permId, not orderId=0")

    def test_unidentifiable_record_is_dropped_not_keyed_zero(self):
        """No orderId AND no permId -> not evidence of anything."""
        MockIB._BROKER_BOOK[3] = _rec(0, 0, "Filled", 0.0)
        c = IbkrOrderClient(ib=MockIB())
        orders = c.get_orders()
        self.assertEqual([o["id"] for o in orders], [],
                         "an unidentifiable record must be dropped entirely")

    def test_live_order_keyed_by_permId(self):
        """CONTRACT CORRECTED 2026-08-06 (was test_live_order_still_keyed_by_orderId):
        permId is the identity even for LIVE orders. orderId is only a
        per-session handle and live orders DO collide on it — both B5 drill
        orders were orderId=4."""
        MockIB._BROKER_BOOK[4] = _rec(77, 999, "Submitted", 0.0, remaining=4.0)
        c = IbkrOrderClient(ib=MockIB())
        self.assertIn(999, [o["id"] for o in (c.get_orders() or [])])


class PhantomFilledStatusTest(unittest.TestCase):
    def setUp(self):
        MockIB._BROKER_BOOK.clear()

    def tearDown(self):
        MockIB._BROKER_BOOK.clear()

    def test_status_Filled_with_zero_quantity_is_not_a_fill(self):
        """A status word is not an execution — quantity is."""
        MockIB._BROKER_BOOK[5] = _rec(0, 2033402226, "Filled", 0.0)
        c = IbkrOrderClient(ib=MockIB())
        o = [x for x in (c.get_orders() or []) if x["id"] == 2033402226][0]
        self.assertNotEqual(o["status"], "filled",
                            "'Filled' with filled=0.0 executed NOTHING")
        self.assertEqual(o["filled_quantity"], 0.0)

    def test_real_fill_still_reads_as_filled(self):
        MockIB._BROKER_BOOK[6] = _rec(88, 111, "Filled", 4.0)
        c = IbkrOrderClient(ib=MockIB())
        o = [x for x in (c.get_orders() or []) if x["id"] == 111][0]   # permId
        self.assertEqual(o["status"], "filled")
        self.assertEqual(o["filled_quantity"], 4.0)

    def test_cancelled_with_zero_filled_is_canceled(self):
        MockIB._BROKER_BOOK[7] = _rec(0, 2033402227, "Cancelled", 0.0)
        c = IbkrOrderClient(ib=MockIB())
        o = [x for x in (c.get_orders() or []) if x["id"] == 2033402227][0]
        self.assertEqual(o["status"], "canceled")


class ExitLoopBreakerTest(unittest.TestCase):
    """No infinite loops in the money path, ever."""

    def test_breaker_halts_after_max_escalations(self):
        import tempfile
        from pathlib import Path
        from src.engine import paper_trader as pt

        with tempfile.TemporaryDirectory() as td:
            orig = pt.ESCALATION_COUNTS_FILE
            pt.ESCALATION_COUNTS_FILE = Path(td) / "esc.json"
            try:
                counts = [pt._bump_exit_escalations("REC-1") for _ in range(5)]
                self.assertEqual(counts, [1, 2, 3, 4, 5])
                self.assertLessEqual(pt.MAX_EXIT_ESCALATIONS, 3)
                # beyond the max the caller must halt (breaker condition true)
                self.assertTrue(counts[-1] > pt.MAX_EXIT_ESCALATIONS)
                pt.clear_exit_escalations("REC-1")
                self.assertEqual(pt._bump_exit_escalations("REC-1"), 1,
                                 "a successful close must reset the counter")
            finally:
                pt.ESCALATION_COUNTS_FILE = orig

    def test_counter_survives_restart(self):
        """A restart must not silently reset the counter and resume looping."""
        import tempfile
        from pathlib import Path
        from src.engine import paper_trader as pt

        with tempfile.TemporaryDirectory() as td:
            orig = pt.ESCALATION_COUNTS_FILE
            pt.ESCALATION_COUNTS_FILE = Path(td) / "esc.json"
            try:
                pt._bump_exit_escalations("REC-2")
                pt._bump_exit_escalations("REC-2")
                # simulate restart: re-read from disk
                self.assertEqual(pt._bump_exit_escalations("REC-2"), 3)
            finally:
                pt.ESCALATION_COUNTS_FILE = orig


if __name__ == "__main__":
    unittest.main()


class LiveOrderIdCollisionTest(unittest.TestCase):
    """2026-08-06: LIVE orders can share an orderId too. Both B5 drill orders
    were orderId=4 with different permIds; the adapter reported 1 open order
    while the gateway held 2. permId must win even when orderId > 0."""

    def setUp(self):
        MockIB._BROKER_BOOK.clear(); MockIB.fail_order_query = False

    def tearDown(self):
        MockIB._BROKER_BOOK.clear()

    def test_two_live_orders_sharing_orderId_do_not_collapse(self):
        MockIB._BROKER_BOOK[1] = _rec(4, 1838451540, "Submitted", 0.0, remaining=1.0)
        MockIB._BROKER_BOOK[2] = _rec(4, 1838451541, "Submitted", 0.0, remaining=1.0)
        c = IbkrOrderClient(ib=MockIB())
        ids = [o["id"] for o in (c.get_orders() or [])]
        self.assertEqual(len(ids), 2, f"two live orders collapsed into {ids}")
        self.assertEqual(set(ids), {1838451540, 1838451541})
