"""End-to-end test of pipeline.run_cycle (queued after the 2026-07-02
UnboundLocalError shipped: no test executed the full cycle function, so a
reference to cfg before its load aborted every market cycle for 16 minutes).

The REAL run_cycle executes top to bottom — real candidate synthesis, real
risk-kernel sizing, real mode resolution, real observation-log write — with
mocks only at the network/side-effect edges (regime, signals, chains, broker,
Claude, notifications, state file).

Covers: the happy path (viable candidate -> Claude approved -> sized order
submitted -> record logged), call ordering (fills/exits BEFORE new entries),
kill switch (blocks entries, never exits), risk gate short-circuiting Claude,
veto handling, and the execution-test path.

Run: .venv/bin/python -m unittest tests.test_run_cycle_e2e -v
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.confirm as cf
import src.engine.live_gate as lg
import src.engine.paper_trader as pt
import src.engine.pipeline as pl
import src.engine.risk as risk


class _MidSession(datetime):
    """The pipeline's clock pinned to 10:00 (today's date, caller's tz) — for
    tests that place a v2 entry, which the 15:30 ET late-entry cutoff blocks
    whenever the suite runs after 12:30 PT (found 2026-09-14)."""
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)


STATE = {"watchlist_override": ["SPY"], "sector": "all", "mode": "all"}

REGIME = {"bias": "bullish", "trend": "mild", "vix": 17.0, "vol_regime": "calm",
          "missing": [], "degraded": False,
          "spy": {"vs_vwap": "above"}, "qqq": {"vs_vwap": "above"}}

SIGNAL = {"last": 743.5, "atr": 2.1, "bar_time": None,   # bar_time set fresh in setUp
          "squeeze": {"5m": "off", "15m": "on", "30m": "off"},
          "ema_stack": {"5m": "bullish", "15m": "bullish", "30m": "bullish"},
          "macd_5m": {"sign": "positive", "direction": "rising"},
          "vwap_dist_atr": 0.8, "volume_ratio": 1.4,
          "rs_vs_spy_30min_pct": 0.2, "opening_range": {"position": "above"}}

VIABLE = {"debit_spread": {"verdict": "viable", "confirms_met": ["vwap_with_trend"]}}
# conf = 0.70 + 0.06*1 = 0.76 >= 0.75 threshold -> enters the funnel

TRADE = {"structure": "call_debit_spread", "strategy": "debit_spread",
         "direction": "long", "net_debit": 3.6, "max_loss": 3.6, "max_profit": 3.4,
         "expiration": "2026-07-10", "breakeven": 743.6,
         "legs": [{"symbol": "SPY260710C00740000", "action": "buy_to_open", "mid": 5.3},
                  {"symbol": "SPY260710C00747000", "action": "sell_to_open", "mid": 1.7}]}
# real sizing: $1,000 budget / $360 per contract -> quantity 2

APPROVED = {"status": "approved", "budget_used": 3,
            "verdict": {"approve": True, "confidence": 0.8, "narrative": "ok"}}


class RunCycleE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._orig = (pl.OBS_DIR, pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG,
                      risk.FILLS_LOG, risk.RISK_STATE, lg.KILL_SWITCH_FILE,
                      cf.CONFIRM_FILE)
        pl.OBS_DIR = base / "observation"
        pt.PENDING_FILE = base / "paper_orders.json"
        pt.POSITIONS_FILE = base / "paper_positions.json"
        pt.FILLS_LOG = base / "fills.jsonl"
        risk.FILLS_LOG = base / "fills.jsonl"
        risk.RISK_STATE = base / "risk_state.json"
        lg.KILL_SWITCH_FILE = base / "KILL_SWITCH"
        cf.CONFIRM_FILE = base / "pending_confirmations.json"
        self.calls = []          # cross-mock call-order journal
        self.submits = []        # (rec_id, tag, quantity)
        # real signals always stamp bar_time; keep it fresh so the 6b freshness
        # guard sees a current bar and exercises its ALLOW path (not a stale block)
        SIGNAL["bar_time"] = datetime.now(ZoneInfo("America/New_York")).isoformat()

    def tearDown(self):
        (pl.OBS_DIR, pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG,
         risk.FILLS_LOG, risk.RISK_STATE, lg.KILL_SWITCH_FILE,
         cf.CONFIRM_FILE) = self._orig
        self.tmp.cleanup()

    def _submit(self, rec, tag="UNVALIDATED-PAPER-ONLY", quantity=1, **kw):
        self.calls.append("submit")
        self.submits.append((rec["rec_id"], tag, quantity))
        return {"submitted": True, "order_id": 42, "status": "ok"}

    @contextlib.contextmanager
    def _harness(self, paper_test=False, check_gate=None, resolve=APPROVED,
                 confirm_mode=False):
        """Real run_cycle with mocks only at the edges. Behavior flags
        (confirm_mode) are pinned regardless of the operator's live config."""
        import yaml
        real_cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
        real_cfg.setdefault("agent", {})["confirm_mode"] = confirm_mode
        # SCALP-v3 (live since 2026-09-25) narrows the lane to index 0DTE in a
        # morning window; this generic e2e drives a debit spread, so pin v3 OFF
        # (v3's own behaviour: tests/test_scalp_v3.py)
        real_cfg["agent"]["scalp_v3"] = {"enabled": False}
        patches = [
            patch.object(pl, "_load_cfg", return_value=real_cfg),
            patch.object(pl, "build_regime", return_value=dict(REGIME)),
            patch.object(pl, "format_regime", return_value="  [regime]"),
            patch.object(pl, "scan_watchlist", return_value=({"SPY": dict(SIGNAL)}, [])),
            patch.object(pl, "evaluate_strategies", return_value=dict(VIABLE)),
            patch.object(pl, "update_state"),
            patch("src.engine.paper_trader.poll_fills",
                  side_effect=lambda *a, **k: self.calls.append("poll") or []),
            patch("src.engine.exit_manager.manage_positions",
                  side_effect=lambda *a, **k: self.calls.append("manage") or []),
            patch("src.engine.options_selector.build_trade",
                  return_value=(dict(TRADE), None)),
            patch("src.engine.paper_trader.submit_paper_order", side_effect=self._submit),
            patch("src.llm.claude.check_gate", return_value=check_gate),
            patch("src.llm.claude.resolve_validation", return_value=dict(resolve)),
            patch("src.engine.recommendation.recent_rec_exists", return_value=None),
            patch("src.engine.recommendation.build_recommendation",
                  return_value={"rec_id": "2026-07-02-T1", "ticker": "SPY",
                                "trade": dict(TRADE)}),
            patch("src.engine.recommendation.format_recommendation", return_value="[rec]"),
            patch("src.engine.recommendation.log_recommendation"),
            patch("src.engine.notify.notify_recommendation"),
            patch("src.engine.notify.notify_fill"),
            patch("src.engine.notify.notify_exit"),
            patch("src.engine.notify.track_health"),
            patch.object(risk, "_funnel_exposure", return_value=(0, 0)),
            # appended LAST to preserve existing mocks[] indices (6b guard):
            # session forced open so the fresh-bar path is exercised, not blocked.
            patch("src.data.freshness.current_session_state", return_value="open"),
        ]
        if not paper_test:  # covered by its own dedicated test
            patches.append(patch.object(pl, "_paper_execution_test"))
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            with contextlib.redirect_stdout(io.StringIO()):  # silence the cycle print
                yield mocks

    def test_happy_path_full_cycle(self):
        """Viable candidate -> approved -> risk-sized order -> record on disk."""
        with self._harness():
            record = pl.run_cycle(999, state=dict(STATE))
        # a real recommendation went out, sized by the REAL risk kernel
        self.assertEqual(self.submits, [("2026-07-02-T1", "UNVALIDATED-PAPER-ONLY", 2)])
        recs = [r for r in record["recommendations"] if "rec_id" in r]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["quantity"], 2)
        self.assertEqual(record["mode"], "observation")
        self.assertIsNone(record["kill_switch"])
        self.assertEqual(record["stamp"], "UNVALIDATED — PAPER ONLY")
        # the cycle record landed in the (redirected) observation log
        obs_files = list(pl.OBS_DIR.glob("*.jsonl"))
        self.assertEqual(len(obs_files), 1)
        on_disk = json.loads(obs_files[0].read_text().splitlines()[-1])
        self.assertEqual(on_disk["cycle"], 999)

    def test_fills_and_exits_run_before_new_entries(self):
        """Position management must precede order placement in every cycle."""
        with self._harness():
            pl.run_cycle(1, state=dict(STATE))
        self.assertIn("submit", self.calls)
        self.assertLess(self.calls.index("poll"), self.calls.index("submit"))
        self.assertLess(self.calls.index("manage"), self.calls.index("submit"))

    def test_kill_switch_blocks_entries_never_exits(self):
        lg.engage_kill_switch("e2e test")
        with self._harness():
            record = pl.run_cycle(1, state=dict(STATE))
        self.assertNotIn("submit", self.calls)      # no new orders
        self.assertIn("poll", self.calls)           # fills still polled
        self.assertIn("manage", self.calls)         # exits still managed
        self.assertIn("e2e test", record["kill_switch"])

    def test_risk_gate_blocks_before_claude(self):
        """Breaker tripped -> candidate skipped as risk_gate, Claude NOT called."""
        risk.FILLS_LOG.parent.mkdir(parents=True, exist_ok=True)
        risk.FILLS_LOG.write_text(json.dumps({
            "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "event": "paper_exit", "pnl_per_contract": -2000.0}) + "\n")
        with patch("src.engine.notify.send"), self._harness() as mocks:
            record = pl.run_cycle(1, state=dict(STATE))
            resolve_mock = mocks[11]  # resolve_validation (after _load_cfg at index 0)
        self.assertNotIn("submit", self.calls)
        resolve_mock.assert_not_called()            # budget preserved
        skips = [r for r in record["recommendations"] if r.get("skipped") == "risk_gate"]
        self.assertEqual(len(skips), 1)
        self.assertIn("breaker", skips[0]["skip_detail"]["detail"])

    def test_veto_produces_no_order(self):
        veto = {"status": "veto_active", "reason": "regime mismatch"}
        with self._harness(check_gate=veto):
            record = pl.run_cycle(1, state=dict(STATE))
        self.assertNotIn("submit", self.calls)
        self.assertEqual(record["recommendations"][0]["skipped"], "veto_active")

    def test_confirm_mode_queues_instead_of_submitting(self):
        """confirm_mode: the approved rec goes to the confirmation queue —
        NO order until a human confirms."""
        with patch("src.engine.notify.send"), self._harness(confirm_mode=True):
            record = pl.run_cycle(1, state=dict(STATE))
        self.assertNotIn("submit", self.calls)
        queued = json.loads(cf.CONFIRM_FILE.read_text())
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["status"], "pending")
        self.assertEqual(queued[0]["quantity"], 2)          # risk-sized
        self.assertTrue(queued[0].get("confirm_token"))     # phone-auth token
        rec = [r for r in record["recommendations"] if "rec_id" in r][0]
        self.assertTrue(rec["paper_order"]["queued_for_confirmation"])

    @patch.object(pl, "datetime", _MidSession)
    def test_execution_test_path_submits_tagged_sized_order(self):
        """The paper-test path runs for real: viable setup -> tagged order with
        risk-kernel quantity (config paper_test_mode must be on)."""
        import yaml
        cfg_live = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
        if not cfg_live.get("agent", {}).get("paper_test_mode"):
            self.skipTest("paper_test_mode disabled in config")
        with self._harness(paper_test=True):
            pl.run_cycle(1, state=dict(STATE))
        test_orders = [s for s in self.submits if s[1].startswith("EXECUTION-TEST")]
        self.assertEqual(len(test_orders), 1)
        self.assertEqual(test_orders[0][2], 2)      # sized, not hardcoded 1


if __name__ == "__main__":
    unittest.main()
