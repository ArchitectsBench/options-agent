"""SCALP `marketable_day` fill mode (built 2026-09-16, operator: "Go ahead").

Same marketable limit as v2 (mid + aggressiveness x spread) but **tif=DAY**,
cancelled by the engine after `scalp_v2.entry_timeout_min`. This is the only
marketable design IBKR accepts on a spread — it refuses IOC on a SMART combo
(error 201, see docs/scalp_v2_spread_rejects_20260915.md).

SHIPS OFF: config/config.yaml keeps fill_mode: marketable_ioc.

What these tests pin is the pair of things that break SILENTLY otherwise:
  1. a cancelled marketable entry is a completed ATTEMPT — it must reach the
     attempt cap and the fill-rate denominator (a v1 expiry must NOT, so every
     historical number is unchanged);
  2. a cancelled entry frees the setup, so a persistent signal must not re-fire
     it every cycle.
UNVALIDATED — PAPER ONLY.
"""
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import src.engine.paper_trader as pt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scalp_forward_check as sfc      # noqa: E402
import scalp_v2_mechanics as svm       # noqa: E402

SINGLE = {"structure": "0DTE long call", "strategy": "0dte_directional", "direction": "long",
          "expiration": "2099-01-01", "net_debit": 1.50, "max_loss": 1.50, "max_profit": None,
          "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open",
                    "bid": 1.45, "ask": 1.55, "mid": 1.50, "spread": 0.10}]}
SPREAD = {"structure": "call debit spread", "strategy": "debit_spread", "direction": "long",
          "expiration": "2099-01-01", "net_debit": 1.00, "max_loss": 1.00, "max_profit": 1.00,
          "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open", "mid": 2.00, "spread": 0.10},
                   {"symbol": "SPY990101C00502000", "action": "sell_to_open", "mid": 1.00, "spread": 0.06}]}

V2CFG = {"enabled": True, "fill_mode": "marketable_day", "aggressiveness": 0.5,
         "min_net_debit": 1.0, "max_per_day": 10, "max_attempts_per_day": 30,
         "entry_timeout_min": 5, "reattempt_cooldown_min": 30}


class _Client:
    def __init__(self):
        self.calls = []
    def available(self): return True
    def place_option_order(self, *a, **k):
        self.calls.append(("single", a, k)); return {"order": {"id": 1, "status": "open"}}
    def place_spread_order(self, *a, **k):
        self.calls.append(("spread", a, k)); return {"order": {"id": 2, "status": "open"}}


class SendsADayOrderAtTheMarketableLimit(unittest.TestCase):
    """The whole point: v2's price, but a time-in-force IBKR will accept."""

    def _submit(self, trade, mode):
        c = _Client()
        with patch.object(pt, "get_order_client", return_value=c), \
                patch.object(pt, "_load", return_value=[]), patch.object(pt, "_save"), \
                patch.object(pt, "rec_id_already_live", return_value=False), \
                patch.object(pt, "scalp_v2_config", return_value={**V2CFG, "fill_mode": mode}):
            out = pt.submit_paper_order({"rec_id": "20990101-SCALP-1-SPY-x", "ticker": "SPY",
                                         "trade": trade}, quantity=1, fill_mode=mode)
        return c.calls[0] if c.calls else None, out

    def test_single_leg_day_at_the_natural(self):
        (kind, a, k), _ = self._submit(SINGLE, "marketable_day")
        self.assertEqual(k.get("tif"), "DAY", "IBKR must never see IOC in this mode")
        self.assertAlmostEqual(a[4], 1.55, places=4)        # the ask — same as v2

    def test_spread_day_at_the_natural(self):
        (kind, a, k), _ = self._submit(SPREAD, "marketable_day")
        self.assertEqual(kind, "spread")
        self.assertEqual(k.get("tif"), "DAY")
        self.assertAlmostEqual(a[2], 1.08, places=4)        # net + 0.5 * summed widths

    def test_price_is_identical_to_marketable_ioc(self):
        (_, a_day, k_day), _ = self._submit(SPREAD, "marketable_day")
        (_, a_ioc, k_ioc), _ = self._submit(SPREAD, "marketable_ioc")
        self.assertAlmostEqual(a_day[2], a_ioc[2], places=6, msg="only the TIF may differ")
        self.assertEqual((k_day["tif"], k_ioc["tif"]), ("DAY", "IOC"))

    def test_v1_is_untouched(self):
        (_, a, k), _ = self._submit(SINGLE, "resting_mid")
        self.assertEqual(k.get("tif"), "DAY")
        self.assertAlmostEqual(a[4], 1.50, places=4)        # v1 rests at the mid

    def test_unknown_mode_still_refused(self):
        _, out = self._submit(SINGLE, "marketable_whatever")
        self.assertEqual(out.get("refused"), "unknown_fill_mode")


class ConfigAcceptsTheModeAndItsKnobs(unittest.TestCase):
    def _cfg(self, v2):
        with patch.object(pt.yaml, "safe_load", return_value={"agent": {"scalp_v2": v2}}):
            return pt.scalp_v2_config()

    def test_marketable_day_with_defaults(self):
        got = self._cfg({"enabled": True, "fill_mode": "marketable_day"})
        self.assertTrue(got["enabled"])
        self.assertEqual(got["fill_mode"], "marketable_day")
        self.assertEqual((got["entry_timeout_min"], got["reattempt_cooldown_min"]), (5, 30))

    def test_knobs_are_read(self):
        got = self._cfg({"enabled": True, "fill_mode": "marketable_day",
                         "entry_timeout_min": 7, "reattempt_cooldown_min": 45})
        self.assertEqual((got["entry_timeout_min"], got["reattempt_cooldown_min"]), (7, 45))

    def test_garbage_mode_is_a_defect_not_a_setting(self):
        got = self._cfg({"enabled": True, "fill_mode": "market"})
        self.assertTrue(got["read_failed"])
        self.assertFalse(got["enabled"])


class _CancelClient:
    """Cancel succeeds and the order is really dead (no fill-during-cancel race)."""
    def __init__(self): self.cancelled = []
    def available(self): return True
    def cancel_order(self, oid): self.cancelled.append(oid); return {"order": {"id": oid, "status": "canceled"}}
    def get_order(self, oid): return {"id": oid, "status": "canceled"}


class CancelUsesThePerModeTimeout(unittest.TestCase):
    def _pending(self, mode, age_min):
        ts = (datetime.now() - timedelta(minutes=age_min)).isoformat(timespec="seconds")
        return [{"intent": "open", "order_id": 11, "rec_id": "20990101-SCALP-1-SPY-debit_spread",
                 "ticker": "SPY", "strategy": "debit_spread", "direction": "long",
                 "fill_mode": mode, "submitted_at": ts}]

    def _run(self, mode, age_min, mode_timeouts):
        c = _CancelClient(); logged = []
        with patch.object(pt, "get_order_client", return_value=c), \
                patch.object(pt, "_load", return_value=self._pending(mode, age_min)), \
                patch.object(pt, "_save"), \
                patch.object(pt, "_log_event", side_effect=lambda e: logged.append(e)):
            out = pt.cancel_stale_entries(30, mode_timeouts=mode_timeouts)
        return out, logged, c

    def test_marketable_day_is_cancelled_at_its_own_short_timeout(self):
        out, logged, c = self._run("marketable_day", 6, {"marketable_day": 5})
        self.assertEqual(len(out), 1)
        self.assertEqual(c.cancelled, [11])
        self.assertEqual(logged[0]["event"], "entry_expired")

    def test_v1_resting_entry_keeps_the_30_minute_default(self):
        out, _, c = self._run("resting_mid", 6, {"marketable_day": 5})
        self.assertEqual(out, [], "a 6-minute-old v1 entry must NOT be cancelled")
        self.assertEqual(c.cancelled, [])

    def test_expiry_row_is_stamped_for_the_accounting(self):
        _, logged, _ = self._run("marketable_day", 6, {"marketable_day": 5})
        e = logged[0]
        self.assertEqual(e["fill_mode"], "marketable_day")
        self.assertEqual((e["strategy"], e["direction"]), ("debit_spread", "long"))


def _ledger(tmp_path, rows):
    p = tmp_path / "fills.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def _expired(ts, tk="SPY", mode="marketable_day", strat="debit_spread", direction="long"):
    r = {"ts": ts, "event": "entry_expired", "rec_id": f"20260916-SCALP-1-{tk}-{strat}",
         "ticker": tk, "strategy": strat, "direction": direction}
    if mode is not None:
        r["fill_mode"] = mode
    return r


class ACancelledAttemptIsCounted(unittest.TestCase):
    """The silent-break guard: an expiry that nothing counts would un-bound the
    attempt cap and make the fill rate read ~100%."""

    def test_attempt_cap_counts_marketable_expiries_only(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            today = f"{datetime.now():%Y-%m-%d}"
            p = _ledger(Path(d), [
                _expired(f"{today}T07:00:00"),                       # counts
                _expired(f"{today}T07:10:00", tk="QQQ"),             # counts
                _expired(f"{today}T07:20:00", tk="IWM", mode=None),  # legacy v1: does NOT
            ])
            with patch.object(pt, "FILLS_LOG", p):
                self.assertEqual(pt.today_v2_attempt_count(), 2)

    def test_fill_rate_denominator_counts_them_and_leaves_history_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _ledger(Path(d), [
                {"ts": "2026-09-16T07:00:00", "event": "paper_fill",
                 "rec_id": "20260916-SCALP-1-SPY-debit_spread", "fill_mode": "marketable_day"},
                _expired("2026-09-16T07:05:00"),
                _expired("2026-09-16T07:10:00", tk="QQQ"),
                _expired("2026-09-16T07:15:00", tk="IWM", mode=None),   # legacy: ignored
            ])
            crit = {"scope": {"fill_mode": "marketable_ioc", "ledger": str(p)},
                    "window_start": "2026-09-12T00:00:00-07:00", "min_fill_rate": 0.25}
            fr = sfc.ioc_fill_rate(crit, [], reject_ids=set())
            self.assertEqual((fr["filled"], fr["unfilled"]), (1, 2))
            self.assertAlmostEqual(fr["rate"], 1 / 3)

    def test_mechanics_treats_the_expiry_as_an_attempt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _ledger(Path(d), [
                _expired("2026-09-16T07:05:00"),
                _expired("2026-09-16T07:15:00", tk="IWM", mode=None),   # legacy: ignored
            ])
            crit = {"status": "FROZEN", "window_start": "2026-09-12T22:55:27-07:00",
                    "scope": {"ledger": "fills.jsonl", "rec_id_tag": "-SCALP-",
                              "fill_mode": "marketable_ioc"},
                    "rules_frozen": {"max_attempts_per_day": 30}}
            m = svm.mechanics(p, crit, lambda e: (None, "pending"), today="2026-09-16",
                              reject_ids=set())
            self.assertEqual(m["cumulative"]["attempts"], 1)
            self.assertEqual(m["cumulative"]["fills"], 0)


class ChurnGuard(unittest.TestCase):
    """A cancelled DAY entry leaves no position and no pending order, so without
    this the same setup re-fires on the very next cycle, forever."""

    def _probe(self, rows, **kw):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _ledger(Path(d), rows)
            with patch.object(pt, "FILLS_LOG", p):
                return pt.recent_entry_expiry(kw.get("tk", "SPY"), "debit_spread", "long",
                                              kw.get("cooldown", 30))

    def _ago(self, minutes):
        return (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def test_blocks_inside_the_cooldown(self):
        self.assertIsNotNone(self._probe([_expired(self._ago(5))]))

    def test_releases_after_the_cooldown(self):
        self.assertIsNone(self._probe([_expired(self._ago(45))]))

    def test_newest_expiry_decides(self):
        self.assertIsNotNone(self._probe([_expired(self._ago(90)), _expired(self._ago(2))]))
        self.assertIsNone(self._probe([_expired(self._ago(2)), _expired(self._ago(90))]))

    def test_other_setups_and_legacy_rows_do_not_block(self):
        self.assertIsNone(self._probe([_expired(self._ago(5), tk="QQQ")]))
        self.assertIsNone(self._probe([_expired(self._ago(5), mode=None)]))
        self.assertIsNone(self._probe([_expired(self._ago(5), direction="short")]))

    def test_zero_cooldown_disables_the_guard(self):
        self.assertIsNone(self._probe([_expired(self._ago(1))], cooldown=0))


class CriteriaScopeGate(unittest.TestCase):
    """ioc_fill_rate returns None for a non-marketable candidate. Matching
    "marketable_ioc" exactly would make the fill-rate criterion silently vanish
    from a future marketable_day candidate's gate."""

    def _crit(self, mode):
        import tempfile
        d = tempfile.mkdtemp()
        p = _ledger(Path(d), [_expired("2026-09-16T07:05:00")])
        return {"scope": {"fill_mode": mode, "ledger": str(p)},
                "window_start": "2026-09-12T00:00:00-07:00", "min_fill_rate": 0.25}

    def test_marketable_day_candidate_still_gets_the_check(self):
        fr = sfc.ioc_fill_rate(self._crit("marketable_day"), [], reject_ids=set())
        self.assertIsNotNone(fr)
        self.assertEqual(fr["unfilled"], 1)

    def test_marketable_ioc_unchanged(self):
        self.assertIsNotNone(sfc.ioc_fill_rate(self._crit("marketable_ioc"), [], reject_ids=set()))

    def test_v1_resting_candidate_has_no_fill_rate_criterion(self):
        self.assertIsNone(sfc.ioc_fill_rate(self._crit("resting_mid"), [], reject_ids=set()))


class ActivatedDeliberately(unittest.TestCase):
    """ACTIVATED 2026-09-16 (operator: option 4). The config and the frozen
    criteria must agree on the mode: if they drift, evidence is scoped to a
    fill mode the engine is not running and the window silently measures
    nothing — the failure that made the IOC window unreadable in the first
    place, in a different disguise."""

    def _files(self):
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())["agent"]["scalp_v2"]
        crit = yaml.safe_load(
            (ROOT / "config" / "scalp_v2_forward_criteria.yaml").read_text())["scalp_v2_forward"]
        return cfg, crit

    def test_config_and_criteria_agree_on_the_fill_mode(self):
        cfg, crit = self._files()
        self.assertEqual(cfg["fill_mode"], "marketable_day")
        self.assertEqual(crit["scope"]["fill_mode"], cfg["fill_mode"])
        self.assertEqual(crit["rules_frozen"]["fill_mode"], cfg["fill_mode"])

    def test_the_knobs_are_part_of_the_frozen_candidate(self):
        cfg, crit = self._files()
        self.assertEqual(crit["rules_frozen"]["entry_timeout_min"], cfg["entry_timeout_min"])
        self.assertEqual(crit["rules_frozen"]["reattempt_cooldown_min"],
                         cfg["reattempt_cooldown_min"])

    def test_the_window_was_reset_at_activation(self):
        _, crit = self._files()
        self.assertGreaterEqual(str(crit["window_start"])[:10], "2026-09-16",
                                "changing the fill mechanism RESETS window_start")
