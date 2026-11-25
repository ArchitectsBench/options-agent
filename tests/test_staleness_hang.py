"""R8 (stale data outside the guarded path) + R9 (hung-cycle blindness) +
R12 (dead half-day layer) robustness tests. OBSERVATION MODE / paper only.

R8 — the freshness guard checked 5m frames only, so a ~6h-stale 30m frame or a
     stale VIX/SPY regime feed fed confidence as if live. Proven fixed:
       * the OLDER of (5m, 30m) governs the freshness gate (a stale 30m frame
         a 5m-only guard would have passed now blocks);
       * the regime object MARKS stale feeds (feed_staleness / stale_feeds).
R9 — a hung cycle (~25-30 min) passed every health check all day. Proven:
       * the per-cycle wall-clock deadline trips;
       * the Tradier data-client failure-breaker opens after N consecutive
         failures and fast-fails without touching the network.
R12 — the half-day calendar-reconstruction path read the wrong dict keys
     (open_time/close_time vs the real open/close) so it was dead code that
     swallowed its own error. Proven: an early 13:00 ET close is reported
     not-open at 13:30 ET via the calendar path, and a raised calendar error is
     logged rather than silently swallowed.

Run: .venv/bin/python -m pytest tests/test_staleness_hang.py -q
"""

import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import freshness as F  # noqa: E402
from src.data.tradier import TradierData  # noqa: E402
from src.engine import pipeline as P  # noqa: E402
from src.indicators import regime_live as rl  # noqa: E402

FLOORS = {"feed_guard_floor_min": {"SCALP": 3, "funnel": 20},
          "stale_frame_floor_min": {"30m": 90}}


# ── R8: slow-frame (30m) staleness governs ──────────────────────────────────
class SlowFrameVerdict(unittest.TestCase):
    def test_fresh_30m_allows(self):
        # a normally-lagging 30m bar (45 min old) is NOT stale (floor 90)
        self.assertEqual(F.slow_frame_verdict(45.0, "30m", "open", FLOORS), (True, "fresh"))

    def test_stale_30m_blocks_during_open(self):
        # a ~6h-stale (prior-session) 30m frame blocks as a feed defect
        self.assertEqual(F.slow_frame_verdict(360.0, "30m", "open", FLOORS), (False, "stale_bar"))

    def test_stale_30m_outside_session_is_expected(self):
        self.assertEqual(F.slow_frame_verdict(360.0, "30m", "postmarket", FLOORS),
                         (False, "market_closed"))

    def test_absent_30m_does_not_block(self):
        # absence handled upstream (SCALP is 5m-only) — must not newly block
        self.assertEqual(F.slow_frame_verdict(None, "30m", "open", FLOORS), (True, "no_slow_frame"))

    def test_default_floor_without_config(self):
        # no config override -> DEFAULT_SLOW_FRAME_FLOOR_MIN (30m: 90)
        self.assertEqual(F.slow_frame_verdict(120.0, "30m", "open", {}), (False, "stale_bar"))
        self.assertEqual(F.slow_frame_verdict(80.0, "30m", "open", {}), (True, "fresh"))


class FreshnessGateOlderFrameGoverns(unittest.TestCase):
    """pipeline._freshness_ok: a stale 30m frame trips the gate that a 5m-only
    guard would have passed — THE R8 regression."""

    def _sig(self, min_5m, min_30m):
        now = datetime.now(F.ET)
        s = {"bar_time": (now - timedelta(minutes=min_5m)).isoformat()}
        s["bar_time_30m"] = None if min_30m is None else (now - timedelta(minutes=min_30m)).isoformat()
        return s

    def test_stale_30m_blocks_even_with_fresh_5m(self):
        sig = self._sig(min_5m=2, min_30m=400)   # 5m fresh, 30m ~6.7h stale
        with patch.object(P, "_log_stale_block"):
            ok = P._freshness_ok("SPY", sig, "funnel", "x", "long", "open")
        self.assertFalse(ok)

    def test_5m_only_would_have_passed(self):
        # same fresh 5m bar, no 30m frame at all -> passes (proves the 30m frame
        # is what blocks above, not the 5m bar)
        sig = self._sig(min_5m=2, min_30m=None)
        with patch.object(P, "_log_stale_block"):
            ok = P._freshness_ok("SPY", sig, "funnel", "x", "long", "open")
        self.assertTrue(ok)

    def test_fresh_30m_passes(self):
        sig = self._sig(min_5m=2, min_30m=40)     # 40 < 90 floor
        with patch.object(P, "_log_stale_block"):
            ok = P._freshness_ok("SPY", sig, "funnel", "x", "long", "open")
        self.assertTrue(ok)


# ── R8: the regime object MARKS stale feeds ─────────────────────────────────
class RegimeFeedStaleness(unittest.TestCase):
    def test_feed_stale_helper(self):
        self.assertTrue(rl._feed_stale(400, "open"))          # old bar, open session
        self.assertFalse(rl._feed_stale(400, "postmarket"))   # outside session = expected
        self.assertFalse(rl._feed_stale(3, "open"))           # fresh bar
        self.assertIsNone(rl._feed_stale(None, "open"))       # unknown age

    def _vix_df(self, age_min, source):
        idx = pd.date_range("2026-07-13 09:30", periods=20, freq="5min",
                             tz="America/New_York")
        df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0,
                           "Close": range(20), "Volume": 1}, index=idx)
        df.attrs["age_min"] = age_min
        df.attrs["source"] = source
        return df

    def _build(self, vix_df):
        def fake_fetch(ticker, interval, period):
            return vix_df if ticker == "^VIX" else None   # SPY/QQQ absent
        null_pc = {"value": None, "source": "chain_proxy_spy_qqq",
                   "n_contracts": 0, "extreme": None}
        with patch.object(rl, "fetch_intraday", side_effect=fake_fetch), \
             patch.object(rl, "fetch_futures_quote", return_value=None), \
             patch.object(rl, "_iv_momentum", return_value=None), \
             patch("src.data.breadth.compute_breadth", return_value=None), \
             patch("src.data.breadth.log_breadth"), \
             patch("src.data.options_chain.put_call_proxy", return_value=null_pc), \
             patch("src.data.freshness.current_session_state", return_value="open"):
            return rl.build_regime()

    def test_stale_vix_marked(self):
        r = self._build(self._vix_df(age_min=400, source="yfinance"))
        fs = r["feed_staleness"]["VIX_5m"]
        self.assertTrue(fs["stale"])
        self.assertEqual(fs["source"], "yfinance")
        self.assertEqual(fs["age_min"], 400)
        self.assertIn("VIX_5m", r["stale_feeds"])

    def test_fresh_vix_not_marked(self):
        r = self._build(self._vix_df(age_min=3, source="tradier"))
        self.assertFalse(r["feed_staleness"]["VIX_5m"]["stale"])
        self.assertNotIn("VIX_5m", r["stale_feeds"])


# ── R9a: per-cycle wall-clock deadline ──────────────────────────────────────
class CycleDeadline(unittest.TestCase):
    def test_ample_budget_not_tripped(self):
        d = P._CycleDeadline(1000)
        self.assertFalse(d.tripped())
        self.assertGreater(d.remaining(), 0)

    def test_expired_budget_trips(self):
        self.assertTrue(P._CycleDeadline(-1).tripped())      # deterministic

    def test_simulated_hang_trips(self):
        d = P._CycleDeadline(0.02)
        time.sleep(0.05)                                     # cycle "hangs" past budget
        self.assertTrue(d.tripped())
        self.assertLessEqual(d.remaining(), 0)


# ── R9b: Tradier data-client failure-breaker ────────────────────────────────
class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"ok": 1}'


class TradierFailureBreaker(unittest.TestCase):
    def _client(self):
        c = TradierData()
        c.token = "test-token"   # force available() without a real credential
        return c

    def test_breaker_opens_then_fast_fails(self):
        c = self._client()
        with patch("src.data.tradier.time.sleep"), \
             patch("src.data.tradier.urllib.request.urlopen",
                   side_effect=OSError("black hole")) as uo:
            for _ in range(c.BREAKER_THRESHOLD):
                self.assertIsNone(c._get("/markets/clock", {}))
            self.assertTrue(c.breaker_open())
            calls_after_open = uo.call_count
            # subsequent call fast-fails WITHOUT another network attempt
            self.assertIsNone(c._get("/markets/clock", {}))
            self.assertEqual(uo.call_count, calls_after_open)

    def test_success_resets_streak(self):
        c = self._client()
        with patch("src.data.tradier.time.sleep"), \
             patch("src.data.tradier.urllib.request.urlopen", side_effect=OSError()):
            for _ in range(c.BREAKER_THRESHOLD - 1):   # one short of tripping
                c._get("/x", {})
        self.assertFalse(c.breaker_open())
        with patch("src.data.tradier.time.sleep"), \
             patch("src.data.tradier.urllib.request.urlopen", return_value=_Resp()):
            self.assertEqual(c._get("/x", {}), {"ok": 1})
        self.assertEqual(c._consecutive_failures, 0)


# ── R12: half-day calendar path is live again ───────────────────────────────
class HalfDaySession(unittest.TestCase):
    def _fake_client(self, calendar=None, calendar_exc=None):
        fake = MagicMock()
        fake.clock.return_value = None          # live clock down -> calendar path
        if calendar_exc is not None:
            fake.calendar.side_effect = calendar_exc
        else:
            fake.calendar.return_value = calendar
        return fake

    def test_early_close_reported_not_open_via_calendar(self):
        # tradier.calendar() returns the intraday times under open/close (R12);
        # a 13:00 ET half-day must NOT be reported open at 13:30 ET.
        cal = {"2026-07-03": {"status": "open", "open": "09:30", "close": "13:00"}}
        now = datetime(2026, 7, 3, 13, 30, tzinfo=F.ET)
        with patch("src.data.tradier.get_client", return_value=self._fake_client(cal)):
            state = F.current_session_state(now)
        self.assertNotEqual(state, "open")   # positions must not ride through the close
        self.assertEqual(state, "postmarket")

    def test_still_open_before_early_close(self):
        cal = {"2026-07-03": {"status": "open", "open": "09:30", "close": "13:00"}}
        now = datetime(2026, 7, 3, 12, 45, tzinfo=F.ET)
        with patch("src.data.tradier.get_client", return_value=self._fake_client(cal)):
            self.assertEqual(F.current_session_state(now), "open")

    def test_calendar_error_is_logged_not_swallowed(self):
        now = datetime(2026, 7, 3, 13, 30, tzinfo=F.ET)
        fake = self._fake_client(calendar_exc=RuntimeError("boom"))
        with patch("src.data.tradier.get_client", return_value=fake):
            with self.assertLogs("trader", level="WARNING") as cm:
                F.current_session_state(now)   # must not raise; falls through to static
        self.assertTrue(any("calendar" in line.lower() for line in cm.output))


if __name__ == "__main__":
    unittest.main()
