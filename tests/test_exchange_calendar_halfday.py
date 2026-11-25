"""Half-day / holiday calendar awareness (2026-11-24).

Two layers used to assume every weekday is a full 06:30-13:00 PT session:

  * `src.scheduler._market_open_static` — the FALLBACK used when the Tradier
    clock is unavailable. The live tick is already half-day-correct (it asks
    the clock), so this is fallback-only correctness, in the fail-safe
    direction (strictly fewer cycles).
  * `scripts/ops/liveness.py::session_open` — the deadman's session window,
    which has no exchange calendar at all. Measured cost: Labor Day
    2026-09-07, engine healthy, paged `cycle-hung-in-session` from 06:34 every
    30 min because `last_cycle` does not advance on a closed market.

Dates are from a live Tradier `calendar()` probe on 2026-11-24: 2026-11-26
status closed, 2026-11-27 close 13:00 ET (= 10:00 PT), 2026-12-24 close
13:00 ET. Every test pins `now` — no test here may depend on the wall clock.
"""
import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.scheduler import _market_open_static

PT = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "liveness_halfday", ROOT / "scripts" / "ops" / "liveness.py")
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)


def pt(date: str, h: int, m: int) -> datetime:
    y, mo, d = map(int, date.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=PT)


NORMAL = "2026-11-24"      # Tuesday, full session
THANKSGIVING = "2026-11-26"  # Thursday, full holiday
EARLY = "2026-11-27"       # Friday, closes 10:00 PT
XMAS_EVE = "2026-12-24"    # Thursday, closes 10:00 PT


class SchedulerStaticFallback(unittest.TestCase):
    def test_normal_day_unchanged(self):
        self.assertTrue(_market_open_static(pt(NORMAL, 11, 0)))
        self.assertTrue(_market_open_static(pt(NORMAL, 12, 59)))
        self.assertFalse(_market_open_static(pt(NORMAL, 13, 0)))
        self.assertFalse(_market_open_static(pt(NORMAL, 6, 29)))

    def test_early_close_day_is_closed_after_1000_pt(self):
        """The item's PASS criterion: 2026-11-27 11:00 PT pins to closed."""
        self.assertTrue(_market_open_static(pt(EARLY, 9, 59)))
        self.assertFalse(_market_open_static(pt(EARLY, 10, 0)))
        self.assertFalse(_market_open_static(pt(EARLY, 11, 0)))
        self.assertFalse(_market_open_static(pt(EARLY, 12, 59)))

    def test_early_close_morning_still_trades(self):
        self.assertTrue(_market_open_static(pt(EARLY, 6, 30)))
        self.assertTrue(_market_open_static(pt(XMAS_EVE, 7, 0)))
        self.assertFalse(_market_open_static(pt(XMAS_EVE, 11, 0)))

    def test_full_holiday_still_closed(self):
        self.assertFalse(_market_open_static(pt(THANKSGIVING, 11, 0)))


class LivenessSessionWindow(unittest.TestCase):
    def test_normal_day_unchanged(self):
        self.assertFalse(L.session_open(pt(NORMAL, 6, 29)))
        self.assertTrue(L.session_open(pt(NORMAL, 6, 30)))
        self.assertTrue(L.session_open(pt(NORMAL, 12, 59)))
        self.assertFalse(L.session_open(pt(NORMAL, 13, 0)))

    def test_weekday_holiday_is_out_of_session(self):
        """Labor Day 2026-09-07 paged all morning for this reason."""
        for h in (6, 7, 10, 12):
            self.assertFalse(L.session_open(pt(THANKSGIVING, h, 0)),
                             f"{THANKSGIVING} {h}:00 PT must be out of session")
        self.assertFalse(L.session_open(pt("2026-09-07", 6, 34)))

    def test_early_close_window_ends_at_1000_pt(self):
        self.assertTrue(L.session_open(pt(EARLY, 9, 59)))
        self.assertFalse(L.session_open(pt(EARLY, 10, 0)))
        self.assertFalse(L.session_open(pt(EARLY, 11, 0)))
        self.assertFalse(L.session_open(pt(XMAS_EVE, 11, 0)))

    def test_weekend_unchanged(self):
        self.assertFalse(L.session_open(pt("2026-11-28", 10, 0)))

    def test_calendar_read_failure_fails_open(self):
        """A bad config read may only fail to suppress a FALSE page; it must
        never suppress a real in-session one, so the fallback is the old
        weekday + static-window behaviour."""
        import sys

        class Broken:
            def safe_load(self, *a, **k):
                raise RuntimeError("config unreadable")

        orig = sys.modules.get("yaml")
        sys.modules["yaml"] = Broken()
        try:
            self.assertEqual(L._exchange_calendar(), (set(), {}))
            # pre-calendar behaviour: weekday + static 06:30-13:00, so a real
            # in-session outage still pages and only the suppression is lost.
            self.assertTrue(L.session_open(pt(NORMAL, 11, 0)))
            self.assertTrue(L.session_open(pt(THANKSGIVING, 11, 0)))
            self.assertTrue(L.session_open(pt(EARLY, 11, 0)))
            self.assertFalse(L.session_open(pt(NORMAL, 13, 0)))
        finally:
            if orig is None:
                del sys.modules["yaml"]
            else:
                sys.modules["yaml"] = orig

    def test_loader_returns_the_probed_dates(self):
        holidays, early = L._exchange_calendar()
        self.assertIn(THANKSGIVING, holidays)
        self.assertEqual(early.get(EARLY), "10:00")
        self.assertEqual(early.get(XMAS_EVE), "10:00")


class DeadmanDecisionOnAHalfDay(unittest.TestCase):
    """The DECISION level, not just `session_open` (written mid_session
    2026-11-24, ahead of its weekend-11-28 due date so the guarantee exists
    BEFORE Friday's early close rather than after it).

    `scripts/deadman.sh::alert()` asks `liveness.py` for exactly two things —
    `slug <cond>` and `throttle <cond>` — and each layer asks for its own
    predicate. Those are the four functions pinned here for the two conditions
    the Labor Day false-page series actually came from (`cycle-hung`,
    `broker-port-dead`). Three columns discriminate: a normal day at 11:00, the
    half-day MORNING (still trading), and the half-day AFTERNOON (closed).

    RESIDUAL, deliberately not covered: the CLI's own `now = datetime.now(PT)`
    (`liveness.py:217`). Pinning it would need a clock hook in a protective
    layer — a stray env var would silently shift the deadman's clock — and that
    line is condition-independent and exercised on every real tick. Not a gap;
    do not reopen it as one.
    """

    CONDS = ("cycle-hung", "broker-port-dead")

    def test_normal_day_is_in_session_urgent(self):
        now = pt(NORMAL, 11, 0)
        for c in self.CONDS:
            with self.subTest(cond=c):
                self.assertEqual(L.slug_for(c, now), f"{c}-in-session")
                self.assertEqual(L.throttle_seconds(c, now),
                                 L.IN_SESSION_THROTTLE_S)
        self.assertTrue(L.broker_port_dead(now, port_open=False))
        hang = L.cycle_hang_minutes(now, pt(NORMAL, 8, 0))
        self.assertIsNotNone(hang)
        self.assertGreater(hang, L.CYCLE_STALE_MIN)

    def test_half_day_morning_still_pages_urgently(self):
        """07:30 PT, past BROKER_PORT_GRACE_MIN (30), market still open."""
        now = pt(EARLY, 7, 30)
        for c in self.CONDS:
            with self.subTest(cond=c):
                self.assertEqual(L.slug_for(c, now), f"{c}-in-session")
                self.assertEqual(L.throttle_seconds(c, now),
                                 L.IN_SESSION_THROTTLE_S)
        self.assertTrue(L.broker_port_dead(now, port_open=False))
        self.assertIsNotNone(L.cycle_hang_minutes(now, pt(EARLY, 6, 40)))

    def test_half_day_afternoon_is_out_of_session_everywhere(self):
        """The whole point: 11:00 PT on 11-27 must look like any evening."""
        now = pt(EARLY, 11, 0)
        for c in self.CONDS:
            with self.subTest(cond=c):
                self.assertEqual(L.slug_for(c, now), c)   # no -in-session
                self.assertEqual(L.throttle_seconds(c, now),
                                 L.OUT_OF_SESSION_THROTTLE_S)
        self.assertFalse(L.broker_port_dead(now, port_open=False))
        self.assertIsNone(L.cycle_hang_minutes(now, pt(EARLY, 6, 40)))
        self.assertIsNone(L.cycle_hang_minutes(now, None))
        self.assertFalse(L.heartbeat_overdue(now, None))

    def test_should_alert_throttle_decision_differs_by_column(self):
        """One stamp, 45 min old: urgent in session (>30m), quiet after the
        early close (<6h). This is the end-to-end throttle decision."""
        for c in self.CONDS:
            with self.subTest(cond=c):
                live = pt(NORMAL, 11, 0)
                self.assertTrue(L.should_alert(
                    c, live, pt(NORMAL, 10, 15).timestamp()))
                closed = pt(EARLY, 11, 0)
                self.assertFalse(L.should_alert(
                    c, closed, pt(EARLY, 10, 15).timestamp()))
                # ... and a stamp older than 6h pages even out of session
                self.assertTrue(L.should_alert(
                    c, closed, pt(EARLY, 4, 0).timestamp()))
                self.assertTrue(L.should_alert(c, closed, None))

    def test_calendar_failure_fails_open_at_the_decision_level(self):
        """Same direction the loader test pins for `session_open`: a broken
        config pages (in-session slug + 30-min throttle) rather than suppresses."""
        import sys

        class Broken:
            @staticmethod
            def safe_load(*_a, **_k):
                raise RuntimeError("broken yaml")

        orig = sys.modules.get("yaml")
        sys.modules["yaml"] = Broken()
        try:
            now = pt(EARLY, 11, 0)
            for c in self.CONDS:
                with self.subTest(cond=c):
                    self.assertEqual(L.slug_for(c, now), f"{c}-in-session")
                    self.assertEqual(L.throttle_seconds(c, now),
                                     L.IN_SESSION_THROTTLE_S)
            self.assertTrue(L.broker_port_dead(now, port_open=False))
        finally:
            if orig is None:
                del sys.modules["yaml"]
            else:
                sys.modules["yaml"] = orig


if __name__ == "__main__":
    unittest.main()
