"""Regime proxy tests (2026-07-01: TICK / put-call wired as labeled proxies).

Covers: put/call volume math from synthetic chains, UNCALIBRATED extreme
classification in both directions, tick_proxy mapping from synthetic breadth
readings, and unavailable-data handling -> null value + feed marked missing
in build_regime (no crash), plus not-missing when both proxies compute.

Run: .venv/bin/python -m unittest tests.test_regime_proxies -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import options_chain as oc
from src.indicators import regime_live as rl


def _contract(opt_type: str, volume: int, strike: float = 100.0) -> dict:
    return {"symbol": f"TST_{opt_type}_{strike}", "type": opt_type,
            "strike": strike, "expiration": "2026-07-02", "bid": 1.0,
            "ask": 1.1, "mid": 1.05, "spread": 0.1, "oi": 1000,
            "volume": volume, "delta": 0.5, "gamma": 0.01, "theta": -0.05,
            "iv": 0.25}


class PutCallMathTest(unittest.TestCase):
    def test_ratio_across_chains(self):
        spy = [_contract("put", 300), _contract("call", 400)]
        qqq = [_contract("put", 300), _contract("call", 200)]
        value, n = oc.put_call_from_chains([spy, qqq])
        self.assertEqual(value, 1.0)   # (300+300) / (400+200)
        self.assertEqual(n, 4)

    def test_zero_volume_contracts_not_counted(self):
        chain = [_contract("put", 500), _contract("call", 250),
                 _contract("put", 0), _contract("call", 0)]
        value, n = oc.put_call_from_chains([chain])
        self.assertEqual(value, 2.0)
        self.assertEqual(n, 2)  # only volume > 0 contracts counted

    def test_no_call_volume_is_null_not_crash(self):
        value, n = oc.put_call_from_chains([[_contract("put", 500)]])
        self.assertIsNone(value)
        self.assertEqual(n, 1)

    def test_empty_chains_null(self):
        self.assertEqual(oc.put_call_from_chains([]), (None, 0))
        self.assertEqual(oc.put_call_from_chains([[]]), (None, 0))


class PutCallExtremeTest(unittest.TestCase):
    def test_fear_above_threshold(self):
        self.assertEqual(oc.classify_put_call(oc.PC_FEAR_THRESHOLD + 0.01), "fear")

    def test_complacency_below_threshold(self):
        self.assertEqual(oc.classify_put_call(oc.PC_COMPLACENCY_THRESHOLD - 0.01),
                         "complacency")

    def test_neutral_band_and_exact_thresholds(self):
        # conservative: exact threshold values do NOT classify as extreme
        for r in (0.9, oc.PC_FEAR_THRESHOLD, oc.PC_COMPLACENCY_THRESHOLD):
            self.assertIsNone(oc.classify_put_call(r))

    def test_none_ratio(self):
        self.assertIsNone(oc.classify_put_call(None))


class PutCallProxyTest(unittest.TestCase):
    def test_proxy_dict_shape_on_success(self):
        chains = {"SPY": [_contract("put", 900), _contract("call", 300)],
                  "QQQ": [_contract("put", 600), _contract("call", 700)]}
        with patch.object(oc, "_cycle_chain", side_effect=lambda t, b: chains[t]):
            out = oc.put_call_proxy()
        self.assertEqual(out["value"], 1.5)   # 1500 / 1000
        self.assertEqual(out["source"], "chain_proxy_spy_qqq")
        self.assertEqual(out["n_contracts"], 4)
        self.assertEqual(out["extreme"], "fear")

    def test_proxy_null_when_chains_unavailable(self):
        with patch.object(oc, "_cycle_chain", return_value=None):
            out = oc.put_call_proxy()
        self.assertIsNone(out["value"])
        self.assertEqual(out["n_contracts"], 0)
        self.assertIsNone(out["extreme"])
        self.assertEqual(out["source"], "chain_proxy_spy_qqq")

    def test_proxy_partial_chains_still_compute(self):
        chains = {"SPY": [_contract("put", 100), _contract("call", 200)], "QQQ": None}
        with patch.object(oc, "_cycle_chain", side_effect=lambda t, b: chains[t]):
            out = oc.put_call_proxy()
        self.assertEqual(out["value"], 0.5)
        self.assertEqual(out["extreme"], "complacency")


class TickProxyTest(unittest.TestCase):
    def _breadth(self, net, side=None):
        return {"net_advancers_pct": net, "advancing": 30, "declining": 15,
                "unchanged": 5, "counted": 50, "extreme": side is not None,
                "extreme_side": side, "threshold": 60.0,
                "label": "breadth_proxy (estimated; 5m snapshot, not tick-level)"}

    def test_maps_breadth_no_extreme(self):
        out = rl.tick_proxy_from_breadth(self._breadth(30.0))
        self.assertEqual(out, {"net_adv_pct": 30.0, "extreme": None,
                               "source": "breadth_proxy"})

    def test_positive_extreme(self):
        out = rl.tick_proxy_from_breadth(self._breadth(72.0, "bullish"))
        self.assertEqual(out["extreme"], "positive")

    def test_negative_extreme(self):
        out = rl.tick_proxy_from_breadth(self._breadth(-68.0, "bearish"))
        self.assertEqual(out["extreme"], "negative")

    def test_unavailable_breadth_is_none(self):
        self.assertIsNone(rl.tick_proxy_from_breadth(None))


class BuildRegimeMissingHandlingTest(unittest.TestCase):
    """build_regime with all feeds mocked: proxies failing -> null + missing
    (no crash); proxies succeeding -> present and NOT in missing."""

    def _build(self, breadth, put_call):
        with patch.object(rl, "fetch_intraday", return_value=None), \
             patch.object(rl, "fetch_futures_quote", return_value=None), \
             patch.object(rl, "_iv_momentum", return_value=None), \
             patch("src.data.breadth.compute_breadth", return_value=breadth), \
             patch("src.data.breadth.log_breadth"), \
             patch("src.data.options_chain.put_call_proxy", return_value=put_call):
            return rl.build_regime()

    def test_proxies_unavailable_marked_missing_no_crash(self):
        null_pc = {"value": None, "source": "chain_proxy_spy_qqq",
                   "n_contracts": 0, "extreme": None}
        r = self._build(breadth=None, put_call=null_pc)
        self.assertIsNone(r["tick_proxy"])
        self.assertIsNone(r["put_call_proxy"]["value"])
        self.assertIn("TICK", r["missing"])
        self.assertIn("put_call", r["missing"])
        self.assertTrue(r["degraded"])

    def test_proxies_available_not_missing(self):
        breadth = {"net_advancers_pct": 20.0, "extreme": False,
                   "extreme_side": None, "threshold": 60.0,
                   "counted": 50, "advancing": 30, "declining": 20, "unchanged": 0,
                   "label": "breadth_proxy (estimated; 5m snapshot, not tick-level)"}
        pc = {"value": 0.85, "source": "chain_proxy_spy_qqq",
              "n_contracts": 120, "extreme": None}
        r = self._build(breadth=breadth, put_call=pc)
        self.assertEqual(r["tick_proxy"],
                         {"net_adv_pct": 20.0, "extreme": None, "source": "breadth_proxy"})
        self.assertEqual(r["put_call_proxy"], pc)
        self.assertNotIn("TICK", r["missing"])
        self.assertNotIn("put_call", r["missing"])
        self.assertNotIn("breadth", r["missing"])


if __name__ == "__main__":
    unittest.main()
