"""FULL-SESSION SIMULATION of SCALP-v2 (2026-09-12). Replays a recorded live day
(2026-09-09: 78 cycles, 20 recorded attempts with decision-time quotes) through
the REAL run_cycle against a fake broker with IBKR-faithful IOC semantics.

Each scenario is a trading-hours surprise we refuse to meet for the first time
on Monday. Hermetic under conftest's O-20 seal. UNVALIDATED — PAPER ONLY."""
import copy, json, unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import src.engine.paper_trader as pt
from tests.sim.session_sim import Session, SimBroker

ROOT = Path(pt.__file__).resolve().parents[2]
DAY = ROOT / "logs" / "observation" / "2026-09-09.jsonl"


def v2_cfg(**over):
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    a = cfg.setdefault("agent", {})
    a["paper_test_mode"] = True; a["confirm_mode"] = False; a["mode"] = "observation"
    a.setdefault("scalp_v2", {}).update({"enabled": True, "fill_mode": "marketable_ioc",
                                          "aggressiveness": 0.5, "min_net_debit": 1.0,
                                          "max_per_day": 10, "max_attempts_per_day": 30})
    a["scalp_v2"].update(over)
    # v2 scenarios: pin the v3 filter layer OFF — this helper reads the LIVE
    # config.yaml, where v3 is enabled since 2026-09-25 (v3 has its own sim test)
    a["scalp_v3"] = {"enabled": False}
    return cfg


def run(broker, drift=0.0, cycles=None, **over):
    cfg = v2_cfg(**over)
    with patch.object(pt, "scalp_v2_config", side_effect=lambda c=None: pt.scalp_v2_config.__wrapped__(cfg)
                      if hasattr(pt.scalp_v2_config, "__wrapped__") else _cfg_reader(cfg)):
        return Session(DAY, broker, cfg, drift_per_cycle=drift, cycles=cycles).run()


def _cfg_reader(cfg):
    v2 = cfg["agent"]["scalp_v2"]
    return {"enabled": True, "read_failed": False, "fill_mode": v2["fill_mode"],
            "aggressiveness": float(v2["aggressiveness"]), "min_net_debit": float(v2["min_net_debit"]),
            "max_per_day": int(v2["max_per_day"]), "max_attempts_per_day": int(v2["max_attempts_per_day"])}


class NormalDay(unittest.TestCase):
    def test_whole_session_runs_without_exceptions_and_respects_caps(self):
        s = run(SimBroker())
        r = s.report()
        self.assertEqual(r["exceptions"], [], f"cycle exceptions: {r['exceptions']}")
        self.assertEqual(r["cycles"], 78)
        self.assertLessEqual(r["v2_fills"], 10, "fill cap")
        self.assertLessEqual(r["attempts"], 30, "attempt cap")
        self.assertGreater(r["ioc_placed"], 0, "v2 must actually send IOC entries")
        self.assertEqual(r["day_placed_entries"], 0, "v2 must never rest a DAY entry")
        self.assertEqual(r["resting_ioc_at_broker"], 0, "an IOC never rests")
        self.assertEqual(r["fill_modes"], ["marketable_ioc"], "no v1-mode fills while v2 is on")
        self.assertEqual(r["pending_intents"], 0, "no intent left pending after the close")
        self.assertEqual(r["open_positions"], 0, "flat after time_flat")
        # every fill event is v2-stamped; every unfilled attempt is logged exactly once
        ev = s.events()
        for e in [x for x in ev if x.get("event") == "paper_fill"]:
            self.assertEqual(e.get("tif"), "IOC"); self.assertIsNotNone(e.get("limit_sent"))
        self.assertEqual(sum(1 for x in ev if x.get("event") == "entry_expired"), 0,
                         "an IOC that did not fill must not ALSO be logged as entry_expired")

    def test_trending_day_exercises_target_and_stop_exits(self):
        # EXIT mechanics need spread entries to exist, which IBKR's combo-IOC
        # refusal prevents (see IbkrRefusesComboIoc) — so this scenario opts
        # back into the pre-09-15 fill model to keep the exit paths covered.
        up = run(SimBroker(combo_ioc_rejected=False), drift=+0.06).report()
        down = run(SimBroker(combo_ioc_rejected=False), drift=-0.06).report()
        for r in (up, down):
            self.assertEqual(r["exceptions"], [])
            self.assertEqual(r["open_positions"], 0)
        reasons = set(up["exit_reasons"]) | set(down["exit_reasons"])
        self.assertTrue(any(x.startswith("profit_target") for x in reasons), reasons)
        self.assertTrue(any(x.startswith("stop_loss") or x.startswith("invalidation") for x in reasons), reasons)


class ZeroLiquidityDay(unittest.TestCase):
    def test_nothing_fills_attempt_cap_binds_and_nothing_rests(self):
        s = run(SimBroker(ioc_fill_prob=0.0)); r = s.report()
        self.assertEqual(r["exceptions"], [])
        self.assertEqual(r["v2_fills"], 0)
        self.assertEqual(r["unfilled_ioc"], r["ioc_placed"], "every unfilled IOC logged exactly once")
        self.assertLessEqual(r["attempts"], 30, "attempt cap must bound a no-fill day")
        self.assertEqual(r["pending_intents"], 0, "unfilled IOC intents must be dropped")
        self.assertEqual(r["resting_ioc_at_broker"], 0)


class IbkrRefusesComboIoc(unittest.TestCase):
    """2026-09-15: IBKR rejects immediate-or-cancel on a SMART-routed option
    combo (error 201, 'Inter exchange combo order should be DAY/GTC order').
    Every live v2 spread died that way while this simulator — which accepted
    IOC combos — stayed green. The fake broker now enforces the rule; the
    engine must log each refusal once, WITH the broker's reason, and leave
    nothing pending or resting."""

    def test_combo_ioc_is_refused_logged_once_with_reason(self):
        s = run(SimBroker()); r = s.report()
        self.assertEqual(r["exceptions"], [])
        combos = [o for o in s.broker.orders.values() if o["tif"] == "IOC" and len(o["legs"]) > 1]
        self.assertGreater(len(combos), 0, "the recording must send a v2 spread, or this is vacuous")
        self.assertTrue(all(o["status"] == "rejected" and o["filled"] == 0 for o in combos))
        refused = [e for e in s.events() if e.get("event") == "entry_ioc_unfilled" and e.get("broker_reject")]
        self.assertEqual(len(refused), len(combos), "each refusal logged exactly once")
        for e in refused:
            self.assertEqual(e["broker_reject"]["code"], 201)
            self.assertIn("DAY/GTC", e["broker_reject"]["text"])
        self.assertEqual(r["broker_rejects"], len(combos))
        self.assertEqual(r["pending_intents"], 0, "a refused entry leaves no intent behind")
        self.assertEqual(r["resting_ioc_at_broker"], 0)


class MarketableDayWholeSession(unittest.TestCase):
    """O-26: a fill-mode change must survive a simulated 78-cycle day before it
    could ever reach a live session. marketable_day sends v2's price with
    tif=DAY, which is the ONLY marketable form IBKR accepts on a combo — so the
    spreads that error 201 refuses must actually trade here, and no order may
    be sent as IOC. (Shipped OFF; this proves the path, not the edge.)"""

    def test_spreads_trade_as_day_orders_and_nothing_is_sent_ioc(self):
        s = run(SimBroker(), fill_mode="marketable_day"); r = s.report()
        self.assertEqual(r["exceptions"], [], f"cycle exceptions: {r['exceptions']}")
        self.assertEqual(r["cycles"], 78)
        self.assertEqual(r["ioc_placed"], 0, "marketable_day must never send IOC")
        self.assertGreater(r["day_placed_entries"], 0, "entries must go out as DAY limits")
        combos = [o for o in s.broker.orders.values() if len(o["legs"]) > 1 and o["order_type"] == "debit"]
        self.assertGreater(len(combos), 0, "the recording must offer a spread, or this is vacuous")
        self.assertEqual([o for o in combos if o["status"] == "rejected"], [],
                         "no combo may be refused in this mode — that was the whole problem")
        self.assertTrue(any(o["status"] == "filled" for o in combos),
                        "spreads must actually fill once they are DAY orders")
        self.assertEqual(r["broker_rejects"], 0)
        self.assertEqual(r["open_positions"], 0, "flat after time_flat")
        self.assertEqual(r["pending_intents"], 0, "no intent left pending after the close")
        self.assertEqual(r["fill_modes"], ["marketable_day"], "no v1-mode fills may leak in")


class PartialIocFills(unittest.TestCase):
    def test_partial_ioc_is_booked_at_executed_qty_and_managed_to_flat(self):
        s = run(SimBroker(partial_prob=1.0)); r = s.report()
        self.assertEqual(r["exceptions"], [])
        ev = s.events()
        booked = [e for e in ev if e.get("event") == "partial_fill_booked"]
        fills = [e for e in ev if e.get("event") == "paper_fill"]
        self.assertTrue(booked or fills, "partials must be booked, not dropped")
        for e in booked:
            self.assertLess(e["quantity"], e["requested"])
        self.assertEqual(r["open_positions"], 0, "partials must still be flattened by the close")
        self.assertEqual(r["pending_intents"], 0)


class BrokerDiesMidSession(unittest.TestCase):
    def test_disconnect_never_crashes_a_cycle_and_leaves_no_phantom(self):
        s = run(SimBroker(disconnect_at_cycle=25)); r = s.report()
        self.assertEqual(r["exceptions"], [], "a dead broker must degrade, never raise")
        self.assertEqual(r["cycles"], 78, "the loop must keep cycling (the deadman watches rejections)")
        # after the disconnect no new fills can occur
        ev = s.events()
        late_fills = [e for e in ev if e.get("event") == "paper_fill" and int(str(e.get("order_id") or 0)) > 0
                      and s.broker.orders.get(int(e["order_id"]), {}).get("create_date", "") >= s.cycles[24]["ts"]]
        self.assertEqual(late_fills, [])


class TransportErrorOnPlace(unittest.TestCase):
    def test_ambiguous_post_leaves_intent_unbound_not_resubmitted(self):
        s = run(SimBroker(transport_error_on_place=True), cycles=12); r = s.report()
        self.assertEqual(r["exceptions"], [])
        self.assertEqual(r["v2_fills"], 0)
        # ambiguity: the engine must not count these as unfilled IOCs (it does not know)
        self.assertEqual(r["unfilled_ioc"], 0)
        self.assertEqual(len(s.broker.orders), 0, "nothing was placed")


class ConfigReadFailure(unittest.TestCase):
    def test_broken_config_read_is_loud_and_does_not_silently_trade_v1(self):
        cfg = v2_cfg()
        with patch.object(pt.yaml, "safe_load", side_effect=RuntimeError("yaml exploded")), \
                self.assertLogs("trader", level="ERROR") as cm:
            got = pt.scalp_v2_config()
        self.assertTrue(got["read_failed"]); self.assertFalse(got["enabled"])
        self.assertTrue(any("config read FAILED" in m for m in cm.output))


class LateEntryCutoff(unittest.TestCase):
    """The recording's only v2-viable afternoon setup is the 12:15 PT SPY
    spread (the 0DTE puts sit under the $1 floor) and nothing is recorded
    after 12:25 — so the 12:00-13:00 slice is re-stamped +30 min, landing that
    setup at 12:45 PT, inside the 15-min pre-flatten window. With the real
    15:45 ET flatten nothing may be placed; the control (flatten 23:59 ET)
    must place it — the assertion cannot pass vacuously."""
    SHIFT_MIN = 30

    def _late(self, flat_time_et):
        from datetime import datetime, timedelta
        from tests.sim.session_sim import Session, SimBroker
        cfg = v2_cfg()
        cfg.setdefault("risk", {}).setdefault("paper_exits", {})["flat_time_et"] = flat_time_et
        b = SimBroker(ioc_fill_prob=0.0)   # no fills: a held position would block later entries
        with patch("src.engine.paper_trader.scalp_v2_config", side_effect=lambda c=None: _cfg_reader(cfg)):
            s = Session(DAY, b, cfg)
            # shift EVERY datetime string in the cycle (cycle ts AND the signal
            # bar timestamps) — shifting only ts leaves the bars 30 min stale and
            # the freshness gate silently drops the setup, which is not what
            # this scenario is about
            import re
            # cycle ts is "YYYY-MM-DDTHH:MM:SS"; bar_time strings use a space
            # separator and may carry seconds and/or a UTC offset — shift all
            iso = re.compile(r"^(\d{4}-\d{2}-\d{2})([T ])(\d{2}:\d{2})(:\d{2})?")
            def shift(o):
                if isinstance(o, dict):
                    return {k: shift(v) for k, v in o.items()}
                if isinstance(o, list):
                    return [shift(v) for v in o]
                if isinstance(o, str):
                    m = iso.match(o)
                    if m:
                        d = datetime.fromisoformat(f"{m.group(1)}T{m.group(3)}{m.group(4) or ':00'}") + timedelta(minutes=self.SHIFT_MIN)
                        head = f"{d:%Y-%m-%d}{m.group(2)}{d:%H:%M}" + (f"{d:%S}" and f":{d:%S}" if m.group(4) else "")
                        return head + o[m.end():]
                return o
            s.cycles = [shift(c) for c in s.cycles if c["ts"][11:16] >= "12:00"]
            s.run()
        stamps = sorted(o["create_date"][11:16] for o in b.orders.values()
                        if o["tif"] == "IOC" and o["order_type"] == "debit")
        return s, stamps

    def test_control_without_cutoff_places_inside_the_window(self):
        s, stamps = self._late("23:59")
        self.assertEqual(s.exceptions, [])
        self.assertTrue(any(t >= "12:30" for t in stamps), stamps)

    def test_no_ioc_entry_inside_the_last_15_minutes(self):
        s, stamps = self._late("15:45")
        self.assertEqual(s.exceptions, [])
        self.assertEqual(stamps, [], stamps)
