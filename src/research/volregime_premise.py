"""Vol-regime strategy — PREMISE TEST (does the causal IV-vs-RV signal work?).

Hypothesis: premium-selling failed Stage-6 because the old gate sold vol whenever it
looked rich by its OWN history (iv_rank) — but in the 2022 bear, implied was high yet
REALIZED was higher (IV < RV), so you were underpaid and lost. A CAUSAL signal that
compares implied to TRAILING realized vol should (a) predict the forward VRP you'd
actually harvest, and (b) correctly say "don't sell" in the bear.

Causal signal (known at t):   sig = iv30(t) - RV_trail(t)        [RV over past 20d]
Realized outcome (t -> t+21): fwd_vrp = iv30(t) - RV_fwd(t)      [what a short-vol pos harvests]

If sig>0 reliably -> fwd_vrp>0 (and sig<0 -> fwd_vrp<0), AND the bear lands in sig<0,
the gate works -> build the adaptive strategy. Else: honest negative, idea is dead.

  python src/research/volregime_premise.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
INP = ROOT / "data" / "vrp_inputs.parquet"
TD = 252
RV_WIN = 20      # trailing realized-vol window (causal)
FWD = 21         # forward realized-vol horizon (~30 calendar days, matches iv30)
INDICES = ["SPY", "QQQ", "IWM"]


def build(df):
    """Per ticker: causal sig and forward-VRP outcome, monthly-sampled (non-overlap)."""
    out = []
    for tk, g in df.groupby("ticker"):
        g = g.sort_values("date").copy()
        if len(g) < TD:
            continue
        lr = np.log(g["spot"] / g["spot"].shift(1))
        g["rv_trail"] = lr.rolling(RV_WIN).std() * np.sqrt(TD)          # causal
        g["rv_fwd"] = lr.shift(-1).rolling(FWD).std().shift(-(FWD - 1)) * np.sqrt(TD)
        g["sig"] = g["iv30"] - g["rv_trail"]                            # known at t
        g["term"] = g["iv60"] - g["iv30"]                               # contango>0 (causal)
        g["fwd_vrp"] = g["iv30"] - g["rv_fwd"]                          # realized harvest
        g["year"] = pd.to_datetime(g["date"]).dt.year
        g["ticker"] = tk
        v = g.dropna(subset=["sig", "fwd_vrp"])
        out.append(v.iloc[::FWD])      # non-overlapping monthly obs
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def summarize(name, d):
    if d.empty:
        print(f"{name}: no data"); return
    corr = d["sig"].corr(d["fwd_vrp"])
    pos, neg = d[d["sig"] > 0], d[d["sig"] < 0]
    uncond = d["fwd_vrp"].mean()
    print(f"\n=== {name} (n={len(d)} monthly obs) ===")
    print(f"  corr(sig, fwd_vrp): {corr:+.2f}   (want strongly positive — signal predicts harvest)")
    print(f"  UNCONDITIONAL fwd_vrp: mean {uncond:+.4f}  hit {(d['fwd_vrp']>0).mean():.0%}")
    print(f"  sig>0 (sell): n={len(pos):4} fwd_vrp mean {pos['fwd_vrp'].mean():+.4f}  hit {(pos['fwd_vrp']>0).mean():.0%}")
    print(f"  sig<0 (don't): n={len(neg):4} fwd_vrp mean {neg['fwd_vrp'].mean():+.4f}  hit {(neg['fwd_vrp']>0).mean():.0%}")
    lift = pos['fwd_vrp'].mean() - uncond
    print(f"  CONDITIONAL LIFT (sig>0 vs unconditional): {lift:+.4f}")


def per_year(name, d):
    print(f"\n  {name} — per-year: does sig<0 catch the bad vol years?")
    print(f"  {'year':>6} {'sig>0 n':>8} {'sig>0 vrp':>10} {'sig<0 n':>8} {'sig<0 vrp':>10} {'all vrp':>9}")
    for y in sorted(d["year"].unique()):
        dy = d[d["year"] == y]
        p, n = dy[dy["sig"] > 0], dy[dy["sig"] < 0]
        tag = "  <- BEAR" if y == 2022 else ""
        print(f"  {y:>6} {len(p):>8} {p['fwd_vrp'].mean() if len(p) else float('nan'):>10.4f} "
              f"{len(n):>8} {n['fwd_vrp'].mean() if len(n) else float('nan'):>10.4f} "
              f"{dy['fwd_vrp'].mean():>9.4f}{tag}")


def term_gate_test(name, d):
    """Does the TERM-STRUCTURE gate (contango, iv60>iv30 — forward-looking) separate
    forward VRP and exclude the bear better than the trailing-RV signal? term is in
    vrp_inputs as iv60-iv30; recompute here on the monthly sample."""
    d = d.copy()
    if "term" not in d:
        return
    print(f"\n=== {name} — TERM-STRUCTURE gate (contango vs backwardation) ===")
    con, bak = d[d["term"] > 0], d[d["term"] <= 0]
    print(f"  contango (term>0, SELL):  n={len(con):5} fwd_vrp {con['fwd_vrp'].mean():+.4f} hit {(con['fwd_vrp']>0).mean():.0%}")
    print(f"  backwardation (term<=0):  n={len(bak):5} fwd_vrp {bak['fwd_vrp'].mean():+.4f} hit {(bak['fwd_vrp']>0).mean():.0%}")
    # combined gate: contango AND sig>0
    both = d[(d["term"] > 0) & (d["sig"] > 0)]
    print(f"  COMBINED (contango & sig>0): n={len(both):5} fwd_vrp {both['fwd_vrp'].mean():+.4f} hit {(both['fwd_vrp']>0).mean():.0%}")
    print(f"  per-year contango fwd_vrp (does it survive 2022/2020 stress?):")
    for y in sorted(d["year"].unique()):
        cy = con[con["year"] == y]
        tag = "  <- BEAR" if y == 2022 else ("  <- COVID" if y == 2020 else "")
        print(f"    {y}: n={len(cy):4} fwd_vrp {cy['fwd_vrp'].mean() if len(cy) else float('nan'):+.4f}{tag}")


def main():
    df = pd.read_parquet(INP)
    df = df[df["ticker"] != "VIX"]
    print(f"vrp_inputs: {df['ticker'].nunique()} tickers, {df['date'].min()}..{df['date'].max()}")

    alld = build(df)
    summarize("ALL LIQUID NAMES", alld)
    per_year("ALL", alld)
    term_gate_test("ALL LIQUID NAMES", alld)

    idx = build(df[df["ticker"].isin(INDICES)])
    summarize("INDICES (SPY/QQQ/IWM)", idx)
    per_year("INDICES", idx)
    term_gate_test("INDICES", idx)


if __name__ == "__main__":
    main()
