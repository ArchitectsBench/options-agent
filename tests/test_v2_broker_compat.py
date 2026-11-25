"""SCALP-v2 SILENT-DEFAULT hunt — the places where the v2 live path can fall
open to v1 (or to nothing) without a loud signal.

Model for what is hunted (activation night, 2026-09-12): the YAML said
`enabled: true`, a YAML-reading test passed, and the engine restarted with v2
OFF because a fail-open handler swallowed a NameError and answered "disabled".
Every test here asks the ENGINE'S OWN reader/path, never a re-parse of a file,
and every synthetic state has teeth (a candidate that WOULD trade).

HERMETIC: the broker is a recording stub, conftest's O-20 seal redirects
LANE_DIR / FILLS_LOG / REC_LOG to tmp, the kill switch is redirected to tmp.

Run: .venv/bin/python -m pytest -q tests/test_v2_broker_compat.py
"""
import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import src.data.order_client as oc
import src.engine.live_gate as lg
import src.engine.paper_trader as pt
import src.engine.pipeline as pl
from src.data.order_client import DEFINITIVE_REJECT, classify_post_result
from src.data.tradier_orders import PaperOrderClient as TradierClient

import pytest


class _MidSession(datetime):
    """The pipeline's clock pinned to 10:00 (today's date, caller's tz)."""
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def _mid_session_clock():
    """Broker-compat tests run a candidate through the pipeline; the 15:30 ET
    v2 late-entry cutoff made them fail whenever the suite ran after 12:30 PT
    (2026-09-14). They test routing and read-failed handling, not the cutoff."""
    with patch.object(pl, "datetime", _MidSession):
        yield

SINGLE = {"structure": "0DTE long call", "strategy": "0dte_directional", "direction": "long",
          "expiration": "2099-01-01", "net_debit": 1.50, "max_loss": 1.50, "max_profit": None,
          "legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open",
                    "bid": 1.45, "ask": 1.55, "mid": 1.50, "spread": 0.10}]}


# ═════════════════════════════════════════════════════════════════════════════
class TradierCannotHonourIoc(unittest.TestCase):
    """SURPRISE PREVENTED: v2 sends tif="IOC"; the Tradier adapter ACCEPTS the
    kwarg and hard-codes duration="day". If the broker ever resolves to
    tradier_sandbox (config unreadable -> DEFAULT_BROKER, or an operator flip),
    every v2 entry silently becomes a RESTING DAY limit at the ASK — worse than
    v1 (pays the whole spread AND suffers adverse selection) — while the intent
    is stamped fill_mode=marketable_ioc, so the mis-executed trades would be
    booked as v2 evidence. An adapter that cannot honour IOC must refuse (a
    definitive non-placement the engine can see), never quietly downgrade."""

    def _client(self):
        c = TradierClient()
        c.token, c.account = "t", "acct"
        posts = []

        def _rec(method, path, data=None):
            posts.append((method, path, data or {}))
            return {"order": {"id": 99, "status": "ok"}}
        return c, posts, _rec

    def test_single_leg_ioc_is_refused_not_rested_as_day(self):
        c, posts, rec = self._client()
        with patch.object(c, "_request", side_effect=rec):
            res = c.place_option_order("SPY", "SPY990101C00500000", "buy_to_open", 1, 1.55,
                                       tif="IOC")
        self.assertFalse(any(d.get("duration") == "day" for _, _, d in posts),
                         "an IOC request was POSTed as a resting DAY order")
        self.assertEqual(classify_post_result(res)["status"], DEFINITIVE_REJECT,
                         "the engine must learn the IOC was never placed")

    def test_spread_ioc_is_refused_not_rested_as_day(self):
        c, posts, rec = self._client()
        legs = [{"option_symbol": "SPY990101C00500000", "side": "buy_to_open", "quantity": 1},
                {"option_symbol": "SPY990101C00502000", "side": "sell_to_open", "quantity": 1}]
        with patch.object(c, "_request", side_effect=rec):
            res = c.place_spread_order("SPY", legs, 1.08, tif="IOC")
        self.assertFalse(any(d.get("duration") == "day" for _, _, d in posts))
        self.assertEqual(classify_post_result(res)["status"], DEFINITIVE_REJECT)

    def test_day_is_still_posted_as_day(self):
        """v1 guarantee: the DAY path is byte-identical."""
        c, posts, rec = self._client()
        with patch.object(c, "_request", side_effect=rec):
            c.place_option_order("SPY", "SPY990101C00500000", "buy_to_open", 1, 1.50)
        self.assertEqual(posts[0][2].get("duration"), "day")


# ═════════════════════════════════════════════════════════════════════════════
class BrokerResolutionAgreesWithV2(unittest.TestCase):
    """SURPRISE PREVENTED: v2's whole mechanism (IOC) exists only at IBKR. The
    engine's broker resolver falls back to tradier_sandbox on ANY config read
    failure — the same fail-open shape as activation night, one layer down."""

    def test_enabled_v2_runs_on_the_ibkr_broker(self):
        """Through the engine's readers: if scalp_v2_config() says enabled,
        _resolve_broker() must name the broker that can honour IOC."""
        v2 = pt.scalp_v2_config()
        self.assertFalse(v2["read_failed"])
        if v2["enabled"]:
            self.assertEqual(oc._resolve_broker(), "ibkr_paper")

    def test_unreadable_config_does_not_silently_pick_the_tradier_default_while_v2_is_on(self):
        """A config that cannot be read must not resolve to a broker that
        drops the IOC on the floor. Documented fail-open: _resolve_broker()
        answers DEFAULT_BROKER (tradier_sandbox) on any exception."""
        v2 = pt.scalp_v2_config()
        if not v2["enabled"]:
            self.skipTest("v2 is off; the tradier default is harmless for v1")
        with tempfile.TemporaryDirectory() as d:
            got = oc._resolve_broker(Path(d) / "does_not_exist.yaml")
        self.assertNotEqual(got, "tradier_sandbox",
                            "an unreadable config resolved to a broker that cannot honour IOC")


# ═════════════════════════════════════════════════════════════════════════════
class IocNeedsASpreadToBeMarketable(unittest.TestCase):
    """SURPRISE PREVENTED: v2_entry_limit() computes mid + aggressiveness*spread
    with `spread or (ask - bid) or 0`. A leg that arrives without spread/bid/ask
    (a chain snapshot missing quote fields) yields limit == MID with tif IOC —
    v1's price with none of v1's patience: a near-guaranteed miss that burns
    one of the 30 attempt slots, thirty times, silently. Missing quote data
    must be refused, not defaulted to zero."""

    def test_leg_without_quote_fields_is_refused(self):
        trade = {"legs": [{"symbol": "SPY990101C00500000", "action": "buy_to_open", "mid": 1.50}],
                 "net_debit": 1.50}
        with self.assertRaises((ValueError, KeyError)):
            pt.v2_entry_limit(trade, 0.5)

    def test_spread_leg_without_quote_fields_is_refused(self):
        trade = {"net_debit": 1.00,
                 "legs": [{"symbol": "A", "action": "buy_to_open", "mid": 2.00},
                          {"symbol": "B", "action": "sell_to_open", "mid": 1.00}]}
        with self.assertRaises((ValueError, KeyError)):
            pt.v2_entry_limit(trade, 0.5)

    def test_explicit_locked_market_is_still_the_mid(self):
        """bid == ask is real information (a locked quote), not missing data."""
        trade = {"legs": [{"symbol": "X", "action": "buy_to_open", "bid": 1.50, "ask": 1.50,
                           "mid": 1.50, "spread": 0.0}], "net_debit": 1.50}
        self.assertAlmostEqual(pt.v2_entry_limit(trade, 0.5), 1.50, places=4)


# ═════════════════════════════════════════════════════════════════════════════
# Pipeline-level: the 5-minute cycle's own function, with a candidate that WOULD trade.
TRADE = {"structure": "call_debit_spread", "strategy": "debit_spread",
         "direction": "long", "net_debit": 3.6, "max_loss": 3.6, "max_profit": 3.4,
         "expiration": "2099-01-09", "breakeven": 743.6,
         "legs": [{"symbol": "SPY990109C00740000", "action": "buy_to_open",
                   "bid": 5.25, "ask": 5.35, "mid": 5.3, "spread": 0.10},
                  {"symbol": "SPY990109C00747000", "action": "sell_to_open",
                   "bid": 1.65, "ask": 1.75, "mid": 1.7, "spread": 0.10}]}
VIABLE = {"debit_spread": {"verdict": "viable", "confirms_met": ["vwap_with_trend"]}}


def _signal():
    now = datetime.now(ZoneInfo("America/New_York")).isoformat()
    return {"last": 743.5, "atr": 2.1, "bar_time": now, "bar_time_30m": now,
            "squeeze": {"5m": "off", "15m": "on", "30m": "off"},
            "ema_stack": {"5m": "bullish", "15m": "bullish", "30m": "bullish"},
            "macd_5m": {"sign": "positive", "direction": "rising"},
            "vwap_dist_atr": 0.8, "volume_ratio": 1.4,
            "rs_vs_spy_30min_pct": 0.2, "opening_range": {"position": "above"}}


def _cfg(scalp_v2):
    agent = {"paper_test_mode": True, "lane_isolation": True,
             "paper_test_max_per_day": 3, "paper_test_max_open": 7, "scalp_v2": scalp_v2}
    return {"agent": agent,
            "risk": {"account_size": 50000, "max_risk_per_trade_pct": 2.0,
                     "max_daily_loss_pct": 3.0, "max_contracts": 10,
                     "max_open_per_lane": {"scalp": 7, "funnel": 3},
                     "max_book_risk_per_underlying_usd": 20000}}


class _PipelineHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._ks = lg.KILL_SWITCH_FILE
        lg.KILL_SWITCH_FILE = Path(self.tmp.name) / "KILL_SWITCH"
        self.submits = []

    def tearDown(self):
        lg.KILL_SWITCH_FILE = self._ks
        self.tmp.cleanup()

    def _submit(self, rec, tag="UNVALIDATED-PAPER-ONLY", quantity=1, **kw):
        self.submits.append({"rec": rec, "tag": tag, "quantity": quantity, **kw})
        return {"submitted": True, "order_id": 42, "status": "ok"}

    def run_cycle(self, cfg):
        signals = {"SPY": _signal()}
        applicability = {"SPY": dict(VIABLE)}
        out = io.StringIO()
        with patch("src.engine.options_selector.build_trade", return_value=(dict(TRADE), None)), \
                patch("src.data.freshness.current_session_state", return_value="open"), \
                patch("src.engine.entry_features.build_entry_features", return_value={}), \
                contextlib.redirect_stdout(out):
            pl._paper_execution_test(signals, applicability, 7, cfg, {"regime": {}},
                                     self._submit, pt.has_open_exposure)
        return out.getvalue()


class PipelineHonoursTheReadFailedFlag(_PipelineHarness):
    """SURPRISE PREVENTED: after activation night scalp_v2_config() became LOUD
    (logs ERROR, sets read_failed) — but _paper_execution_test branches only on
    `enabled`. A malformed v2 block (here: a scalar where the mapping belongs)
    therefore still TRADES, as v1, at v1's cap, with resting-mid fills, on the
    day the operator believes v2 is live. A read failure post-activation must
    place nothing."""

    def test_sanity_the_harness_candidate_trades_when_v2_is_healthy(self):
        """Teeth: the same candidate DOES submit under a well-formed v2 block."""
        out = self.run_cycle(_cfg({"enabled": True, "fill_mode": "marketable_ioc",
                                   "aggressiveness": 0.5, "min_net_debit": 1.0,
                                   "max_per_day": 10, "max_attempts_per_day": 30}))
        self.assertEqual(len(self.submits), 1, out)
        self.assertEqual(self.submits[0]["fill_mode"], "marketable_ioc")

    def test_failed_v2_read_places_nothing(self):
        # The pipeline imports scalp_v2_config from paper_trader inside the
        # function, so patching the module attribute IS the engine's reader.
        failed = {"enabled": False, "read_failed": True, "fill_mode": "marketable_ioc",
                  "aggressiveness": 0.5, "min_net_debit": 1.0, "max_per_day": 10,
                  "max_attempts_per_day": 30}
        with patch.object(pt, "scalp_v2_config", return_value=failed):
            out = self.run_cycle(_cfg({"enabled": True}))
        self.assertEqual(self.submits, [],
                         f"pipeline traded on a FAILED v2 config read (as v1): {out}")

    def test_scalar_v2_block_does_not_crash_the_reader(self):
        """Found while writing the test above: a scalar where the mapping
        belongs escapes the try (the .get chain in the RETURN statement is
        outside it) and RAISES from the cycle instead of flagging read_failed."""
        got = pt.scalp_v2_config(_cfg("yes"))
        self.assertTrue(got["read_failed"])
        self.assertFalse(got["enabled"])


if __name__ == "__main__":
    unittest.main()
