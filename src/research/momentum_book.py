"""Target book for the forward paper-test: residual-momentum + earnings-momentum,
cross-sectional, dollar-neutral, vol-scaled. Causal — uses only data through the
latest available close. Fundamentals are point-in-time (filing_date).

target_book() -> (asof_date, weights Series)  ; weights sum to 0, gross ~= 2.0
"""
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CLOSE = ROOT / "data" / "universe_daily.parquet"
FUND = ROOT / "data" / "fundamentals_raw.parquet"
QUANT = 0.2


def _neutral(score: pd.Series) -> pd.Series:
    r = score.rank(pct=True)
    hi, lo = r >= 1 - QUANT, r <= QUANT
    w = pd.Series(0.0, index=score.index)
    if hi.sum():
        w[hi] = 1.0 / hi.sum()
    if lo.sum():
        w[lo] = w[lo] - 1.0 / lo.sum()
    return w


def score_panels():
    """Daily RESMOM and EARNMOM score panels (causal/PIT)."""
    close = pd.read_parquet(CLOSE).dropna(axis=1, thresh=int(1255 * 0.6))
    ret = close.pct_change()
    mkt = ret.mean(axis=1)
    beta = ret.rolling(126).cov(mkt).div(mkt.rolling(126).var(), axis=0)
    resid = ret.sub(beta.mul(mkt, axis=0))
    resmom = resid.rolling(126).sum().shift(5)

    fund = pd.read_parquet(FUND).sort_values(["ticker", "filing_date"])
    g = fund.groupby("ticker")
    eps_ttm = g["eps"].transform(lambda s: s.rolling(4).sum())
    eps_prev = g["eps"].transform(lambda s: s.rolling(4).sum().shift(4))
    fund["earnmom"] = eps_ttm - eps_prev
    # ffill each ticker's earnmom DOWN its own column first: each filing_date
    # pivot row holds only names that filed that exact day, so reindex(ffill)
    # alone copies the single nearest prior row wholesale and collapses the
    # sleeve onto whoever filed last (2026-06-24 audit: AZO got 100% of the
    # long quintile). Column-wise ffill carries every name's last-known value.
    em = (fund.pivot_table(index="filing_date", columns="ticker", values="earnmom", aggfunc="last")
          .sort_index().ffill().reindex(close.index, method="ffill"))
    em = em.reindex(columns=close.columns)
    return close, resmom, em


def target_book(asof=None):
    close, resmom, em = score_panels()
    idx = close.index[-1] if asof is None else pd.Timestamp(asof)
    cols = close.columns
    w_rm = _neutral(resmom.loc[idx, cols].dropna())
    w_em = _neutral(em.loc[idx, cols].dropna())
    w = (0.5 * w_rm.reindex(cols).fillna(0) + 0.5 * w_em.reindex(cols).fillna(0))
    # renormalize to dollar-neutral, gross 2.0
    w = w - w.mean()
    gross = w.abs().sum()
    if gross > 0:
        w = w / gross * 2.0
    return idx, w[w != 0].round(5)


if __name__ == "__main__":
    d, w = target_book()
    print(f"target book asof {d.date()}: {len(w)} names, gross {w.abs().sum():.2f}, net {w.sum():+.3f}")
    print("top longs:", list(w.sort_values(ascending=False).head(6).index))
    print("top shorts:", list(w.sort_values().head(6).index))
