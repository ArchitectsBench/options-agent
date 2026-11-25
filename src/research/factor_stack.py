"""Stack documented cross-sectional factors onto the vol-scaled momentum anchor.

Each factor is a PRE-SPECIFIED, documented anomaly (not data-mined). All are
dollar-neutral (market-neutral), constant-vol scaled, costed. We report EVERY
factor (winners and losers), then principled ensembles, judged OOS.

Factors (close-only):
  MOM        126-5d return (long winners)                 [anchor]
  RESMOM     residual momentum: momentum on market-beta residuals (cleaner)
  HI52       close / 252d-high  (long near 52wk high)
  MAXLOTT    max daily ret over 21d (long LOW — short lottery demand)
  REV5       5d return (long losers)
  LOWBETA    126d beta vs universe (long low beta — BAB style)

  python src/research/factor_stack.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.research.monte_carlo import worst_year_sharpe, REGIME_MIN_WORST_YEAR_SHARPE

PANEL = ROOT / "data" / "universe_daily.parquet"
COST_BPS = 2.0
QUANT = 0.2


def neutral_w(score, long_high=True):
    r = score.rank(axis=1, pct=True)
    hi, lo = (r >= 1 - QUANT), (r <= QUANT)
    a, b = (hi, lo) if long_high else (lo, hi)
    return a.div(a.sum(axis=1), axis=0).fillna(0) - b.div(b.sum(axis=1), axis=0).fillna(0)


def fac_ret(ret, score, long_high=True):
    w = neutral_w(score, long_high).shift(1)
    cost = (COST_BPS / 1e4) * w.diff().abs().sum(axis=1).fillna(0)
    return (w * ret).sum(axis=1) - cost


def vol_scale(d, target=0.10, win=21, cap=3.0):
    rv = (d.rolling(win).std() * np.sqrt(252)).shift(1)
    return d * (target / rv).clip(upper=cap).fillna(1.0)


def stats(d):
    d = d.dropna()
    if len(d) < 100 or d.std() == 0:
        return {"sharpe": 0, "oos": 0, "maxdd": 1, "mc5": -9, "ann": 0, "wy": float("nan"), "by": {}}
    sh = d.mean() / d.std() * np.sqrt(252)
    eq = (1 + d).cumprod(); mdd = float((1 - eq / eq.cummax()).max())
    k = int(len(d) * 0.7)
    oos = d.iloc[k:].mean() / d.iloc[k:].std() * np.sqrt(252)
    rng = np.random.default_rng(0); a = d.to_numpy()
    mc = float(np.percentile([(s := rng.choice(a, len(a), True)).mean() / s.std() * np.sqrt(252)
                              for _ in range(1500)], 5))
    wy, by = worst_year_sharpe(d)        # regime acid test (same gate as Stage-5/6)
    return {"sharpe": float(sh), "oos": float(oos), "maxdd": mdd, "mc5": mc,
            "ann": float(d.mean()*252), "wy": wy, "by": by}


def row(name, d):
    m = stats(d)
    # bar revised 2026-07-01 — keep in sync with monte_carlo.acceptance_gate
    ok = (m["sharpe"] >= 0.8 and m["oos"] >= 0.6 and m["maxdd"] <= 0.2 and m["mc5"] >= 0.0
          and not pd.isna(m["wy"]) and m["wy"] >= REGIME_MIN_WORST_YEAR_SHARPE)
    print(f"{name:30} {m['sharpe']:>7.2f} {m['oos']:>6.2f} {m['maxdd']:>6.1%} "
          f"{m['mc5']:>6.2f} {m['ann']:>6.1%} {m['wy']:>6.2f}  {'PASS ✅' if ok else ''}")
    return m


def main():
    close = pd.read_parquet(PANEL).dropna(axis=1, thresh=int(1255 * 0.6))
    ret = close.pct_change()
    mkt = ret.mean(axis=1)
    print(f"universe {close.shape[1]} names, {len(close)} sessions, "
          f"{close.index.min().date()}..{close.index.max().date()}\n")

    # residual momentum
    w = 126
    beta = ret.rolling(w).cov(mkt).div(mkt.rolling(w).var(), axis=0)
    resid = ret.sub(beta.mul(mkt, axis=0))
    factors_raw = {
        "MOM (126-5d)":   fac_ret(ret, close.pct_change(126).shift(5), True),
        "RESMOM (126-5d)": fac_ret(ret, resid.rolling(126).sum().shift(5), True),
        "HI52":           fac_ret(ret, close / close.rolling(252).max(), True),
        "MAXLOTT (21d)":  fac_ret(ret, ret.rolling(21).max(), False),
        "REV5":           fac_ret(ret, close.pct_change(5), False),
        "LOWBETA":        fac_ret(ret, beta, False),
    }
    # constant-vol scale every factor for fair stacking
    facs = {k: vol_scale(v) for k, v in factors_raw.items()}

    print(f"{'factor (vol-scaled)':30} {'Sharpe':>7} {'OOS':>6} {'maxDD':>6} {'MC5':>6} {'annRet':>7} {'WorstYr':>6}  gate")
    ms = {k: row(k, v) for k, v in facs.items()}

    # keep factors with positive OOS Sharpe (out-of-sample, the honest filter)
    keep = [k for k, m in ms.items() if m["oos"] > 0.2]
    print(f"\npositive-OOS factors kept for ensemble: {keep}")

    def eq_risk(keys):
        sc = lambda d: d / (d.std() * np.sqrt(252)) * 0.10
        return pd.concat([sc(facs[k]) for k in keys], axis=1).mean(axis=1)

    print(f"\n── ENSEMBLES ──\n{'ensemble':30} {'Sharpe':>7} {'OOS':>6} {'maxDD':>6} {'MC5':>6} {'annRet':>7}  gate")
    row("ALL factors", eq_risk(list(facs)))
    if keep:
        row("positive-OOS only", eq_risk(keep))
    row("MOM + RESMOM", eq_risk([k for k in ["MOM (126-5d)", "RESMOM (126-5d)"] if k in facs]))

    print("\nfactor correlations:")
    print(pd.concat(facs, axis=1).corr().round(2).to_string())


if __name__ == "__main__":
    main()
