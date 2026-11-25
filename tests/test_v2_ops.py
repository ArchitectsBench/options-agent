"""SCALP-v2 OPERATIONS surface (2026-09-12, activation night).

v2 was ACTIVATED tonight and trades Monday 06:30 PT. The activation-night
surprise — YAML said `enabled: true`, a test that read the YAML passed, and the
engine restarted with v2 OFF because scalp_v2_config() swallowed a NameError
and returned {} — is the model for everything here: every assertion goes
through the ENGINE'S OWN reader/path, on synthetic state with teeth, never by
re-parsing a file the engine does not read.

Surfaces covered (each test docstring names the trading-hours surprise it
prevents):
  * restart_blocking_positions with a pending v2 IOC intent
  * scripts/deadman.sh 'broker-execution-dead' regex vs v2 rejection lines
  * exit_manager.evaluate_exit / watchdog.evaluate_tick are fill_mode-blind
  * src/api/feed.py renders the new v2 ledger rows without raising
  * scalp_v2_config() read failures are LOUD and never silently downgrade v2

Hermetic: conftest's O-20 seal redirects FILLS_LOG / lane books to tmp and
refuses production writes; the broker is a stub; deadman's regex is exercised
on a synthetic log file, never on logs/agent.log.

Tests marked "DOCUMENTED DEFECT" assert the CORRECT behaviour and are expected
to FAIL until src/ is fixed (src/ is not edited here).
"""
import json
import re
import shlex
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import src.engine.paper_trader as pt

ROOT = Path(pt.__file__).resolve().parents[2]
TODAY = datetime.now()
TODAY_ID = f"{TODAY:%Y%m%d}"
TODAY_TS = f"{TODAY:%Y-%m-%d}"
YESTERDAY = TODAY - timedelta(days=1)

SINGLE = {"structure": "0DTE long call", "strategy": "0dte_directional", "direction": "long",
          "expiration": "2099-01-01", "net_debit": 1.50, "max_loss": 1.50, "max_profit": None,
          "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open",
                    "bid": 1.45, "ask": 1.55, "mid": 1.50, "spread": 0.10}]}


def _ioc_intent(rec_id: str, submitted_at: datetime, order_id=99) -> dict:
    """A pending v2 entry intent exactly as submit_paper_order writes it."""
    return {"intent": "open", "intent_seq": submitted_at.isoformat(timespec="microseconds"),
            "order_id": order_id, "rec_id": rec_id, "ticker": "SPY", "quantity": 1,
            "structure": "0DTE long call", "strategy": "0dte_directional", "direction": "long",
            "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open"}],
            "rec_price": 1.50, "max_profit": None, "max_loss": 1.50, "expiration": "2099-01-01",
            "submitted_at": submitted_at.isoformat(timespec="seconds"),
            "tag": "EXECUTION-TEST", "fill_mode": "marketable_ioc",
            "limit_sent": 1.55, "tif": "IOC"}


# ═══════════════════════════════════════════════════════════════════════════
# 1. restart_blocking_positions vs a pending v2 IOC intent
# ═══════════════════════════════════════════════════════════════════════════

class RestartGateWithPendingIocIntent(unittest.TestCase):
    """The 06:20 restart gate (scripts/ops/daily_engine_restart.sh GATE 3)
    refuses when restart_blocking_positions() > 0 and pages the operator
    'N LIVE position(s) at the broker ... Flatten or reconcile'.

    DOCUMENTED behaviour (the function's own docstring): only a LIVE position
    the engine can still manage — not past expiry AND confirmed at the broker —
    deserves protection; an UNKNOWN broker view never blocks.

    UNDOCUMENTED: what a PENDING ORDER record does. The docstring speaks only
    of positions; the code adds len(pending) unconditionally, never checking a
    pending order against the broker. Flagged here so the parent can decide."""

    CLEAN = {"status": "clean", "alarms": []}
    UNKNOWN = {"status": "could_not_check", "alarms": []}

    def _run(self, positions, pending, checked):
        with patch.object(pt, "load_positions", return_value=positions), \
                patch.object(pt, "_load", return_value=pending):
            return pt.restart_blocking_positions(today=f"{TODAY:%Y-%m-%d}", checked=checked)

    def test_stale_ioc_intent_from_yesterday_does_not_block_restart(self):
        """DOCUMENTED DEFECT — prevents a 06:20 restart REFUSAL and a false
        'LIVE at the broker' page over an IOC the broker cancelled in
        milliseconds yesterday.

        Monday 12:55 the last cycle sends a v2 IOC; the 13:00 tick skips (market
        closed) so poll_fills never sees the broker's Cancelled and the intent
        sits in the scalp orders book overnight. An IOC is terminal within the
        placing second — it is by construction NOT a manageable live order, so
        by the function's own documented rule it must not block."""
        stale = _ioc_intent(f"{YESTERDAY:%Y%m%d}-SCALP-77-SPY-0dte_directional",
                            YESTERDAY.replace(hour=12, minute=55, second=3))
        n, why = self._run([], [stale], self.CLEAN)
        self.assertEqual(n, 0, f"a day-old IOC intent blocked the restart: {why}")

    def test_stale_ioc_intent_with_unknown_broker_view_does_not_block(self):
        """Prevents the deadlock re-entering through the ORDERS book: with the
        broker unreachable nothing is being managed, so the restart is the
        remedy (already the documented rule for positions)."""
        stale = _ioc_intent(f"{YESTERDAY:%Y%m%d}-SCALP-77-SPY-0dte_directional",
                            YESTERDAY.replace(hour=12, minute=55, second=3))
        n, _ = self._run([], [stale], self.UNKNOWN)
        self.assertEqual(n, 0)

    def test_ioc_intent_confirmed_open_at_broker_is_undocumented_territory(self):
        """FLAG (not a defect claim): a today-dated, bound IOC intent with a
        CLEAN broker view currently blocks (n == 1). Nothing in the docstring
        says a pending ORDER should, and the audit never consults the broker's
        order book to confirm it is live. Pinned so a future change of this
        behaviour is deliberate, not accidental."""
        fresh = _ioc_intent(f"{TODAY_ID}-SCALP-3-SPY-0dte_directional",
                            TODAY.replace(hour=6, minute=35, second=0))
        n, why = self._run([], [fresh], self.CLEAN)
        self.assertEqual(n, 1, why)
        self.assertIn("confirmed at the broker", why,
                      "the reason string claims broker confirmation for an order the "
                      "audit never checked — see docstring")

    def test_live_position_plus_stale_ioc_counts_only_the_position(self):
        """DOCUMENTED DEFECT — prevents the page saying '2 LIVE position(s)'
        when one manageable position and one dead IOC remainder are in the
        book: the number the operator is told to 'flatten' must be real."""
        live = {"rec_id": f"{TODAY_ID}-SCALP-2-SPY-0dte_directional", "ticker": "SPY",
                "expiration": "2099-01-01", "legs": [{"symbol": "SPY990101C00500000"}]}
        stale = _ioc_intent(f"{YESTERDAY:%Y%m%d}-SCALP-77-QQQ-0dte_directional",
                            YESTERDAY.replace(hour=12, minute=55, second=3))
        n, _ = self._run([live], [stale], self.CLEAN)
        self.assertEqual(n, 1)


# ═══════════════════════════════════════════════════════════════════════════
# 2. deadman.sh 'broker-execution-dead' regex vs v2 rejection lines
# ═══════════════════════════════════════════════════════════════════════════

class DeadmanBrokerExecutionRegex(unittest.TestCase):
    """Layer 3b of scripts/deadman.sh greps TODAY's agent.log for the dead-
    client signature. The regex is extracted from the SCRIPT (so the test
    follows any edit to it) and exercised with real `grep -cE` on a synthetic
    log in tmp — never on logs/agent.log."""

    SCRIPT = ROOT / "scripts" / "deadman.sh"

    @classmethod
    def setUpClass(cls):
        text = cls.SCRIPT.read_text()
        m = re.search(r'grep -cE "([^"]+)"', text)
        assert m, "deadman.sh no longer greps with -cE — update this test"
        cls.pattern = m.group(1)
        m2 = re.search(r'\[ "\$\{DEAD:-0\}" -ge (\d+) \]', text)
        assert m2, "deadman.sh threshold line not found — update this test"
        cls.threshold = int(m2.group(1))

    def _count(self, tmp: Path, lines: list[str]) -> int:
        f = tmp / "agent.log"
        f.write_text("\n".join(lines) + "\n")
        cmd = (f'grep "^{TODAY_TS}" {shlex.quote(str(f))} '
               f'| grep -cE {shlex.quote(self.pattern)} || true')
        out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=10)
        return int((out.stdout.strip() or "0").splitlines()[-1])

    def _line(self, level: str, msg: str, day: str = TODAY_TS) -> str:
        return f"{day} 06:35:12,001 {level} {msg}"

    def test_v2_qualify_failure_on_a_dead_client_matches(self):
        """Prevents a Monday where every v2 IOC dies at contract qualification
        on a dead IBKR client and the dead-man never pages: the v2 path logs
        'qualify failed for <occ>: Not connected' (ibkr_orders.place_option_order)
        and that line must count."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            n = self._count(Path(d), [
                self._line("WARNING", "[ibkr] qualify failed for SPY260914C00650000: Not connected"),
            ])
        self.assertEqual(n, 1, f"pattern {self.pattern!r} missed the v2 qualify-failed line")

    def test_three_v2_rejections_reach_the_threshold(self):
        """Prevents the threshold arithmetic silently drifting: three dead-client
        rejections in today's log is the documented page condition."""
        import tempfile
        lines = [self._line("WARNING", f"[ibkr] qualify failed for SPY260914C0065{i}000: Not connected")
                 for i in range(3)]
        lines.append(self._line("WARNING", "paper entry rejected for SPY: {'error': 'qualify_failed: Not connected'}"))
        with tempfile.TemporaryDirectory() as d:
            n = self._count(Path(d), lines)
        self.assertGreaterEqual(n, self.threshold)

    def test_yesterdays_rejections_do_not_count(self):
        """Prevents Friday's dead-client lines paging on Monday morning."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            n = self._count(Path(d), [
                self._line("WARNING", "[ibkr] qualify failed for SPY: Not connected",
                           day=f"{YESTERDAY:%Y-%m-%d}")] * 5)
        self.assertEqual(n, 0)

    def test_an_unfilled_ioc_is_not_a_dead_client(self):
        """Prevents a FALSE 'broker-execution-dead' page (and a needless
        engine kickstart mid-session) when v2 IOCs simply do not fill: the
        engine's 'ended: canceled' / 'ended: rejected' / 'paper entry rejected'
        lines for a healthy client carry no 'Not connected' and must not match."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            n = self._count(Path(d), [
                self._line("INFO", "paper order 1523 for SPY ended: canceled"),
                self._line("INFO", "paper order 1524 for QQQ ended: rejected"),
                self._line("WARNING", "paper entry rejected for IWM: {'error': 'rejected', "
                                      "'reason': 'Order Canceled - reason:'}"),
                self._line("WARNING", "paper entry AMBIGUOUS for SPY: transport_error — intent left unbound"),
            ] * 3)
        self.assertEqual(n, 0)

    def test_a_session_of_ioc_rejects_with_zero_fills_has_a_liveness_check(self):
        """DOCUMENTED DEFECT (gap) — prevents a full session of IOC attempts
        that never fill (or all come back rejected) passing every liveness
        layer: pid alive, cycles advancing, ops sweep fresh, no 'Not connected'.
        The 08-19..21 blind spot, re-entered through the new fill mechanism.

        The engine's own counters can see it (today_v2_attempt_count() +
        rejects >= 5 with today_test_entry_count() == 0 after open+60min); no
        deadman layer or liveness.py helper reads them."""
        import importlib
        import sys
        sys.path.insert(0, str(ROOT / "scripts" / "ops"))
        lv = importlib.import_module("liveness")
        helpers = [n for n in dir(lv) if callable(getattr(lv, n))
                   and re.search(r"v2|fill|attempt|reject", n, re.I)]
        script = self.SCRIPT.read_text()
        wired = bool(helpers) or bool(re.search(
            r"today_v2_attempt_count|entry_ioc_unfilled|paper entry rejected|v2-no-fills", script))
        self.assertTrue(wired, "no liveness layer reads the v2 attempt/fill counters — a v2 "
                               "session with N attempts and 0 fills is invisible to deadman")


# ═══════════════════════════════════════════════════════════════════════════
# 3. evaluate_exit / evaluate_tick are blind to fill_mode
# ═══════════════════════════════════════════════════════════════════════════

class ExitRulesAreFillModeBlind(unittest.TestCase):
    """v2 changes HOW a position is entered, never how it is managed. A v2
    position carries fill_mode/tif/limit_sent it never carried before; the
    exit rules must return the identical verdict with or without those keys."""

    CFG = {"flat_time_et": "15:45", "stop_loss_pct": 0.50, "profit_target_pct": 0.50,
           "single_leg_target_gain_pct": 0.50}

    def _pos(self, **over) -> dict:
        p = {"rec_id": f"{TODAY_ID}-SCALP-4-SPY-0dte_directional", "ticker": "SPY",
             "direction": "long", "entry_price": 1.50, "max_profit": None, "max_loss": 1.50,
             "quantity": 3, "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open"}]}
        p.update(over)
        return p

    def _v2(self, p: dict) -> dict:
        return {**p, "fill_mode": "marketable_ioc", "tif": "IOC", "limit_sent": 1.55,
                "partial": True}

    def _cases(self):
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        midday = datetime(2026, 9, 14, 11, 0, tzinfo=et)
        flat = datetime(2026, 9, 14, 15, 46, tzinfo=et)
        spread = self._pos(max_profit=1.00, entry_price=1.00, structure="call debit spread")
        return [
            ("stop_loss", self._pos(), 0.70, None, midday),
            ("single_leg_target", self._pos(), 2.30, None, midday),
            ("spread_target", spread, 1.55, None, midday),
            ("hold", self._pos(), 1.52, {"vwap_dist_atr": 0.4}, midday),
            ("vwap_invalidation", self._pos(), 1.52, {"vwap_dist_atr": -0.3}, midday),
            ("time_flat", self._pos(), 1.52, None, flat),
            ("no_quote", self._pos(), None, None, midday),
        ]

    def test_evaluate_exit_identical_with_and_without_v2_stamps(self):
        """Prevents a Monday where a v2 position's stop/target/VWAP/13:00 exits
        fire differently (or not at all) because the position record now
        carries fill_mode='marketable_ioc'."""
        from src.engine.exit_manager import evaluate_exit
        for name, pos, value, sig, now in self._cases():
            with self.subTest(case=name):
                v1 = evaluate_exit(pos, value, sig, now, self.CFG)
                v2 = evaluate_exit(self._v2(pos), value, sig, now, self.CFG)
                self.assertEqual(v1, v2)
                # the case must actually exercise a rule (teeth)
                if name in ("hold", "no_quote"):
                    self.assertIsNone(v1)
                else:
                    self.assertIsNotNone(v1, f"{name} did not fire")
                    self.assertTrue(v1.startswith(name.split("_")[0]) or name.endswith("target")
                                    and v1.startswith("profit_target")
                                    or name == "vwap_invalidation" and v1.startswith("invalidation"),
                                    v1)

    def test_watchdog_tick_identical_with_and_without_v2_stamps(self):
        """Prevents the streaming watchdog's value-based exits diverging from
        the 5-minute loop on a v2 position."""
        from src.engine.watchdog import evaluate_tick
        for name, pos, value in [("stop", self._pos(), 0.70), ("target", self._pos(), 2.30),
                                 ("hold", self._pos(), 1.52),
                                 ("spread_target", self._pos(max_profit=1.0, entry_price=1.0), 1.55)]:
            with self.subTest(case=name):
                self.assertEqual(evaluate_tick(pos, value, self.CFG),
                                 evaluate_tick(self._v2(pos), value, self.CFG))
                if name != "hold":
                    self.assertIsNotNone(evaluate_tick(pos, value, self.CFG))


# ═══════════════════════════════════════════════════════════════════════════
# 4. src/api/feed.py with the new v2 ledger rows present
# ═══════════════════════════════════════════════════════════════════════════

class FeedRendersV2LedgerRows(unittest.TestCase):
    """The cockpit reads the same fills.jsonl the engine writes. Monday adds
    two row shapes it has never seen: entry_ioc_unfilled (rec_id, no
    fill_price/slippage) and entry_blocked_v2_price_floor (NO rec_id at all)."""

    RID = f"{TODAY_ID}-SCALP-5-SPY-0dte_directional"

    def _ledger(self) -> list[dict]:
        t = lambda hh, mm: f"{TODAY_TS}T{hh:02d}:{mm:02d}:00"  # noqa: E731
        return [
            {"ts": t(6, 35), "event": "entry_blocked_v2_price_floor", "ticker": "QQQ",
             "strategy": "0dte_directional", "net_debit": 0.45, "floor": 1.0,
             "stamp": "UNVALIDATED — PAPER ONLY"},
            {"ts": t(6, 40), "event": "entry_ioc_unfilled", "rec_id": f"{TODAY_ID}-SCALP-4-IWM-debit_spread",
             "ticker": "IWM", "order_id": 7, "limit_sent": 1.08, "fill_mode": "marketable_ioc",
             "stamp": "UNVALIDATED — PAPER ONLY"},
            {"ts": t(6, 45), "event": "paper_fill", "rec_id": self.RID, "ticker": "SPY",
             "quantity": 3, "fill_price": 1.55, "slippage": 0.05, "fill_mode": "marketable_ioc",
             "limit_sent": 1.55, "tif": "IOC"},
            {"ts": t(7, 30), "event": "paper_exit", "rec_id": self.RID, "ticker": "SPY",
             "exit_reason": "profit_target", "close_reason": "profit_target",
             "pnl_per_contract": 60.0, "pnl_total": 180.0},
        ]

    def setUp(self):
        import tempfile
        import src.api.feed as feed
        self.feed = feed
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logs" / "observation").mkdir(parents=True)
        (self.tmp / "data").mkdir()
        (self.tmp / "logs" / "observation" / "fills.jsonl").write_text(
            "\n".join(json.dumps(r) for r in self._ledger()) + "\n")
        feed._CACHE.clear()
        self._patches = [
            patch.object(feed, "ROOT", self.tmp),
            patch.object(feed, "_cfg", return_value={"risk": {"account_size": 50000,
                                                              "max_daily_loss_pct": 3.0},
                                                     "agent": {"mode": "observation"},
                                                     "watchlist": {"default": []}}),
            patch.object(feed, "_state", return_value={"pid": None, "cycle_count": 9,
                                                       "last_cycle": f"{TODAY_TS}T07:35:00"}),
            patch.object(feed, "_market_open", return_value=False),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.feed._CACHE.clear()

    def test_logs_section_renders_every_row_without_raising(self):
        """Prevents the cockpit's activity log 500-ing (or dropping the whole
        section) on the first unfilled IOC or price-floor block of the day."""
        entries = self.feed.logs_section({"last_cycle": f"{TODAY_TS}T07:35:00", "cycle_count": 9})
        texts = [e["text"] for e in entries]
        self.assertTrue(any("entry_ioc_unfilled" in s and "IWM" in s for s in texts), texts)
        self.assertTrue(any("entry_blocked_v2_price_floor" in s and "QQQ" in s for s in texts), texts)
        self.assertEqual(sum(1 for e in entries if e["tag"] == "FILL"), 1)
        self.assertEqual(sum(1 for e in entries if e["tag"] == "EXIT"), 1)
        self.assertEqual(entries[0]["tag"], "CYCLE")

    def test_unfilled_ioc_is_not_presented_as_a_safety_event(self):
        """DOCUMENTED DEFECT (cosmetic) — prevents the operator scanning the
        cockpit for SAFETY events on Monday and seeing every unfilled IOC and
        every sub-$1 price-floor block under that tag: an unfilled IOC is the
        DESIGN outcome ('liquidity was not there at our price'), not a safety
        intervention."""
        entries = self.feed.logs_section({})
        v2 = [e for e in entries if e["text"].startswith(("entry_ioc_unfilled", "entry_blocked_v2"))]
        self.assertEqual(len(v2), 2)
        for e in v2:
            self.assertNotEqual(e["tag"], "SAFETY", e)

    def test_today_section_counts_the_v2_round_trip_once(self):
        """Prevents the TODAY strip double-counting (or losing) a v2 round trip
        because rec_id-less rows sit beside it in the ledger."""
        today = self.feed.today_section()
        self.assertEqual(today["scalp"]["roundTrips"], 1)
        self.assertEqual(today["funnel"]["roundTrips"], 0)
        self.assertAlmostEqual(today["scalp"]["realizedBooked"], 180.0)
        self.assertFalse(today["breakerTripped"])

    def test_perf_section_builds_over_the_v2_ledger(self):
        """Prevents the Perf page dying on rows without rec_id (the price-floor
        block) — sleeve_of() splits rec_id on '-', and '' must not raise."""
        perf = self.feed.perf_section()
        self.assertIsInstance(perf, dict)
        self.assertTrue(perf)  # a degraded {} would hide a raise inside _cached

    def test_positions_section_renders_a_partial_v2_position(self):
        """Prevents the positions panel breaking on a partially-filled v2
        position (partial=True, fill_mode stamped) — the COMMON IOC outcome."""
        pos = [{"rec_id": self.RID, "ticker": "SPY", "structure": "0DTE long call",
                "direction": "long", "entry_price": 1.55, "quantity": 2, "max_loss": 1.55,
                "max_profit": None, "opened_at": f"{TODAY_TS}T06:45:10", "partial": True,
                "requested": 5, "fill_mode": "marketable_ioc", "tif": "IOC",
                "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open"}]}]
        with patch.object(pt, "load_positions", return_value=pos):
            rows, unreal = self.feed.positions_section({"pid": None})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sleeve"], "test")
        self.assertEqual(rows[0]["qty"], 2)

    def test_build_feed_end_to_end_does_not_raise(self):
        """Prevents GET /feed returning 500 on Monday: the whole payload must
        assemble over a ledger holding every v2 row shape."""
        with patch.object(pt, "load_positions", return_value=[]):
            payload = self.feed.build_feed()
        self.assertIn("logs", payload)
        self.assertEqual(payload["today"]["scalp"]["roundTrips"], 1)
        self.assertTrue(any("entry_ioc_unfilled" in e["text"] for e in payload["logs"]))


# ═══════════════════════════════════════════════════════════════════════════
# 5. scalp_v2_config() — a failed or malformed read must be LOUD, never a
#    quiet downgrade to v1
# ═══════════════════════════════════════════════════════════════════════════

class _Client:
    def __init__(self): self.calls = []
    def available(self): return True
    def place_option_order(self, *a, **k): self.calls.append(("single", a, k)); return {"order": {"id": 1, "status": "open"}}
    def place_spread_order(self, *a, **k): self.calls.append(("spread", a, k)); return {"order": {"id": 2, "status": "open"}}


class ScalpV2ConfigReadFailureSurfaces(unittest.TestCase):
    """ACTIVATION NIGHT: the reader swallowed an exception and answered 'v2
    off'. Now it answers read_failed=True and logs ERROR — but the ONLY
    consumers of that flag are tests. These pin the flag and assert the paths
    that should honour it."""

    def _cfg(self, **v2):
        return {"agent": {"scalp_v2": v2}}

    def test_read_failure_is_logged_at_error_and_flagged(self):
        """Prevents a silent v1 session after a config read blows up: the
        flag and the ERROR line are the only things that make it findable."""
        with patch.object(pt.yaml, "safe_load", side_effect=RuntimeError("torn yaml")), \
                self.assertLogs("trader", level="ERROR") as cm:
            got = pt.scalp_v2_config()
        self.assertTrue(got["read_failed"])
        self.assertFalse(got["enabled"])
        self.assertTrue(any("config read FAILED" in m and "torn yaml" in m for m in cm.output), cm.output)

    def test_clean_read_is_not_flagged(self):
        got = pt.scalp_v2_config(self._cfg(enabled=True, fill_mode="marketable_ioc", aggressiveness=0.5,
                                           min_net_debit=1.0, max_per_day=10, max_attempts_per_day=30))
        self.assertFalse(got["read_failed"])
        self.assertTrue(got["enabled"])
        self.assertEqual(got["max_attempts_per_day"], 30)

    def test_malformed_numeric_value_is_read_failed_not_a_raise(self):
        """DOCUMENTED DEFECT — prevents an operator typo in agent.scalp_v2
        (`max_per_day:` left blank mid-edit) killing EVERY cycle from
        _paper_execution_test onward: exits already ran, but the observation
        record and health tracking are written after the raise, so every
        5 minutes the scheduler logs 'cycle failed' and the day's forward-test
        dataset is lost. The coercions sit OUTSIDE the guarded region."""
        with self.assertLogs("trader", level="ERROR"):
            try:
                got = pt.scalp_v2_config(self._cfg(enabled=True, max_per_day=None))
            except (TypeError, ValueError) as exc:
                self.fail(f"scalp_v2_config raised {type(exc).__name__} on a malformed value "
                          "instead of returning read_failed=True")
        self.assertTrue(got["read_failed"])

    def test_non_mapping_scalp_v2_block_is_read_failed_not_a_raise(self):
        """DOCUMENTED DEFECT — same class: `scalp_v2: yes` (a scalar where a
        mapping is expected) raises AttributeError outside the try."""
        with self.assertLogs("trader", level="ERROR"):
            try:
                got = pt.scalp_v2_config({"agent": {"scalp_v2": "yes"}})
            except AttributeError:
                self.fail("scalp_v2_config raised AttributeError on a scalar block")
        self.assertTrue(got["read_failed"])

    def test_quoted_false_does_not_enable_v2(self):
        """DOCUMENTED DEFECT — prevents a quoted "false" (copy-pasted from a
        shell/JSON snippet, or editor auto-quoting) leaving v2 ON for the
        afternoon: bool("false") is True. The prereg says flipping `enabled`
        is THE activation act, so a mis-read corrupts the evidence window."""
        for bad in ("false", "off", "0", "no"):
            with self.subTest(value=bad):
                got = pt.scalp_v2_config(self._cfg(enabled=bad))
                self.assertFalse(got["enabled"], f"enabled={bad!r} activated v2")

    def test_unknown_fill_mode_is_flagged_not_passed_through(self):
        """DOCUMENTED DEFECT — prevents a fill_mode typo ('ioc',
        'marketable-ioc') running v1 resting-mid DAY orders under the v2 label:
        the pipeline stamps the typo on every rec, submit_paper_order's equality
        test fails and silently rests at mid, the forward checker excludes every
        fill as wrong_fill_mode, and today_v2_attempt_count counts nothing."""
        for bad in ("ioc", "marketable-ioc", "IOC"):
            with self.subTest(value=bad):
                got = pt.scalp_v2_config(self._cfg(enabled=True, fill_mode=bad))
                self.assertTrue(got["read_failed"] or got["fill_mode"] == "marketable_ioc",
                                f"fill_mode={bad!r} passed through unvalidated as {got['fill_mode']!r}")


class SubmitHonoursTheReadFailedFlag(unittest.TestCase):
    """submit_paper_order re-reads config.yaml from DISK per order (for
    aggressiveness), independent of the per-cycle cfg the pipeline already
    decided fill_mode on. A torn read there must REFUSE, not send with the
    default aggressiveness the operator did not set."""

    def _submit(self, fill_mode: str, saved: dict, yaml_side_effect=None):
        c = _Client()
        ctx = [patch.object(pt, "get_order_client", return_value=c),
               patch.object(pt, "_load", return_value=[]),
               patch.object(pt, "_save", side_effect=lambda path, items: saved.__setitem__("p", items)),
               patch.object(pt, "rec_id_already_live", return_value=False)]
        if yaml_side_effect is not None:
            ctx.append(patch.object(pt.yaml, "safe_load", side_effect=yaml_side_effect))
        for p in ctx:
            p.start()
        try:
            res = pt.submit_paper_order({"rec_id": f"{TODAY_ID}-SCALP-6-SPY-0dte_directional",
                                         "ticker": "SPY", "trade": SINGLE},
                                        quantity=1, fill_mode=fill_mode)
        finally:
            for p in ctx:
                p.stop()
        return res, c

    def test_ioc_submit_refuses_when_the_config_read_fails(self):
        """DOCUMENTED DEFECT — prevents an IOC being sent at an aggressiveness
        the operator never set: with the read torn, submit_paper_order today
        logs ERROR and proceeds with the 0.5 default (harmless at today's 0.5,
        silently wrong the day it is changed). read_failed has no consumer."""
        saved: dict = {}
        with self.assertLogs("trader", level="ERROR"):
            res, c = self._submit("marketable_ioc", saved, yaml_side_effect=RuntimeError("torn yaml"))
        self.assertEqual(c.calls, [], f"broker was called on a failed config read: {c.calls}")
        self.assertIsNotNone(res)
        self.assertFalse(res.get("submitted", False))
        self.assertFalse(any(o.get("order_id") is not None for o in saved.get("p", [])),
                         "an intent was bound to a broker order despite the refusal")

    def test_unknown_fill_mode_refuses_instead_of_resting_a_day_order(self):
        """DOCUMENTED DEFECT — prevents fill_mode='ioc' (a typo the pipeline
        happily stamps on the rec) falling through to v1's resting DAY limit at
        mid: no broker call, no intent, an explicit refusal."""
        saved: dict = {}
        res, c = self._submit("ioc", saved)
        self.assertEqual(c.calls, [], f"a DAY order was rested under an unknown fill_mode: {c.calls}")
        self.assertIsNotNone(res)
        self.assertFalse(res.get("submitted", False))

    def test_recognised_modes_still_submit(self):
        """The guard must not over-reach: both documented modes reach the broker
        with the documented tif."""
        for mode, tif in (("resting_mid", "DAY"), ("marketable_ioc", "IOC")):
            with self.subTest(mode=mode):
                saved: dict = {}
                with patch.object(pt, "scalp_v2_config", return_value={
                        "enabled": True, "read_failed": False, "fill_mode": "marketable_ioc",
                        "aggressiveness": 0.5, "min_net_debit": 1.0, "max_per_day": 10,
                        "max_attempts_per_day": 30}):
                    res, c = self._submit(mode, saved)
                self.assertTrue(res["submitted"])
                self.assertEqual(c.calls[0][2].get("tif"), tif)
                self.assertEqual(saved["p"][-1]["fill_mode"], mode)


class RestartPreflightConfirmsV2(unittest.TestCase):
    """The 06:20 restart job is the moment the activation-night surprise
    happened (engine came up with v2 OFF while the YAML said ON). Its preflight
    verifies pid + 'activated paper account' only."""

    def test_restart_script_asserts_v2_enabled_and_read_ok(self):
        """DOCUMENTED DEFECT (gap) — prevents the 06:20 job reporting 'Ready
        for the open' with v2 silently off: the preflight must go through the
        engine's reader (scalp_v2_config) and refuse/page on
        enabled != True or read_failed."""
        script = (ROOT / "scripts" / "ops" / "daily_engine_restart.sh").read_text()
        self.assertRegex(script, r"scalp_v2_config|read_failed",
                         "daily_engine_restart.sh never confirms v2 is on after the restart")


if __name__ == "__main__":
    unittest.main()
