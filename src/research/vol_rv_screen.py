"""Cheap screen #3: cross-sectional VOLATILITY relative-value.

Naive premium-selling died because ALL vol spiked together in the bear (systematic
short-vol). A market-NEUTRAL book — short the relatively-rich names' vol, long the
relatively-cheap — cancels that systemic move. Test whether the edge exists:

  signal  = IV-rank (iv30 percentile vs the name's OWN 252d history) -> "richness"
  outcome = forward VRP_i = iv30_i - realized_vol_i over next 21d
  Each day, quintile names by richness; measure forward VRP per quintile.
  Edge: VRP rises monotonically with richness, AND the long-short spread
  (Q5_rich - Q1_cheap) is positive AND SURVIVES THE BEAR (unlike long-only).

Data: vrp_inputs.parquet (iv30 + spot, 484 names, 2020-2026). Already on disk.

  python src/research/vol_rv_screen.py
"""
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
INP = ROOT / "data" / "vrp_inputs.parquet"
FWD = 21
RANK_WIN = 252


def _regime(d):
    d = pd.Timestamp(d)
    if pd.Timestamp("2020-02-20") <= d <= pd.Timestamp("2020-04-30"): return "covid_crash"
    if d.year == 2021: return "calm_2021"
    if pd.Timestamp("2022-01-01") <= d <= pd.Timestamp("2022-10-15"): return "bear_2022"
    if 2023 <= d.year <= 2024: return "recovery_23_24"
    return "recent_25_26"


def main():
    df = pd.read_parquet(INP)
    iv = df.pivot_table(index="date", columns="ticker", values="iv30").sort_index()
    spot = df.pivot_table(index="date", columns="ticker", values="spot").sort_index()
    iv.index = pd.to_datetime(iv.index); spot.index = pd.to_datetime(spot.index)

    # forward 21d realized vol per name (annualized)
    ret = np.log(spot / spot.shift(1))
    fwd_rv = ret.shift(-1).rolling(FWD).std().shift(-(FWD - 1)) * np.sqrt(252)
    # IV-rank per name (own 252d percentile)
    iv_rank = iv.rolling(RANK_WIN).rank(pct=True)
    vrp = iv - fwd_rv

    # stack to long form
    long = pd.DataFrame({
        "iv_rank": iv_rank.stack(), "vrp": vrp.stack(),
    }).dropna()
    long = long.reset_index().rename(columns={"level_0": "date", "level_1": "ticker"})
    long["regime"] = long["date"].map(_regime)

    # per-day cross-sectional quintile by richness
    def q5(x):
        return pd.qcut(x, 5, labels=False, duplicates="drop")
    long["quint"] = long.groupby("date")["iv_rank"].transform(q5)
    long = long.dropna(subset=["quint"])

    print(f"=== CROSS-SECTIONAL VOL RV: {len(long):,} name-days, "
          f"{long.date.min().date()}..{long.date.max().date()} ===\n")
    print("Forward VRP by richness quintile (Q0=cheapest IV vs own history, Q4=richest):")
    qm = long.groupby("quint")["vrp"].mean()
    for q in sorted(qm.index):
        print(f"  Q{int(q)}: {qm[q]:+.4f}")
    monotonic = all(qm[i] <= qm[i + 1] for i in range(len(qm) - 1))
    print(f"  monotonic (richer -> higher VRP)? {monotonic}")

    # long-short spread: short richest (collect VRP), long cheapest (pay VRP)
    daily = long.pivot_table(index="date", columns="quint", values="vrp", aggfunc="mean")
    hi, lo = daily[daily.columns.max()], daily[daily.columns.min()]
    spread = (hi - lo).dropna()
    print(f"\nLONG-SHORT spread (Q4 rich - Q0 cheap) — the market-NEUTRAL edge:")
    print(f"  mean {spread.mean():+.4f}  | % days positive {(spread>0).mean():.0%}  | "
          f"daily Sharpe {spread.mean()/spread.std():.2f}")
    m = spread.iloc[::FWD]
    print(f"  non-overlapping monthly: mean {m.mean():+.4f}, ann Sharpe "
          f"{m.mean()/m.std()*np.sqrt(12):.2f}, {(m>0).mean():.0%} positive (n={len(m)})")

    print(f"\n  BY REGIME (spread vs naive long-only short-vol = mean VRP all names):")
    sp_reg = spread.groupby(spread.index.map(_regime)).mean()
    naive_reg = long.groupby("regime")["vrp"].mean()
    order = ["covid_crash", "calm_2021", "bear_2022", "recovery_23_24", "recent_25_26"]
    print(f"    {'regime':16} {'L-S spread':>12} {'naive long-only':>16}")
    for rg in [o for o in order if o in sp_reg.index]:
        print(f"    {rg:16} {sp_reg[rg]:>+12.4f} {naive_reg.get(rg, float('nan')):>+16.4f}")
    print("\n  READ: positive L-S spread that stays positive in bear_2022 (while naive "
          "long-only goes negative) = a real, hedged cross-sectional edge.")


if __name__ == "__main__":
    main()
