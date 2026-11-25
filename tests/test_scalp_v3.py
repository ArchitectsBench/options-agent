"""SCALP-v3 filter layer (docs/scalp_v3_prereg.md) as the PIPELINE enforces it.

Drives the real ``pipeline._paper_execution_test`` with explicit cfg dicts, a
recording submit stub and a pinned clock — same hermetic harness as
tests/test_v2_caps_gates.py (O-20 seal redirects ledgers; broker never touched).
"""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import src.engine.live_gate as lg
import src.engine.paper_trader as pt
import src.engine.pipeline as pl

ET = ZoneInfo("America/New_York")


def _clock(h, m):
    class _C(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime.now(ET).replace(hour=h, minute=m, second=0, microsecond=0)
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)
    return _C


TRADE_0DTE = {"structure": "long_call", "strategy": "0dte_directional", "direction": "long",
              "net_debit": 2.10, "max_loss": 2.10, "max_profit": 99.0,
              "expiration": "2099-01-09", "breakeven": 745.6,
              "legs": [{"symbol": "SPY990109C00743000", "action": "buy_to_open",
                        "bid": 2.09, "ask": 2.11, "mid": 2.10, "spread": 0.02}]}
TRADE_SPREAD = {"structure": "call_debit_spread", "strategy": "debit_spread",
                "direction": "long", "net_debit": 3.6, "max_loss": 3.6, "max_profit": 3.4,
                "expiration": "2099-01-09", "breakeven": 743.6,
                "legs": [{"symbol": "SPY990109C00740000", "action": "buy_to_open",
                          "bid": 5.25, "ask": 5.35, "mid": 5.3, "spread": 0.10},
                         {"symbol": "SPY990109C00747000", "action": "sell_to_open",
                          "bid": 1.65, "ask": 1.75, "mid": 1.7, "spread": 0.10}]}


def _signal(volume_ratio=1.4, ema5="bearish"):
    now = datetime.now(ET).isoformat()
    return {"last": 743.5, "atr": 2.1, "bar_time": now, "bar_time_30m": now,
            "squeeze": {"5m": "off", "15m": "on", "30m": "off"},
            "ema_stack": {"5m": ema5, "15m": "bullish", "30m": "bullish"},
            "macd_5m": {"sign": "positive", "direction": "rising"},
            "vwap_dist_atr": 0.8, "volume_ratio": volume_ratio,
            "rs_vs_spy_30min_pct": 0.2, "opening_range": {"position": "above"}}


def _cfg(v2=True, v3=True, v2_fill="marketable_day", v3_block=None):
    agent = {"paper_test_mode": True, "lane_isolation": True,
             "paper_test_max_per_day": 3, "paper_test_max_open": 7,
             "scalp_v2": {"enabled": v2, "fill_mode": v2_fill, "aggressiveness": 0.5,
                          "min_net_debit": 1.00, "max_per_day": 10,
                          "max_attempts_per_day": 30}}
    if v3_block is not None:
        agent["scalp_v3"] = v3_block
    elif v3 is not None:
        agent["scalp_v3"] = {"enabled": v3, "tickers": ["SPY", "QQQ", "IWM"],
                             "strategies": ["0dte_directional"],
                             "entry_window_et": ["09:35", "11:30"],
                             "min_volume_ratio": 0.8, "fill_mode": "marketable_ioc"}
    return {"agent": agent,
            "risk": {"account_size": 50000, "max_risk_per_trade_pct": 2.0,
                     "max_daily_loss_pct": 3.0, "max_contracts": 10,
                     "max_open_per_lane": {"scalp": 7, "funnel": 3},
                     "max_book_risk_per_underlying_usd": 20000}}


class _H(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._ks = lg.KILL_SWITCH_FILE
        lg.KILL_SWITCH_FILE = Path(self.tmp.name) / "KILL_SWITCH"
        self.submits = []

    def tearDown(self):
        lg.KILL_SWITCH_FILE = self._ks
        self.tmp.cleanup()

    def _submit(self, rec, tag="X", quantity=1, **kw):
        self.submits.append({"rec": rec, "tag": tag, "quantity": quantity, **kw})
        return {"submitted": True, "order_id": 42, "status": "ok"}

    def run_gate(self, cfg, *, tickers=("SPY",), strat="0dte_directional",
                 trade=TRADE_0DTE, at=(10, 0), sig=None):
        signals = {t: (sig or _signal()) for t in tickers}
        applicability = {t: {strat: {"verdict": "viable", "confirms_met": []}} for t in tickers}
        out = io.StringIO()
        with patch.object(pl, "datetime", _clock(*at)), \
                patch("src.engine.options_selector.build_trade",
                      return_value=(dict(trade), None)), \
                patch("src.data.freshness.current_session_state", return_value="open"), \
                patch("src.engine.entry_features.build_entry_features", return_value={}), \
                contextlib.redirect_stdout(out):
            pl._paper_execution_test(signals, applicability, 7, cfg,
                                     {"regime": {"bias": "bullish"}},
                                     self._submit, pt.has_open_exposure)
        return out.getvalue()


class V3Places(_H):
    def test_index_0dte_in_window_is_sent_as_ioc_and_stamped_v3(self):
        """Prevents: v3 inheriting v2's marketable_day mode or losing its stamp."""
        self.run_gate(_cfg())
        self.assertEqual(len(self.submits), 1)
        s = self.submits[0]
        self.assertEqual(s["fill_mode"], "marketable_ioc")
        self.assertEqual(s["rec"]["fill_mode"], "marketable_ioc")
        self.assertEqual(s["rec"]["scalp_version"], "v3")
        self.assertEqual(s["rec"]["strategy"], "0dte_directional")
        # shadow frame recorded, but direction still from the 15m stack (bullish -> long)
        self.assertEqual(s["rec"]["shadow_direction"],
                         {"frame": "5m", "ema_stack": "bearish", "traded_direction": "long"})

    def test_window_edges(self):
        """09:35 inclusive, 11:30 exclusive (prereg §3)."""
        self.run_gate(_cfg(), at=(9, 35))
        self.assertEqual(len(self.submits), 1)
        for at in [(9, 34), (11, 30), (11, 45), (14, 0)]:
            self.submits.clear()
            out = self.run_gate(_cfg(), at=at)
            self.assertEqual(self.submits, [], at)
            self.assertIn("outside entry window", out)


class V3Blocks(_H):
    def test_single_names_are_excluded(self):
        self.run_gate(_cfg(), tickers=("XLF", "TSLA"))
        self.assertEqual(self.submits, [])

    def test_debit_spreads_and_the_liquid_fallback_are_excluded(self):
        """Prevents: the SPY/QQQ/IWM debit-spread fallback sneaking back in."""
        self.run_gate(_cfg(), strat="debit_spread", trade=TRADE_SPREAD,
                      tickers=("SPY", "QQQ", "IWM"))
        self.assertEqual(self.submits, [])

    def test_volume_gate(self):
        for vr in (0.79, None):
            self.submits.clear()
            self.run_gate(_cfg(), sig=_signal(volume_ratio=vr))
            self.assertEqual(self.submits, [], vr)
        rows = [json.loads(l) for l in pt.FILLS_LOG.read_text().splitlines()
                if "entry_blocked_v3_volume" in l]
        self.assertGreaterEqual(len(rows), 2)
        self.submits.clear()
        self.run_gate(_cfg(), sig=_signal(volume_ratio=0.8))
        self.assertEqual(len(self.submits), 1)

    def test_v3_without_v2_places_nothing(self):
        out = self.run_gate(_cfg(v2=False))
        self.assertEqual(self.submits, [])
        self.assertIn("without SCALP-v2", out)

    def test_malformed_v3_places_nothing(self):
        for bad in ("yes", {"enabled": True, "entry_window_et": ["11:30", "09:35"]},
                    {"enabled": True, "fill_mode": "resting_mid"},
                    {"enabled": True, "tickers": []}):
            self.submits.clear()
            out = self.run_gate(_cfg(v3_block=bad))
            self.assertEqual(self.submits, [], bad)
            self.assertIn("[SCALP-v3] config read FAILED", out)


class V3OffIsV2(_H):
    def test_v3_disabled_or_absent_keeps_v2_behaviour(self):
        """Prevents: the v3 layer changing anything while off."""
        for v3 in (False, None):
            self.submits.clear()
            self.last_out = self.run_gate(_cfg(v3=v3), tickers=("XLF",), strat="debit_spread",
                          trade=TRADE_SPREAD, at=(14, 0))
            out = self.last_out
            self.assertEqual(len(self.submits), 1, (v3, out))
            self.assertEqual(self.submits[0]["fill_mode"], "marketable_day")
            self.assertEqual(self.submits[0]["rec"]["scalp_version"], "v2")
            self.assertNotIn("shadow_direction", self.submits[0]["rec"])


class V3WholeSession(unittest.TestCase):
    """Replays the recorded 2026-09-09 session through the REAL run_cycle with
    v3 ON (same SimBroker as tests/test_v2_session_sim.py). Guards the surprise
    we refuse to meet first on a Monday: an exception mid-session, a non-index
    or spread entry, a DAY-resting entry, or a position left open."""

    def test_recorded_day_runs_clean_and_only_trades_v3_scope(self):
        from tests.test_v2_session_sim import v2_cfg, _cfg_reader, DAY
        from tests.sim.session_sim import Session, SimBroker
        cfg = v2_cfg()
        cfg["agent"]["scalp_v3"] = {"enabled": True, "tickers": ["SPY", "QQQ", "IWM"],
                                    "strategies": ["0dte_directional"],
                                    "entry_window_et": ["09:35", "11:30"],
                                    "min_volume_ratio": 0.8, "fill_mode": "marketable_ioc"}
        with patch.object(pt, "scalp_v2_config", side_effect=lambda c=None: _cfg_reader(cfg)):
            s = Session(DAY, SimBroker(), cfg).run()
        r = s.report()
        self.assertEqual(r["exceptions"], [], f"cycle exceptions: {r['exceptions']}")
        self.assertEqual(r["day_placed_entries"], 0, "v3 must never rest a DAY entry")
        self.assertEqual(r["resting_ioc_at_broker"], 0)
        self.assertEqual(r["open_positions"], 0, "flat after time_flat")
        self.assertEqual(r["pending_intents"], 0)
        entries = [e for e in s.events() if e.get("event") in ("paper_fill", "entry_ioc_unfilled")]
        for e in entries:
            rid = e.get("rec_id", "")
            self.assertTrue(rid.endswith("-0dte_directional"), rid)
            self.assertIn(rid.split("-")[-2], ("SPY", "QQQ", "IWM"), rid)
            self.assertEqual(e.get("fill_mode"), "marketable_ioc", rid)
        print(f"\n  [v3 sim 2026-09-09] {r}")
