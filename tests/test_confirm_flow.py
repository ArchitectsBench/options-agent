"""Recommend-and-confirm workflow tests (Phase L step 3, 2026-07-02).

Covers: queue -> notify -> confirm/decline/expire transitions, execution of
confirmed entries with risk RE-GATING at execution time, kill-switch hold,
idempotency (no double execution), the live path refusing (NotImplemented),
and the LiveOrderClient refusing construction against today's repo state.

Run: .venv/bin/python -m unittest tests.test_confirm_flow -v
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.confirm as cf
import src.engine.live_gate as lg
import src.engine.risk as risk

REC = {"rec_id": "2026-07-02-C1", "ticker": "SPY",
       "trade": {"structure": "call_debit_spread", "direction": "long",
                 "net_debit": 3.6, "max_loss": 3.6, "max_profit": 3.4,
                 "expiration": "2026-07-10",
                 "legs": [{"symbol": "SPY260710C00740000", "action": "buy_to_open", "mid": 5.3},
                          {"symbol": "SPY260710C00747000", "action": "sell_to_open", "mid": 1.7}]}}

CFG = {"risk": {"account_size": 50000, "max_risk_per_trade_pct": 2.0,
                "max_open_recommendations": 3, "max_recommendations_per_day": 6,
                "max_daily_loss_pct": 3.0, "max_contracts": 10}}

ALLOW = {"allowed": True, "quantity": 2, "reason": "ok", "sizing": {}}
BLOCK = {"allowed": False, "quantity": 0, "reason": "daily-loss breaker tripped (test)"}


class TmpQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._orig = (cf.CONFIRM_FILE, lg.KILL_SWITCH_FILE)
        cf.CONFIRM_FILE = base / "pending_confirmations.json"
        lg.KILL_SWITCH_FILE = base / "KILL_SWITCH"
        self.submitted = []

    def tearDown(self):
        cf.CONFIRM_FILE, lg.KILL_SWITCH_FILE = self._orig
        self.tmp.cleanup()

    def _submit(self, rec, tag="UNVALIDATED-PAPER-ONLY", quantity=1):
        self.submitted.append((rec["rec_id"], quantity))
        return {"submitted": True, "order_id": 7}

    def _queue(self, expiry_min=15):
        with patch("src.engine.notify.send") as send:
            entry = cf.queue_recommendation(dict(REC), 2, expiry_min)
        return entry, send


class QueueTransitionsTest(TmpQueueTest):
    def test_queue_notifies_with_confirm_command(self):
        entry, send = self._queue()
        self.assertEqual(entry["status"], "pending")
        body = send.call_args[0][1]
        self.assertIn("trader confirm 2026-07-02-C1", body)
        self.assertEqual(len(cf.list_entries()), 1)

    def test_confirm_then_decline_rejected(self):
        self._queue()
        self.assertIn("confirmed", cf.confirm("2026-07-02-C1"))
        self.assertIn("is confirmed, not pending", cf.decline("2026-07-02-C1"))

    def test_confirm_unknown_and_expired(self):
        self.assertIn("not found", cf.confirm("nope"))
        self._queue(expiry_min=15)
        # force the deadline into the past
        entries = json.loads(cf.CONFIRM_FILE.read_text())
        entries[0]["expires_at"] = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
        cf.CONFIRM_FILE.write_text(json.dumps(entries))
        self.assertIn("EXPIRED", cf.confirm("2026-07-02-C1"))
        self.assertEqual(cf.list_entries(), [])  # no longer active

    def test_expire_stale_sweep(self):
        self._queue(expiry_min=0)
        expired = cf.expire_stale()
        self.assertEqual(expired, ["2026-07-02-C1"])


class ProcessQueueTest(TmpQueueTest):
    def test_confirmed_executes_once_with_regated_quantity(self):
        self._queue()
        cf.confirm("2026-07-02-C1")
        with patch.object(risk, "gate_new_entry", return_value=dict(ALLOW)):
            ev1 = cf.process_queue(CFG, self._submit)
            ev2 = cf.process_queue(CFG, self._submit)  # idempotent
        self.assertEqual(self.submitted, [("2026-07-02-C1", 2)])
        self.assertEqual(ev1[0]["event"], "confirmation_executed")
        self.assertEqual(ev2, [])
        entries = json.loads(cf.CONFIRM_FILE.read_text())
        self.assertEqual(entries[0]["status"], "executed")

    def test_risk_drift_blocks_at_execution(self):
        """Breaker trips between confirm and execution -> NO order, notified."""
        self._queue()
        cf.confirm("2026-07-02-C1")
        with patch.object(risk, "gate_new_entry", return_value=dict(BLOCK)), \
                patch("src.engine.notify.send") as send:
            events = cf.process_queue(CFG, self._submit)
        self.assertEqual(self.submitted, [])
        self.assertEqual(events[0]["event"], "confirmation_blocked_by_risk")
        self.assertEqual(send.call_count, 1)
        entries = json.loads(cf.CONFIRM_FILE.read_text())
        self.assertEqual(entries[0]["status"], "blocked_by_risk")

    def test_kill_switch_holds_execution(self):
        self._queue()
        cf.confirm("2026-07-02-C1")
        lg.engage_kill_switch("halt")
        events = cf.process_queue(CFG, self._submit)
        self.assertEqual(self.submitted, [])
        self.assertEqual(events[0]["event"], "confirmation_held_kill_switch")
        # still confirmed — executes once the switch is released
        lg.release_kill_switch()
        with patch.object(risk, "gate_new_entry", return_value=dict(ALLOW)):
            cf.process_queue(CFG, self._submit)
        self.assertEqual(self.submitted, [("2026-07-02-C1", 2)])

    def test_live_mode_path_not_implemented(self):
        self._queue()
        cf.confirm("2026-07-02-C1")
        with patch.object(risk, "gate_new_entry", return_value=dict(ALLOW)):
            with self.assertRaises(NotImplementedError):
                cf.process_queue(CFG, self._submit, mode="live")
        self.assertEqual(self.submitted, [])


class LiveClientTest(unittest.TestCase):
    def test_live_client_refuses_today(self):
        """Against the real repo state, constructing the live client MUST fail."""
        from src.data.tradier_live import LiveOrderClient, LiveSafetyError
        with self.assertRaises(LiveSafetyError):
            LiveOrderClient()

    def test_request_path_unimplemented_even_if_constructed(self):
        from src.data import tradier_live as tl
        client = tl.LiveOrderClient.__new__(tl.LiveOrderClient)  # bypass ctor for the test
        with patch.object(tl, "assert_live_safety"):
            with self.assertRaises(NotImplementedError):
                client._request("GET", "/x")


if __name__ == "__main__":
    unittest.main()
