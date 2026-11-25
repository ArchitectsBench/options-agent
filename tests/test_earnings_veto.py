"""Earnings-calendar hard-veto tests (2026-07-02).

The gate's contract: an earnings date inside [today, expiration] blocks the
entry; missing/stale/unreadable calendar data blocks single names
(fail-closed); ETFs/indices are always exempt.

Run: .venv/bin/python -m unittest tests.test_earnings_veto -v
"""

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.earnings as ea

NOW = datetime.now().isoformat(timespec="seconds")
IN3 = (date.today() + timedelta(days=3)).isoformat()
IN10 = (date.today() + timedelta(days=10)).isoformat()
EXP7 = (date.today() + timedelta(days=7)).isoformat()


def cal(ticker="NVDA", dates=None, fetched_at=NOW):
    return {ticker: {"dates": dates or [], "fetched_at": fetched_at}}


class VetoTest(unittest.TestCase):
    def test_earnings_inside_window_vetoed(self):
        out = ea.earnings_veto("NVDA", EXP7, cal(dates=[IN3]))
        self.assertIn("earnings", out)
        self.assertIn(IN3, out)

    def test_earnings_today_vetoed(self):
        today = date.today().isoformat()
        self.assertIsNotNone(ea.earnings_veto("NVDA", EXP7, cal(dates=[today])))

    def test_earnings_on_expiration_day_vetoed(self):
        self.assertIsNotNone(ea.earnings_veto("NVDA", EXP7, cal(dates=[EXP7])))

    def test_earnings_after_expiration_allowed(self):
        self.assertIsNone(ea.earnings_veto("NVDA", EXP7, cal(dates=[IN10])))

    def test_no_upcoming_earnings_allowed(self):
        self.assertIsNone(ea.earnings_veto("NVDA", EXP7, cal(dates=[])))

    def test_etf_always_exempt(self):
        for etf in ("SPY", "QQQ", "IWM", "TLT", "SLV", "XLE", "XLF"):
            self.assertIsNone(ea.earnings_veto(etf, EXP7, {}))  # even with NO calendar

    def test_fail_closed_no_entry(self):
        self.assertIn("fail-closed", ea.earnings_veto("NVDA", EXP7, {}))

    def test_fail_closed_stale_entry(self):
        old = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
        out = ea.earnings_veto("NVDA", EXP7, cal(dates=[], fetched_at=old))
        self.assertIn("stale", out)

    def test_fail_closed_missing_expiration(self):
        self.assertIn("fail-closed", ea.earnings_veto("NVDA", None, cal(dates=[])))

    def test_fail_closed_unreadable(self):
        self.assertIn("fail-closed",
                      ea.earnings_veto("NVDA", EXP7, {"NVDA": {"dates": []}}))
        self.assertIn("fail-closed",
                      ea.earnings_veto("NVDA", "garbage", cal(dates=[])))


class CalendarStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = ea.CALENDAR_FILE
        ea.CALENDAR_FILE = Path(self.tmp.name) / "earnings_calendar.json"

    def tearDown(self):
        ea.CALENDAR_FILE = self._orig
        self.tmp.cleanup()

    def test_freshness_check(self):
        self.assertFalse(ea.calendar_is_fresh_today())
        ea.CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
        ea.CALENDAR_FILE.write_text(json.dumps(cal(fetched_at=NOW)))
        self.assertTrue(ea.calendar_is_fresh_today())

    def test_refresh_keeps_prior_entry_on_failure(self):
        ea.CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
        ea.CALENDAR_FILE.write_text(json.dumps(cal("NVDA", [IN10], NOW)))
        with patch.dict(sys.modules, {"yfinance": None}):  # import raises
            out = ea.refresh_calendar(["NVDA", "SPY"])
        self.assertEqual(out["NVDA"]["dates"], [IN10])  # prior entry survives


if __name__ == "__main__":
    unittest.main()
