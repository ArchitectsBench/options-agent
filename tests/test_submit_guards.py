"""Expiry guard + rec_id dedup at WRITE (2026-08-07)."""
import unittest
from datetime import date, timedelta

import src.engine.paper_trader as pt

# TIME BOMB FIXED (2026-08-17): this hardcoded expiration "2026-08-14", so from
# 2026-08-15 the expiry guard refused the order and the DEDUP assertions below
# never ran — the suite reported a failure whose message was about expiry, in a
# test about duplicate rec_ids. The dates in ExpiryGuard are deliberately fixed
# (they pass an explicit `today`), but anything reaching submit_paper_order must
# be relative to the real clock. Prefer a relative future expiry over a literal.
_FUTURE_EXPIRY = (date.today() + timedelta(days=7)).isoformat()

REC = {"rec_id": "2026-08-07-001", "ticker": "SPY",
       "trade": {"expiration": _FUTURE_EXPIRY, "structure": "call_debit_spread",
                 "strategy": "debit_spread", "direction": "long", "net_debit": 1.0,
                 "max_profit": 1.0, "max_loss": 1.0,
                 "legs": [{"symbol": "SPY260814C00770000", "action": "buy_to_open"}]}}


class ExpiryGuard(unittest.TestCase):
    def test_past_expiry_is_past(self):
        self.assertTrue(pt.expiry_is_past("2026-07-10", date(2026, 8, 7)))

    def test_today_is_not_past(self):
        self.assertFalse(pt.expiry_is_past("2026-08-07", date(2026, 8, 7)),
                         "0DTE is legitimate — only BEFORE today is refused")

    def test_future_is_not_past(self):
        self.assertFalse(pt.expiry_is_past("2026-08-14", date(2026, 8, 7)))

    def test_unparseable_does_not_block(self):
        self.assertFalse(pt.expiry_is_past("not-a-date", date(2026, 8, 7)))

    def test_submit_refuses_a_lapsed_contract(self):
        rec = {**REC, "trade": {**REC["trade"],
               "expiration": (date.today() - timedelta(days=3)).isoformat()}}
        res = pt.submit_paper_order(rec)
        self.assertEqual(res["refused"], "expiry_in_past")
        self.assertFalse(res["submitted"])


class RecIdDedup(unittest.TestCase):
    def test_duplicate_rec_id_is_refused_at_write(self):
        pt._save(pt.POSITIONS_FILE, [{"rec_id": REC["rec_id"], "ticker": "SPY",
                                      "quantity": 1, "legs": []}])
        self.assertTrue(pt.rec_id_already_live(REC["rec_id"]))
        res = pt.submit_paper_order(REC)
        self.assertEqual(res["refused"], "duplicate_rec_id")

    def test_unknown_rec_id_is_not_a_duplicate(self):
        pt._save(pt.POSITIONS_FILE, [])
        pt._save(pt.PENDING_FILE, [])
        self.assertFalse(pt.rec_id_already_live("2026-08-07-999"))
