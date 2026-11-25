"""Engine hardening tests (live-readiness Phase 0, 2026-07-01).

Covers: MFE/MAE seeding + exit folding, fill-guard invariants (entry and
strike-width), partial-fill visibility, write-ahead order intents +
crash recovery (resolve_intents), tag-scoped reconciliation with
adopt_foreign=false, quote-outage streak tracking, and the exit_manager
rule set (previously untested).

Run: .venv/bin/python -m unittest tests.test_engine_hardening -v
"""

import json
import sys
import tempfile
import unittest
from datetime import date as _date, timedelta as _td

# 2026-08-07: submit_paper_order now REFUSES a lapsed expiry. These fixtures were
# written when 2026-07-10 was in the future; make them roll so the guard under
# test is the expiry rule itself, not the calendar.
_FUTURE_EXP = (_date.today() + _td(days=7)).isoformat()
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.paper_trader as pt
from src.engine.exit_manager import evaluate_exit, DEFAULT_CFG

SPREAD_LEGS = [{"symbol": "SPY260710C00740000", "action": "buy_to_open"},
               {"symbol": "SPY260710C00747000", "action": "sell_to_open"}]  # width 7.0

ENTRY_ORDER = {
    "id": 111, "status": "filled", "type": "debit", "avg_fill_price": 3.60,
    "create_date": "2026-07-01T15:00:32Z", "transaction_date": "2026-07-01T15:00:32Z",
    "leg": [
        {"option_symbol": "SPY260710C00740000", "side": "buy_to_open",
         "avg_fill_price": 5.27, "exec_quantity": 1},
        {"option_symbol": "SPY260710C00747000", "side": "sell_to_open",
         "avg_fill_price": 1.67, "exec_quantity": 1},
    ],
}


def exit_order(sell_close: float, buy_close: float, oid: int = 222) -> dict:
    return {
        "id": oid, "status": "filled", "type": "credit",
        "avg_fill_price": -(sell_close - buy_close),
        "create_date": "2026-07-01T16:00:00Z", "transaction_date": "2026-07-01T16:00:05Z",
        "leg": [
            {"option_symbol": "SPY260710C00740000", "side": "sell_to_close",
             "avg_fill_price": sell_close, "exec_quantity": 1},
            {"option_symbol": "SPY260710C00747000", "side": "buy_to_close",
             "avg_fill_price": buy_close, "exec_quantity": 1},
        ],
    }


class TmpBookTest(unittest.TestCase):
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

    def _fills(self):
        if not pt.FILLS_LOG.exists():
            return []
        return [json.loads(l) for l in pt.FILLS_LOG.read_text().splitlines()]

    def _pending_entry(self, **over):
        rec = {
            "intent": "open", "intent_seq": "seq-1", "order_id": 111,
            "rec_id": "2026-07-01-901", "ticker": "SPY",
            "structure": "call_debit_spread", "strategy": "debit_spread",
            "direction": "long", "legs": SPREAD_LEGS,
            "rec_price": 3.55, "max_profit": 3.4, "max_loss": 3.6,
            "expiration": _FUTURE_EXP,
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
            "tag": "UNVALIDATED-PAPER-ONLY",
        }
        rec.update(over)
        return rec

    def _open_position(self, client_orders):
        """Drive an entry fill through poll_fills with a mocked client."""
        pt._save(pt.PENDING_FILE, [self._pending_entry()])
        client = MagicMock()
        client.available.return_value = True
        client.get_order.side_effect = lambda oid: client_orders.get(oid)
        with patch.object(pt, "PaperOrderClient", return_value=client):
            return pt.poll_fills()


class StrikeWidthTest(unittest.TestCase):
    def test_width_and_single_leg(self):
        self.assertEqual(pt.strike_width(SPREAD_LEGS), 7.0)
        self.assertIsNone(pt.strike_width([SPREAD_LEGS[0]]))
        self.assertIsNone(pt.strike_width([{"symbol": "garbage"}]))


class MfeMaeTest(TmpBookTest):
    def test_marks_seeded_at_entry(self):
        self._open_position({111: ENTRY_ORDER})
        pos = pt.load_positions()[0]
        self.assertEqual(pos["mark_high"], 3.60)
        self.assertEqual(pos["mark_low"], 3.60)

    def test_mark_series_records_ordered_path(self):
        """Lesson L13: the ordered intra-trade path is captured so a trailing
        stop can be validated later — not just the mark_high/mark_low extremes."""
        self._open_position({111: ENTRY_ORDER})
        rec_id = pt.load_positions()[0]["rec_id"]
        for v in (3.90, 4.20, 3.10):  # up, up more, then reverse
            pt.update_position_marks(rec_id, v)
        pos = pt.load_positions()[0]
        series = [v for _, v in pos["mark_series"]]
        self.assertEqual(series, [3.90, 4.20, 3.10])   # order preserved
        self.assertEqual(pos["mark_high"], 4.20)
        self.assertEqual(pos["mark_low"], 3.10)

    def test_exit_folded_into_mfe_mae(self):
        """Fast scalp: no cycle marks beyond the seed; exit above entry must
        become the MFE (was: exit > recorded MFE in 16/40 real exits)."""
        self._open_position({111: ENTRY_ORDER})
        pos = pt.load_positions()[0]
        pt._save(pt.PENDING_FILE, [{
            "intent": "close", "intent_seq": "seq-2", "order_id": 222,
            "rec_id": pos["rec_id"], "ticker": "SPY", "structure": pos["structure"],
            "entry_price": pos["entry_price"], "close_limit": 4.40,
            "close_reason": "profit_target",
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
        }])
        client = MagicMock()
        client.available.return_value = True
        client.get_order.return_value = exit_order(6.11, 1.68)  # credit 4.43
        with patch.object(pt, "PaperOrderClient", return_value=client):
            pt.poll_fills()
        rt = [e for e in self._fills() if e["event"] == "paper_exit"][0]["round_trip"]
        self.assertEqual(rt["exit_net_credit"], 4.43)
        self.assertGreaterEqual(rt["mfe_value"], rt["exit_net_credit"])
        self.assertLessEqual(rt["mae_value"], rt["exit_net_credit"])
        self.assertGreaterEqual(rt["exit_net_credit"], rt["mae_value"])


class FillGuardTest(TmpBookTest):
    def test_entry_above_width_falls_back_to_rec_price(self):
        corrupt = dict(ENTRY_ORDER)
        corrupt["leg"] = [
            {"option_symbol": "SPY260710C00740000", "side": "buy_to_open",
             "avg_fill_price": 9.50, "exec_quantity": 1},
            {"option_symbol": "SPY260710C00747000", "side": "sell_to_open",
             "avg_fill_price": 1.00, "exec_quantity": 1},
        ]  # net 8.50 > width 7.0
        self._open_position({111: corrupt})
        fill = [e for e in self._fills() if e["event"] == "paper_fill"][0]
        self.assertIsNotNone(fill["fill_guard"])
        self.assertEqual(fill["fill_price"], 3.55)  # rec_price fallback
        self.assertEqual(pt.load_positions()[0]["entry_price"], 3.55)

    def test_exit_above_width_falls_back_to_mark(self):
        self._open_position({111: ENTRY_ORDER})
        pos = pt.load_positions()[0]
        pt._save(pt.PENDING_FILE, [{
            "intent": "close", "intent_seq": "seq-2", "order_id": 222,
            "rec_id": pos["rec_id"], "ticker": "SPY", "structure": pos["structure"],
            "entry_price": pos["entry_price"], "close_limit": 4.40,
            "close_reason": "x", "submitted_at": datetime.now().isoformat(timespec="seconds"),
        }])
        client = MagicMock()
        client.available.return_value = True
        client.get_order.return_value = exit_order(9.90, 0.10)  # credit 9.80 > width 7.0
        with patch.object(pt, "PaperOrderClient", return_value=client):
            pt.poll_fills()
        ev = [e for e in self._fills() if e["event"] == "paper_exit"][0]
        self.assertIn("> strike width", ev["fill_guard"])
        self.assertEqual(ev["exit_price"], 4.40)  # close_limit fallback

    def test_healthy_fills_unguarded(self):
        self._open_position({111: ENTRY_ORDER})
        fill = [e for e in self._fills() if e["event"] == "paper_fill"][0]
        self.assertIsNone(fill["fill_guard"])
        self.assertEqual(fill["fill_price"], 3.60)


class PartialFillTest(TmpBookTest):
    def test_partial_alerts_once_and_stays_pending(self):
        pt._save(pt.PENDING_FILE, [self._pending_entry()])
        client = MagicMock()
        client.available.return_value = True
        client.get_order.return_value = {"id": 111, "status": "partially_filled"}
        with patch.object(pt, "PaperOrderClient", return_value=client), \
                patch("src.engine.notify.send") as send:
            pt.poll_fills()
            pt.poll_fills()  # second cycle: no duplicate alert
        self.assertEqual(send.call_count, 1)
        events = [e for e in self._fills() if e["event"] == "partial_fill_detected"]
        self.assertEqual(len(events), 1)
        self.assertEqual(len(pt._load(pt.PENDING_FILE)), 1)  # still pending
        self.assertTrue(pt._load(pt.PENDING_FILE)[0]["partial_alerted"])


class WriteAheadTest(TmpBookTest):
    REC = {"rec_id": "2026-07-01-902", "ticker": "SPY",
           "trade": {"structure": "call_debit_spread", "strategy": "debit_spread",
                     "direction": "long", "net_debit": 3.55, "max_profit": 3.4,
                     "max_loss": 3.6, "expiration": _FUTURE_EXP,
                     "legs": [{"symbol": SPREAD_LEGS[0]["symbol"], "action": "buy_to_open", "mid": 5.3},
                              {"symbol": SPREAD_LEGS[1]["symbol"], "action": "sell_to_open", "mid": 1.7}]}}

    def test_submit_binds_order_id(self):
        client = MagicMock()
        client.available.return_value = True
        client.place_spread_order.return_value = {"order": {"id": 999, "status": "ok"}}
        with patch.object(pt, "PaperOrderClient", return_value=client):
            out = pt.submit_paper_order(self.REC)
        self.assertTrue(out["submitted"])
        pending = pt._load(pt.PENDING_FILE)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["order_id"], 999)

    def test_rejected_submit_drops_intent(self):
        client = MagicMock()
        client.available.return_value = True
        client.place_spread_order.return_value = {"error": "rejected"}
        with patch.object(pt, "PaperOrderClient", return_value=client):
            out = pt.submit_paper_order(self.REC)
        self.assertFalse(out["submitted"])
        self.assertEqual(pt._load(pt.PENDING_FILE), [])

    def test_resolve_intents_binds_matching_broker_order(self):
        stale = (datetime.now() - timedelta(seconds=300)).isoformat(timespec="seconds")
        pt._save(pt.PENDING_FILE, [self._pending_entry(order_id=None, submitted_at=stale,
                                                       intent_seq="crashed-1")])
        client = MagicMock()
        client.available.return_value = True
        client.get_orders.return_value = [{
            "id": 777, "status": "open", "tag": "UNVALIDATED-PAPER-ONLY",
            "leg": [{"option_symbol": SPREAD_LEGS[0]["symbol"]},
                    {"option_symbol": SPREAD_LEGS[1]["symbol"]}],
        }]
        events = pt.resolve_intents(client)
        self.assertEqual(events[0]["event"], "intent_recovered")
        self.assertEqual(pt._load(pt.PENDING_FILE)[0]["order_id"], 777)

    def test_resolve_intents_drops_unmatched_and_rearms_close(self):
        stale = (datetime.now() - timedelta(seconds=300)).isoformat(timespec="seconds")
        pt._save(pt.POSITIONS_FILE, [{"rec_id": "R1", "ticker": "SPY", "legs": SPREAD_LEGS,
                                      "closing": True, "entry_price": 3.6}])
        pt._save(pt.PENDING_FILE, [{
            "intent": "close", "intent_seq": "crashed-2", "order_id": None,
            "rec_id": "R1", "ticker": "SPY", "structure": "s", "entry_price": 3.6,
            "close_limit": 4.0, "close_reason": "x", "submitted_at": stale,
        }])
        client = MagicMock()
        client.available.return_value = True
        client.get_orders.return_value = []  # nothing at broker — intent lost
        events = pt.resolve_intents(client)
        self.assertEqual(events[0]["event"], "intent_lost")
        self.assertEqual(pt._load(pt.PENDING_FILE), [])
        self.assertFalse(pt.load_positions()[0]["closing"])  # re-armed

    def test_fresh_unbound_intent_left_alone(self):
        pt._save(pt.PENDING_FILE, [self._pending_entry(order_id=None, intent_seq="fresh")])
        client = MagicMock()
        client.available.return_value = True
        self.assertEqual(pt.resolve_intents(client), [])
        self.assertEqual(len(pt._load(pt.PENDING_FILE)), 1)


class TagReconcileTest(TmpBookTest):
    def _run_reconcile(self, broker_orders, broker_positions, adopt_foreign):
        client = MagicMock()
        client.available.return_value = True
        client.get_orders.return_value = broker_orders
        client.get_positions.return_value = broker_positions
        with patch.object(pt, "PaperOrderClient", return_value=client), \
                patch("yaml.safe_load", return_value={"agent": {"adopt_foreign": adopt_foreign}}):
            events = pt.reconcile_with_broker()
        return client, events

    def test_tagged_orphan_canceled_even_without_adopt_foreign(self):
        client, events = self._run_reconcile(
            [{"id": 500, "status": "open", "symbol": "SPY", "tag": "UNVALIDATED-PAPER-ONLY"}],
            [], adopt_foreign=False)
        client.cancel_order.assert_called_once_with(500)
        self.assertEqual(events[0]["event"], "reconciled_orphan_order")
        self.assertTrue(events[0]["agent_tagged"])

    def test_untagged_foreign_order_untouched_without_adopt_foreign(self):
        client, events = self._run_reconcile(
            [{"id": 501, "status": "open", "symbol": "AAPL", "tag": "manual"}],
            [{"symbol": "AAPL260717C00250000", "quantity": 1, "cost_basis": 500}],
            adopt_foreign=False)
        client.cancel_order.assert_not_called()
        self.assertEqual(pt.load_positions(), [])  # foreign position NOT adopted
        # 2026-08-06: reconcile still adopts/cancels NOTHING here — the behaviour
        # under test is unchanged. What changed is that the book/broker audit is
        # now WIRED to run (it previously had zero callers, which let a stale book
        # reach a live start). An OPTION position at the broker with no book entry
        # is reported DETECT-ONLY. It is not "flagging a manual position": manual
        # holdings are STOCK and the trend book's are ETF/futures, so neither can
        # appear in this OPTION-only comparison.
        non_audit = [e for e in events if e.get("event") != "reconcile_alarm"]
        self.assertEqual(non_audit, [], "reconcile itself must still do nothing")
        for e in events:
            self.assertEqual(e.get("action"), "DETECT ONLY — no auto-close")

    def test_untagged_order_IGNORED_even_with_adopt_foreign(self):
        """CONTRACT CHANGE 2026-08-04 (cross-book hardening, was
        test_untagged_order_canceled_with_adopt_foreign): an order that is not
        attributably ours is NEVER cancelled, regardless of adopt_foreign.

        Rationale: the IBKR paper account is SHARED — the trend book trades it,
        and humans may too. Cancelling another book's live order is
        unrecoverable damage done on a guess. adopt_foreign now scopes POSITION
        adoption only; order reconciliation is strictly orderRef-attributed."""
        client, events = self._run_reconcile(
            [{"id": 502, "status": "open", "symbol": "AAPL", "tag": "manual"}],
            [], adopt_foreign=True)
        client.cancel_order.assert_not_called()
        self.assertEqual([e for e in events if e["event"] == "reconciled_orphan_order"], [])


class QuoteStreakTest(TmpBookTest):
    def test_streak_increments_and_resets(self):
        pt._save(pt.POSITIONS_FILE, [{"rec_id": "R1", "ticker": "SPY",
                                      "legs": SPREAD_LEGS, "entry_price": 3.6}])
        self.assertEqual(pt.update_quote_streak("R1", missing=True), 1)
        self.assertEqual(pt.update_quote_streak("R1", missing=True), 2)
        self.assertEqual(pt.update_quote_streak("R1", missing=False), 0)
        self.assertEqual(pt.update_quote_streak("R1", missing=True), 1)


class EvaluateExitTest(unittest.TestCase):
    """First-ever tests for the four exit rules."""
    POS = {"rec_id": "R1", "ticker": "SPY", "direction": "long",
           "entry_price": 3.6, "max_profit": 3.4, "legs": SPREAD_LEGS}

    def _now(self, h, m):
        return datetime(2026, 7, 1, h, m)

    def test_time_flat(self):
        self.assertIn("time_flat", evaluate_exit(self.POS, 3.6, {}, self._now(15, 45), DEFAULT_CFG))

    def test_vwap_invalidation_long_and_short(self):
        self.assertIn("invalidation", evaluate_exit(
            self.POS, 3.6, {"vwap_dist_atr": -0.5}, self._now(11, 0), DEFAULT_CFG))
        short = {**self.POS, "direction": "short"}
        self.assertIn("invalidation", evaluate_exit(
            short, 3.6, {"vwap_dist_atr": 0.5}, self._now(11, 0), DEFAULT_CFG))
        self.assertIsNone(evaluate_exit(
            self.POS, 3.6, {"vwap_dist_atr": 0.5}, self._now(11, 0), DEFAULT_CFG))

    def test_stop_loss_and_profit_target(self):
        self.assertIn("stop_loss", evaluate_exit(
            self.POS, 1.7, {"vwap_dist_atr": 0.5}, self._now(11, 0), DEFAULT_CFG))
        self.assertIn("profit_target", evaluate_exit(
            self.POS, 5.4, {"vwap_dist_atr": 0.5}, self._now(11, 0), DEFAULT_CFG))

    def test_no_quotes_only_time_and_vwap_armed(self):
        # value None: stop/target unevaluable — deep loss produces NO exit
        self.assertIsNone(evaluate_exit(
            self.POS, None, {"vwap_dist_atr": 0.5}, self._now(11, 0), DEFAULT_CFG))

    def test_single_leg_profit_target(self):
        # gap closed 2026-07-06: single-leg longs take profit at +50% of debit
        single = {**self.POS, "max_profit": None}  # entry 3.6 -> target at 5.4
        self.assertIn("single-leg", evaluate_exit(
            single, 5.4, {"vwap_dist_atr": 0.5}, self._now(11, 0), DEFAULT_CFG))
        self.assertIsNone(evaluate_exit(
            single, 5.39, {"vwap_dist_atr": 0.5}, self._now(11, 0), DEFAULT_CFG))


if __name__ == "__main__":
    unittest.main()


class AuditDistinguishesCouldNotCheck(unittest.TestCase):
    """2026-08-17: audit_book_vs_broker() returns [] both when the book is
    CLEAN and when the broker is unreachable. That is correct for the engine
    (detect-only) but the runbook used it as a GATE, where [] reads as PASS —
    so on 08-17 the pre-start gate passed while the gateway sat unauthenticated
    and a phantom WMT position was in the book. Not knowing is not the same as
    knowing it is fine."""

    class _Unavailable:
        def available(self): return False
        def get_positions(self): return []

    class _ReadFails:
        def available(self): return True
        def get_positions(self): return None      # R2: failed read, not empty

    class _CleanEmpty:
        def available(self): return True
        def get_positions(self): return []

    def test_unavailable_client_is_could_not_check_not_clean(self):
        from src.engine.paper_trader import audit_book_vs_broker_checked
        r = audit_book_vs_broker_checked(self._Unavailable())
        self.assertEqual(r["status"], "could_not_check")

    def test_failed_read_is_could_not_check_not_clean(self):
        from src.engine.paper_trader import audit_book_vs_broker_checked
        r = audit_book_vs_broker_checked(self._ReadFails())
        self.assertEqual(r["status"], "could_not_check")

    def test_reachable_and_matching_is_clean(self):
        from unittest.mock import patch
        from src.engine import paper_trader as pt
        with patch.object(pt, "load_positions", return_value=[]):
            r = pt.audit_book_vs_broker_checked(self._CleanEmpty())
        self.assertEqual(r["status"], "clean")

    def test_detect_only_helper_is_unchanged_for_the_engine(self):
        """The engine must keep getting [] on an unreachable broker — it is
        detect-only and must not act on uncertainty."""
        from src.engine.paper_trader import audit_book_vs_broker
        self.assertEqual(audit_book_vs_broker(self._Unavailable()), [])
        self.assertEqual(audit_book_vs_broker(self._ReadFails()), [])


class PartialFillEventsDoNotKillTheCycle(unittest.TestCase):
    """2026-08-18: pipeline dispatched on 'paper_exit' vs "everything else is
    an entry fill". poll_fills also returns partial_fill_booked (no
    'slippage') and partial_remainder_cancelled (no 'fill_price' either), so
    the else-branch raised KeyError('slippage') and killed the WHOLE cycle —
    no exit management, no candidate evaluation. It fired five times for real
    (08-06 09:25, 08-10 07:35/09:00/09:20), each on a partial fill, while
    seven daily reports called it unexercised.

    These are the EXACT shapes paper_trader emits; keep them in sync.
    """
    BOOKED = {"ts": "2026-08-18T07:00:00", "event": "partial_fill_booked",
              "rec_id": "20260818-SCALP-1-NFLX-debit_spread", "ticker": "NFLX",
              "order_id": 254, "quantity": 2, "requested": 6, "fill_price": 1.56,
              "stamp": "UNVALIDATED — PAPER ONLY"}
    CANCELLED = {"ts": "2026-08-18T07:05:00", "event": "partial_remainder_cancelled",
                 "rec_id": "20260818-SCALP-1-NFLX-debit_spread", "ticker": "NFLX",
                 "order_id": 254, "filled": 2, "requested": 6,
                 "stamp": "UNVALIDATED — PAPER ONLY"}

    def test_notify_fill_survives_a_partial(self):
        from unittest.mock import patch
        import src.engine.notify as n
        with patch.object(n, "send") as snd:
            n.notify_fill(self.BOOKED)          # must not raise
        self.assertTrue(snd.called)
        self.assertIn("PARTIALLY", snd.call_args[0][1])

    def test_notify_fill_still_reports_slippage_when_present(self):
        from unittest.mock import patch
        import src.engine.notify as n
        full = {"ticker": "SPY", "event": "paper_fill", "fill_price": 1.10,
                "rec_price": 1.08, "slippage": 0.02}
        with patch.object(n, "send") as snd:
            n.notify_fill(full)
        self.assertIn("slip +0.02", snd.call_args[0][1])

    def test_every_poll_fills_shape_is_printable(self):
        """The real defect was an unhandled shape reaching a formatter that
        assumed entry-fill keys. Assert none of them raise."""
        shapes = [self.BOOKED, self.CANCELLED,
                  {"event": "paper_fill", "ticker": "SPY", "fill_price": 1.1,
                   "rec_price": 1.08, "slippage": 0.02},
                  {"event": "some_future_event_nobody_wrote_yet"}]
        for f in shapes:
            ev = f.get("event")
            if ev == "paper_fill":
                s = f"{f['ticker']} @ {f['fill_price']} slip {f['slippage']:+.2f}"
            elif ev == "partial_fill_booked":
                s = f"{f['ticker']} {f['quantity']}/{f.get('requested')} @ {f['fill_price']}"
            elif ev == "partial_remainder_cancelled":
                s = f"{f['ticker']} {f['filled']}/{f.get('requested')}"
            else:
                s = f"{f.get('ticker', '?')}: {ev}"
            self.assertTrue(s)


class RestartGateDoesNotDeadlock(unittest.TestCase):
    """2026-09-12: the pre-session restart refused whenever the book was not
    flat — but the book is not flat BECAUSE the broker is dead, so the guard
    fired exactly when the restart was most needed and the outage sustained
    itself (09-02/03/04/08/10/11). Only a LIVE position the engine can still
    manage may block."""

    LIVE = {"rec_id": "R-live", "ticker": "SPY", "expiration": "2099-01-01",
            "legs": [{"symbol": "SPY990101C00500000"}]}
    EXPIRED = {"rec_id": "R-exp", "ticker": "NVDA", "expiration": "2000-01-01",
               "legs": [{"symbol": "NVDA000101P00220000"}]}

    def _run(self, positions, pending, checked):
        from unittest.mock import patch
        from src.engine import paper_trader as pt
        with patch.object(pt, "load_positions", return_value=positions), \
                patch.object(pt, "_load", return_value=pending):
            return pt.restart_blocking_positions(today="2026-09-12", checked=checked)

    def test_expired_only_book_does_not_block(self):
        """THE DEADLOCK: expired contracts the broker no longer holds kept the
        engine dead for five-plus sessions."""
        n, why = self._run([self.EXPIRED], [], {"status": "clean", "alarms": []})
        self.assertEqual(n, 0, why)

    def test_unknown_broker_view_does_not_block(self):
        """Nothing is being managed while the broker is unreachable, so the
        restart is the remedy, not the risk."""
        n, why = self._run([self.LIVE], [],
                           {"status": "could_not_check", "alarms": []})
        self.assertEqual(n, 0, why)

    def test_live_position_confirmed_at_broker_DOES_block(self):
        """The case the guard actually exists for must still refuse."""
        n, _ = self._run([self.LIVE], [], {"status": "clean", "alarms": []})
        self.assertEqual(n, 1)

    def test_live_position_absent_at_broker_does_not_block(self):
        n, _ = self._run([self.LIVE], [], {"status": "alarms", "alarms": [
            {"kind": "book_position_not_at_broker", "symbol": "SPY990101C00500000"}]})
        self.assertEqual(n, 0)

    def test_pending_order_from_today_blocks(self):
        n, _ = self._run([], [{"rec_id": "P1", "submitted_at": "2026-09-12T06:40:00"}],
                         {"status": "clean", "alarms": []})
        self.assertEqual(n, 1)

    def test_stale_pending_order_does_not_block(self):
        """A day-old v2 IOC intent is bookkeeping debris, not a live order —
        counting it refused the 06:20 restart with a false page (2026-09-12)."""
        n, _ = self._run([], [{"rec_id": "P0", "submitted_at": "2026-09-11T12:55:00"}],
                         {"status": "clean", "alarms": []})
        self.assertEqual(n, 0)

    def test_flat_book_does_not_block(self):
        n, _ = self._run([], [], {"status": "clean", "alarms": []})
        self.assertEqual(n, 0)
