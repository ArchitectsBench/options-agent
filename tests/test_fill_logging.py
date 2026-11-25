"""Lifecycle logging tests (post-session fix 2026-06-11 #2/#3).

Covers: submit -> entry fill logged with legs -> position created ->
exit fill -> linked round-trip record with corrected credit accounting and
exit_reason enum; plus startup reconciliation adopting an orphan position
and canceling an unknown stale order.

Run: .venv/bin/python -m unittest tests.test_fill_logging -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.paper_trader as pt

ENTRY_ORDER = {  # Tradier debit multileg fill (leg prices authoritative)
    "id": 111, "status": "filled", "type": "debit", "avg_fill_price": 1.39,
    "create_date": "2026-06-11T15:00:32Z", "transaction_date": "2026-06-11T15:00:32Z",
    "leg": [
        {"option_symbol": "NFLX260618P00082000", "side": "buy_to_open",
         "avg_fill_price": 2.10, "exec_quantity": 1},
        {"option_symbol": "NFLX260618P00079000", "side": "sell_to_open",
         "avg_fill_price": 0.71, "exec_quantity": 1},
    ],
}
EXIT_ORDER = {  # credit close: parent avg_fill_price NEGATIVE (the 6/11 root cause)
    "id": 222, "status": "filled", "type": "credit", "avg_fill_price": -1.52,
    "create_date": "2026-06-11T16:00:00Z", "transaction_date": "2026-06-11T16:00:05Z",
    "leg": [
        {"option_symbol": "NFLX260618P00082000", "side": "sell_to_close",
         "avg_fill_price": 2.30, "exec_quantity": 1},
        {"option_symbol": "NFLX260618P00079000", "side": "buy_to_close",
         "avg_fill_price": 0.78, "exec_quantity": 1},
    ],
}


class FillLoggingTest(unittest.TestCase):
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

    def _fills(self) -> list[dict]:
        if not pt.FILLS_LOG.exists():
            return []
        return [json.loads(l) for l in pt.FILLS_LOG.read_text().splitlines()]

    def _client(self, **overrides) -> MagicMock:
        client = MagicMock()
        client.available.return_value = True
        for name, value in overrides.items():
            getattr(client, name).side_effect = value if callable(value) else None
            if not callable(value):
                getattr(client, name).return_value = value
        return client

    def test_full_round_trip_logged(self):
        # 1. pending entry (what submit_paper_order writes)
        pt._save(pt.PENDING_FILE, [{
            "intent": "open", "order_id": 111, "rec_id": "T-001", "ticker": "NFLX",
            "structure": "put debit spread", "strategy": "debit_spread",
            "direction": "short",
            "legs": [{"symbol": "NFLX260618P00082000", "action": "buy_to_open"},
                     {"symbol": "NFLX260618P00079000", "action": "sell_to_open"}],
            "rec_price": 1.41, "max_profit": 1.61, "max_loss": 1.39,
            "expiration": "2026-06-18", "submitted_at": "2026-06-11T08:00:00",
        }])
        with patch.object(pt, "PaperOrderClient",
                          return_value=self._client(get_order={111: ENTRY_ORDER}.get)):
            events = pt.poll_fills()

        # 2. entry fill logged with legs + leg-derived net debit (2.10-0.71)
        self.assertEqual(len(events), 1)
        fill = self._fills()[0]
        self.assertEqual(fill["event"], "paper_fill")
        self.assertEqual(fill["fill_price"], 1.39)
        self.assertEqual(len(fill["legs"]), 2)
        self.assertEqual(fill["broker_filled"], "2026-06-11T15:00:32Z")
        positions = pt.load_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["entry_order_id"], 111)

        # 3. exit fill -> round trip with CORRECTED credit sign + exit_reason
        pt._save(pt.PENDING_FILE, [{
            "intent": "close", "order_id": 222, "rec_id": "T-001", "ticker": "NFLX",
            "structure": "put debit spread", "entry_price": 1.39,
            "close_reason": "time_flat (day-trading mandate)",
            "submitted_at": "2026-06-11T12:45:00",
        }])
        with patch.object(pt, "PaperOrderClient",
                          return_value=self._client(get_order={222: EXIT_ORDER}.get)):
            events = pt.poll_fills()

        self.assertEqual(len(events), 1)
        exit_ev = self._fills()[-1]
        self.assertEqual(exit_ev["event"], "paper_exit")
        self.assertEqual(exit_ev["exit_price"], 1.52)       # NOT -1.52
        self.assertEqual(exit_ev["pnl_per_contract"], 13.0)  # (1.52-1.39)*100
        self.assertEqual(exit_ev["exit_reason"], "flatten_1545")
        rt = exit_ev["round_trip"]
        self.assertEqual(rt["entry_order_id"], 111)
        self.assertEqual(rt["exit_order_id"], 222)
        self.assertEqual(len(rt["entry_legs"]), 2)
        self.assertEqual(len(rt["exit_legs"]), 2)
        self.assertEqual(rt["pnl_per_contract"], 13.0)
        self.assertEqual(pt.load_positions(), [])  # position closed

    def test_exit_reason_codes(self):
        for raw, code in [("time_flat (day-trading mandate)", "flatten_1545"),
                          ("invalidation (reclaimed VWAP, +0.14 ATR)", "vwap_invalidation"),
                          ("stop_loss (value 1.5 vs entry 3.17)", "stop_50"),
                          ("profit_target (value 5.4 vs entry 3.17)", "target_50"),
                          (None, "unknown")]:
            self.assertEqual(pt.exit_reason_code(raw), code)

    def test_reconciliation_adopts_orphan_and_IGNORES_unknown_order(self):
        client = self._client()
        client.get_orders.side_effect = None
        client.get_orders.return_value = [
            {"id": 999, "status": "open", "symbol": "SPY"},  # unknown -> cancel
        ]
        client.get_positions.side_effect = None
        client.get_positions.return_value = [
            {"symbol": "AMD260618C00460000", "quantity": 1, "cost_basis": 350.0},
            {"symbol": "AMD260618C00470000", "quantity": -1, "cost_basis": -120.0},
        ]
        # hermetic config (2026-07-01): this scenario is the adopt_foreign=true
        # behavior — the real config.yaml has it false, which silently made this
        # test dead (reconcile early-returned). Pin the config explicitly.
        with patch.object(pt, "PaperOrderClient", return_value=client), \
                patch("yaml.safe_load", return_value={"agent": {"adopt_foreign": True}}):
            events = pt.reconcile_with_broker()

        # CONTRACT CHANGE 2026-08-04 (cross-book hardening): the untagged order
        # 999 is NOT attributably ours, so it is now logged and IGNORED rather
        # than cancelled — the shared IBKR paper account makes cancelling a
        # foreign order unrecoverable. POSITION adoption is unchanged.
        kinds = sorted(e["event"] for e in events)
        self.assertEqual(kinds, ["reconciled_orphan"])
        client.cancel_order.assert_not_called()   # cross-book: never cancel a foreign order
        positions = pt.load_positions()
        self.assertEqual(len(positions), 1)
        adopted = positions[0]
        self.assertEqual(adopted["ticker"], "AMD")
        self.assertTrue(adopted["orphan"])
        self.assertEqual(adopted["entry_price"], 2.30)  # (350-120)/100
        self.assertEqual({l["action"] for l in adopted["legs"]},
                         {"buy_to_open", "sell_to_open"})
        # logged to fills.jsonl as well — 1 event now (the adopted position);
        # the foreign order is ignored, so no reconciled_orphan_order event.
        self.assertEqual(len(self._fills()), 1)

    def test_reconciliation_ignores_known_state(self):
        pt._save(pt.PENDING_FILE, [{"intent": "open", "order_id": 999, "rec_id": "T-9",
                                    "ticker": "SPY", "submitted_at": "2026-06-11T08:00:00"}])
        client = self._client()
        client.get_orders.side_effect = None
        client.get_orders.return_value = [{"id": 999, "status": "open", "symbol": "SPY"}]
        client.get_positions.side_effect = None
        client.get_positions.return_value = []
        with patch.object(pt, "PaperOrderClient", return_value=client):
            events = pt.reconcile_with_broker()
        self.assertEqual(events, [])
        client.cancel_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
