"""Cross-sectional factor ENSEMBLE on the expanded universe (the pro approach).

Three classic, largely-uncorrelated cross-sectional factors, each dollar-neutral
(no market beta), long top quantile / short bottom quantile:
  MOMENTUM   trailing 126d return, skip last 5d (12-1 style)   long winners
  REVERSAL   trailing 5d return                                long losers
  LOWVOL     trailing 63d realized vol                         long low-vol
Each is a weak edge; the ENSEMBLE (equal-risk blend) should beat any single one
because the factors diversify (momentum & reversal are negatively correlated).

Honest gate: report each factor + the ensemble through Stage-6, plus the factor
correlation matrix (proof the diversification is real) and cost sensitivity.

  python src/research/xsec_ensemble.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PANEL = ROOT / "data" / "universe_daily.parquet"
COST_BPS = 2.0
QUANT = 0.2          # top/bottom 20%


def _neutral_weights(score: pd.DataFrame, long_high: bool) -> pd.DataFrame:
    """Dollar-neutral weights: +1/n_long on top quantile, -1/n_short on bottom."""
    r = score.rank(axis=1, pct=True)
    hi = (r >= 1 - QUANT)
    lo = (r <= QUANT)
    long_, short_ = (hi, lo) if long_high else (lo, hi)
    w = long_.div(long_.sum(axis=1), axis=0).fillna(0) - short_.div(short_.sum(axis=1), axis=0).fillna(0)
    return w


def _factor_return(close, ret, score, long_high) -> pd.Series:
    w = _neutral_weights(score, long_high).shift(1)               # causal
    cost = (COST_BPS / 1e4) * w.diff().abs().sum(axis=1).fillna(0)
    return (w * ret).sum(axis=1) - cost


def _stats(d: pd.Series) -> dict:
    d = d.dropna()
    if len(d) < 100 or d.std() == 0:
        return {"sharpe": 0, "oos": 0, "maxdd": 1, "mc5": -9, "ann": 0, "n": len(d)}
    sh = d.mean() / d.std() * np.sqrt(252)
    eq = (1 + d).cumprod(); mdd = float((1 - eq / eq.cummax()).max())
    k = int(len(d) * 0.7)
    oos = d.iloc[k:].mean() / d.iloc[k:].std() * np.sqrt(252)
    rng = np.random.default_rng(0); a = d.to_numpy()
    mc = np.percentile([(s := rng.choice(a, len(a), True)).mean() / s.std() * np.sqrt(252)
                        for _ in range(2000)], 5)
    return {"sharpe": float(sh), "oos": float(oos), "maxdd": mdd, "mc5": float(mc),
            "ann": float(d.mean() * 252), "n": len(d)}


def _verdict(m, trades=200):
    c = {"sharpe>=1.2": m["sharpe"] >= 1.2, "oos>=0.8": m["oos"] >= 0.8,
         "maxdd<=20%": m["maxdd"] <= 0.20, "mc5>0.5": m["mc5"] > 0.5, "trades>=100": trades >= 100}
    return all(c.values()), c


def main():
    if not PANEL.exists():
        sys.exit(f"missing {PANEL} — run pull_universe_daily.py first")
    close = pd.read_parquet(PANEL)
    close = close.dropna(axis=1, thresh=int(len(close) * 0.6))     # drop sparse names
    ret = close.pct_change()
    print(f"universe {close.shape[1]} names, {len(close)} sessions, "
          f"{close.index.min().date()}..{close.index.max().date()}, cost {COST_BPS}bp\n")

    mom = close.pct_change(126).shift(5)            # 12-1 style (skip last week)
    rev = close.pct_change(5)
    lowvol = ret.rolling(63).std()
    factors = {
        "MOMENTUM (126-5d)": _factor_return(close, ret, mom, long_high=True),
        "REVERSAL (5d)":     _factor_return(close, ret, rev, long_high=False),
        "LOWVOL (63d)":      _factor_return(close, ret, lowvol, long_high=False),
    }
    # equal-RISK ensemble: scale each to 10% ann vol, average
    scaled = {}
    for k, d in factors.items():
        v = d.std() * np.sqrt(252)
        scaled[k] = d / v * 0.10 if v > 0 else d
    ens = pd.concat(scaled.values(), axis=1).mean(axis=1)
    factors["── ENSEMBLE (equal-risk) ──"] = ens

    print(f"{'factor':28} {'Sharpe':>7} {'OOS':>6} {'maxDD':>6} {'MC5':>6} {'annRet':>7}  gate")
    for k, d in factors.items():
        m = _stats(d); ok, _ = _verdict(m)
        print(f"{k:28} {m['sharpe']:>7.2f} {m['oos']:>6.2f} {m['maxdd']:>6.1%} "
              f"{m['mc5']:>6.2f} {m['ann']:>6.1%}  {'PASS ✅' if ok else ''}")

    print("\nfactor return correlations (low/negative = diversification works):")
    corr = pd.concat({k: v for k, v in factors.items() if "ENSEMBLE" not in k}, axis=1).corr()
    print(corr.round(2).to_string())

    # ── refinement: constant-vol momentum (textbook crash fix) + 21d reversal ──
    def vol_scale(d, target=0.10, win=21, cap=3.0):
        rv = (d.rolling(win).std() * np.sqrt(252)).shift(1)     # causal
        return d * (target / rv).clip(upper=cap).fillna(1.0)

    mom_vs = vol_scale(factors["MOMENTUM (126-5d)"])
    rev21 = _factor_return(close, ret, close.pct_change(21), long_high=False)
    sc = lambda d: d / (d.std() * np.sqrt(252)) * 0.10
    ens2 = pd.concat([sc(mom_vs), sc(rev21)], axis=1).mean(axis=1)
    refined = {"MOMENTUM vol-scaled": mom_vs, "REVERSAL (21d)": rev21,
               "── ENSEMBLE2 (volMom + rev21) ──": ens2}
    print(f"\n── refined ──\n{'factor':28} {'Sharpe':>7} {'OOS':>6} {'maxDD':>6} {'MC5':>6} {'annRet':>7}  gate")
    for k, d in refined.items():
        m = _stats(d); ok, _ = _verdict(m)
        print(f"{k:28} {m['sharpe']:>7.2f} {m['oos']:>6.2f} {m['maxdd']:>6.1%} "
              f"{m['mc5']:>6.2f} {m['ann']:>6.1%}  {'PASS ✅' if ok else ''}")


if __name__ == "__main__":
    main()
