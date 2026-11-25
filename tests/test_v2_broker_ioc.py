"""SCALP-v2 at the BROKER BOUNDARY — src/data/ibkr_orders.py + ibkr_thread.py.

v2 was ACTIVATED on 2026-09-12 and sends its first IOC orders Monday 06:30 PT.
The activation-night surprise (scalp_v2_config() swallowed a NameError and
returned {} — YAML said ON, engine ran OFF) is the model for everything here:
a silent default in a fail-open handler. The adapter is the LAST hop before
the wire, so this file pins, through the adapter's OWN readers:

  * tif=IOC actually reaches the ib_async Order — single leg, BAG, and through
    the thread-confined proxy (a dropped kwarg = a resting DAY order under the
    v2 label, invisible to every counter).
  * the tif whitelist never lets an unknown tif through.
  * _status_of on every terminal shape an IOC can take: Cancelled,
    ApiCancelled, Inactive, partial-then-Cancelled, Filled — and, the blocker,
    Cancelled WITH IB error 202 ("Order Canceled - reason:"), which today maps
    to 'rejected' and so hides every unfilled IOC from the attempt cap and the
    fill-rate denominator.
  * IOC partial-fill normalization: filled_quantity/avg_fill_price are the
    quantity truth the engine books from. (Known limitation, not asserted
    either way: raw Cancelled with filled>0 maps to 'partially_filled', the
    same word as a LIVE partial with the remainder still working — the adapter
    cannot tell a dead IOC partial from a resting DAY partial, so the engine
    infers it from a 60s timeout. The tests pin quantity, not the word.)
  * the ambiguity boundary (R1) is unchanged by tif=IOC: a lost confirmation is
    transport_error/AMBIGUOUS, never a definitive reject.
  * restart identity: an order bound by orderId before a restart must still be
    findable when the broker book is keyed by permId.

Hermetic: everything runs against tests/mock_ib.MockIB — no gateway socket.
The conftest O-20 seal redirects every paper_trader path to tmp; nothing here
touches data/, logs/ or config/. Tests that document a VERIFIED src defect are
written for the CORRECT behaviour and are expected to FAIL until the fix lands
(each one says so in its docstring).

Run: .venv/bin/python -m pytest -q tests/test_v2_broker_ioc.py
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.data import order_client as oc
from src.data.ibkr_orders import ORDER_TAG, IbkrOrderClient
from src.data.ibkr_thread import ThreadConfinedIbkrClient
from src.engine import paper_trader as pt
from tests.mock_ib import (MockIB, MockLogEntry, MockOrderStatus, leg_fill,
                           scen_filled, scen_open, scen_rejected)

CALL = "NVDA260717C00120000"       # NVDA 2026-07-17 C 120.0
PUT = "NVDA260717C00125000"        # short leg of a debit call spread
LEGS = [{"option_symbol": CALL, "side": "buy_to_open", "quantity": 3},
        {"option_symbol": PUT, "side": "sell_to_open", "quantity": 3}]
IB_202 = "Order Canceled - reason:"   # the exact string IB emits with error 202


def _client(mock=None):
    mock = mock or MockIB()
    return IbkrOrderClient(ib=mock), mock


def _ioc_single(client, qty=1, sym=CALL, px=1.55):
    res = client.place_option_order("NVDA", sym, "buy_to_open", qty, px, tif="IOC")
    assert "order" in res, res
    return int(res["order"]["id"])


def _fake_trade(order_id: int, status: str, filled: float, remaining: float,
                tif: str = "IOC", log=None, total=None):
    """A minimal ib_async Trade stand-in for exercising _status_of directly."""
    order = SimpleNamespace(orderId=order_id, permId=0, orderRef=ORDER_TAG,
                            tif=tif, totalQuantity=float(total or (filled + remaining)))
    return SimpleNamespace(
        order=order, contract=SimpleNamespace(secType="OPT"),
        orderStatus=MockOrderStatus(status=status, filled=filled, remaining=remaining),
        fills=[], log=list(log or []))


def _bare_client():
    """An IbkrOrderClient with no IB at all — for pure _status_of probes."""
    c = IbkrOrderClient.__new__(IbkrOrderClient)
    c._account = "DU1234567"
    c._trades, c._meta, c._errors = {}, {}, {}
    c._partial_logged, c._foreign_logged, c._recent = set(), set(), {}
    return c


class _BookIsolation(unittest.TestCase):
    """MockIB keeps a class-level broker book so restart tests can recover
    orders; clear it around every test so cases never see each other."""
    def setUp(self):
        MockIB._BROKER_BOOK.clear()
        MockIB.fail_order_query = False

    def tearDown(self):
        MockIB._BROKER_BOOK.clear()
        MockIB.fail_order_query = False


# ── 1. tif reaches the wire ──────────────────────────────────────────────────
class TifReachesTheOrder(_BookIsolation):
    def test_single_leg_ioc_is_stamped_on_the_ib_order(self):
        """Prevents: v2's single-leg IOC silently resting as a DAY limit at the
        broker (the exact class of the activation-night YAML surprise — the
        engine believes v2 is on while every order behaves like v1)."""
        c, m = _client()
        oid = _ioc_single(c)
        self.assertEqual(m._trades[oid].order.tif, "IOC")
        self.assertEqual(m._trades[oid].order.orderRef, ORDER_TAG)

    def test_bag_combo_ioc_is_stamped_on_the_parent_order(self):
        """Prevents: a debit-spread IOC resting as DAY because the BAG parent
        order path forgot the kwarg the single-leg path honours."""
        c, m = _client()
        res = c.place_spread_order("NVDA", LEGS, 1.08, order_type="debit", tif="IOC")
        self.assertIn("order", res)
        tr = m._trades[res["order"]["id"]]
        self.assertEqual(tr.contract.secType, "BAG")
        self.assertEqual(tr.order.tif, "IOC")

    def test_default_tif_is_still_day_for_v1_callers(self):
        """Prevents: the v2 plumbing changing v1's resting-mid semantics — an
        omitted tif must remain a DAY order (the byte-identical-v1 guarantee)."""
        c, m = _client()
        res = c.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.50)
        self.assertEqual(m._trades[res["order"]["id"]].order.tif, "DAY")
        res = c.place_spread_order("NVDA", LEGS, 1.00, order_type="debit")
        self.assertEqual(m._trades[res["order"]["id"]].order.tif, "DAY")


class TifWhitelist(unittest.TestCase):
    def test_only_day_and_ioc_pass_everything_else_is_day(self):
        """Prevents: an unknown tif (GTC, FOK, a lowercase 'ioc') reaching IBKR
        — anything outside the two modes the engine reasons about degrades to
        the conservative DAY order, never to something that can rest overnight
        or behave as a third fill mode."""
        c = _bare_client()
        self.assertEqual(c._limit_order("BUY", 1, 1.0).tif, "DAY")
        self.assertEqual(c._limit_order("BUY", 1, 1.0, tif="DAY").tif, "DAY")
        self.assertEqual(c._limit_order("BUY", 1, 1.0, tif="IOC").tif, "IOC")
        for bad in ("GTC", "FOK", "OPG", "ioc", "", None):
            self.assertEqual(c._limit_order("BUY", 1, 1.0, tif=bad).tif, "DAY",
                             f"tif={bad!r} must not leak to the broker")

    def test_whitelist_applies_to_bag_parent_too(self):
        """Prevents: the BAG path accepting a tif the single-leg path refuses."""
        c, m = _client()
        res = c.place_spread_order("NVDA", LEGS, 1.00, order_type="debit", tif="GTC")
        self.assertEqual(m._trades[res["order"]["id"]].order.tif, "DAY")


# ── 2. the thread proxy forwards tif ─────────────────────────────────────────
class ThreadProxyForwardsTif(_BookIsolation):
    def test_tif_kwarg_reaches_inner_client_single_leg(self):
        """Prevents: the thread-confined proxy (the object the engine actually
        holds) dropping tif so a v2 IOC rests as a DAY order."""
        m = MockIB()
        proxy = ThreadConfinedIbkrClient(factory=lambda: IbkrOrderClient(ib=m))
        res = proxy.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.55, tif="IOC")
        self.assertIn("order", res, res)
        self.assertEqual(m._trades[res["order"]["id"]].order.tif, "IOC")

    def test_tif_kwarg_reaches_inner_client_spread(self):
        """Prevents: the same dropped-kwarg hole on the spread path."""
        m = MockIB()
        proxy = ThreadConfinedIbkrClient(factory=lambda: IbkrOrderClient(ib=m))
        res = proxy.place_spread_order("NVDA", LEGS, 1.08, order_type="debit", tif="IOC")
        self.assertIn("order", res, res)
        self.assertEqual(m._trades[res["order"]["id"]].order.tif, "IOC")

    def test_proxy_timeout_on_ioc_is_ambiguous_not_reject(self):
        """Prevents: a wedged IOC submit being read as a reject (intent dropped,
        order possibly live) instead of AMBIGUOUS — tif must not change R1."""
        class _Wedged:
            def available(self): return True
            def place_option_order(self, *a, **k):
                import time; time.sleep(60)
        proxy = ThreadConfinedIbkrClient(factory=_Wedged,
                                         timeouts={"read": 1, "place": 1, "default": 1})
        res = proxy.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.55, tif="IOC")
        self.assertTrue(res.get("transport_error"), res)
        self.assertEqual(oc.classify_post_result(res)["status"], oc.AMBIGUOUS)


# ── 3. _status_of on every terminal shape an IOC can take ────────────────────
class StatusOfTerminalShapes(_BookIsolation):
    def test_cancelled_zero_fill_is_canceled(self):
        """Prevents: a plain unfilled IOC (Cancelled, filled=0, no message)
        being read as anything but 'canceled' — the status poll_fills keys
        entry_ioc_unfilled on."""
        c, m = _client()
        oid = _ioc_single(c)
        m.transition(oid, {"status": "Cancelled", "filled": 0.0})
        o = c.get_order(oid)
        self.assertEqual(o["status"], "canceled")
        self.assertEqual(o["filled_quantity"], 0.0)
        self.assertNotIn("reason", o)

    def test_api_cancelled_zero_fill_is_canceled(self):
        """Prevents: an engine-side cancel acknowledged as ApiCancelled being
        misread (e.g. as open) so cancel_stale_entries loops on it."""
        c, m = _client()
        oid = _ioc_single(c)
        m.transition(oid, {"status": "ApiCancelled", "filled": 0.0})
        self.assertEqual(c.get_order(oid)["status"], "canceled")

    def test_pending_cancel_is_canceled(self):
        """Prevents: a PendingCancel snapshot (cancel sent, ack not yet in)
        being read as open, so cancel_stale_entries re-issues the cancel every
        cycle or poll_fills keeps a dead IOC in still_pending."""
        c, m = _client()
        oid = _ioc_single(c)
        m.transition(oid, {"status": "PendingCancel", "filled": 0.0})
        self.assertEqual(c.get_order(oid)["status"], "canceled")

    def test_inactive_is_rejected(self):
        """Prevents: an Inactive order (IB's 'not accepted' terminal state)
        being polled as open forever — it must be terminal and it is a reject,
        NOT an attempt that reached the market."""
        c, m = _client()
        oid = _ioc_single(c)
        m.transition(oid, {"status": "Inactive", "filled": 0.0})
        self.assertEqual(c.get_order(oid)["status"], "rejected")

    def test_filled_ioc_is_filled_with_quantity(self):
        """Prevents: a full IOC fill being booked from a status word instead of
        the executed quantity (the 08-05 phantom-Filled misread)."""
        c, m = _client()
        oid = _ioc_single(c, qty=5)
        m.transition(oid, scen_filled(avg_fill_price=1.55, filled=5))
        o = c.get_order(oid)
        self.assertEqual(o["status"], "filled")
        self.assertEqual(o["filled_quantity"], 5.0)
        self.assertEqual(o["avg_fill_price"], 1.55)
        self.assertEqual(pt.order_fill_detail(o)["net_debit"], 1.55)

    def test_phantom_filled_with_zero_quantity_is_not_a_fill(self):
        """Prevents: an IOC that IB summarises 'Filled' with filled=0 (the
        reqCompletedOrders shape) booking a position that does not exist."""
        c, m = _client()
        oid = _ioc_single(c)
        m.transition(oid, {"status": "Filled", "filled": 0.0, "remaining": 0.0})
        with self.assertLogs("trader", level="WARNING"):
            o = c.get_order(oid)
        self.assertEqual(o["status"], "canceled")
        self.assertEqual(o["filled_quantity"], 0.0)

    def test_genuine_reject_code_201_is_still_rejected_under_ioc(self):
        """Prevents: over-correcting the 202 fix — a real reject (201, e.g.
        buying power) on an IOC must stay 'rejected' so it is never counted as
        an attempt that reached the market."""
        c, m = _client()
        oid = _ioc_single(c)
        m.transition(oid, scen_rejected(
            "Order rejected - reason: insufficient buying power", code=201, status="Cancelled"))
        o = c.get_order(oid)
        self.assertEqual(o["status"], "rejected")
        self.assertIn("buying power", o["reason"])


# ── 4. IOC partial fill normalization ────────────────────────────────────────
class IocPartialFillNormalization(_BookIsolation):
    def test_partial_then_cancelled_reports_executed_quantity(self):
        """Prevents: a 5-lot IOC that sweeps 2 and is exchange-cancelled being
        booked at the wrong size — filled_quantity must be the executed 2, the
        avg price the executed price, and the status must NOT be 'filled'."""
        c, m = _client()
        oid = _ioc_single(c, qty=5)
        m.transition(oid, {"status": "Cancelled", "filled": 2.0, "remaining": 3.0,
                           "avg_fill_price": 1.55})
        with self.assertLogs("trader", level="WARNING") as cm:
            o = c.get_order(oid)
        # QUANTITY is the load-bearing truth; the status WORD is contested (the
        # adapter cannot today distinguish a live partial from a dead one, see
        # the module docstring) — so accept either terminal-safe word, never
        # 'filled', and pin the executed quantity/price exactly.
        self.assertIn(o["status"], ("partially_filled", "canceled"))
        self.assertNotEqual(o["status"], "filled")
        self.assertEqual(o["filled_quantity"], 2.0)
        self.assertEqual(o["avg_fill_price"], 1.55)
        self.assertTrue(any("PARTIAL FILL" in x for x in cm.output))

    def test_partial_bag_reports_per_leg_exec_quantity(self):
        """Prevents: a spread IOC that fills 1 of 3 combos being booked as 3
        (or as 0) — each leg's exec_quantity must be the executed 1."""
        c, m = _client()
        res = c.place_spread_order("NVDA", LEGS, 1.08, order_type="debit", tif="IOC")
        oid = res["order"]["id"]
        m.transition(oid, {"status": "Cancelled", "filled": 1.0, "remaining": 2.0,
                           "avg_fill_price": 1.08,
                           "fills": [leg_fill(CALL, shares=1, price=2.05, side="BOT"),
                                     leg_fill(PUT, shares=1, price=0.97, side="SLD")]})
        with self.assertLogs("trader", level="WARNING"):
            o = c.get_order(oid)
        self.assertIn(o["status"], ("partially_filled", "canceled"))
        self.assertNotEqual(o["status"], "filled")
        self.assertEqual(o["filled_quantity"], 1.0)
        legs = {l["option_symbol"]: l for l in o["leg"]}
        self.assertEqual(legs[CALL]["exec_quantity"], 1.0)
        self.assertEqual(legs[PUT]["exec_quantity"], 1.0)
        self.assertAlmostEqual(pt.order_fill_detail(o)["net_debit"], 1.08, places=4)

    def test_partial_is_stable_across_repeated_polls(self):
        """Prevents: a second poll of the same dead partial flipping to 'filled'
        or 'canceled' and re-booking / un-booking the executed contracts."""
        c, m = _client()
        oid = _ioc_single(c, qty=4)
        m.transition(oid, {"status": "Cancelled", "filled": 2.0, "remaining": 2.0,
                           "avg_fill_price": 1.55})
        with self.assertLogs("trader", level="WARNING"):
            first = c.get_order(oid)
        second = c.get_order(oid)
        self.assertEqual((first["status"], first["filled_quantity"]),
                         (second["status"], second["filled_quantity"]))


# ── 5. the ambiguity boundary is unchanged by tif=IOC ────────────────────────
class AmbiguityBoundaryWithIoc(_BookIsolation):
    class _BoomIB(MockIB):
        def placeOrder(self, contract, order):
            raise TimeoutError("socket timeout after transmit")

    def test_single_leg_ioc_place_failure_is_transport_error(self):
        """Prevents: a lost IOC confirmation being read as a reject — the order
        may be live (or have filled) at IBKR; the intent must stay unbound."""
        c = IbkrOrderClient(ib=self._BoomIB())
        res = c.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.55, tif="IOC")
        self.assertTrue(res.get("transport_error"), res)
        self.assertEqual(oc.classify_post_result(res)["status"], oc.AMBIGUOUS)

    def test_spread_ioc_place_failure_is_transport_error(self):
        """Prevents: a lost spread-IOC confirmation dropping the intent while a
        multi-leg order may be live — the blind close-resubmit that opens an
        unintended opposite position."""
        c = IbkrOrderClient(ib=self._BoomIB())
        res = c.place_spread_order("NVDA", LEGS, 1.08, order_type="debit", tif="IOC")
        self.assertTrue(res.get("transport_error"), res)
        self.assertEqual(oc.classify_post_result(res)["status"], oc.AMBIGUOUS)

    def test_ioc_confirmation_loss_after_place_is_transport_error(self):
        """placeOrder succeeded (IOC is live/executing) but the status read
        blew up — must be AMBIGUOUS, never DEFINITIVE_REJECT."""
        c, _ = _client()
        with patch.object(IbkrOrderClient, "_status_of",
                          side_effect=ConnectionResetError("gateway dropped")):
            res = c.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.55, tif="IOC")
        self.assertTrue(res.get("transport_error"), res)
        self.assertEqual(oc.classify_post_result(res)["status"], oc.AMBIGUOUS)
        self.assertNotEqual(oc.classify_post_result(res)["status"], oc.DEFINITIVE_REJECT)

    def test_ioc_qualify_failure_stays_definitive(self):
        """An IOC that was never sent (bad symbol) must remain definitive — the
        engine must not strand an unbound intent for an order that never
        existed."""
        c, _ = _client()
        res = c.place_option_order("NVDA", "BADSYMBOL", "buy_to_open", 1, 1.55, tif="IOC")
        self.assertFalse(res.get("transport_error"))
        self.assertEqual(oc.classify_post_result(res)["status"], oc.DEFINITIVE_REJECT)


# ── 6. THE BLOCKER: IB cancels carry error 202 ───────────────────────────────
class CancelWith202IsCanceledNotRejected(_BookIsolation):
    """IB's documented behaviour for a cancel — including an IOC that finds no
    liquidity — is orderStatus Cancelled PLUS `Error 202, reqId N: Order
    Canceled - reason:` on the same orderId (ib_async appends a Cancelled
    TradeLogEntry carrying that message). Today _status_of checks
    `if reject: return "rejected"` BEFORE the Cancelled mapping, and _on_error
    records EVERY order-scoped code, so a 202 turns an unfilled IOC into a
    reject. Production evidence: 162 'ended: rejected' vs 4 'ended: canceled'
    in agent.log; order 969 (SLV, 08-28) was 'cancel requested -> canceled'
    then re-polled as 'rejected'.

    Consequence chain Monday: poll_fills' entry_ioc_unfilled branch requires
    status == 'canceled', so nothing is written; today_v2_attempt_count() and
    the fill-rate denominator stay 0; max_attempts_per_day never binds; the
    engine can fire one IOC per cycle all day.

    VERIFIED SRC DEFECT (src/data/ibkr_orders.py:_status_of / _on_error /
    _reject_reason). These tests assert the CORRECT behaviour and FAIL today.
    """

    def test_poll_time_202_on_unfilled_ioc_is_canceled(self):
        """Prevents: an all-day IOC machine-gun and a fabricated 100% fill
        rate when IBKR reports its cancels with error 202."""
        c, m = _client()
        oid = _ioc_single(c)
        m.transition(oid, scen_rejected(IB_202, code=202, status="Cancelled"))
        o = c.get_order(oid)
        self.assertEqual(o["filled_quantity"], 0.0)
        self.assertEqual(o["status"], "canceled",
                         "Cancelled + error 202 with zero fills is a CANCEL, not a reject")

    def test_202_via_error_event_on_bare_status_of(self):
        """The narrowest reproduction: the errorEvent handler alone flips a
        clean Cancelled/filled=0 trade to 'rejected'."""
        c = _bare_client()
        tr = _fake_trade(7, "Cancelled", filled=0.0, remaining=1.0, tif="IOC")
        c._trades[7] = tr
        self.assertEqual(c._status_of(tr), "canceled")          # precondition
        c._on_error(7, 202, IB_202)
        self.assertEqual(c._status_of(tr), "canceled",
                         "error 202 is IB's cancel notification, not a reject reason")

    def test_202_via_trade_log_entry_on_bare_status_of(self):
        """ib_async's wrapper appends TradeLogEntry(status=Cancelled, message=
        'Error 202, reqId N: Order Canceled - reason:') even when no errorEvent
        handler is wired; the log-fallback in _reject_reason must not turn that
        into a reject either."""
        c = _bare_client()
        tr = _fake_trade(8, "Cancelled", filled=0.0, remaining=1.0, tif="IOC",
                         log=[MockLogEntry(status="Cancelled", errorCode=202,
                                           message=f"Error 202, reqId 8: {IB_202}")])
        c._trades[8] = tr
        self.assertEqual(c._status_of(tr), "canceled")

    def test_201_via_error_event_is_still_rejected(self):
        """The fix must be code-aware, not 'ignore all errors': 201 (a true
        reject, e.g. buying power) must remain 'rejected'."""
        c = _bare_client()
        tr = _fake_trade(9, "Cancelled", filled=0.0, remaining=1.0, tif="IOC")
        c._trades[9] = tr
        c._on_error(9, 201, "Order rejected - reason: insufficient buying power")
        self.assertEqual(c._status_of(tr), "rejected")

    def test_202_during_settle_does_not_return_a_reject_envelope(self):
        """Prevents: the placement-time branch of the same hole — the 202 lands
        inside _settle, place_option_order returns _reject_result,
        classify_post_result says DEFINITIVE_REJECT, submit_paper_order drops
        the intent, and the attempt vanishes from every counter. An IOC that
        was accepted and cancelled unfilled must come back with its order id
        and a non-reject status so the engine binds it and the next poll logs
        entry_ioc_unfilled."""
        c, m = _client()
        m.scenarios = [scen_rejected(IB_202, code=202, status="Cancelled")]
        res = c.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.55, tif="IOC")
        self.assertIn("order", res, res)
        self.assertIsNotNone(res["order"].get("id"))
        self.assertNotEqual(res["order"]["status"], "rejected", res)
        self.assertNotEqual(oc.classify_post_result(res)["status"], oc.DEFINITIVE_REJECT,
                            "an accepted-then-cancelled IOC is an ATTEMPT, not a reject")

    def test_202_during_settle_on_bag_does_not_return_a_reject_envelope(self):
        """Same hole on the spread path — every spread candidate would be
        dropped and re-attempted each cycle, uncounted."""
        c, m = _client()
        m.scenarios = [scen_rejected(IB_202, code=202, status="Cancelled")]
        res = c.place_spread_order("NVDA", LEGS, 1.08, order_type="debit", tif="IOC")
        self.assertIn("order", res, res)
        self.assertNotEqual(res["order"]["status"], "rejected", res)
        self.assertNotEqual(oc.classify_post_result(res)["status"], oc.DEFINITIVE_REJECT)

    def test_cancel_order_ack_with_202_reports_canceled(self):
        """Prevents: the engine's OWN cancel (cancel_stale_entries / the close
        path) being acknowledged as 'rejected' — which also increments
        close_reject_count toward the broker_lost breaker on a position the
        broker still holds (order 969 SLV, 2026-08-28)."""
        c, m = _client()
        res = c.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.50)   # resting DAY
        oid = res["order"]["id"]
        out = c.cancel_order(oid)
        self.assertEqual(out["order"]["status"], "canceled")
        # IB then confirms the cancel the way it always does: error 202
        m.errorEvent.emit(oid, 202, IB_202, m._trades[oid].contract)
        self.assertEqual(c.get_order(oid)["status"], "canceled",
                         "a re-poll after IB's 202 ack must still read canceled")


# ── 7. restart identity: orderId bound before, permId keyed after ────────────
class OrderIdentitySurvivesRestart(_BookIsolation):
    """place_*_order returns trade.order.orderId and the engine binds THAT into
    the pending record. _refresh_from_broker re-keys the broker book by permId
    (correctly — permId is the only stable identity). A fresh process therefore
    cannot find the order it bound: get_order(orderId) -> None every poll, the
    record sits in still_pending for 30 min (blocking has_open_exposure and the
    restart gate), then drops as entry_expired — while, if the IOC actually
    FILLED, the contracts are live at IBKR with no book position.

    VERIFIED SRC DEFECT (src/data/ibkr_orders.py get_order / _refresh_from_broker
    vs place_*_order's returned id). Expected to FAIL today."""
    PERM = 2033402227

    def test_bound_orderId_resolves_after_restart_when_broker_keys_by_permId(self):
        """Prevents: a filled IOC placed just before a crash-restart never
        being booked because the fresh process looks it up by orderId."""
        m = MockIB()
        A = IbkrOrderClient(ib=m)
        oid = _ioc_single(A)
        m._trades[oid].order.permId = self.PERM          # broker assigns permId
        m.transition(oid, scen_filled(avg_fill_price=1.55, filled=1))

        B = IbkrOrderClient(ib=MockIB())                 # the restarted process
        got = B.get_order(oid)
        self.assertIsNotNone(got, "the id the engine bound must resolve after a restart")
        self.assertEqual(got["status"], "filled")
        self.assertEqual(got["filled_quantity"], 1.0)

    def test_get_orders_does_not_list_one_trade_twice(self):
        """Prevents: open-order counts doubling in audit/preflight sentinels —
        after any broker refresh the cache holds BOTH keys for one trade."""
        m = MockIB()
        A = IbkrOrderClient(ib=m)
        oid = _ioc_single(A)
        m._trades[oid].order.permId = self.PERM
        orders = A.get_orders()
        self.assertIsNotNone(orders)
        self.assertEqual(len(orders), 1, f"one trade listed as {[o['id'] for o in orders]}")

    def test_restart_reconcile_can_resolve_our_bound_order_as_ours(self):
        """Prevents: startup reconcile cancelling our own still-live tagged
        order (a v1 DAY close placed before the restart) as an orphan because
        the engine only knows the orderId it bound. The adapter's contract
        under test: the bound id must RESOLVE to the live order from a fresh
        process (reconcile_with_broker's known_orders logic itself lives in
        paper_trader and is covered there)."""
        m = MockIB()
        A = IbkrOrderClient(ib=m)
        res = A.place_option_order("NVDA", CALL, "sell_to_close", 1, 1.60)   # resting DAY
        oid = res["order"]["id"]
        m._trades[oid].order.permId = self.PERM
        B = IbkrOrderClient(ib=MockIB())
        live = [o for o in (B.get_orders() or []) if o["status"] in ("open", "pending")]
        self.assertEqual(len(live), 1)
        got = B.get_order(oid)
        self.assertIsNotNone(got, f"bound orderId {oid} must resolve to the live order")
        self.assertEqual(got["status"], "open")
        self.assertEqual(got["tag"], ORDER_TAG)

if __name__ == "__main__":
    unittest.main()


class CancelNoticeNeedsAnEmptyReason(unittest.TestCase):
    """IB's plain cancel is 'Order Canceled - reason:' with NOTHING after it.
    A 202 that carries a reason (margin, buying power) is a refusal."""
    def test_empty_reason_is_cancel_reasoned_is_reject(self):
        from src.data.ibkr_orders import IbkrOrderClient as C
        self.assertTrue(C._is_cancel_notice(202, "Order Canceled - reason:"))
        self.assertTrue(C._is_cancel_notice(202, "Order Canceled - reason: "))
        self.assertFalse(C._is_cancel_notice(202, "Order Canceled - reason: insufficient buying power"))
        self.assertFalse(C._is_cancel_notice(202, "Order Canceled - reason: Your order is not accepted, margin"))
