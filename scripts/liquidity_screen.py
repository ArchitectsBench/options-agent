"""Empirical option-liquidity screen for watchlist selection.

For each candidate ticker, pulls the live 2-7 DTE chain and measures, on
near-ATM contracts (strikes within ±5% of spot): median bid-ask spread,
share passing the system's own filters (spread <= $0.20, OI >= 500), and
spread as % of mid. Run during market hours for honest spreads.

Usage: python scripts/liquidity_screen.py TICKER [TICKER ...]
"""

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.feeds import fetch_quotes
from src.data.options_chain import MAX_SPREAD, MIN_OI, get_chain, select_expiration, spread_ok


def screen(ticker: str, spot: float) -> dict | None:
    exp = select_expiration(ticker, "2-7dte") or select_expiration(ticker, "7-21dte")
    if exp is None:
        return None
    chain = get_chain(ticker, exp)
    if not chain:
        return None
    near = [c for c in chain
            if abs(c["strike"] - spot) / spot <= 0.05 and c["bid"] > 0]
    if len(near) < 6:
        return None
    spreads = [c["spread"] for c in near]
    pct_pass = sum(1 for c in near if spread_ok(c["spread"], c["mid"]) and c["oi"] >= MIN_OI) / len(near)
    rel = [c["spread"] / c["mid"] for c in near if c["mid"] > 0.05]
    return {
        "ticker": ticker, "spot": spot, "expiration": exp,
        "contracts": len(near),
        "median_spread": round(statistics.median(spreads), 3),
        "pct_passing_filters": round(pct_pass, 2),
        "median_spread_pct_of_mid": round(statistics.median(rel), 3) if rel else None,
    }


def main(tickers: list[str]):
    quotes = fetch_quotes(tickers) or {}
    rows = []
    for t in tickers:
        spot = (quotes.get(t) or {}).get("last")
        if not spot:
            print(f"{t:<6} no quote")
            continue
        r = screen(t, float(spot))
        if r is None:
            print(f"{t:<6} no usable chain")
            continue
        rows.append(r)
    rows.sort(key=lambda r: (-r["pct_passing_filters"], r["median_spread"]))
    print(f"\n{'ticker':<7}{'spot':>9}{'medSpread':>11}{'%passing':>10}{'spread/mid':>12}{'nATM':>6}")
    for r in rows:
        print(f"{r['ticker']:<7}{r['spot']:>9.2f}{r['median_spread']:>11.3f}"
              f"{r['pct_passing_filters']:>10.0%}{r['median_spread_pct_of_mid'] or 0:>12.1%}{r['contracts']:>6}")
    return rows


if __name__ == "__main__":
    main(sys.argv[1:] if len(sys.argv) > 1 else [])
