"""O-21 — session-aware liveness alerting.

Regression for 2026-08-07: the engine was down for the first 2.5h of the session
in silence. The deadman WORKED (it alerted at 03:12:55 on a dead pid) but its
6-hour per-condition throttle then suppressed the identical condition across the
06:30 open. An out-of-session alert consumed the window that mattered.
"""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "liveness", Path(__file__).resolve().parents[1] / "scripts/ops/liveness.py")
L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)

PT = ZoneInfo("America/Los_Angeles")
MON = lambda h, m: datetime(2026, 8, 10, h, m, tzinfo=PT)      # a Monday
SAT = lambda h, m: datetime(2026, 8, 8, h, m, tzinfo=PT)


class SessionWindow(unittest.TestCase):
    def test_open_window(self):
        self.assertFalse(L.session_open(MON(6, 29)))
        self.assertTrue(L.session_open(MON(6, 30)))
        self.assertTrue(L.session_open(MON(12, 59)))
        self.assertFalse(L.session_open(MON(13, 0)))

    def test_weekend_is_never_open(self):
        self.assertFalse(L.session_open(SAT(10, 0)))


class TheO807Regression(unittest.TestCase):
    """THE DEFECT: an alert at 03:12 must NOT silence the same condition at the
    06:30 open."""

    def test_overnight_alert_does_not_suppress_the_in_session_alert(self):
        overnight = MON(3, 12).timestamp()
        at_open = MON(6, 40)
        # pre-fix behaviour was: same slug, 6h throttle -> suppressed until 09:12
        self.assertLess(at_open.timestamp() - overnight, 21600,
                        "precondition: inside the OLD 6h window")
        # post-fix: the in-session condition has its OWN slug, so the overnight
        # stamp is not even consulted
        self.assertEqual(L.slug_for("agent-dead", at_open), "agent-dead-in-session")
        self.assertNotEqual(L.slug_for("agent-dead", MON(3, 12)),
                            L.slug_for("agent-dead", at_open),
                            "out-of-session and in-session must be DIFFERENT slugs")
        self.assertTrue(L.should_alert("agent-dead", at_open, None),
                        "the in-session slug has no prior stamp -> must fire")

    def test_in_session_outage_keeps_announcing_itself(self):
        first = MON(6, 40)
        self.assertTrue(L.should_alert("agent-dead", first, None))
        # 20 min later: still throttled (30-min in-session window)
        self.assertFalse(L.should_alert("agent-dead", MON(7, 0), first.timestamp()))
        # 35 min later: fires again — a live outage must not go quiet
        self.assertTrue(L.should_alert("agent-dead", MON(7, 15), first.timestamp()))

    def test_out_of_session_keeps_the_quiet_6h_throttle(self):
        t = MON(3, 12)
        self.assertEqual(L.throttle_seconds("agent-dead", t), 21600)
        self.assertFalse(L.should_alert("agent-dead", MON(5, 0), t.timestamp()))


class DeadManAtOpenPlus10(unittest.TestCase):
    def test_no_heartbeat_after_grace_is_overdue(self):
        self.assertTrue(L.heartbeat_overdue(MON(6, 41), None),
                        "no heartbeat at all, past grace -> overdue")

    def test_stale_heartbeat_from_a_PREVIOUS_session_is_overdue(self):
        """THE 08-07 SHAPE: last_cycle was 08-06T13:00 — a previous session."""
        prev = datetime(2026, 8, 6, 13, 0, tzinfo=PT)
        self.assertTrue(L.heartbeat_overdue(MON(6, 41), prev))

    def test_within_grace_is_not_overdue(self):
        self.assertFalse(L.heartbeat_overdue(MON(6, 35), None),
                         "inside the 10-min grace, a slow start is not an outage")

    def test_fresh_heartbeat_this_session_is_fine(self):
        self.assertFalse(L.heartbeat_overdue(MON(6, 45), MON(6, 44)))

    def test_out_of_session_never_overdue(self):
        self.assertFalse(L.heartbeat_overdue(MON(5, 0), None))
        self.assertFalse(L.heartbeat_overdue(SAT(10, 0), None))


class CycleHangAtTheOpen(unittest.TestCase):
    """Regression for 2026-09-30: the cycle-hung deadman paged the operator's
    phone every trading morning (09-25 06:35, 09-28 06:35, 09-30 06:34).
    `last_cycle` only advances on an in-session cycle and the engine's first one
    lands ~06:35, so between 06:30 and that cycle the raw age was yesterday's
    ~1,050 min. The age is now measured from max(last_cycle, today's open)."""

    YESTERDAY = datetime(2026, 8, 7, 13, 0, tzinfo=PT)   # Friday 13:00 close

    def test_quiet_in_the_open_gap(self):
        """06:34 with only yesterday's cycle — the artifact. Must be silent."""
        self.assertIsNone(L.cycle_hang_minutes(MON(6, 34), self.YESTERDAY))

    def test_pages_when_the_engine_never_cycles(self):
        """Past open+15 with still no cycle from this session — the real 09-24
        signature. Detection must survive the fix."""
        mins = L.cycle_hang_minutes(MON(6, 46), self.YESTERDAY)
        self.assertIsNotNone(mins)
        self.assertAlmostEqual(mins, 16.0, places=1)

    def test_pages_on_a_mid_session_hang(self):
        last = MON(9, 44)
        mins = L.cycle_hang_minutes(MON(10, 0), last)
        self.assertIsNotNone(mins)
        self.assertAlmostEqual(mins, 16.0, places=1)

    def test_quiet_when_cycles_are_advancing(self):
        self.assertIsNone(L.cycle_hang_minutes(MON(10, 0), MON(9, 55)))

    def test_missing_last_cycle_gets_the_same_grace(self):
        self.assertIsNone(L.cycle_hang_minutes(MON(6, 34), None))
        self.assertIsNotNone(L.cycle_hang_minutes(MON(6, 46), None))

    def test_out_of_session_never_alarms(self):
        self.assertIsNone(L.cycle_hang_minutes(MON(5, 0), self.YESTERDAY))
        self.assertIsNone(L.cycle_hang_minutes(MON(14, 0), self.YESTERDAY))
        self.assertIsNone(L.cycle_hang_minutes(SAT(10, 0), self.YESTERDAY))


class SweepOfThrottledConditions(unittest.TestCase):
    """Every throttled alert swept for session-blindness (O-21 requirement)."""

    def test_session_critical_conditions_are_session_aware(self):
        for cond in ("agent-dead", "cycle-hung"):
            self.assertEqual(L.slug_for(cond, MON(7, 0)), f"{cond}-in-session")
            self.assertEqual(L.throttle_seconds(cond, MON(7, 0)), 1800)

    def test_non_session_conditions_unchanged(self):
        """ops-sweep-stale is a daily hygiene check — 6h is correct for it."""
        self.assertEqual(L.slug_for("ops-sweep-stale", MON(7, 0)), "ops-sweep-stale")
        self.assertEqual(L.throttle_seconds("ops-sweep-stale", MON(7, 0)), 21600)


if __name__ == "__main__":
    unittest.main()
