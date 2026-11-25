"""Stage 3 backtester tests (src/research/backtester.py).

Two levels:
  * BacktestResult metrics (win rate, profit factor, drawdown, Sharpe) on
    hand-built trades / equity with known answers;
  * backtest() end to end on a 5-bar synthetic series — a bar-0 entry fills at
    bar-1 open and a bar-2 exit fills at bar-3 open (the read-at-close /
    fill-next-bar execution model), giving one long trade 100 -> 110.

Run: .venv/bin/python -m unittest tests.test_backtester -v
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.backtester import BacktestResult, Trade, backtest  # noqa: E402
from src.research.signals import Signals  # noqa: E402

T0 = pd.Timestamp("2022-01-03 09:30")
T1 = pd.Timestamp("2022-01-03 10:30")


class TestMetrics(unittest.TestCase):
    def _trades(self):
        # long P&L: +10%, -10%, +5%
        return [Trade(T0, T1, 100, 110, 1, 1),
                Trade(T0, T1, 100, 90, 1, 1),
                Trade(T0, T1, 100, 105, 1, 1)]

    def test_win_rate_and_profit_factor(self):
        res = BacktestResult(trades=self._trades(), equity=pd.Series([1.0]))
        self.assertAlmostEqual(res.win_rate, 2 / 3)
        # gross profit 0.10+0.05=0.15, gross loss 0.10 -> PF 1.5
        self.assertAlmostEqual(res.profit_factor, 1.5)

    def test_max_drawdown(self):
        # peak 1.2 then 0.9 -> -25%
        eq = pd.Series([1.0, 1.2, 0.9, 1.0], index=pd.date_range("2022-01-03", periods=4))
        res = BacktestResult(trades=[], equity=eq)
        self.assertAlmostEqual(res.max_drawdown, -0.25)

    def test_sharpe_annualised(self):
        # returns [0.10, 0.20]: mean 0.15, std(ddof=1) sqrt(0.005); pp=1 -> 2.12132
        eq = pd.Series([1.0, 1.1, 1.32], index=pd.date_range("2022-01-03", periods=3))
        res = BacktestResult(trades=[], equity=eq, periods_per_year=1)
        self.assertAlmostEqual(res.sharpe, 2.121320, places=5)


class TestBacktestEndToEnd(unittest.TestCase):
    def test_single_long_trade_fills_next_bar_open(self):
        idx = pd.date_range("2022-01-03 09:30", periods=5, freq="1h")
        opens = [99, 100, 105, 110, 111]
        closes = [99, 101, 106, 110, 111]
        df = pd.DataFrame({
            "Open": opens,
            "High": [c + 2 for c in closes],
            "Low": [c - 2 for c in closes],
            "Close": closes,
        }, index=idx, dtype=float)
        entry = pd.Series([True, False, False, False, False], index=idx)
        exit_ = pd.Series([False, False, True, False, False], index=idx)

        res = backtest(df, Signals(entry_long=entry, exit_long=exit_))

        self.assertEqual(res.n_trades, 1)
        t = res.trades[0]
        self.assertAlmostEqual(t.entry_price, 100.0)  # bar-1 open
        self.assertAlmostEqual(t.exit_price, 110.0)   # bar-3 open
        self.assertEqual(t.bars_held, 2)
        self.assertEqual(t.direction, 1)
        self.assertAlmostEqual(t.pnl_pct, 0.10)
        self.assertAlmostEqual(res.win_rate, 1.0)


if __name__ == "__main__":
    unittest.main()
