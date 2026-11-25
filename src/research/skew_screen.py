"""Mine a DIRECTIONAL regime feature: 25-delta put/call SKEW.

GEX, term-slope, IV-momentum all predict vol MAGNITUDE, not direction. Skew
(puts richer than calls = downside-hedging demand / fear) is the candidate to add
a directional dimension. Computed from full chains (SPY/QQQ/IWM):

  skew = iv(25d put) - iv(25d call)   at the ~35-DTE expiry   (higher = more fear)

Tests (cross-validated across the 3 ETFs):
  - DIRECTION:  corr(skew, signed next-day return)  -> does fear predict down (neg)
                or bounce (contrarian, pos)?
  - DOWNSIDE :  mean next-day return on high-skew vs low-skew days
  - also corr(skew, |move|) to see if it doubles as a vol signal.
  - skew CHANGE (steepening) tested too — often more informative than level.

  python src/research/skew_screen.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.backtest.chain import load

TICKERS = ["SPY", "QQQ", "IWM"]


def daily_skew(store) -> pd.DataFrame:
    df = store._df
    df = df[(df["dte"] >= 14) & (df["dte"] <= 60)]

    def one(g):
        exp = g.loc[(g["dte"] - 35).abs().idxmin(), "expiration"]
        ge = g[g["expiration"] == exp]
        pe = ge[ge["put_iv"] > 0]
        ce = ge[ge["call_iv"] > 0]
        if pe.empty or ce.empty:
            return np.nan
        put_iv = pe.iloc[((pe["delta"] - 1) - (-0.25)).abs().argmin()]["put_iv"]
        call_iv = ce.iloc[(ce["delta"] - 0.25).abs().argmin()]["call_iv"]
        return float(put_iv - call_iv)

    g = df.groupby("trade_date").apply(lambda x: pd.Series({
        "spot": x["spot"].iloc[0], "skew": one(x)})).sort_index().dropna()
    g["ret_fwd"] = np.log(g["spot"].shift(-1) / g["spot"])
    g["abs_fwd"] = g["ret_fwd"].abs()
    g["skew_chg5"] = g["skew"] - g["skew"].shift(5)
    return g.dropna(subset=["ret_fwd"])


def main():
    print("25-DELTA SKEW SCREEN — directional regime feature?\n")
    dir_rows, chg_rows = [], []
    for tk in TICKERS:
        try:
            g = daily_skew(load(tk))
        except FileNotFoundError:
            continue
        cdir = g["skew"].corr(g["ret_fwd"])
        cvol = g["skew"].corr(g["abs_fwd"])
        cchg = g["skew_chg5"].dropna().corr(g.loc[g["skew_chg5"].dropna().index, "ret_fwd"])
        q = g["skew"].quantile([0.25, 0.75])
        hi_ret = g[g["skew"] >= q[0.75]]["ret_fwd"].mean()
        lo_ret = g[g["skew"] <= q[0.25]]["ret_fwd"].mean()
        print(f"  {tk}: mean skew {g['skew'].mean():+.3f} | "
              f"corr(skew, signed ret)={cdir:+.3f}  corr(skew,|move|)={cvol:+.3f}  "
              f"corr(Δskew, ret)={cchg:+.3f}")
        print(f"        next-day mean ret: high-skew {hi_ret*1e4:+.1f}bp vs "
              f"low-skew {lo_ret*1e4:+.1f}bp")
        dir_rows.append(cdir); chg_rows.append(cchg)
    if dir_rows:
        print(f"\n  AVG corr(skew, direction) {np.mean(dir_rows):+.3f} "
              f"(consistent sign: {len(set(np.sign(dir_rows)))==1})")
        print(f"  AVG corr(Δskew, direction) {np.mean(chg_rows):+.3f} "
              f"(consistent sign: {len(set(np.sign(chg_rows)))==1})")
    print("\n  READ: a consistent non-trivial directional corr = the directional "
          "feature we lack. Near-zero/inconsistent = skew is just another fear/vol "
          "proxy, not directional.")


if __name__ == "__main__":
    main()
