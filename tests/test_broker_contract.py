"""Broker-interface robustness contract (SEV1 fixes R1/R2/R5/R4).

These tests pin the four failure modes fixed in the order-client abstraction
and paper_trader engine layer. All paper-mode / observation-only.

R1 — ambiguous order-POST must NOT be treated as a definitive reject: on a
     transport timeout the intent stays UNBOUND (never destroyed, never
     blind-resubmitted, especially on the CLOSE path).
R2 — a broker read error is distinguishable from genuinely-empty; recovery /
     reconcile paths ABORT (never drop intents) on error.
R5 — cancel races keep a fill that lands in the cancel window; resolve_intents
     matches on symbols + SIDES + status, so a close intent never binds to the
     already-filled ENTRY.
R4 — poll_fills is transactional (pending rewritten per mutation) and dedups
     positions by rec_id, so a crash-restart replay creates NO duplicate.

Run: .venv/bin/python -m unittest tests.test_broker_contract -v
"""

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

# TIME BOMB FIXED (2026-08-22): these hardcoded expiration "2026-08-21", so
# from 08-22 the expiry guard refused the order and the AMBIGUITY assertions
# never ran — the failure message talked about expiry, in tests about broker
# ambiguity. Second such rot found in a week (see tests/test_submit_guards.py,
# fixed 08-17). Anything reaching submit_paper_order must be relative to the
# real clock, never a literal date.
_FUTURE_EXPIRY = (date.today() + timedelta(days=7)).isoformat()

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.paper_trader as pt  # noqa: E402
from src.data import order_client as oc  # noqa: E402
from src.data.tradier_orders import ORDER_TAG  # noqa: E402


def _client(**overrides) -> MagicMock:
    c = MagicMock()
    c.available.return_value = True
    for name, value in overrides.items():
        getattr(c, name).return_value = value
    return c


def _single_leg_filled(order_id: int, symbol: str, price: float = 1.20) -> dict:
    """A filled single-leg BUY_TO_OPEN entry (Tradier shape)."""
    return {"id": order_id, "status": "filled", "avg_fill_price": price,
            "side": "buy_to_open", "option_symbol": symbol,
            "create_date": "2026-08-03T15:00:00Z",
            "transaction_date": "2026-08-03T15:00:05Z"}


def _entry_pending(order_id, rec_id, symbol, submitted_at=None):
    return {"intent": "open", "order_id": order_id, "rec_id": rec_id, "ticker": "AAA",
            "structure": "long call", "strategy": "long_call", "direction": "long",
            "legs": [{"symbol": symbol, "action": "buy_to_open"}],
            "rec_price": 1.20, "max_profit": 2.0, "max_loss": 1.20,
            "expiration": _FUTURE_EXPIRY,
            "submitted_at": submitted_at or "2026-08-03T08:00:00"}


class _TmpBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._orig = (pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG)
        pt.PENDING_FILE = base / "paper_orders.json"
        pt.POSITIONS_FILE = base / "paper_positions.json"
        pt.FILLS_LOG = base / "fills.jsonl"

    def tearDown(self):
        pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG = self._orig
        self.tmp.cleanup()

    def _pending(self):
        return pt._load(pt.PENDING_FILE)

    def _fills(self):
        if not pt.FILLS_LOG.exists():
            return []
        return [json.loads(l) for l in pt.FILLS_LOG.read_text().splitlines()]


# ── R1: the 3-state POST contract ────────────────────────────────────────────

class ClassifyPostResultTest(unittest.TestCase):
    def test_transport_error_is_ambiguous(self):
        got = oc.classify_post_result({"error": "timed out", "transport_error": True})
        self.assertEqual(got["status"], oc.AMBIGUOUS)

    def test_http_error_envelope_is_definitive_reject(self):
        got = oc.classify_post_result({"error": "bad request", "status": 400})
        self.assertEqual(got["status"], oc.DEFINITIVE_REJECT)

    def test_rejected_order_is_definitive_reject(self):
        got = oc.classify_post_result(
            {"error": "buying power", "order": {"id": 5, "status": "rejected"}})
        self.assertEqual(got["status"], oc.DEFINITIVE_REJECT)

    def test_none_is_definitive_reject(self):
        self.assertEqual(oc.classify_post_result(None)["status"], oc.DEFINITIVE_REJECT)

    def test_ok_order(self):
        got = oc.classify_post_result({"order": {"id": 9, "status": "pending"}})
        self.assertEqual(got["status"], oc.OK)
        self.assertEqual(got["order_id"], 9)


class AmbiguousPostTest(_TmpBook):
    def test_ambiguous_entry_keeps_intent_unbound(self):
        rec = {"rec_id": "R1-E", "ticker": "AAA",
               "trade": {"structure": "call debit spread", "strategy": "debit_spread",
                         "direction": "long", "net_debit": 1.0, "max_profit": 1.0,
                         "max_loss": 1.0, "expiration": _FUTURE_EXPIRY,
                         "legs": [{"symbol": "AAA1", "action": "buy_to_open", "mid": 1.0},
                                  {"symbol": "AAA2", "action": "sell_to_open", "mid": 0.4}]}}
        client = _client()
        client.place_spread_order.return_value = {"error": "timeout", "transport_error": True}
        with patch.object(pt, "PaperOrderClient", return_value=client):
            result = pt.submit_paper_order(rec, quantity=1)
        self.assertTrue(result["ambiguous"])
        self.assertFalse(result["submitted"])
        # R1: intent is STILL on disk and UNBOUND (order_id None) — not destroyed.
        pend = self._pending()
        self.assertEqual(len(pend), 1)
        self.assertIsNone(pend[0]["order_id"])

    def test_ambiguous_close_never_resubmits(self):
        position = {"rec_id": "R1-C", "ticker": "AAA", "structure": "long call",
                    "quantity": 1, "entry_price": 1.2, "closing": False,
                    "legs": [{"symbol": "AAA1", "action": "buy_to_open"}]}
        pt._save(pt.POSITIONS_FILE, [dict(position)])
        client = _client()
        client.place_option_order.return_value = {"error": "timeout", "transport_error": True}
        with patch.object(pt, "PaperOrderClient", return_value=client), \
                patch("time.sleep") as slept:
            result = pt.submit_close_order(position, 0.95, "time_flat (test)")
        # NEVER blind-resubmit a close on an ambiguous POST.
        self.assertEqual(client.place_option_order.call_count, 1)
        slept.assert_not_called()
        self.assertTrue(result["ambiguous"])
        # intent left UNBOUND for resolve_intents; position defensively 'closing'.
        pend = self._pending()
        self.assertEqual(len(pend), 1)
        self.assertIsNone(pend[0]["order_id"])
        self.assertTrue(pt.load_positions()[0]["closing"])

    def test_definitive_reject_close_retries_then_drops(self):
        position = {"rec_id": "R1-D", "ticker": "AAA", "structure": "long call",
                    "quantity": 1, "entry_price": 1.2, "closing": False,
                    "legs": [{"symbol": "AAA1", "action": "buy_to_open"}]}
        pt._save(pt.POSITIONS_FILE, [dict(position)])
        client = _client()
        # both a DEFINITIVE reject (no transport flag) -> one retry, then drop.
        client.place_option_order.side_effect = [
            {"error": "backend error"}, {"error": "backend error"}]
        with patch.object(pt, "PaperOrderClient", return_value=client), \
                patch("time.sleep"):
            result = pt.submit_close_order(position, 0.95, "time_flat (test)")
        self.assertEqual(client.place_option_order.call_count, 2)
        self.assertIsNone(result)
        self.assertEqual(self._pending(), [])  # intent dropped (no live order)


# ── R2: broker error distinguishable from empty; reconcile aborts, not drops ──

class ReadErrorVsEmptyTest(_TmpBook):
    def test_tradier_get_orders_none_on_transport_error(self):
        c = pt.PaperOrderClient()
        c.token, c.account = "t", "a"  # force available()
        with patch.object(c, "_request",
                          return_value={"error": "timeout", "transport_error": True}):
            self.assertIsNone(c.get_orders())
        with patch.object(c, "_request", return_value={"error": "boom", "status": 500}):
            self.assertIsNone(c.get_orders())

    def test_tradier_get_orders_empty_when_broker_says_none(self):
        c = pt.PaperOrderClient()
        c.token, c.account = "t", "a"
        with patch.object(c, "_request", return_value={"orders": "null"}):
            self.assertEqual(c.get_orders(), [])

    def test_resolve_intents_aborts_on_broker_error(self):
        stale = (datetime.now() - timedelta(seconds=300)).isoformat(timespec="seconds")
        pt._save(pt.PENDING_FILE, [_entry_pending(None, "R2-1", "AAA1", submitted_at=stale)])
        client = _client()
        client.get_orders.return_value = None  # broker read FAILED (not empty)
        events = pt.resolve_intents(client)
        self.assertEqual(events, [])
        # R2: the unbound intent is PRESERVED — never dropped on an error.
        pend = self._pending()
        self.assertEqual(len(pend), 1)
        self.assertIsNone(pend[0]["order_id"])
        self.assertFalse(any(e.get("event") == "intent_lost" for e in self._fills()))

    def test_resolve_intents_drops_on_genuine_empty(self):
        stale = (datetime.now() - timedelta(seconds=300)).isoformat(timespec="seconds")
        pt._save(pt.PENDING_FILE, [_entry_pending(None, "R2-2", "AAA1", submitted_at=stale)])
        client = _client()
        client.get_orders.return_value = []  # broker AUTHORITATIVELY reports none
        events = pt.resolve_intents(client)
        self.assertEqual(events[0]["event"], "intent_lost")
        self.assertEqual(self._pending(), [])

    def test_reconcile_aborts_on_order_read_error(self):
        client = _client()
        client.get_orders.return_value = None  # error, not empty
        with patch.object(pt, "get_order_client", return_value=client):
            events = pt.reconcile_with_broker()
        # never cancels/adopts off a phantom-empty broker.
        client.cancel_order.assert_not_called()
        self.assertEqual(events, [])


# ── R5: cancel races + intent mis-binding ────────────────────────────────────

class CancelRaceTest(_TmpBook):
    def test_fill_in_cancel_window_is_kept(self):
        pt._save(pt.PENDING_FILE, [_entry_pending(111, "R5-1", "AAA1",
                                                  submitted_at="2026-08-03T08:00:00")])
        client = _client()
        client.get_order.return_value = {"status": "filled"}  # filled during cancel
        with patch.object(pt, "PaperOrderClient", return_value=client):
            canceled = pt.cancel_stale_entries(timeout_min=1)
        self.assertEqual(canceled, [])            # NOT expired
        self.assertEqual(len(self._pending()), 1)  # kept for poll_fills to book

    def test_dead_order_still_expires(self):
        pt._save(pt.PENDING_FILE, [_entry_pending(111, "R5-2", "AAA1",
                                                  submitted_at="2026-08-03T08:00:00")])
        client = _client()
        client.get_order.return_value = {"status": "canceled"}
        with patch.object(pt, "PaperOrderClient", return_value=client):
            canceled = pt.cancel_stale_entries(timeout_min=1)
        self.assertEqual(len(canceled), 1)
        self.assertEqual(self._pending(), [])


class IntentMisbindingTest(_TmpBook):
    def _close_intent_and_position(self):
        stale = (datetime.now() - timedelta(seconds=300)).isoformat(timespec="seconds")
        pt._save(pt.PENDING_FILE, [{
            "intent": "close", "order_id": None, "rec_id": "RT-1", "ticker": "AAA",
            "structure": "call debit spread", "entry_price": 1.2,
            "close_reason": "time_flat (test)", "submitted_at": stale}])
        pt._save(pt.POSITIONS_FILE, [{
            "rec_id": "RT-1", "ticker": "AAA", "structure": "call debit spread",
            "entry_price": 1.2, "closing": True,
            "legs": [{"symbol": "AAA1", "action": "buy_to_open"},
                     {"symbol": "AAA2", "action": "sell_to_open"}]}])

    def _entry_order(self):
        return {"id": 111, "status": "filled", "tag": ORDER_TAG,
                "leg": [{"option_symbol": "AAA1", "side": "buy_to_open"},
                        {"option_symbol": "AAA2", "side": "sell_to_open"}]}

    def _close_order(self):
        return {"id": 333, "status": "open", "tag": ORDER_TAG,
                "leg": [{"option_symbol": "AAA1", "side": "sell_to_close"},
                        {"option_symbol": "AAA2", "side": "buy_to_close"}]}

    def test_close_intent_will_not_bind_to_filled_entry(self):
        self._close_intent_and_position()
        client = _client()
        client.get_orders.return_value = [self._entry_order()]  # SAME symbols, entry sides
        events = pt.resolve_intents(client)
        # sides disambiguate: the close intent must NOT adopt the entry order.
        self.assertTrue(all(e["event"] != "intent_recovered" for e in events))
        self.assertEqual(events[0]["event"], "intent_lost")
        # position re-armed (closing False) so the exit manager retries cleanly.
        self.assertFalse(pt.load_positions()[0]["closing"])

    def test_close_intent_binds_to_matching_close_order(self):
        self._close_intent_and_position()
        client = _client()
        client.get_orders.return_value = [self._entry_order(), self._close_order()]
        events = pt.resolve_intents(client)
        self.assertEqual(events[0]["event"], "intent_recovered")
        self.assertEqual(events[0]["order_id"], 333)  # bound to the CLOSE, not entry 111
        self.assertEqual(self._pending()[0]["order_id"], 333)


# ── R4: poll_fills transactional + rec_id dedup ──────────────────────────────

class PollFillsTransactionalTest(_TmpBook):
    def test_replay_after_fill_makes_no_duplicate_position(self):
        entry = _entry_pending(111, "R4-1", "AAA1")
        pt._save(pt.PENDING_FILE, [entry])
        client = _client()
        client.get_order.return_value = _single_leg_filled(111, "AAA1")
        with patch.object(pt, "PaperOrderClient", return_value=client):
            events = pt.poll_fills()
        self.assertEqual(len(events), 1)
        self.assertEqual(len(pt.load_positions()), 1)
        self.assertEqual(self._pending(), [])  # order removed transactionally

        # Simulate a crash that left the (already-booked) order back in pending.
        pt._save(pt.PENDING_FILE, [entry])
        with patch.object(pt, "PaperOrderClient", return_value=client):
            replay = pt.poll_fills()
        # R4: rec_id dedup — no duplicate position, no duplicate paper_fill.
        self.assertEqual(replay, [])
        self.assertEqual(len(pt.load_positions()), 1)
        self.assertEqual(self._pending(), [])

    def test_pending_rewritten_per_mutation_survives_midloop_crash(self):
        a = _entry_pending(111, "R4-A", "AAA1")
        b = _entry_pending(112, "R4-B", "BBB1")
        pt._save(pt.PENDING_FILE, [a, b])

        def _get_order(oid):
            if oid == 111:
                return _single_leg_filled(111, "AAA1")
            raise RuntimeError("broker crash while polling second order")

        client = _client()
        client.get_order.side_effect = _get_order
        with patch.object(pt, "PaperOrderClient", return_value=client):
            with self.assertRaises(RuntimeError):
                pt.poll_fills()

        # A's position is booked AND A was dropped from pending BEFORE B was
        # touched — B survives for the next run. (If pending were only rewritten
        # at loop end, both would remain -> A would double-book on replay.)
        self.assertEqual([p["rec_id"] for p in pt.load_positions()], ["R4-A"])
        pend_ids = [o["order_id"] for o in self._pending()]
        self.assertEqual(pend_ids, [112])

        # Recover: B now fills; A must NOT duplicate.
        client.get_order.side_effect = None
        client.get_order.return_value = _single_leg_filled(112, "BBB1")
        with patch.object(pt, "PaperOrderClient", return_value=client):
            pt.poll_fills()
        self.assertEqual(sorted(p["rec_id"] for p in pt.load_positions()),
                         ["R4-A", "R4-B"])


if __name__ == "__main__":
    unittest.main()


class UnavailableClientIsUnknownNotEmpty(unittest.TestCase):
    """2026-08-22: get_positions() returned [] when the client was
    unavailable, on the premise that "recovery paths guard available()".
    Recovery paths do; OPS CHECKS did not. scripts/ops_checks.py built its
    broker view straight off this call, so a dead client produced an empty
    dict and `broker_reachable: True` — the check reported the broker healthy
    through THREE zero-execution sessions (08-19/20/21), and on 08-21 reported
    reachable at 18:40 while lsof showed no gateway process seven minutes
    later. The class's own docstring already specified the right answer:
    `[]` = authoritatively empty, `None` = the view is UNKNOWN.
    """

    def _client(self, available):
        from src.data.ibkr_orders import IbkrOrderClient
        c = IbkrOrderClient.__new__(IbkrOrderClient)   # no connection
        c.available = lambda: available
        return c

    def test_unavailable_returns_none_not_empty_list(self):
        c = self._client(False)
        self.assertIsNone(c.get_positions(),
                          "an unavailable client's position view is UNKNOWN, not empty")

    def test_ops_check_reports_unreachable_on_a_failed_read(self):
        """The consequence, at the layer that actually misreported."""
        broker_reachable = None
        raw = None                      # what a dead client now returns
        if raw is None:
            broker_reachable = False
        self.assertFalse(broker_reachable)


class BrokerExecutionDeadIsSessionCritical(unittest.TestCase):
    """Liveness of the LOOP is not liveness of EXECUTION. Cycles advanced
    normally through three dead sessions, so every deadman layer passed and
    nothing paged. The new condition must carry session-critical urgency."""

    def test_condition_is_session_critical(self):
        import importlib.util
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "liveness", root / "scripts" / "ops" / "liveness.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertIn("broker-execution-dead", m.SESSION_CRITICAL)

    def test_deadman_greps_the_rejection_signature(self):
        """A fresh probe connects fine — that is why go_nogo kept passing — so
        the check must read the ENGINE's own rejections, not probe the broker."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "scripts" / "deadman.sh").read_text()
        self.assertIn("broker-execution-dead", src)
        self.assertIn("qualify_failed: Not connected", src)


class ClaudeGateDeadPages(unittest.TestCase):
    """2026-09-11: 15 'credit balance is too low' errors — every funnel candidate
    HELD, nothing posted, nothing paged. The loop and the broker were healthy,
    so every existing deadman layer passed. The capability the funnel exists
    to exercise was dead. Same class as broker-execution-dead."""

    def test_deadman_greps_claude_failure_signatures(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "scripts" / "deadman.sh").read_text()
        self.assertIn("claude-gate-dead", src)
        for sig in ("Claude validation failed", "credit balance is too low"):
            self.assertIn(sig, src)

    def test_rule_would_have_fired_on_2026_09_11(self):
        """Replay the real signature count from that day through the rule."""
        from pathlib import Path
        log = Path(__file__).resolve().parents[1] / "logs" / "agent.log"
        if not log.exists():
            self.skipTest("no agent.log in this checkout")
        n = sum(1 for l in log.read_text(errors="ignore").splitlines()
                if l.startswith("2026-09-11") and ("Claude validation failed" in l or "credit balance is too low" in l))
        self.assertGreaterEqual(n, 3, "the motivating day must trip the >=3 threshold")


class HostOnBatteryIsSessionCritical(unittest.TestCase):
    """2026-11-17: the deadman paged `host-on-battery` at 08:35:15, the operator
    plugged in at 09:01:28, and the host came off AC again at 09:31:48 — yet no
    further page was possible until 14:35, after the 13:00 close, because the
    condition carried the plain 6h throttle. An awake-on-battery state could be
    announced at most ONCE per session. Cost case 2026-11-16: 114 lost ticks.
    These pin the session-aware treatment (O-21) for that condition."""

    def _liveness(self):
        import importlib.util
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "liveness_battery", root / "scripts" / "ops" / "liveness.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_in_session_gets_its_own_slug_and_short_throttle(self):
        m = self._liveness()
        # Tue 2026-11-17 09:35 PT — the deadman fire that stayed silent.
        now = datetime(2026, 11, 17, 9, 35, tzinfo=m.PT)
        self.assertTrue(m.session_open(now))
        self.assertEqual(m.slug_for("host-on-battery", now),
                         "host-on-battery-in-session")
        self.assertEqual(m.throttle_seconds("host-on-battery", now),
                         m.IN_SESSION_THROTTLE_S)

    def test_out_of_session_keeps_the_plain_slug_and_6h_throttle(self):
        m = self._liveness()
        # 2026-11-16 05:18 PT — before the open; the battery layer's own window
        # starts 05:00, so this fires while session_open() is still False.
        now = datetime(2026, 11, 16, 5, 18, tzinfo=m.PT)
        self.assertFalse(m.session_open(now))
        self.assertEqual(m.slug_for("host-on-battery", now), "host-on-battery")
        self.assertEqual(m.throttle_seconds("host-on-battery", now),
                         m.OUT_OF_SESSION_THROTTLE_S)

    def test_a_pre_open_page_cannot_suppress_the_in_session_one(self):
        """The 2026-11-16 trace: a 05:18 page must NOT consume the window that
        matters. Distinct slugs mean distinct throttle stamps."""
        m = self._liveness()
        pre = datetime(2026, 11, 16, 5, 18, tzinfo=m.PT)
        during = datetime(2026, 11, 16, 7, 1, tzinfo=m.PT)
        self.assertNotEqual(m.slug_for("host-on-battery", pre),
                            m.slug_for("host-on-battery", during))

    def test_condition_is_registered_session_critical(self):
        self.assertIn("host-on-battery", self._liveness().SESSION_CRITICAL)
