"""Quote-outage protective-flatten tests (2026-07-02).

Policy under test: after quote_outage_flatten_cycles consecutive unpriceable
cycles, manage_positions submits a protective close at 75% of the last known
mark (entry*0.5 anchor if never priced); disabled at 0; a returning quote
resets the streak; failed submissions retry next cycle.

Run: .venv/bin/python -m unittest tests.test_quote_outage_flatten -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.exit_manager as em
import src.engine.paper_trader as pt

LEGS = [{"symbol": "SPY260710C00740000", "action": "buy_to_open"},
        {"symbol": "SPY260710C00747000", "action": "sell_to_open"}]

GOOD_QUOTES = {"SPY260710C00740000": {"bid": 6.0, "ask": 6.2},
               "SPY260710C00747000": {"bid": 1.6, "ask": 1.8}}

CFG = {"quote_outage_flatten_cycles": 4, "flat_time_et": "23:59"}  # keep time_flat out of the way


class FlattenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._orig = (pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG)
        pt.PENDING_FILE = base / "paper_orders.json"
        pt.POSITIONS_FILE = base / "paper_positions.json"
        pt.FILLS_LOG = base / "fills.jsonl"
        pt._save(pt.POSITIONS_FILE, [{
            "rec_id": "R1", "ticker": "SPY", "structure": "call_debit_spread",
            "direction": "long", "legs": LEGS, "entry_price": 3.6,
            "quantity": 1, "max_profit": 3.4, "max_loss": 3.6,
            "expiration": "2026-07-10", "closing": False,
        }])

    def tearDown(self):
        pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG = self._orig
        self.tmp.cleanup()

    def _run_cycles(self, quote_seq, cfg=None):
        """Drive manage_positions once per quotes-dict in quote_seq."""
        submits = []
        client = MagicMock()
        client.available.return_value = True
        with patch.object(em, "get_client", return_value=client), \
                patch.object(em, "cancel_stale_entries", return_value=[]), \
                patch.object(em, "submit_close_order",
                             side_effect=lambda p, limit, reason:
                             submits.append((round(limit, 2), reason)) or {"submitted": True}), \
                patch("src.engine.notify.send"):
            for quotes in quote_seq:
                client.quotes.return_value = quotes
                events = em.manage_positions({}, cfg or CFG)
        return submits, events

    def test_flatten_fires_at_threshold_with_last_mark_anchor(self):
        # one good cycle records last_value (mid net = 6.1 - 1.7 = 4.4),
        # then 4 blind cycles -> protective close at 4.4 * 0.75 = 3.30
        submits, _ = self._run_cycles([GOOD_QUOTES, {}, {}, {}, {}])
        self.assertEqual(len(submits), 1)
        limit, reason = submits[0]
        self.assertEqual(limit, 3.30)
        self.assertIn("quote_outage_flatten", reason)
        self.assertIn("unpriceable 4 cycles", reason)

    def test_no_flatten_below_threshold(self):
        submits, _ = self._run_cycles([GOOD_QUOTES, {}, {}, {}])  # streak 3
        self.assertEqual(submits, [])

    def test_streak_resets_on_quote_recovery(self):
        submits, _ = self._run_cycles([GOOD_QUOTES, {}, {}, {}, GOOD_QUOTES, {}, {}, {}])
        self.assertEqual(submits, [])  # never reaches 4 consecutive

    def test_never_priced_anchors_to_half_entry(self):
        # no good cycle first: anchor = entry 3.6 * 0.5 = 1.8 -> limit 1.35
        submits, _ = self._run_cycles([{}, {}, {}, {}])
        self.assertEqual(submits[0][0], 1.35)

    def test_disabled_by_config(self):
        cfg = {**CFG, "quote_outage_flatten_cycles": 0}
        submits, _ = self._run_cycles([{}, {}, {}, {}, {}, {}], cfg=cfg)
        self.assertEqual(submits, [])

    def test_failed_submission_retries_next_cycle(self):
        client = MagicMock()
        client.available.return_value = True
        client.quotes.return_value = {}
        attempts = []
        with patch.object(em, "get_client", return_value=client), \
                patch.object(em, "cancel_stale_entries", return_value=[]), \
                patch.object(em, "submit_close_order",
                             side_effect=lambda *a: attempts.append(1) and None), \
                patch("src.engine.notify.send"):
            for _ in range(6):  # broker down: submit returns None every time
                em.manage_positions({}, CFG)
        # fires at streak 4, 5, 6 — keeps trying while blind and open
        self.assertEqual(len(attempts), 3)
        self.assertFalse(pt.load_positions()[0].get("closing"))

    def test_exit_reason_code(self):
        self.assertEqual(pt.exit_reason_code("quote_outage_flatten (unpriceable 4 cycles)"),
                         "quote_outage_flatten")


if __name__ == "__main__":
    unittest.main()
