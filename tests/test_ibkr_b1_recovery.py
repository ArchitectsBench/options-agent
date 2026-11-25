"""B1/B2 acceptance — get_orders/get_order read BROKER truth, not a local cache.

The defect: `_trades` is populated only by this process's own placeOrder calls,
so a fresh instance (after a crash/restart) reported [] no matter what IBKR
actually held. `resolve_intents` reads that as a genuine empty and DROPS every
unbound intent while live orders sit at the broker; `poll_fills` cannot see a
post-restart fill. `get_orders()` also had no way to signal a failed read.

Acceptance path (as specified): place -> tear down the client -> a FRESH
instance still sees the order via a broker query -> a broken connection yields
None (never a silent []).
"""
import unittest

from src.data.ibkr_orders import IbkrOrderClient
from tests.mock_ib import MockIB, scen_filled

_SYM = "SPY260710C00740000"


class B1RecoveryTest(unittest.TestCase):
    def setUp(self):
        MockIB._BROKER_BOOK.clear()
        MockIB.fail_order_query = False

    def tearDown(self):
        MockIB._BROKER_BOOK.clear()
        MockIB.fail_order_query = False

    def _place(self):
        c = IbkrOrderClient(ib=MockIB())
        res = c.place_option_order("SPY", _SYM, "buy_to_open", 1, 1.25)
        return c, int(res["order"]["id"])

    # ---- the core acceptance case ----
    def test_fresh_instance_sees_order_placed_by_a_previous_client(self):
        old, oid = self._place()
        del old                                   # tear the client down entirely

        fresh = IbkrOrderClient(ib=MockIB())      # brand-new instance, empty cache
        self.assertEqual(fresh._trades, {}, "precondition: cache starts empty")

        orders = fresh.get_orders()
        self.assertIsNotNone(orders, "broker read succeeded, must not be None")
        ids = [o["id"] for o in orders]
        self.assertIn(oid, ids,
                      "B1: a fresh instance MUST see the live order via the broker")

    def test_fresh_instance_get_order_by_id_after_restart(self):
        old, oid = self._place()
        del old
        fresh = IbkrOrderClient(ib=MockIB())
        got = fresh.get_order(oid)
        self.assertIsNotNone(got, "B1: get_order must fall back to a broker query")
        self.assertEqual(got["id"], oid)

    def test_fresh_instance_sees_a_fill_it_did_not_place(self):
        """poll_fills after a restart must still be able to book the fill."""
        old, oid = self._place()
        old._ib.transition(oid, scen_filled(avg_fill_price=1.25, filled=1))
        del old
        fresh = IbkrOrderClient(ib=MockIB())
        got = fresh.get_order(oid)
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "filled")
        self.assertAlmostEqual(got["avg_fill_price"], 1.25, places=4)

    # ---- B2: failed read is None, never a silent [] ----
    def test_broken_connection_yields_none_not_empty(self):
        c = IbkrOrderClient(ib=MockIB())
        MockIB.fail_order_query = True
        self.assertIsNone(c.get_orders(),
                          "B2: a failed broker read must be None, never []")

    def test_broken_connection_get_order_is_none(self):
        old, oid = self._place()
        del old
        fresh = IbkrOrderClient(ib=MockIB())
        MockIB.fail_order_query = True
        self.assertIsNone(fresh.get_order(oid),
                          "B2: unknown (read failed) must not look like absent")

    def test_genuinely_empty_broker_is_empty_list_not_none(self):
        """The other half of R2: [] must still mean 'broker says none'."""
        c = IbkrOrderClient(ib=MockIB())
        got = c.get_orders()
        self.assertEqual(got, [], "no orders at broker -> [] (not None)")


if __name__ == "__main__":
    unittest.main()


class AccountIsolationTest(unittest.TestCase):
    """The IBKR paper account is SHARED with the trend book (ETF/futures legs).
    The options engine must never see another strategy's orders — reconcile
    treats unknown broker orders as orphans and would CANCEL them."""

    def setUp(self):
        MockIB._BROKER_BOOK.clear(); MockIB.fail_order_query = False

    def tearDown(self):
        MockIB._BROKER_BOOK.clear(); MockIB.fail_order_query = False

    def test_non_option_broker_orders_are_invisible(self):
        from tests.mock_ib import MockContract, MockOrderStatus, MockTrade

        class _Stk:            # a trend-book ETF order living in the same account
            contract = MockContract(secType="STK", conId=999, localSymbol="CPER")
            order = type("O", (), {"orderId": 4242})()
            orderStatus = MockOrderStatus(status="Filled")
            fills, log = [], []
        MockIB._BROKER_BOOK[4242] = _Stk()

        c = IbkrOrderClient(ib=MockIB())
        orders = c.get_orders()
        self.assertIsNotNone(orders)
        self.assertNotIn(4242, [o["id"] for o in orders],
                         "trend-book STK order must be invisible to the options engine")
        self.assertIsNone(c.get_order(4242),
                          "options engine must not resolve a non-option order")


class CrossBookAttributionTest(unittest.TestCase):
    """Cross-book hardening: this IBKR paper account is SHARED (trend book,
    manual trades). We claim only what carries our orderRef; anything else is
    logged and IGNORED — never cancelled."""

    def setUp(self):
        MockIB._BROKER_BOOK.clear(); MockIB.fail_order_query = False

    def tearDown(self):
        MockIB._BROKER_BOOK.clear(); MockIB.fail_order_query = False

    def test_our_orders_carry_orderRef(self):
        from src.data.ibkr_orders import ORDER_TAG
        c = IbkrOrderClient(ib=MockIB())
        res = c.place_option_order("SPY", _SYM, "buy_to_open", 1, 1.25)
        oid = int(res["order"]["id"])
        self.assertEqual(MockIB._BROKER_BOOK[oid].order.orderRef, ORDER_TAG,
                         "every order we place must be stamped with our orderRef")

    def test_foreign_option_order_is_ignored_not_claimed(self):
        """A FOREIGN OPTION order — passes the secType filter, so only orderRef
        attribution can protect it."""
        from tests.mock_ib import MockContract, MockOrderStatus

        class _ForeignOpt:
            contract = MockContract(secType="OPT", conId=777, localSymbol="AAPL  260710C00200000")
            order = type("O", (), {"orderId": 9911, "orderRef": "SOMEONE-ELSES-BOT"})()
            orderStatus = MockOrderStatus(status="Submitted")
            fills, log = [], []
        MockIB._BROKER_BOOK[9911] = _ForeignOpt()

        c = IbkrOrderClient(ib=MockIB())
        orders = c.get_orders()
        self.assertIsNotNone(orders)
        self.assertNotIn(9911, [o["id"] for o in orders],
                         "foreign OPTION order must not be claimed (secType alone "
                         "would have let it through)")

    def test_foreign_order_survives_reconcile_with_sectype_bypassed(self):
        """THE ACCEPTANCE CASE. Even if the secType filter is bypassed entirely,
        reconcile must NOT cancel an order that is not attributably ours."""
        from unittest.mock import patch
        from tests.mock_ib import MockContract, MockOrderStatus
        from src.engine import paper_trader as pt

        cancelled = []

        class _Client:
            def available(self): return True
            def get_orders(self):   # secType filter BYPASSED: foreign order visible
                return [{"id": 5150, "status": "open", "symbol": "AAPL",
                         "tag": "SOMEONE-ELSES-BOT"}]
            def get_positions(self): return []
            def cancel_order(self, oid): cancelled.append(oid); return {"ok": True}

        with patch.object(pt, "get_order_client", lambda: _Client()), \
             patch.object(pt, "resolve_intents", lambda c: []), \
             patch.object(pt, "_load", lambda p: []):
            pt.reconcile_with_broker()

        self.assertEqual(cancelled, [],
                         "a foreign order must SURVIVE reconcile — cancelling "
                         "another book's live order is unrecoverable")
