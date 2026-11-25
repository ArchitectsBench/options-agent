"""Stack fundamental factors (value/quality/earnings) onto the residual-momentum
anchor. Fundamentals are aligned POINT-IN-TIME (known only from filing_date, then
forward-filled) — no lookahead — and there's a further 1-day execution lag.

Fundamental factors (per-share / ratio, so no shares-outstanding needed):
  VALUE     earnings yield = TTM EPS / price            (long cheap)
  ROE       TTM net income / equity                     (long high)
  GMARGIN   TTM gross profit / TTM revenue              (long high)
  EARNMOM   YoY change in TTM EPS                        (long improving)

  python src/research/fundamental_stack.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.research.factor_stack import fac_ret, vol_scale, row   # reuse machinery

CLOSE = ROOT / "data" / "universe_daily.parquet"
FUND = ROOT / "data" / "fundamentals_raw.parquet"


def pit_panel(fund, metric, dates):
    """Daily point-in-time panel: each trading day gets the most recent value
    filed on/before it (reindex+ffill on filing_date)."""
    # ffill DOWN each ticker column before aligning to the trading calendar:
    # each filing_date row holds only names that filed that exact day, so a bare
    # reindex(method="ffill") copies the single nearest prior row wholesale and
    # collapses every fundamental panel onto whoever filed last (2026-06-24 audit).
    p = (fund.pivot_table(index="filing_date", columns="ticker", values=metric, aggfunc="last")
         .sort_index().ffill())
    return p.reindex(dates, method="ffill")


def main():
    close = pd.read_parquet(CLOSE).dropna(axis=1, thresh=int(1255 * 0.6))
    ret = close.pct_change()
    fund = pd.read_parquet(FUND).sort_values(["ticker", "filing_date"])

    # TTM aggregates + year-ago EPS (point-in-time, by filing order)
    g = fund.groupby("ticker")
    for col in ["eps", "ni", "rev", "gp"]:
        fund[f"{col}_ttm"] = g[col].transform(lambda s: s.rolling(4).sum())
    fund["eps_ttm_prev"] = g["eps"].transform(lambda s: s.rolling(4).sum().shift(4))
    fund["equity_last"] = fund["equity"]

    dates = close.index
    eps_ttm = pit_panel(fund, "eps_ttm", dates)
    ni_ttm = pit_panel(fund, "ni_ttm", dates)
    rev_ttm = pit_panel(fund, "rev_ttm", dates)
    gp_ttm = pit_panel(fund, "gp_ttm", dates)
    equity = pit_panel(fund, "equity_last", dates)
    eps_prev = pit_panel(fund, "eps_ttm_prev", dates)

    cols = close.columns.intersection(eps_ttm.columns)
    px = close[cols]
    eps_ttm, ni_ttm, rev_ttm, gp_ttm, equity, eps_prev = (
        p.reindex(columns=cols) for p in (eps_ttm, ni_ttm, rev_ttm, gp_ttm, equity, eps_prev))

    value = (eps_ttm / px).where(px > 0)                       # earnings yield
    roe = (ni_ttm / equity.where(equity > 0))
    gmargin = (gp_ttm / rev_ttm.where(rev_ttm > 0))
    earnmom = (eps_ttm - eps_prev)                            # YoY EPS change

    # price anchors (residual momentum is the best price factor)
    mkt = ret.mean(axis=1)
    beta = ret.rolling(126).cov(mkt).div(mkt.rolling(126).var(), axis=0)
    resid = ret.sub(beta.mul(mkt, axis=0))

    facs = {
        "RESMOM (anchor)": vol_scale(fac_ret(ret, resid.rolling(126).sum().shift(5), True)),
        "VALUE (E/P)":     vol_scale(fac_ret(ret, value, True)),
        "ROE":             vol_scale(fac_ret(ret, roe, True)),
        "GMARGIN":         vol_scale(fac_ret(ret, gmargin, True)),
        "EARNMOM (YoY EPS)": vol_scale(fac_ret(ret, earnmom, True)),
    }
    print(f"universe {len(cols)} names w/ fundamentals, {len(close)} sessions, "
          f"{close.index.min().date()}..{close.index.max().date()}\n")
    print(f"{'factor (vol-scaled, PIT)':24} {'Sharpe':>7} {'OOS':>6} {'maxDD':>6} {'MC5':>6} {'annRet':>7}  gate")
    ms = {k: row(k, v) for k, v in facs.items()}

    sc = lambda d: d / (d.std() * np.sqrt(252)) * 0.10
    eq = lambda keys: pd.concat([sc(facs[k]) for k in keys], axis=1).mean(axis=1)
    fundamentals = [k for k in facs if k != "RESMOM (anchor)"]
    keep = [k for k in fundamentals if ms[k]["oos"] > 0.2]

    print(f"\npositive-OOS fundamentals: {keep}")
    print(f"\n── ENSEMBLES ──\n{'ensemble':24} {'Sharpe':>7} {'OOS':>6} {'maxDD':>6} {'MC5':>6} {'annRet':>7}  gate")
    row("RESMOM + all-fund", eq(list(facs)))
    if keep:
        row("RESMOM + good-fund", eq(["RESMOM (anchor)"] + keep))
    row("all 5 equal-risk", eq(list(facs)))

    print("\nfactor correlations (vs RESMOM — want LOW for diversification):")
    print(pd.concat(facs, axis=1).corr().round(2)["RESMOM (anchor)"].to_string())


if __name__ == "__main__":
    main()
