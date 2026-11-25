"""Signal-freshness session-awareness tests (mark 4, 2026-07-24).

Covers all six session cases the feed-guard must get right: regular session,
pre-open, post-close, weekend, half-day / early close, and halt. The half-day
case is the one that matters most — a wrong (static 16:00) calendar would turn a
half-day's afternoon into a stale-bar ALARM STORM, so we prove the early
close_time is what prevents it. Also the verdict matrix (fresh / stale-during-
open / stale-outside-session / no-bar) and bar-age math.

Run: .venv/bin/python -m unittest tests.test_freshness -v
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import freshness as F  # noqa: E402

ET = ZoneInfo("America/New_York")
FLOORS = {"feed_guard_floor_min": {"SCALP": 3, "funnel": 20}}
REGULAR = {"status": "open", "open_time": "09:30", "close_time": "16:00"}
HALFDAY = {"status": "open", "open_time": "09:30", "close_time": "13:00"}  # early close
HOLIDAY = {"status": "closed"}


def et(h, m):
    return datetime(2026, 7, 13, h, m, tzinfo=ET)


class SessionState(unittest.TestCase):
    def test_regular_open(self):
        self.assertEqual(F.session_state(et(10, 0), REGULAR), "open")

    def test_pre_open(self):
        self.assertEqual(F.session_state(et(9, 0), REGULAR), "premarket")

    def test_post_close(self):
        self.assertEqual(F.session_state(et(16, 30), REGULAR), "postmarket")

    def test_weekend_or_holiday(self):
        self.assertEqual(F.session_state(et(11, 0), HOLIDAY), "closed")
        self.assertEqual(F.session_state(et(11, 0), None), "closed")

    def test_half_day_afternoon_is_postmarket(self):
        # 13:30 on a half-day (close 13:00) MUST be postmarket, not open
        self.assertEqual(F.session_state(et(13, 30), HALFDAY), "postmarket")
        # and still open just before the early close
        self.assertEqual(F.session_state(et(12, 59), HALFDAY), "open")

    def test_half_day_alarm_storm_guard(self):
        # THE mark-4 case: at 13:30 on a half-day, a stale bar must NOT alarm.
        age = 90  # bar 90 min old (last bar was the 12:00 print, say)
        # correct calendar (early close) -> postmarket -> market_closed (no alarm)
        allow, reason = F.freshness_verdict(age, "SCALP",
                                            F.session_state(et(13, 30), HALFDAY), FLOORS)
        self.assertFalse(allow)
        self.assertEqual(reason, "market_closed")  # would-alarm=False
        # WRONG calendar (static 16:00) -> open -> stale_bar (alarm storm)
        allow2, reason2 = F.freshness_verdict(age, "SCALP",
                                              F.session_state(et(13, 30), REGULAR), FLOORS)
        self.assertEqual(reason2, "stale_bar")  # demonstrates the storm we avoid


class Verdict(unittest.TestCase):
    def test_fresh_allows(self):
        self.assertEqual(F.freshness_verdict(1.0, "SCALP", "open", FLOORS), (True, "fresh"))

    def test_stale_during_open_is_feed_defect(self):
        self.assertEqual(F.freshness_verdict(30.0, "SCALP", "open", FLOORS), (False, "stale_bar"))

    def test_stale_outside_session_is_expected(self):
        self.assertEqual(F.freshness_verdict(30.0, "SCALP", "premarket", FLOORS), (False, "market_closed"))
        self.assertEqual(F.freshness_verdict(30.0, "SCALP", "closed", FLOORS), (False, "market_closed"))

    def test_no_bar_blocks(self):
        self.assertEqual(F.freshness_verdict(None, "SCALP", "open", FLOORS), (False, "no_bar"))

    def test_lane_floors_differ(self):
        # a 10-min-old bar: blocks SCALP (floor 3), allows funnel (floor 20)
        self.assertFalse(F.freshness_verdict(10.0, "SCALP", "open", FLOORS)[0])
        self.assertTrue(F.freshness_verdict(10.0, "funnel", "open", FLOORS)[0])

    def test_halt_blocks_during_open(self):
        # a halt = open session + stale bar -> block (don't trade on stale data)
        allow, reason = F.freshness_verdict(45.0, "SCALP", "open", FLOORS)
        self.assertFalse(allow)
        self.assertEqual(reason, "stale_bar")


class BarAge(unittest.TestCase):
    def test_minutes_and_tz(self):
        now = et(10, 5)
        self.assertAlmostEqual(F.bar_age_min("2026-07-13 10:00:00-04:00", now), 5.0, places=1)
        # naive bar assumed ET
        self.assertAlmostEqual(F.bar_age_min("2026-07-13T09:35:00", now), 30.0, places=1)

    def test_unparseable_is_none(self):
        self.assertIsNone(F.bar_age_min("not-a-time", et(10, 0)))


if __name__ == "__main__":
    unittest.main()
