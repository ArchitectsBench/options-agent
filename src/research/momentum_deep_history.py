"""Momentum book across MORE bears — the last technical swing at the project's best lead.

RESMOM (residual momentum) is the closest thing to an edge (full Sharpe ~0.81 combined)
but failed Stage-6 only on regime-robustness in the 2022 bear. This tests whether that
failure was bad luck or structural by running it across EVERY bear since 2008:
  2008 GFC | 2011 euro/downgrade | 2015-16 | 2018 Q4 | 2020 COVID | 2022.

Prices: yfinance, ONE consistent adjusted series 2007-2026 (avoids Polygon/yf mismatch).
CAVEAT (stated, not hidden): the universe is TODAY's ~477 names -> survivorship bias.
Names that IPO'd later are NaN pre-IPO (naturally excluded). For a market-NEUTRAL book
the bias is milder than long-only but real -> read as DIRECTIONAL evidence, not a clean
Stage-6 pass.

  python src/research/momentum_deep_history.py            # pull (cache) + analyze
  python src/research/momentum_deep_history.py --analyze  # use cached panel
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.research.factor_stack import fac_ret, vol_scale, stats
from src.research.monte_carlo import per_year_sharpe, REGIME_MIN_WORST_YEAR_SHARPE

PANEL = ROOT / "data" / "universe_daily.parquet"          # today's universe (tickers)
DEEP = ROOT / "data" / "universe_daily_deep.parquet"      # cached deep history
BEARS = [2008, 2011, 2015, 2018, 2020, 2022]


def pull():
    import yfinance as yf
    tickers = list(pd.read_parquet(PANEL).columns)
    print(f"pulling {len(tickers)} names, 2007-2026, via yfinance (adjusted)...", flush=True)
    raw = yf.download(tickers, start="2007-01-01", end="2026-06-27", interval="1d",
                      auto_adjust=True, progress=True, group_by="ticker", threads=True)
    close = {}
    for t in tickers:
        try:
            s = raw[t]["Close"].dropna()
            if len(s) > 252:
                close[t] = s
        except (KeyError, Exception):
            continue
    panel = pd.DataFrame(close).sort_index()
    panel.to_parquet(DEEP)
    print(f"cached {panel.shape[1]} names x {panel.shape[0]} sessions "
          f"({panel.index.min().date()}..{panel.index.max().date()}) -> {DEEP}", flush=True)
    return panel


def resmom_factor(close):
    """Market-neutral, vol-scaled residual-momentum factor return (same as factor_stack)."""
    ret = close.pct_change()
    mkt = ret.mean(axis=1)
    beta = ret.rolling(126).cov(mkt).div(mkt.rolling(126).var(), axis=0)
    resid = ret.sub(beta.mul(mkt, axis=0))
    return vol_scale(fac_ret(ret, resid.rolling(126).sum().shift(5), True))


def analyze():
    close = pd.read_parquet(DEEP).dropna(axis=1, thresh=252)
    # names available per era (transparency on the shrinking early universe)
    avail = close.notna().sum(axis=1)
    print(f"deep panel: {close.shape[1]} names, {close.index.min().date()}..{close.index.max().date()}")
    print(f"  names with data: 2008={int(avail[avail.index.year==2008].mean())}, "
          f"2015={int(avail[avail.index.year==2015].mean())}, "
          f"2022={int(avail[avail.index.year==2022].mean())}, "
          f"2025={int(avail[avail.index.year==2025].mean())}")

    rm = resmom_factor(close)
    m = stats(rm)
    print(f"\nRESMOM (deep history {close.index.min().year}-{close.index.max().year}):")
    print(f"  full Sharpe {m['sharpe']:.2f} | OOS {m['oos']:.2f} | maxDD {m['maxdd']:.1%} "
          f"| MC5 {m['mc5']:.2f} | worst-yr {m['wy']:.2f}")

    by = per_year_sharpe(rm)
    print(f"\n  per-year Sharpe (BEAR years are the acid test):")
    bear_neg = 0
    for y in sorted(by):
        is_bear = y in BEARS
        if is_bear and by[y] < 0:
            bear_neg += 1
        tag = "  <- BEAR" + ("  ✗ negative" if (is_bear and by[y] < 0) else ("  ✓" if is_bear else "")) if is_bear else ""
        print(f"    {y}: {by[y]:+.2f}{tag}")
    bears_present = [y for y in BEARS if y in by]
    print(f"\n  bears covered: {bears_present}")
    print(f"  bears where RESMOM was NEGATIVE: {bear_neg}/{len(bears_present)}")
    print(f"  worst-year Sharpe {min(by.values()):+.2f} (gate needs >= {REGIME_MIN_WORST_YEAR_SHARPE})")
    verdict = ("STRUCTURAL: RESMOM fails multiple bears -> regime-fragility is real, not bad luck"
               if bear_neg >= 2 else
               "ENCOURAGING: RESMOM survives most bears -> 2022 may have been an outlier")
    print(f"\n  VERDICT: {verdict}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze", action="store_true", help="use cached deep panel")
    a = ap.parse_args()
    if not a.analyze or not DEEP.exists():
        pull()
    analyze()


if __name__ == "__main__":
    main()
