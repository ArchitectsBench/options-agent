"""GEX gate — the screen #2 signal as a tradeable entry/exit filter.

Drop-in replacement for RegimeGate (same .ok / .hard_break interface) so it runs
through the existing backtester. Hypothesis: premium-selling failed on a VRP gate,
but high dealer gamma (high GEX) predicts a ~25-30% smaller next-day move (screen
result) — so sell defined-risk premium ONLY on high-GEX days.

  ok(tk, d)         -> GEX_z >= entry_z          (dealers long gamma -> expect calm)
  hard_break(tk, d) -> GEX_z <= exit_z           (dealers short gamma -> expect moves)

GEX_z = 252d z-score of net dealer $gamma = sum_k gamma*(cOi-pOi)*spot^2*0.01*100.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.backtest.chain import load


@dataclass
class GexParams:
    entry_z: float = 0.5      # "high GEX" = above +0.5 sd dealer gamma
    exit_z: float = -1.0      # bail if dealers flip deeply short gamma


class GexGate:
    def __init__(self, tickers, params: GexParams | None = None):
        self.p = params or GexParams()
        self.z: dict[str, dict] = {}
        for tk in tickers:
            try:
                store = load(tk)
            except FileNotFoundError:
                continue
            df = store._df
            df = df[(df["dte"] >= 0) & (df["dte"] <= 90)]
            g = df.groupby("trade_date").apply(lambda x: float(
                (x["gamma"].fillna(0) * (x["cOi"].fillna(0) - x["pOi"].fillna(0))
                 * x["spot"] ** 2 * 0.01 * 100).sum())).sort_index()
            z = (g - g.rolling(252, min_periods=60).mean()) / g.rolling(252, min_periods=60).std()
            self.z[tk] = {pd.Timestamp(d).strftime("%Y-%m-%d"): float(v)
                          for d, v in z.dropna().items()}

    def _z(self, tk, date):
        return self.z.get(tk, {}).get(pd.Timestamp(date).strftime("%Y-%m-%d"))

    def ok(self, tk, date) -> bool:
        z = self._z(tk, date)
        return z is not None and z >= self.p.entry_z

    def hard_break(self, tk, date) -> bool:
        z = self._z(tk, date)
        return z is not None and z <= self.p.exit_z
