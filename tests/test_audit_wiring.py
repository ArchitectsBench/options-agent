"""The 08-06 fixes: detection that nothing calls is not detection.

audit_book_vs_broker() existed but had ZERO callers — it only ran when a human
invoked it, which is how a stale book (7 phantom contracts) reached a live
engine start. These tests pin the WIRING, not the function's existence.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.engine.paper_trader as pt

SYM = "GOOGL260805C00380000"


class StartupAuditWiring(unittest.TestCase):
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

    def test_startup_reconcile_RUNS_the_audit_and_surfaces_a_stale_book(self):
        """THE 08-06 CASE: book holds contracts the broker does not. Startup
        reconcile must surface it WITHOUT anyone calling the audit by hand."""
        pt._save(pt.POSITIONS_FILE, [{
            "rec_id": "R-STALE", "ticker": "GOOGL", "quantity": 5,
            "legs": [{"symbol": SYM, "action": "buy_to_open"}]}])

        client = MagicMock()
        client.available.return_value = True
        client.get_orders.return_value = []
        client.get_positions.return_value = []          # broker flat

        with patch.object(pt, "get_order_client", lambda: client), \
             patch.object(pt, "resolve_intents", lambda c: []):
            events = pt.reconcile_with_broker()

        kinds = [e.get("kind") for e in events if e.get("event") == "reconcile_alarm"]
        self.assertIn("book_position_not_at_broker", kinds,
                      "startup reconcile must RUN the audit — an uncalled "
                      "detector is not detection")

    def test_audit_failure_never_blocks_startup(self):
        pt._save(pt.POSITIONS_FILE, [])
        client = MagicMock()
        client.available.return_value = True
        client.get_orders.return_value = []
        with patch.object(pt, "get_order_client", lambda: client), \
             patch.object(pt, "resolve_intents", lambda c: []), \
             patch.object(pt, "audit_book_vs_broker",
                          side_effect=RuntimeError("audit blew up")):
            events = pt.reconcile_with_broker()      # must not raise
        self.assertIsInstance(events, list)

    def test_clean_book_produces_no_alarm(self):
        pt._save(pt.POSITIONS_FILE, [])
        client = MagicMock()
        client.available.return_value = True
        client.get_orders.return_value = []
        client.get_positions.return_value = []
        with patch.object(pt, "get_order_client", lambda: client), \
             patch.object(pt, "resolve_intents", lambda c: []):
            events = pt.reconcile_with_broker()
        self.assertEqual([e for e in events if e.get("event") == "reconcile_alarm"], [])


class PreflightSurfacesDivergence(unittest.TestCase):
    def test_preflight_check_h_exists_and_reports_divergence(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gng", Path(__file__).resolve().parents[1] / "scripts/ops/ibkr_go_nogo_checks.py")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        out = []
        m.emit = lambda cid, status, detail: out.append((cid, status, detail))
        with patch("src.engine.paper_trader.audit_book_vs_broker",
                   return_value=[{"kind": "book_position_not_at_broker",
                                  "symbol": SYM, "book_quantity": 5,
                                  "broker_quantity": 0}]):
            m.check_h_book_vs_broker()
        self.assertEqual(out[0][0], "h")
        self.assertEqual(out[0][1], "FAIL",
                         "a book/broker divergence must FAIL preflight, not warn")


if __name__ == "__main__":
    unittest.main()
