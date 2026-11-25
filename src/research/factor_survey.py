"""Survey candidate factors to DIVERSIFY the RESMOM+EARNMOM book and lift MC5.

Premise (2026-06-26): the market-neutral RESMOM+EARNMOM book is the best lead
(full Sharpe ~0.81) but FAILS Stage-6 on robustness (MC 5th-pct Sharpe ~0.08 vs
the >0.5 bar). Raw Sharpe isn't the wall; MC5 is. The principled way to lift the
5th-percentile is BREADTH: add documented anomalies that are (a) low-correlation
with the existing book and (b) not negative-mean this sample. Tuning won't do it.

Every candidate is a PRE-SPECIFIED, documented factor (not data-mined), built with
the SAME bug-fixed PIT alignment and the shared factor_stack machinery (fac_ret /
vol_scale / stats). We report each factor's standalone stats, its correlation with
the base book, and whether stacking it lifts the base book's MC5.

  python src/research/factor_survey.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.research.factor_stack import fac_ret, vol_scale, stats

PANEL = ROOT / "data" / "universe_daily.parquet"
FUND = ROOT / "data" / "fundamentals_raw.parquet"


def pit_panel(fund, metric, dates):
    """Point-in-time panel: ffill DOWN each ticker column first (so every name's
    last-known filing carries forward), THEN align to the price calendar. The bare
    reindex(method='ffill') bug (2026-06-24) is avoided here."""
    p = (fund.pivot_table(index="filing_date", columns="ticker", values=metric, aggfunc="last")
         .sort_index().ffill())
    return p.reindex(dates, method="ffill")


def build_factors():
    close = pd.read_parquet(PANEL).dropna(axis=1, thresh=int(1255 * 0.6))
    ret = close.pct_change()
    mkt = ret.mean(axis=1)
    beta = ret.rolling(126).cov(mkt).div(mkt.rolling(126).var(), axis=0)
    resid = ret.sub(beta.mul(mkt, axis=0))
    dates, cols = close.index, close.columns

    fund = pd.read_parquet(FUND).sort_values(["ticker", "filing_date"])
    g = fund.groupby("ticker")
    for c in ["eps", "rev", "gp"]:
        fund[f"{c}_ttm"] = g[c].transform(lambda s: s.rolling(4).sum())
    fund["eps_ttm_prev"] = g["eps"].transform(lambda s: s.rolling(4).sum().shift(4))
    fund["rev_ttm_prev"] = g["rev"].transform(lambda s: s.rolling(4).sum().shift(4))

    eps_ttm  = pit_panel(fund, "eps_ttm", dates).reindex(columns=cols)
    eps_prev = pit_panel(fund, "eps_ttm_prev", dates).reindex(columns=cols)
    rev_ttm  = pit_panel(fund, "rev_ttm", dates).reindex(columns=cols)
    rev_prev = pit_panel(fund, "rev_ttm_prev", dates).reindex(columns=cols)
    gp_ttm   = pit_panel(fund, "gp_ttm", dates).reindex(columns=cols)
    equity   = pit_panel(fund, "equity", dates).reindex(columns=cols)

    # ── factor return series (each dollar-neutral, vol-scaled, costed) ──
    F = {}
    # BASE sleeves (the existing book)
    F["RESMOM"]  = vol_scale(fac_ret(ret, resid.rolling(126).sum().shift(5), True))
    F["EARNMOM"] = vol_scale(fac_ret(ret, eps_ttm - eps_prev, True))
    # CANDIDATE diversifiers — documented anomalies, chosen orthogonal to mom/earn:
    F["GPROF"]   = vol_scale(fac_ret(ret, gp_ttm / equity.where(equity > 0), True))      # Novy-Marx gross profitability (gp/equity != gmargin)
    F["REVMOM"]  = vol_scale(fac_ret(ret, (rev_ttm / rev_prev.where(rev_prev > 0)) - 1, True))  # YoY sales growth
    F["VALUE"]   = vol_scale(fac_ret(ret, eps_ttm / close.where(close > 0), True))        # E/P (earnings yield)
    F["REV21"]   = vol_scale(fac_ret(ret, close.pct_change(21), False))                   # 1-month reversal (long losers)
    F["IVOL"]    = vol_scale(fac_ret(ret, resid.rolling(21).std(), False))                # low idiosyncratic vol (long low)
    F["EQGROW"]  = vol_scale(fac_ret(ret, equity / pit_panel(fund, "equity", dates).reindex(columns=cols).shift(252), False))  # investment factor (short high asset growth)
    F["LOWBETA"] = vol_scale(fac_ret(ret, beta, False))                                   # betting-against-beta (long low beta) — canonical bear-defensive
    return F


def eq_risk(F, keys):
    """Equal-risk combine of vol-scaled sleeves (same as factor_stack ensembles)."""
    sc = lambda d: d / (d.std() * np.sqrt(252)) * 0.10
    return pd.concat([sc(F[k]) for k in keys], axis=1).mean(axis=1)


def main():
    F = build_factors()
    base_keys = ["RESMOM", "EARNMOM"]
    cands = ["GPROF", "REVMOM", "VALUE", "REV21", "IVOL", "EQGROW"]

    base = eq_risk(F, base_keys)
    bm = stats(base)
    print("BASE BOOK = RESMOM + EARNMOM")
    print(f"  Sharpe {bm['sharpe']:.2f}  OOS {bm['oos']:.2f}  maxDD {bm['maxdd']:.1%}  "
          f"MC5 {bm['mc5']:.2f}  ann {bm['ann']:.1%}  worstYr {bm['wy']:.2f}  by-year {bm['by']}\n")

    # correlation of each candidate with the BASE book return (want LOW |corr|)
    print(f"{'candidate':10} {'Sharpe':>7} {'OOS':>6} {'MC5':>6} {'corr_base':>10}  {'mean/day(bp)':>12}")
    diversifiers = []
    for k in cands:
        m = stats(F[k])
        corr = pd.concat([F[k], base], axis=1).dropna().corr().iloc[0, 1]
        meanbp = F[k].dropna().mean() * 1e4
        tag = ""
        if abs(corr) < 0.3 and meanbp > -0.2:        # low-corr AND not meaningfully negative
            diversifiers.append(k); tag = "  <- diversifier"
        print(f"{k:10} {m['sharpe']:>7.2f} {m['oos']:>6.2f} {m['mc5']:>6.2f} {corr:>10.2f}  {meanbp:>12.2f}{tag}")

    print(f"\nQualifying diversifiers (|corr|<0.3, mean not negative): {diversifiers}")

    # ── does stacking lift MC5? test base + each, and base + all qualifiers ──
    print(f"\n{'stacked book':28} {'Sharpe':>7} {'OOS':>6} {'maxDD':>6} {'MC5':>6} {'annRet':>7} {'WorstYr':>7}  {'ΔMC5':>6}  gate")
    from src.research.monte_carlo import REGIME_MIN_WORST_YEAR_SHARPE
    def show(name, keys):
        m = stats(eq_risk(F, keys))
        # bar revised 2026-07-01 — keep in sync with monte_carlo.acceptance_gate
        ok = (m["sharpe"] >= 0.8 and m["oos"] >= 0.6 and m["maxdd"] <= 0.2 and m["mc5"] >= 0.0
              and not pd.isna(m["wy"]) and m["wy"] >= REGIME_MIN_WORST_YEAR_SHARPE)
        print(f"{name:28} {m['sharpe']:>7.2f} {m['oos']:>6.2f} {m['maxdd']:>6.1%} "
              f"{m['mc5']:>6.2f} {m['ann']:>7.1%} {m['wy']:>7.2f}  {m['mc5']-bm['mc5']:>+6.2f}  {'PASS ✅' if ok else ''}")
    show("base (RESMOM+EARNMOM)", base_keys)
    for k in cands:
        show(f"base + {k}", base_keys + [k])
    if diversifiers:
        show("base + ALL diversifiers", base_keys + diversifiers)


if __name__ == "__main__":
    main()
