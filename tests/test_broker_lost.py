"""L20 circuit-breaker tests: >=3 consecutive close rejects flags broker_lost,
stops retries; the exit manager and submit path both skip flagged positions.

Run: .venv/bin/python -m unittest tests.test_broker_lost -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.engine import paper_trader as pt  # noqa: E402


class BrokerLostBreakerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._patches = [
            patch.object(pt, "POSITIONS_FILE", base / "paper_positions.json"),
            patch.object(pt, "PENDING_FILE", base / "paper_orders.json"),
        ]
        for p in self._patches:
            p.start()
        self.position = {"rec_id": "2026-07-13-003", "ticker": "SMCI",
                         "quantity": 10, "entry_price": 0.86, "closing": True,
                         "legs": [{"symbol": "SMCI260717P00028500",
                                   "action": "buy_to_open"}]}
        pt.POSITIONS_FILE.write_text(json.dumps([self.position]))

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _poll_one_reject(self):
        pt.PENDING_FILE.write_text(json.dumps([
            {"intent": "close", "rec_id": "2026-07-13-003", "ticker": "SMCI",
             "order_id": 1, "submitted_at": "2026-07-14T12:00:00"}]))
        fake_client = unittest.mock.MagicMock()
        fake_client.available.return_value = True
        fake_client.get_order.return_value = {"status": "rejected"}
        with patch.object(pt, "PaperOrderClient", return_value=fake_client):
            pt.poll_fills()

    def test_three_rejects_trip_breaker(self):
        for i in range(3):
            self._poll_one_reject()
            q = json.loads(pt.POSITIONS_FILE.read_text())[0]
            if i < 2:
                self.assertFalse(q.get("broker_lost"), f"tripped too early at {i+1}")
        self.assertTrue(q["broker_lost"])
        self.assertEqual(q["close_reject_count"], 3)
        self.assertFalse(q["closing"])  # re-armed but suppressed via flag

    def test_flagged_position_never_resubmits(self):
        pos = dict(self.position, broker_lost=True)
        result = pt.submit_close_order(pos, 0.95, "vwap_invalidation")
        self.assertIsNone(result)


class FastRetryTest(unittest.TestCase):
    """2026-07-14 fast retry: rejected/failed closes retry at watchdog speed
    instead of waiting for the next 5-min cycle (decision rules unchanged)."""

    def test_submit_retries_once_on_transient_failure(self):
        from unittest.mock import MagicMock
        position = {"rec_id": "r1", "ticker": "SPY", "structure": "debit spread",
                    "quantity": 2, "entry_price": 1.0, "direction": "long",
                    "legs": [{"symbol": "A", "action": "buy_to_open"},
                             {"symbol": "B", "action": "sell_to_open"}]}
        client = MagicMock()
        client.available.return_value = True
        client.place_spread_order.side_effect = [
            {"error": "An error occurred while communicating with the backend."},
            {"order": {"id": 99, "status": "pending"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.object(pt, "POSITIONS_FILE", base / "p.json"), \
                 patch.object(pt, "PENDING_FILE", base / "o.json"), \
                 patch.object(pt, "INTENT_FILE", base / "i.jsonl", create=True), \
                 patch.object(pt, "PaperOrderClient", return_value=client), \
                 patch("time.sleep"):
                pt.POSITIONS_FILE.write_text(json.dumps([dict(position)]))
                result = pt.submit_close_order(position, 0.95, "time_flat (test)")
        self.assertIsNotNone(result)
        self.assertEqual(client.place_spread_order.call_count, 2)

    def test_watchdog_fast_retries_rejected_close(self):
        from src.engine import watchdog as wd
        import threading
        p = {"rec_id": "r2", "ticker": "SPY", "quantity": 2, "entry_price": 1.0,
             "close_reject_count": 1, "close_reason": "time_flat (day-trading mandate)",
             "closing": False, "max_profit": 2.0,
             "legs": [{"symbol": "A", "action": "buy_to_open"}]}
        dog = wd.RiskWatchdog(threading.Lock(), {"risk": {"paper_exits": {}}})
        dog.quotes = {"A": (1.0, 1.1)}   # value ~1.05: neither stop nor target
        submitted = []
        with patch.object(wd, "load_positions", return_value=[dict(p)]), \
             patch.object(wd, "submit_close_order",
                          side_effect=lambda pos, v, r: submitted.append(r) or {"ok": 1}), \
             patch.object(wd, "update_position_marks"):
            dog._check_positions()
        self.assertEqual(len(submitted), 1)
        self.assertTrue(submitted[0].startswith("fast_retry(1)"), submitted)

    def test_watchdog_respects_breaker(self):
        from src.engine import watchdog as wd
        import threading
        p = {"rec_id": "r3", "ticker": "SPY", "quantity": 2, "entry_price": 1.0,
             "close_reject_count": 3, "broker_lost": True,
             "close_reason": "time_flat", "closing": False, "max_profit": 2.0,
             "legs": [{"symbol": "A", "action": "buy_to_open"}]}
        dog = wd.RiskWatchdog(threading.Lock(), {"risk": {"paper_exits": {}}})
        dog.quotes = {"A": (1.0, 1.1)}
        submitted = []
        with patch.object(wd, "load_positions", return_value=[dict(p)]), \
             patch.object(wd, "submit_close_order",
                          side_effect=lambda pos, v, r: submitted.append(r) or {"ok": 1}), \
             patch.object(wd, "update_position_marks"):
            dog._check_positions()
        self.assertEqual(submitted, [])


if __name__ == "__main__":
    unittest.main()
