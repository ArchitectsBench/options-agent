"""SCALP-v2 pre-registered candidate (2026-09-12) — mechanics, and the guarantee
that v1 is byte-identical while v2 is OFF."""
import json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import src.engine.paper_trader as pt

SINGLE = {"structure": "0DTE long call", "strategy": "0dte_directional", "direction": "long",
          "expiration": "2099-01-01", "net_debit": 1.50, "max_loss": 1.50, "max_profit": None,
          "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open",
                    "bid": 1.45, "ask": 1.55, "mid": 1.50, "spread": 0.10}]}
SPREAD = {"structure": "call debit spread", "strategy": "debit_spread", "direction": "long",
          "expiration": "2099-01-01", "net_debit": 1.00, "max_loss": 1.00, "max_profit": 1.00,
          "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open", "mid": 2.00, "spread": 0.10},
                   {"symbol": "SPY990101C00502000", "action": "sell_to_open", "mid": 1.00, "spread": 0.06}]}


class V2EntryLimit(unittest.TestCase):
    def test_single_leg_is_mid_plus_half_spread_at_default(self):
        self.assertAlmostEqual(pt.v2_entry_limit(SINGLE, 0.5), 1.55, places=4)   # the ask

    def test_aggressiveness_zero_is_v1_mid(self):
        self.assertAlmostEqual(pt.v2_entry_limit(SINGLE, 0.0), 1.50, places=4)

    def test_spread_uses_sum_of_leg_spreads(self):
        # net 1.00 + 0.5 * (0.10 + 0.06)
        self.assertAlmostEqual(pt.v2_entry_limit(SPREAD, 0.5), 1.08, places=4)


class _Client:
    def __init__(self): self.calls = []
    def available(self): return True
    def place_option_order(self, *a, **k): self.calls.append(("single", a, k)); return {"order": {"id": 1, "status": "open"}}
    def place_spread_order(self, *a, **k): self.calls.append(("spread", a, k)); return {"order": {"id": 2, "status": "open"}}


class FillModeReachesTheBroker(unittest.TestCase):
    def _submit(self, trade, **kw):
        c = _Client()
        with patch.object(pt, "get_order_client", return_value=c), \
                patch.object(pt, "_load", return_value=[]), patch.object(pt, "_save"), \
                patch.object(pt, "rec_id_already_live", return_value=False), \
                patch.object(pt, "scalp_v2_config", return_value={"enabled": True, "fill_mode": "marketable_ioc",
                                                                   "aggressiveness": 0.5, "min_net_debit": 1.0, "max_per_day": 10}):
            pt.submit_paper_order({"rec_id": "20990101-SCALP-1-SPY-x", "ticker": "SPY", "trade": trade},
                                  quantity=1, **kw)
        return c.calls[0]

    def test_v1_default_is_resting_day_limit_at_mid(self):
        """The guarantee: with v2 off, nothing changes."""
        kind, a, k = self._submit(SINGLE)
        self.assertEqual(k.get("tif"), "DAY")
        self.assertAlmostEqual(a[4], 1.50, places=4)

    def test_v2_single_leg_is_ioc_at_the_ask(self):
        kind, a, k = self._submit(SINGLE, fill_mode="marketable_ioc")
        self.assertEqual(k.get("tif"), "IOC")
        self.assertAlmostEqual(a[4], 1.55, places=4)

    def test_v2_spread_is_ioc_at_net_plus_half_of_summed_spreads(self):
        kind, a, k = self._submit(SPREAD, fill_mode="marketable_ioc")
        self.assertEqual(kind, "spread")
        self.assertEqual(k.get("tif"), "IOC")
        self.assertAlmostEqual(a[2], 1.08, places=4)


class IntentCarriesTheMode(unittest.TestCase):
    def test_intent_records_fill_mode_limit_and_tif(self):
        saved = {}
        c = _Client()
        with patch.object(pt, "get_order_client", return_value=c), \
                patch.object(pt, "_load", return_value=[]), \
                patch.object(pt, "_save", side_effect=lambda path, items: saved.setdefault("p", items)), \
                patch.object(pt, "rec_id_already_live", return_value=False), \
                patch.object(pt, "scalp_v2_config", return_value={"enabled": True, "fill_mode": "marketable_ioc",
                                                                   "aggressiveness": 0.5, "min_net_debit": 1.0, "max_per_day": 10}):
            pt.submit_paper_order({"rec_id": "20990101-SCALP-2-SPY-x", "ticker": "SPY", "trade": SINGLE},
                                  quantity=1, fill_mode="marketable_ioc")
        intent = saved["p"][-1]
        self.assertEqual(intent["fill_mode"], "marketable_ioc")
        self.assertEqual(intent["tif"], "IOC")
        self.assertAlmostEqual(intent["limit_sent"], 1.55, places=4)


class GateNeverBlendsModes(unittest.TestCase):
    """The v2 gate counts ONLY v2-mode entries; a v1 resting-mid trade with the
    same marker is excluded as wrong_fill_mode and REPORTED."""
    def test_scope_fill_mode_excludes_other_modes(self):
        import sys
        sys.path.insert(0, str(Path(pt.__file__).resolve().parents[2] / "scripts"))
        import importlib; sfc = importlib.import_module("scalp_forward_check")
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "fills.jsonl"
            rows = [
                {"event": "paper_fill", "rec_id": "20990101-SCALP-1-SPY-x", "fill_mode": "marketable_ioc"},
                {"event": "paper_exit", "rec_id": "20990101-SCALP-1-SPY-x", "ticker": "SPY", "ts": "2099-01-01T10:00:00", "pnl_total": 50},
                {"event": "paper_fill", "rec_id": "20990101-SCALP-2-QQQ-x"},              # v1: no fill_mode
                {"event": "paper_exit", "rec_id": "20990101-SCALP-2-QQQ-x", "ticker": "QQQ", "ts": "2099-01-01T10:05:00", "pnl_total": 70},
            ]
            ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            crit = {"scope": {"rec_id_tag": "-SCALP-", "ledger": str(ledger), "fill_mode": "marketable_ioc"},
                    "window_start": None, "scoring": None}
            with patch.object(sfc, "mode_map", return_value={}):
                trades, excluded = sfc.qualifying(crit)
        self.assertEqual([t["rec_id"] for t in trades], ["20990101-SCALP-1-SPY-x"])
        self.assertEqual([r["rec_id"] for r in excluded["wrong_fill_mode"]], ["20990101-SCALP-2-QQQ-x"])


class ActivationInvariants(unittest.TestCase):
    """v2 was ACTIVATED 2026-09-12 (operator: 'go ahead'). From here the config
    and the two frozen criteria files must AGREE — an enabled candidate with an
    unfrozen window, or a closed v1 that still claims FROZEN, is exactly the
    kind of drift that makes forward evidence worthless."""
    ROOT = Path(pt.__file__).resolve().parents[2]

    def _y(self, rel):
        import yaml
        return yaml.safe_load((self.ROOT / rel).read_text())

    def test_enabled_implies_frozen_window_and_closed_v1(self):
        v2cfg = self._y("config/config.yaml")["agent"]["scalp_v2"]
        crit = self._y("config/scalp_v2_forward_criteria.yaml")["scalp_v2_forward"]
        v1 = self._y("config/scalp_forward_criteria.yaml")["scalp_forward"]
        v3cfg = self._y("config/config.yaml")["agent"].get("scalp_v3") or {}
        if v2cfg["enabled"] and v3cfg.get("enabled"):
            # 2026-09-25: v2 is the BASE LAYER under v3 — its own window is closed
            # (superseded) and v3's window is the live, frozen one
            v3crit = self._y("config/scalp_v3_forward_criteria.yaml")["scalp_v3_forward"]
            self.assertEqual(crit["status"], "CLOSED_SUPERSEDED")
            self.assertTrue(crit.get("window_end"))
            self.assertEqual(v3crit["status"], "FROZEN")
            self.assertTrue(v3crit["window_start"])
            self.assertEqual(crit["window_end"], v3crit["window_start"],
                             "v2 must close exactly where v3 opens")
            self.assertEqual(v1["status"], "CLOSED_FAILED")
        elif v2cfg["enabled"]:
            self.assertEqual(crit["status"], "FROZEN")
            self.assertTrue(crit["window_start"], "an active candidate must have a window_start")
            self.assertEqual(v1["status"], "CLOSED_FAILED", "v1 must be closed when v2 is live")
            self.assertTrue(v1.get("window_end"))
        else:
            self.assertEqual(crit["status"], "DRAFT")

    def test_frozen_rules_match_config(self):
        """The rules the engine RUNS must be the rules the gate FROZE."""
        v2cfg = self._y("config/config.yaml")["agent"]["scalp_v2"]
        rules = self._y("config/scalp_v2_forward_criteria.yaml")["scalp_v2_forward"]["rules_frozen"]
        for k in ("fill_mode", "aggressiveness", "min_net_debit", "max_per_day", "max_attempts_per_day"):
            self.assertEqual(v2cfg[k], rules[k], f"{k}: config {v2cfg[k]} != frozen {rules[k]}")
        self.assertEqual(v2cfg["max_per_day"], 10)

    def test_v3_frozen_rules_match_config(self):
        """v3: the filter the engine RUNS must be the filter the gate FROZE."""
        v3cfg = self._y("config/config.yaml")["agent"].get("scalp_v3") or {}
        if not v3cfg.get("enabled"):
            self.skipTest("v3 not enabled")
        rules = self._y("config/scalp_v3_forward_criteria.yaml")["scalp_v3_forward"]["rules_frozen"]
        self.assertEqual(v3cfg["tickers"], rules["universe"])
        self.assertEqual(v3cfg["strategies"], rules["structures"])
        self.assertEqual(v3cfg["entry_window_et"], rules["entry_window_et"])
        self.assertEqual(float(v3cfg["min_volume_ratio"]), float(rules["min_volume_ratio"]))
        self.assertEqual(v3cfg["fill_mode"], rules["fill_mode"])
        v2cfg = self._y("config/config.yaml")["agent"]["scalp_v2"]
        for k in ("aggressiveness", "min_net_debit", "max_per_day", "max_attempts_per_day"):
            self.assertEqual(v2cfg[k], rules[k], f"inherited {k} drifted")


class AttemptCapCountsUnfilledIocs(unittest.TestCase):
    """An unfilled IOC consumes no fill slot; without a separate attempts
    counter the engine could fire unbounded IOCs on a day nothing fills."""
    def test_counts_v2_fills_and_unfilled_today_only(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        rows = [
            {"ts": f"{today}T07:00:00", "event": "paper_fill", "fill_mode": "marketable_ioc"},
            {"ts": f"{today}T07:05:00", "event": "entry_ioc_unfilled"},
            {"ts": f"{today}T07:10:00", "event": "paper_fill"},                       # v1 mode: not an attempt
            {"ts": "2000-01-01T07:00:00", "event": "entry_ioc_unfilled"},           # another day
            {"ts": f"{today}T07:15:00", "event": "paper_exit"},
        ]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "fills.jsonl"
            f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            with patch.object(pt, "FILLS_LOG", f):
                self.assertEqual(pt.today_v2_attempt_count(), 2)

    def test_ibkr_limit_order_tif_only_day_or_ioc(self):
        from src.data.ibkr_orders import IbkrOrderClient
        c = IbkrOrderClient.__new__(IbkrOrderClient); c._account = None
        import src.data.ibkr_orders as m
        if m.LimitOrder is None: self.skipTest("ib_async not installed")
        self.assertEqual(c._limit_order("BUY", 1, 1.0).tif, "DAY")
        self.assertEqual(c._limit_order("BUY", 1, 1.0, tif="IOC").tif, "IOC")
        self.assertEqual(c._limit_order("BUY", 1, 1.0, tif="GTC").tif, "DAY")   # never anything else


class TheEngineReadsWhatTheOperatorSet(unittest.TestCase):
    """ACTIVATION NIGHT SURPRISE (2026-09-12): the YAML said enabled: true, the
    invariant test (which read the YAML itself) passed, and the engine restarted
    with v2 OFF — scalp_v2_config() swallowed a NameError (`yaml` was only
    imported inside other functions) and returned {}. The test must go through
    the ENGINE'S reader, and a read failure must be loud."""
    def test_engine_reader_agrees_with_raw_yaml(self):
        import yaml
        raw = yaml.safe_load((Path(pt.__file__).resolve().parents[2] / "config" / "config.yaml").read_text())
        raw_v2 = raw["agent"]["scalp_v2"]
        got = pt.scalp_v2_config()
        self.assertFalse(got["read_failed"], "the engine's config read must not fail")
        for k in ("enabled", "fill_mode", "aggressiveness", "min_net_debit", "max_per_day", "max_attempts_per_day"):
            self.assertEqual(got[k], raw_v2[k], k)

    def test_read_failure_is_logged_and_flagged_not_silent(self):
        with patch.object(pt.yaml, "safe_load", side_effect=RuntimeError("boom")), \
                self.assertLogs("trader", level="ERROR") as cm:
            got = pt.scalp_v2_config()
        self.assertTrue(got["read_failed"])
        self.assertFalse(got["enabled"])
        self.assertTrue(any("config read FAILED" in m for m in cm.output))


class LateEntryCutoff(unittest.TestCase):
    """v2 fills instantly; an entry at 12:5x PT has no cycle left before the
    15:45 ET flatten and goes home unmanaged. Drives the real placement
    function under a patched clock: at 15:35 ET it returns BEFORE consulting
    the attempt counter; at 15:29 ET it proceeds to it."""
    def _run_at(self, hh, mm):
        import src.engine.pipeline as pl
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                t = datetime(2026, 9, 14, hh, mm, tzinfo=ET)
                return t.astimezone(tz) if tz else t.astimezone(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
        cfg = {"agent": {"paper_test_mode": True}, "risk": {"paper_exits": {"flat_time_et": "15:45"}}}
        v2 = {"enabled": True, "read_failed": False, "fill_mode": "marketable_ioc", "aggressiveness": 0.5,
              "min_net_debit": 1.0, "max_per_day": 10, "max_attempts_per_day": 30}
        with patch.object(pl, "datetime", FakeDT), \
                patch("src.engine.live_gate.kill_switch_engaged", return_value=None), \
                patch("src.engine.risk.circuit_breaker", return_value={"tripped": False}), \
                patch("src.engine.risk.risk_config", return_value={}), \
                patch.object(pt, "scalp_v2_config", return_value=v2), \
                patch.object(pt, "today_v2_attempt_count", return_value=10**6) as attempts, \
                patch("builtins.print"):
            submit = MagicMock()
            pl._paper_execution_test({}, {}, 1, cfg, {}, submit, lambda *a, **k: False)
        return attempts.called, submit.called

    def test_inside_cutoff_places_nothing_and_never_reaches_the_cap(self):
        reached_cap, submitted = self._run_at(15, 35)
        self.assertFalse(reached_cap); self.assertFalse(submitted)

    def test_before_cutoff_proceeds(self):
        reached_cap, _ = self._run_at(15, 29)
        self.assertTrue(reached_cap)


class UnquotedLegIsARefusalNotADeadCycle(unittest.TestCase):
    def test_submit_refuses_instead_of_raising(self):
        t = {**SINGLE, "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open", "mid": 1.5}]}
        c = _Client()
        with patch.object(pt, "get_order_client", return_value=c), \
                patch.object(pt, "_load", return_value=[]), patch.object(pt, "_save") as save, \
                patch.object(pt, "rec_id_already_live", return_value=False), \
                patch.object(pt, "scalp_v2_config", return_value={"enabled": True, "read_failed": False,
                                                                   "fill_mode": "marketable_ioc", "aggressiveness": 0.5,
                                                                   "min_net_debit": 1.0, "max_per_day": 10,
                                                                   "max_attempts_per_day": 30}), \
                self.assertLogs("trader", level="ERROR"):
            out = pt.submit_paper_order({"rec_id": "20990101-SCALP-9-SPY-x", "ticker": "SPY", "trade": t},
                                        quantity=1, fill_mode="marketable_ioc")
        self.assertEqual(out["refused"], "unquoted_leg"); self.assertEqual(c.calls, [])
        save.assert_not_called()  # a refusal leaves NO pending intent behind
