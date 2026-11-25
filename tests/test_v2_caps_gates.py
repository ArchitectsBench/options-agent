"""SCALP-v2 daily CAPS and GATES as the PIPELINE actually enforces them.

Every test drives the real ``pipeline._paper_execution_test`` (the function the
5-minute cycle calls) with an explicit cfg dict and synthetic ledger/book state,
and asserts through the engine's own readers (today_test_entry_count,
today_v2_attempt_count, open_paper_count) plus the pipeline's own decision
(submit called / not called, which cap message it printed). Nothing here
re-parses config.yaml and calls that a pass — the activation-night lesson
(2026-09-12: YAML said `enabled: true`, a YAML-reading test passed, the engine
ran v2 OFF) is that only the engine's path counts.

Hermetic: conftest's O-20 seal redirects LANE_DIR / FILLS_LOG to tmp; the broker
is never touched (submit_paper_order is a recording stub); build_trade is
patched; the kill-switch file is redirected to tmp.

Run: .venv/bin/python -m pytest -q tests/test_v2_caps_gates.py
"""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import src.engine.live_gate as lg
import src.engine.paper_trader as pt
import src.engine.pipeline as pl

import pytest

TODAY = datetime.now()
YESTERDAY = TODAY - timedelta(days=1)


class _MidSession(datetime):
    """The pipeline's clock pinned to 10:00 (today's date, caller's tz)."""
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def _mid_session_clock():
    """These tests exercise caps, gates and routing — not the 15:30 ET v2
    late-entry cutoff. Without a pinned clock every test in this file failed
    whenever the suite ran after 12:30 PT (found 2026-09-14: 17 red after the
    close, all green at a pinned 10:00 ET), which also made the post-close
    merge gate impossible to pass on a weekday. Tests that exercise the cutoff
    patch pl.datetime themselves; their patch nests inside this one."""
    with patch.object(pl, "datetime", _MidSession):
        yield

# A viable, affordable, above-floor trade: $1,000 budget / $360 -> qty 2.
TRADE = {"structure": "call_debit_spread", "strategy": "debit_spread",
         "direction": "long", "net_debit": 3.6, "max_loss": 3.6, "max_profit": 3.4,
         "expiration": "2099-01-09", "breakeven": 743.6,
         "legs": [{"symbol": "SPY990109C00740000", "action": "buy_to_open",
                   "bid": 5.25, "ask": 5.35, "mid": 5.3, "spread": 0.10},
                  {"symbol": "SPY990109C00747000", "action": "sell_to_open",
                   "bid": 1.65, "ask": 1.75, "mid": 1.7, "spread": 0.10}]}

VIABLE = {"debit_spread": {"verdict": "viable", "confirms_met": ["vwap_with_trend"]}}


def _signal():
    """Fresh bullish 5m/30m signal so the VWAP-consistency and 6b freshness
    gates take their ALLOW branches (we are testing the CAPS, not those)."""
    now = datetime.now(ZoneInfo("America/New_York")).isoformat()
    return {"last": 743.5, "atr": 2.1, "bar_time": now, "bar_time_30m": now,
            "squeeze": {"5m": "off", "15m": "on", "30m": "off"},
            "ema_stack": {"5m": "bullish", "15m": "bullish", "30m": "bullish"},
            "macd_5m": {"sign": "positive", "direction": "rising"},
            "vwap_dist_atr": 0.8, "volume_ratio": 1.4,
            "rs_vs_spy_30min_pct": 0.2, "opening_range": {"position": "above"}}


def _cfg(v2_enabled=True, **agent_overrides):
    """Explicit, hermetic config. The v2 block mirrors the frozen rules
    (10 filled / 30 attempts / $1.00 floor / marketable_ioc); the v1 caps are
    deliberately DIFFERENT (3/day) so a test can tell which cap the engine
    applied."""
    agent = {"paper_test_mode": True, "lane_isolation": True,
             "paper_test_max_per_day": 3, "paper_test_max_open": 7,
             "scalp_v2": {"enabled": v2_enabled, "fill_mode": "marketable_ioc",
                          "aggressiveness": 0.5, "min_net_debit": 1.00,
                          "max_per_day": 10, "max_attempts_per_day": 30}}
    agent.update(agent_overrides)
    return {"agent": agent,
            "risk": {"account_size": 50000, "max_risk_per_trade_pct": 2.0,
                     "max_daily_loss_pct": 3.0, "max_contracts": 10,
                     "max_open_per_lane": {"scalp": 7, "funnel": 3},
                     "max_book_risk_per_underlying_usd": 20000}}


# ── synthetic ledger / book state (through the engine's own writers) ──────────
def _rid(n: int, day: datetime = TODAY, ticker: str = "SPY") -> str:
    return f"{day:%Y%m%d}-SCALP-{n}-{ticker}-debit_spread"


def _ts(n: int, day: datetime = TODAY) -> str:
    return f"{day:%Y-%m-%d}T07:{n % 60:02d}:00"


def unfilled_row(n: int, day: datetime = TODAY) -> dict:
    """Shape of poll_fills' entry_ioc_unfilled event (paper_trader ~L1850)."""
    return {"ts": _ts(n, day), "event": "entry_ioc_unfilled", "rec_id": _rid(n, day),
            "ticker": "SPY", "order_id": 1000 + n, "limit_sent": 3.7,
            "fill_mode": "marketable_ioc", "stamp": "UNVALIDATED — PAPER ONLY"}


def v2_fill_row(n: int, day: datetime = TODAY) -> dict:
    return {"ts": _ts(n, day), "event": "paper_fill", "rec_id": _rid(n, day),
            "ticker": "SPY", "order_id": 1000 + n, "fill_mode": "marketable_ioc",
            "limit_sent": 3.7, "tif": "IOC", "rec_price": 3.6, "fill_price": 3.7,
            "quantity": 2, "slippage": 0.1, "stamp": "UNVALIDATED — PAPER ONLY"}


def v1_fill_row(n: int, day: datetime = TODAY, rec_id: str | None = None) -> dict:
    """A resting-mid (v1) fill: fill_mode resting_mid, tif DAY."""
    return {"ts": _ts(n, day), "event": "paper_fill", "rec_id": rec_id or _rid(n, day),
            "ticker": "SPY", "order_id": 2000 + n, "fill_mode": "resting_mid",
            "limit_sent": 3.6, "tif": "DAY", "rec_price": 3.6, "fill_price": 3.6,
            "quantity": 2, "slippage": 0.0, "stamp": "UNVALIDATED — PAPER ONLY"}


def seed_ledger(rows):
    for r in rows:
        pt._log_event(r)


def position(rec_id: str, ticker: str) -> dict:
    return {"rec_id": rec_id, "ticker": ticker, "strategy": "debit_spread",
            "direction": "long", "structure": "call_debit_spread", "quantity": 1,
            "max_loss": 1.0, "entry_price": 1.0, "legs": []}


def pending_open(rec_id: str, ticker: str, order_id: int) -> dict:
    return {"intent": "open", "rec_id": rec_id, "ticker": ticker,
            "strategy": "debit_spread", "direction": "long",
            "structure": "call_debit_spread", "order_id": order_id, "quantity": 1,
            "max_loss": 1.0, "fill_mode": "marketable_ioc", "tif": "IOC",
            "submitted_at": datetime.now().isoformat(timespec="seconds")}


class _Harness(unittest.TestCase):
    """Runs the REAL _paper_execution_test with a recording submit stub."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._ks = lg.KILL_SWITCH_FILE
        lg.KILL_SWITCH_FILE = Path(self.tmp.name) / "KILL_SWITCH"
        self.submits: list[dict] = []

    def tearDown(self):
        lg.KILL_SWITCH_FILE = self._ks
        self.tmp.cleanup()

    def _submit(self, rec, tag="UNVALIDATED-PAPER-ONLY", quantity=1, **kw):
        self.submits.append({"rec": rec, "tag": tag, "quantity": quantity, **kw})
        return {"submitted": True, "order_id": 42, "status": "ok"}

    def run_gate(self, cfg, trade=TRADE, tickers=("SPY",), viable=True, cycle=7):
        signals = {t: _signal() for t in tickers}
        applicability = {t: dict(VIABLE) for t in tickers} if viable else {}
        record = {"regime": {"bias": "bullish"}}
        out = io.StringIO()
        with patch("src.engine.options_selector.build_trade",
                   return_value=(dict(trade) if trade else None, None)), \
                patch("src.data.freshness.current_session_state", return_value="open"), \
                patch("src.engine.entry_features.build_entry_features", return_value={}), \
                contextlib.redirect_stdout(out):
            pl._paper_execution_test(signals, applicability, cycle, cfg, record,
                                     self._submit, pt.has_open_exposure)
        return out.getvalue(), record

    # message fragments the pipeline prints for each gate
    ATTEMPT_CAP = "daily IOC ATTEMPT cap reached"
    ENTRY_CAP = "daily entry cap reached"
    CONCURRENT_CAP = "concurrent cap reached"


# ═════════════════════════════════════════════════════════════════════════════
class AttemptCapBindsBeforeFillCap(_Harness):
    """max_attempts_per_day (30) is the outer bound on IOC attempts and is
    checked BEFORE the filled-entry cap."""

    def test_thirty_attempts_block_with_the_attempt_cap_not_the_fill_cap(self):
        """Prevents: a day where nothing fills firing IOCs every 5 minutes — 30
        unfilled IOCs must end the day via the ATTEMPT cap, and the engine
        must say so (not mislabel it as the filled-entry cap)."""
        seed_ledger([unfilled_row(n) for n in range(30)])
        self.assertEqual(pt.today_v2_attempt_count(), 30)
        printed, _ = self.run_gate(_cfg())
        self.assertEqual(self.submits, [])
        self.assertIn(self.ATTEMPT_CAP, printed)
        self.assertNotIn(self.ENTRY_CAP, printed)

    def test_ten_attempts_and_zero_fills_still_permits_an_entry(self):
        """Prevents: the v2 window going quiet with ZERO positions on a thin
        morning. 10 unfilled IOCs are 10 attempts (of 30) and 0 fills (of 10),
        so the 11th attempt must be permitted.

        DOCUMENTED DEFECT (expected to FAIL until fixed): today_test_entry_count
        counts every FILLS_LOG row carrying today's -SCALP- rec_id — including
        entry_ioc_unfilled — so 10 unfilled IOCs trip the FILLED-entries cap
        and the engine prints 'daily entry cap reached' with nothing booked.
        max_attempts_per_day can never bind."""
        seed_ledger([unfilled_row(n) for n in range(10)])
        self.assertEqual(pt.today_v2_attempt_count(), 10)
        printed, _ = self.run_gate(_cfg())
        self.assertNotIn(self.ENTRY_CAP, printed,
                         "10 UNFILLED IOCs were charged to the FILLED-entries cap")
        self.assertEqual(len(self.submits), 1)

    def test_a_cycle_places_at_most_one_order_even_with_many_candidates(self):
        """Prevents: one cycle blowing through the attempt cap by placing an IOC
        per candidate. The loop returns after the first submitted order, so the
        once-per-cycle cap check cannot be overshot by more than one."""
        # 3 viable + 3 liquid fallbacks = 6 candidates, all buildable
        printed, _ = self.run_gate(_cfg(), tickers=("SPY", "QQQ", "IWM"))
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(printed.count("[PAPER-TEST] EXECUTION-TEST"), 1)


# ═════════════════════════════════════════════════════════════════════════════
class FillCapIsTenFilledEntries(_Harness):
    """max_per_day: 10 FILLED entries."""

    def test_ten_v2_fills_block_with_the_entry_cap_message(self):
        """Prevents: an 11th filled entry on a day the frozen rule allows 10."""
        seed_ledger([v2_fill_row(n) for n in range(10)])
        self.assertEqual(pt.today_test_entry_count(), 10)
        self.assertEqual(pt.today_v2_attempt_count(), 10)   # 10 < 30: attempts not the binder
        printed, _ = self.run_gate(_cfg())
        self.assertEqual(self.submits, [])
        self.assertIn(self.ENTRY_CAP, printed)
        self.assertNotIn(self.ATTEMPT_CAP, printed)

    def test_nine_v2_fills_permit_a_tenth_in_v2_mode(self):
        """Prevents: the fill cap binding one early (off-by-one) or the rec
        losing its v2 fill_mode stamp on the way to the broker."""
        seed_ledger([v2_fill_row(n) for n in range(9)])
        printed, record = self.run_gate(_cfg())
        self.assertEqual(len(self.submits), 1)
        s = self.submits[0]
        self.assertEqual(s["fill_mode"], "marketable_ioc")
        self.assertEqual(s["rec"]["fill_mode"], "marketable_ioc")
        self.assertIn("-SCALP-", s["rec"]["rec_id"])
        self.assertEqual(s["quantity"], 2)          # risk-kernel sized, not 1
        self.assertEqual(record["paper_execution_tests"][0]["rec_id"], s["rec"]["rec_id"])

    def test_v1_cap_of_three_is_not_applied_while_v2_is_on(self):
        """Prevents: the legacy paper_test_max_per_day (3 here) silently
        throttling the v2 window that pre-registered 10."""
        seed_ledger([v2_fill_row(n) for n in range(5)])   # > v1's 3, < v2's 10
        printed, _ = self.run_gate(_cfg())
        self.assertEqual(len(self.submits), 1)
        self.assertNotIn(self.ENTRY_CAP, printed)

    def test_book_rows_count_toward_the_fill_cap_and_dedup_against_the_ledger(self):
        """Prevents: a position/pending intent that has not (yet) produced a
        ledger row slipping under the cap — and the same rec_id being counted
        twice when it is in BOTH the book and the ledger."""
        seed_ledger([v2_fill_row(n) for n in range(8)])
        # rec 0 is also an open position (dedup: still 8), plus one pending v2
        # IOC intent on QQQ and one open position on IWM that never logged
        pt._save(pt.POSITIONS_FILE, [position(_rid(0), "SPY"),
                                     position(_rid(50, ticker="IWM"), "IWM")])
        pt._save(pt.PENDING_FILE, [pending_open(_rid(51, ticker="QQQ"), "QQQ", 777)])
        self.assertEqual(pt.today_test_entry_count(), 10)
        printed, _ = self.run_gate(_cfg())
        self.assertEqual(self.submits, [])
        self.assertIn(self.ENTRY_CAP, printed)

    def test_reader_counts_fills_not_unfilled_attempts(self):
        """Prevents: the filled-entry counter and the attempt counter agreeing
        with each other instead of with their names — 9 unfilled + 1 fill is
        1 filled entry and 10 attempts.

        DOCUMENTED DEFECT (expected to FAIL until fixed): today_test_entry_count
        returns 10 here because it never looks at the event type."""
        seed_ledger([unfilled_row(n) for n in range(9)] + [v2_fill_row(9)])
        self.assertEqual(pt.today_v2_attempt_count(), 10)
        self.assertEqual(pt.today_test_entry_count(), 1)


# ═════════════════════════════════════════════════════════════════════════════
class PriceFloorBlocksAndLogs(_Harness):
    """min_net_debit: a sub-floor contract is logged entry_blocked_v2_price_floor
    and skipped — never submitted, never charged to a cap."""

    CHEAP = {**TRADE, "net_debit": 0.45, "max_loss": 0.45}

    def _floor_rows(self):
        rows = [json.loads(l) for l in pt.FILLS_LOG.read_text().splitlines()] \
            if pt.FILLS_LOG.exists() else []
        return [r for r in rows if r.get("event") == "entry_blocked_v2_price_floor"]

    def test_sub_floor_trade_is_logged_and_skipped_not_submitted(self):
        """Prevents: a $0.45 0DTE contract (11% spread cost) being sent under
        the v2 label, or being dropped silently with no ledger trace."""
        printed, record = self.run_gate(_cfg(), trade=self.CHEAP, viable=False)  # fallback only
        self.assertEqual(self.submits, [])
        rows = self._floor_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "SPY")
        self.assertAlmostEqual(rows[0]["net_debit"], 0.45)
        self.assertAlmostEqual(rows[0]["floor"], 1.0)
        self.assertEqual(rows[0]["stamp"], "UNVALIDATED — PAPER ONLY")
        self.assertIn("no buildable setup", printed)
        self.assertNotIn("paper_execution_tests", record)
        # a floor block is neither a fill nor an attempt
        self.assertEqual(pt.today_test_entry_count(), 0)
        self.assertEqual(pt.today_v2_attempt_count(), 0)

    def test_floor_is_inclusive_at_exactly_min_net_debit(self):
        """Prevents: an off-by-one that blocks a $1.00 contract the rule allows."""
        at_floor = {**TRADE, "net_debit": 1.00, "max_loss": 1.00}
        self.run_gate(_cfg(), trade=at_floor, viable=False)
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(self._floor_rows(), [])

    def test_floor_blocks_every_sub_floor_candidate_in_the_cycle(self):
        """Prevents: the loop stopping at the first floor block instead of
        trying the next candidate (and documents: one row per blocked
        candidate per cycle, no dedupe)."""
        self.run_gate(_cfg(), trade=self.CHEAP, tickers=("SPY", "QQQ", "IWM"), viable=False)
        self.assertEqual(self.submits, [])
        self.assertEqual(sorted(r["ticker"] for r in self._floor_rows()), ["IWM", "QQQ", "SPY"])

    def test_floor_does_not_apply_when_v2_is_off(self):
        """Prevents: the v2 price floor leaking into v1 (byte-identical while off)."""
        self.run_gate(_cfg(v2_enabled=False), trade=self.CHEAP, viable=False)
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(self.submits[0]["fill_mode"], "resting_mid")
        self.assertEqual(self._floor_rows(), [])


# ═════════════════════════════════════════════════════════════════════════════
class GatesUseTodayOnly(_Harness):
    """Both caps are DAILY: yesterday's ledger must not throttle today."""

    def test_yesterdays_attempts_and_fills_do_not_count_today(self):
        """Prevents: Tuesday 06:30 opening already capped by Monday's 30
        attempts / 10 fills (the counters key on the day, twice over: ts for
        attempts, rec_id date-prefix for fills)."""
        seed_ledger([unfilled_row(n, YESTERDAY) for n in range(30)]
                    + [v2_fill_row(100 + n, YESTERDAY) for n in range(10)])
        self.assertEqual(pt.today_v2_attempt_count(), 0)
        self.assertEqual(pt.today_test_entry_count(), 0)
        printed, _ = self.run_gate(_cfg())
        self.assertEqual(len(self.submits), 1)
        self.assertNotIn(self.ATTEMPT_CAP, printed)
        self.assertNotIn(self.ENTRY_CAP, printed)

    def test_yesterdays_positions_still_open_do_not_count_as_todays_entries(self):
        """Prevents: an overnight SCALP position (yesterday's rec_id) being
        charged to today's filled-entry cap. It still counts for concurrency."""
        pt._save(pt.POSITIONS_FILE, [position(_rid(n, YESTERDAY, "AAPL"), "AAPL")
                                     for n in range(3)])
        self.assertEqual(pt.today_test_entry_count(), 0)
        self.assertEqual(pt.open_paper_count("scalp"), 3)
        self.run_gate(_cfg())
        self.assertEqual(len(self.submits), 1)

    def test_v1_mode_fills_are_not_ioc_attempts(self):
        """Prevents: resting-mid (v1) fills inflating the IOC attempt count —
        they are filled SCALP entries (fill cap) but never IOC attempts."""
        seed_ledger([v1_fill_row(n) for n in range(5)])
        self.assertEqual(pt.today_v2_attempt_count(), 0)
        self.assertEqual(pt.today_test_entry_count(), 5)
        printed, _ = self.run_gate(_cfg())
        self.assertEqual(len(self.submits), 1)
        # ...and when ten of them are on the book, it is the ENTRY cap that speaks
        seed_ledger([v1_fill_row(n) for n in range(5, 10)])
        self.submits.clear()
        printed, _ = self.run_gate(_cfg())
        self.assertEqual(self.submits, [])
        self.assertIn(self.ENTRY_CAP, printed)
        self.assertNotIn(self.ATTEMPT_CAP, printed)
        self.assertEqual(pt.today_v2_attempt_count(), 0)

    def test_funnel_lane_rows_never_count_toward_scalp_caps(self):
        """Prevents: the Claude-approved funnel's fills (rec_ids without the
        SCALP marker) consuming the SCALP daily cap."""
        seed_ledger([v1_fill_row(n, rec_id=f"{TODAY:%Y-%m-%d}-{n:03d}") for n in range(10)]
                    + [{**v2_fill_row(n), "rec_id": f"{TODAY:%Y-%m-%d}-{50 + n:03d}",
                        "fill_mode": "resting_mid"} for n in range(3)])
        self.assertEqual(pt.today_test_entry_count(), 0)
        self.assertEqual(pt.today_v2_attempt_count(), 0)
        self.run_gate(_cfg())
        self.assertEqual(len(self.submits), 1)


# ═════════════════════════════════════════════════════════════════════════════
class V2OffKeepsV1CapsUnchanged(_Harness):
    """With scalp_v2.enabled false, the legacy paper_test_max_per_day cap and
    resting-mid mechanism apply, and nothing v2 is consulted."""

    def test_v1_cap_binds_at_paper_test_max_per_day(self):
        """Prevents: flipping v2 off leaving the v2 cap (10) in force."""
        seed_ledger([v1_fill_row(n) for n in range(3)])      # v1 cap is 3 in _cfg
        printed, _ = self.run_gate(_cfg(v2_enabled=False))
        self.assertEqual(self.submits, [])
        self.assertIn(self.ENTRY_CAP, printed)
        self.assertNotIn(self.ATTEMPT_CAP, printed)

    def test_v1_orders_are_resting_mid_and_carry_no_v2_stamp(self):
        """Prevents: v2 OFF sending IOC orders (byte-identical v1 guarantee)."""
        seed_ledger([v1_fill_row(n) for n in range(2)])
        self.run_gate(_cfg(v2_enabled=False))
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(self.submits[0]["fill_mode"], "resting_mid")
        self.assertEqual(self.submits[0]["rec"]["fill_mode"], "resting_mid")

    def test_attempt_counter_is_never_consulted_while_off(self):
        """Prevents: a v1 session being throttled (or crashed) by the v2
        attempt counter — it must not be on the v1 path at all."""
        def _boom():
            raise AssertionError("today_v2_attempt_count consulted with v2 OFF")
        with patch.object(pt, "today_v2_attempt_count", side_effect=_boom):
            self.run_gate(_cfg(v2_enabled=False))
        self.assertEqual(len(self.submits), 1)

    def test_v2_max_per_day_does_not_loosen_v1(self):
        """Prevents: the v2 block's max_per_day (10) raising the v1 cap while
        v2 is off — 5 fills is under 10 but over v1's 3."""
        seed_ledger([v1_fill_row(n) for n in range(5)])
        printed, _ = self.run_gate(_cfg(v2_enabled=False))
        self.assertEqual(self.submits, [])
        self.assertIn(self.ENTRY_CAP, printed)


# ═════════════════════════════════════════════════════════════════════════════
class ConcurrentCapIsLaneScoped(_Harness):
    """paper_test_max_open / risk.max_open_per_lane.scalp bound the SCALP lane's
    own book; the funnel's positions are not SCALP's concurrency."""

    def test_seven_scalp_positions_block_on_the_concurrent_cap(self):
        """Prevents: an 8th concurrent SCALP position past the frozen max_open 7."""
        pt._save(pt.POSITIONS_FILE, [position(_rid(n, ticker="AAPL"), "AAPL") for n in range(7)])
        self.assertEqual(pt.open_paper_count("scalp"), 7)
        printed, _ = self.run_gate(_cfg())
        self.assertEqual(self.submits, [])
        self.assertIn(self.CONCURRENT_CAP, printed)

    def test_pending_scalp_intents_count_toward_concurrency(self):
        """Prevents: unfilled-yet IOC intents (write-ahead records) being
        invisible to the concurrent cap."""
        pt._save(pt.POSITIONS_FILE, [position(_rid(n, ticker="AAPL"), "AAPL") for n in range(4)])
        pt._save(pt.PENDING_FILE, [pending_open(_rid(10 + n, ticker="MSFT"), "MSFT", 500 + n)
                                   for n in range(3)])
        self.assertEqual(pt.open_paper_count("scalp"), 7)
        printed, _ = self.run_gate(_cfg())
        self.assertEqual(self.submits, [])
        self.assertIn(self.CONCURRENT_CAP, printed)

    def test_funnel_positions_do_not_consume_scalp_slots(self):
        """Prevents: three (here seven) funnel positions starving SCALP-v2 of
        its own 7 concurrent slots on exactly the adverse-regime day the
        frozen criteria require.

        DOCUMENTED DEFECT (expected to FAIL until fixed): pipeline.py calls
        open_paper_count() with no lane (pooled) against paper_test_max_open;
        max_open_for_lane('scalp') exists but has no production caller."""
        funnel = [position(f"{TODAY:%Y-%m-%d}-{n:03d}", t)
                  for n, t in enumerate(("AAPL", "MSFT", "NVDA", "AMZN", "META", "AMD", "GOOGL"))]
        pt._save(pt.POSITIONS_FILE, funnel)
        self.assertEqual(pt.open_paper_count("funnel"), 7)   # they landed in the funnel lane
        self.assertEqual(pt.open_paper_count("scalp"), 0)
        self.assertEqual(pt.max_open_for_lane("scalp", _cfg()), 7)
        printed, _ = self.run_gate(_cfg())
        self.assertNotIn(self.CONCURRENT_CAP, printed,
                         "funnel positions were charged to SCALP's concurrent cap")
        self.assertEqual(len(self.submits), 1)


if __name__ == "__main__":
    unittest.main()
