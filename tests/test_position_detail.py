"""Position-detail payoff math tests (2026-07-05).

The payoff curve backs the cockpit's per-position diagram — the numbers a
human uses to understand their risk, so the math is pinned exactly.

Run: .venv/bin/python -m unittest tests.test_position_detail -v
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.detail import parse_occ, payoff_curve  # noqa: E402


def pnl_at(curve: dict, s: float) -> float:
    return min(curve["points"], key=lambda p: abs(p["s"] - s))["pnl"]


class ParseOccTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_occ("SPY260710C00740000"), {"strike": 740.0, "type": "call"})
        self.assertEqual(parse_occ("TLT260711P00088000"), {"strike": 88.0, "type": "put"})
        self.assertEqual(parse_occ("XSP1!240119C04750500")["strike"], 4750.5)
        self.assertIsNone(parse_occ("garbage"))
        self.assertIsNone(parse_occ(""))


class PayoffTest(unittest.TestCase):
    def test_call_debit_spread(self):
        # buy 740C / sell 747C, 3.60 debit, x2 — width 7, max profit 3.4/sh
        legs = [{"symbol": "SPY260710C00740000", "action": "buy_to_open"},
                {"symbol": "SPY260710C00747000", "action": "sell_to_open"}]
        c = payoff_curve(legs, 3.6, 2, spot=744.0)
        self.assertEqual(c["maxProfit"], 680.0)   # (7 - 3.6) * 100 * 2
        self.assertEqual(c["maxLoss"], -720.0)    # 3.6 * 100 * 2
        self.assertEqual(c["breakevens"], [743.6])
        self.assertEqual(pnl_at(c, 750), 680.0)

    def test_put_debit_spread_the_drill_trade(self):
        # the real 07-02 drill: buy 88P / sell 86P, 0.16 debit, x10
        legs = [{"symbol": "TLT260711P00088000", "action": "buy_to_open"},
                {"symbol": "TLT260711P00086000", "action": "sell_to_open"}]
        c = payoff_curve(legs, 0.16, 10, spot=87.5)
        self.assertEqual(c["maxProfit"], 1840.0)  # (2 - 0.16) * 100 * 10
        self.assertEqual(c["maxLoss"], -160.0)
        self.assertEqual(c["breakevens"], [87.84])

    def test_single_leg_long_call(self):
        legs = [{"symbol": "AAPL260711C00100000", "action": "buy_to_open"}]
        c = payoff_curve(legs, 2.0, 1, spot=100.0)
        self.assertEqual(c["maxLoss"], -200.0)
        self.assertEqual(c["breakevens"], [102.0])
        self.assertAlmostEqual(pnl_at(c, 110), 800.0, delta=15)  # grid resolution

    def test_unparseable_leg_refuses(self):
        self.assertIsNone(payoff_curve([{"symbol": "nope", "action": "buy_to_open"}], 1.0, 1))
        self.assertIsNone(payoff_curve([], 1.0, 1))


if __name__ == "__main__":
    unittest.main()
