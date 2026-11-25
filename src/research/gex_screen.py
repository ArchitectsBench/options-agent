"""Cheap screen #2: does DEALER GAMMA (GEX) predict next-day behaviour?

Reconstructs daily dealer gamma exposure from the per-strike store (gamma + call/put
OI) and tests the documented effect:
  high positive GEX (dealers long gamma -> hedge AGAINST moves) -> SMALLER next-day
  moves (mean-reversion);  negative GEX -> LARGER moves (trend/vol).

Naive dealer convention (SpotGamma-style): dealers long call OI, short put OI ->
  net $gamma per 1% = sum_k gamma_k * (cOi_k - pOi_k) * spot^2 * 0.01 * 100

Edge exists if next-day |return| is materially LOWER on high-GEX days than low-GEX
days (and GEX level negatively correlates with subsequent realized move).

  python src/research/gex_screen.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.backtest.chain import load   # reuse the parsed full-chain store

TICKERS = ["SPY", "QQQ", "IWM"]


def daily_gex(store) -> pd.DataFrame:
    df = store._df          # already parsed: trade_date, strike, spot, gamma, cOi, pOi, dte
    df = df[(df["dte"] >= 0) & (df["dte"] <= 90)]            # near-term carries the gamma
    g = df.groupby("trade_date").apply(lambda x: pd.Series({
        "spot": x["spot"].iloc[0],
        "gex": float((x["gamma"].fillna(0) * (x["cOi"].fillna(0) - x["pOi"].fillna(0))
                      * x["spot"] ** 2 * 0.01 * 100).sum()),
    }))
    g = g.sort_index()
    g["ret_fwd"] = np.log(g["spot"].shift(-1) / g["spot"])
    return g.dropna()


def screen(tk: str):
    g = daily_gex(load(tk))
    # normalize GEX by its trailing 252d scale so quartiles are regime-fair
    g["gex_z"] = (g["gex"] - g["gex"].rolling(252, min_periods=60).mean()) \
        / g["gex"].rolling(252, min_periods=60).std()
    g = g.dropna(subset=["gex_z"])
    g["abs_fwd"] = g["ret_fwd"].abs()
    q = g["gex_z"].quantile([0.25, 0.75])
    lo = g[g["gex_z"] <= q[0.25]]   # most negative GEX
    hi = g[g["gex_z"] >= q[0.75]]   # most positive GEX
    corr = g["gex_z"].corr(g["abs_fwd"])
    print(f"\n=== {tk}: {len(g)} days ===")
    print(f"  corr(GEX_z, |next-day ret|): {corr:+.3f}   (negative = high GEX -> calmer)")
    print(f"  next-day |ret|  high-GEX quartile: {hi['abs_fwd'].mean():.4f}  "
          f"vs low-GEX quartile: {lo['abs_fwd'].mean():.4f}  "
          f"ratio {hi['abs_fwd'].mean()/lo['abs_fwd'].mean():.2f}")
    # directional mean-reversion: sign of today's move vs next day, by GEX
    g["ret_today"] = np.log(g["spot"] / g["spot"].shift(1))
    hi2 = g[g["gex_z"] >= q[0.75]].dropna(subset=["ret_today"])
    lo2 = g[g["gex_z"] <= q[0.25]].dropna(subset=["ret_today"])
    ac_hi = hi2["ret_today"].corr(hi2["ret_fwd"])
    ac_lo = lo2["ret_today"].corr(lo2["ret_fwd"])
    print(f"  return autocorr(t, t+1)  high-GEX: {ac_hi:+.3f} (neg=mean-revert)  "
          f"low-GEX: {ac_lo:+.3f} (pos=trend)")
    return corr, hi["abs_fwd"].mean(), lo["abs_fwd"].mean()


def main():
    print("DEALER GAMMA (GEX) SCREEN — does positioning predict next-day moves?")
    for tk in TICKERS:
        try:
            screen(tk)
        except FileNotFoundError:
            print(f"  {tk}: no chain store")
    print("\nREAD: negative corr + high-GEX |moves| < low-GEX |moves| + high-GEX "
          "return autocorr negative -> GEX mean-reversion signal is real & tradable.")


if __name__ == "__main__":
    main()
