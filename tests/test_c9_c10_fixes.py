"""C-9 / C-10 (operator-approved 2026-09-26 "Go for it") — the 09-21..09-24 outage fixes.

  C-9.1  reconcile in the loop: broker_lost / quantity adoption / legged alarm,
         acting only on drift that PERSISTS two cycles, never on a failed read,
         never on a position with a working order.
  C-9.2  (shell) the advisory go/no-go pages on check (h) — covered by reading
         the script, not here.
  C-9.3  same-day-expiry flatten at 15:30 ET, ahead of the 15:45 flatten.
  C-10   "cancel after N minutes" = the first cycle check at or after N.
Plus the report labels: a candidate is compared with ITS OWN prereg numbers.

Hermetic under the O-20 seal; the session scenarios drive the REAL run_cycle
against SimBroker (O-26: a broker-path change survives a 78-cycle day first).
UNVALIDATED — PAPER ONLY.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import src.engine.paper_trader as pt
from src.engine.exit_manager import evaluate_exit, DEFAULT_CFG

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
ET = ZoneInfo("America/New_York")


# ═══════════════════ C-10 ═══════════════════
class _CancelClient:
    def __init__(self): self.cancelled = []
    def available(self): return True
    def cancel_order(self, oid): self.cancelled.append(oid); return {"order": {"id": oid, "status": "canceled"}}
    def get_order(self, oid): return {"id": oid, "status": "canceled"}


class CancelFiresAtTheDeclaredMinute(unittest.TestCase):
    def _run(self, mode, age_min, timeouts):
        ts = (datetime.now() - timedelta(minutes=age_min)).isoformat(timespec="seconds")
        pend = [{"intent": "open", "order_id": 11, "rec_id": "20990101-SCALP-1-SPY-x", "ticker": "SPY",
                 "strategy": "0dte_directional", "direction": "long", "fill_mode": mode, "submitted_at": ts}]
        c = _CancelClient()
        with patch.object(pt, "get_order_client", return_value=c), patch.object(pt, "_load", return_value=pend), \
                patch.object(pt, "_save"), patch.object(pt, "_log_event"):
            return pt.cancel_stale_entries(30, mode_timeouts=timeouts), c

    def test_first_check_seconds_short_of_five_minutes_cancels(self):
        out, c = self._run("marketable_day", 4.7, {"marketable_day": 5})
        self.assertEqual(c.cancelled, [11], "declared 5 min; the 4.9-min first check must fire, not wait a cycle")

    def test_four_minutes_does_not(self):
        out, c = self._run("marketable_day", 4.0, {"marketable_day": 5})
        self.assertEqual(c.cancelled, [])

    def test_v1_resting_entry_keeps_thirty(self):
        out, c = self._run("resting_mid", 6.0, {"marketable_day": 5})
        self.assertEqual(c.cancelled, [])


# ═══════════════════ C-9.3 ═══════════════════
class SameDayExpiryFlat(unittest.TestCase):
    def _pos(self, exp): return {"rec_id": "r", "entry_price": 2.0, "direction": "long", "expiration": exp, "max_profit": None}
    def _at(self, h, m): return datetime(2026, 9, 28, h, m, tzinfo=ET)

    def test_expires_today_at_1530_closes(self):
        r = evaluate_exit(self._pos("2026-09-28"), 2.1, {"vwap_dist_atr": 0.5}, self._at(15, 30), DEFAULT_CFG)
        self.assertTrue(r and r.startswith("expiry_flat"), r)

    def test_expires_today_at_1529_holds(self):
        self.assertIsNone(evaluate_exit(self._pos("2026-09-28"), 2.1, {"vwap_dist_atr": 0.5}, self._at(15, 29), DEFAULT_CFG))

    def test_expires_tomorrow_at_1530_holds_until_1545(self):
        self.assertIsNone(evaluate_exit(self._pos("2026-09-29"), 2.1, {"vwap_dist_atr": 0.5}, self._at(15, 30), DEFAULT_CFG))
        self.assertTrue(evaluate_exit(self._pos("2026-09-29"), 2.1, {"vwap_dist_atr": 0.5}, self._at(15, 45), DEFAULT_CFG).startswith("time_flat"))

    def test_rule_absent_from_cfg_is_the_old_behaviour(self):
        cfg = {k: v for k, v in DEFAULT_CFG.items() if k != "same_day_expiry_flat_et"}
        self.assertIsNone(evaluate_exit(self._pos("2026-09-28"), 2.1, {"vwap_dist_atr": 0.5}, self._at(15, 30), cfg))

    def test_production_config_carries_the_rule(self):
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
        self.assertEqual(cfg["risk"]["paper_exits"]["same_day_expiry_flat_et"], "15:30")
        self.assertTrue(cfg["risk"]["reconcile_in_loop"])


# ═══════════════════ C-9.1 unit ═══════════════════
class _PosClient:
    def __init__(self, views): self.views = list(views); self.i = -1
    def available(self): return True
    def get_positions(self):
        self.i = min(self.i + 1, len(self.views) - 1)
        return self.views[self.i]


def _spread(rec="20260921-SCALP-1-NVDA-debit_spread", qty=4, closing=False):
    return {"rec_id": rec, "ticker": "NVDA", "structure": "call debit spread", "quantity": qty, "closing": closing,
            "entry_price": 2.0, "direction": "long", "expiration": "2099-01-01",
            "legs": [{"symbol": "NVDA990101C00222500", "action": "buy_to_open"},
                     {"symbol": "NVDA990101C00227500", "action": "sell_to_open"}]}


def _broker(qty):
    return [{"symbol": "NVDA990101C00222500", "quantity": qty, "cost_basis": 0.0},
            {"symbol": "NVDA990101C00227500", "quantity": -qty, "cost_basis": 0.0}]


CFG_ON = {"risk": {"reconcile_in_loop": True}}


class ReconcileInLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); base = Path(self.tmp.name)
        self._p = [patch.object(pt, "POSITIONS_FILE", base / "positions.json"),
                   patch.object(pt, "PENDING_FILE", base / "orders.json"),
                   patch.object(pt, "FILLS_LOG", base / "fills.jsonl"),
                   patch.object(pt, "_notify_drift")]
        for p in self._p: p.start()

    def tearDown(self):
        for p in reversed(self._p): p.stop()
        self.tmp.cleanup()

    def _seed(self, positions, pending=()):
        pt._save(pt.POSITIONS_FILE, list(positions)); pt._save(pt.PENDING_FILE, list(pending))

    def _events(self, kind):
        p = pt.FILLS_LOG
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip() and json.loads(l).get("event") == kind] if p.exists() else []

    def test_broker_lost_needs_two_consecutive_cycles(self):
        self._seed([_spread()]); c = _PosClient([[], []])
        self.assertEqual(pt.apply_drift_in_loop(CFG_ON, c), [], "first sight: no action")
        self.assertFalse(pt.load_positions()[0].get("broker_lost"))
        ev = pt.apply_drift_in_loop(CFG_ON, c)
        self.assertEqual([e["event"] for e in ev], ["position_broker_lost"])
        self.assertTrue(pt.load_positions()[0]["broker_lost"])
        self.assertEqual(len(self._events("position_broker_lost")), 1)

    def test_one_lying_empty_read_then_truth_flags_nothing(self):
        self._seed([_spread()]); c = _PosClient([[], _broker(4), _broker(4)])
        for _ in range(3):
            pt.apply_drift_in_loop(CFG_ON, c)
        p = pt.load_positions()[0]
        self.assertFalse(p.get("broker_lost")); self.assertNotIn("drift_class", p)

    def test_failed_read_is_not_empty(self):
        self._seed([_spread()]); c = _PosClient([None, None, None])
        for _ in range(3):
            self.assertEqual(pt.apply_drift_in_loop(CFG_ON, c), [])
        self.assertFalse(pt.load_positions()[0].get("broker_lost"))

    def test_quantity_adopted_when_every_leg_agrees(self):
        self._seed([_spread(qty=1)]); c = _PosClient([_broker(3), _broker(3)])
        pt.apply_drift_in_loop(CFG_ON, c); ev = pt.apply_drift_in_loop(CFG_ON, c)
        self.assertEqual(ev[0]["event"], "position_quantity_adopted"); self.assertEqual(ev[0]["new_quantity"], 3)
        self.assertEqual(pt.load_positions()[0]["quantity"], 3)

    def test_legged_position_is_paged_and_untouched(self):
        self._seed([_spread(qty=4)])
        legged = [{"symbol": "NVDA990101C00222500", "quantity": 4, "cost_basis": 0.0}]   # short leg gone
        c = _PosClient([legged, legged, legged])
        pt.apply_drift_in_loop(CFG_ON, c); ev = pt.apply_drift_in_loop(CFG_ON, c)
        self.assertEqual(ev[0]["event"], "position_legged_alarm")
        p = pt.load_positions()[0]
        self.assertEqual(p["quantity"], 4); self.assertFalse(p.get("broker_lost"))
        self.assertEqual(pt.apply_drift_in_loop(CFG_ON, c), [], "alarm once, not every cycle")

    def test_working_order_blocks_any_action(self):
        rec = _spread()["rec_id"]
        self._seed([_spread()], pending=[{"intent": "close", "rec_id": rec, "order_id": 5}])
        c = _PosClient([[], [], []])
        for _ in range(3):
            self.assertEqual(pt.apply_drift_in_loop(CFG_ON, c), [])
        self.assertFalse(pt.load_positions()[0].get("broker_lost"))

    def test_flag_off_is_detect_only(self):
        self._seed([_spread()]); c = _PosClient([[], [], []])
        for _ in range(3):
            self.assertEqual(pt.apply_drift_in_loop({"risk": {"reconcile_in_loop": False}}, c), [])
        self.assertFalse(pt.load_positions()[0].get("broker_lost"))


# ═══════════════════ C-9.1 / C-9.3 whole-session (O-26) ═══════════════════
class OutageScenarios(unittest.TestCase):
    def _run(self, **broker_kw):
        from tests.sim.session_sim import SimBroker
        from tests.test_v2_session_sim import run
        s = run(SimBroker(combo_ioc_rejected=False, **broker_kw), fill_mode="marketable_day")
        return s, s.report()

    def test_positions_lost_at_the_broker_are_flagged_and_never_chased(self):
        s, r = self._run(lose_positions_at_cycle=20)
        self.assertEqual(r["exceptions"], [])
        self.assertGreaterEqual(r["broker_lost_flags"], 1, "the recording must have held a position at cycle 20")
        lost_syms = set(s.broker.lost_positions)
        # no close (credit) order for a lost leg after the flag: every close in the
        # broker's audit trail on a lost symbol was created before cycle 22
        late_closes = [o for o in s.broker.orders.values() if o["order_type"] == "credit"
                       and any(l["symbol"] in lost_syms for l in o["legs"]) and o["create_date"] >= s.cycles[22]["ts"]]
        self.assertEqual(late_closes, [], f"closes still being sent for lost positions: {late_closes[:3]}")
        book = s.events()
        flagged = [e["rec_id"] for e in book if e.get("event") == "position_broker_lost"]
        self.assertEqual(len(flagged), len(set(flagged)), "each position flagged once")

    def test_one_empty_position_read_flags_nothing(self):
        s, r = self._run(empty_positions_at_cycles=(30,))
        self.assertEqual(r["exceptions"], [])
        self.assertEqual(r["broker_lost_flags"], 0)
        self.assertEqual(r["legged_alarms"], 0)

    def test_same_day_expiries_are_closed_before_the_general_flatten(self):
        s, r = self._run()
        self.assertEqual(r["exceptions"], [])
        import re
        ev = s.events(); day = ev[0]["ts"][:10] if ev else None
        yymmdd = day.replace("-", "")[2:] if day else None
        def expires_today(e):            # expiry lives in the OCC symbol: ROOT + YYMMDD + C/P + strike
            rt = e.get("round_trip") or {}
            legs = (rt.get("exit_legs") or rt.get("entry_legs") or e.get("legs") or [])   # entry_legs is [] in practice
            syms = [(l.get("symbol") or l.get("option_symbol")) if isinstance(l, dict) else str(l) for l in legs]
            occ = any((m := re.match(r"^[A-Z]+(\d{6})[CP]\d{8}$", str(x))) and m.group(1) == yymmdd for x in syms)
            return occ or "0dte_directional" in str(e.get("rec_id", ""))   # 0DTE = expires today by definition
        exits = [e for e in ev if e.get("event") == "paper_exit"]
        same_day = {e["rec_id"] for e in exits if expires_today(e)}
        self.assertTrue(same_day, "the recording must include a same-day-expiry (0DTE) round trip, or this is vacuous")
        for e in exits:
            if e.get("rec_id") in same_day:
                self.assertFalse(str(e.get("close_reason", "")).startswith("time_flat"),
                                 f"a same-day expiry reached the 15:45 flatten: {e.get('rec_id')}")
                self.assertLessEqual(e["ts"][11:16], "12:31", "PT clock: 15:30 ET = 12:30 PT")


# ═══════════════════ labels ═══════════════════
class ReportsUseTheCandidatesOwnNumbers(unittest.TestCase):
    def test_mechanics_labels_and_predictions_follow_the_candidate(self):
        import scalp_v2_mechanics as m
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "fills.jsonl"; p.write_text("")
            crit = {"status": "FROZEN", "window_start": "2026-09-25T15:19:27-07:00", "_candidate": "v3",
                    "scope": {"ledger": "fills.jsonl", "rec_id_tag": "-SCALP-", "fill_mode": "marketable_ioc"},
                    "rules_frozen": {"max_attempts_per_day": 30}}
            r = m.mechanics(p, crit, lambda e: (None, "pending"), today="2026-09-28", reject_ids=set())
            self.assertEqual(r["candidate"], "v3")
            self.assertEqual(r["cumulative"]["fill_rate_floor"], 0.60)
            self.assertEqual(r["predictions"]["truth_per_trade_usd"], 1.0)
            line = m.summary_line(r)
            self.assertTrue(line.startswith("SCALP-v3 "), line); self.assertIn("no v3 activity", line)
            self.assertIn("(pred —)", line)                      # v3 declared no VWAP-exit share
            self.assertTrue(m.render_md(r).startswith("# SCALP-v3 mechanics"))
            crit.pop("_candidate")
            r2 = m.mechanics(p, crit, lambda e: (None, "pending"), today="2026-09-28", reject_ids=set())
            self.assertEqual(r2["cumulative"]["fill_rate_floor"], 0.25, "no candidate tag -> v2, unchanged")

    def test_closed_window_is_not_a_dry_run(self):
        import scalp_forward_check as sfc
        r = sfc.evaluate(v2=True)
        self.assertEqual(r["status"], "CLOSED_SUPERSEDED")
        import io, contextlib
        buf = io.StringIO()
        with patch.object(sys, "argv", ["x", "--v2"]), contextlib.redirect_stdout(buf):
            sfc.main()
        self.assertNotIn("DRY RUN", buf.getvalue().splitlines()[0])
