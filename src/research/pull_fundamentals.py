"""Pull Polygon quarterly financials for the universe -> point-in-time long table.
Every row tagged with filing_date (when it became public) so backtests can lag
correctly (no lookahead).

  python src/research/pull_fundamentals.py
Output: data/fundamentals_raw.parquet  (ticker, filing_date, eps, ni, rev, gp, equity)
"""
import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.data.polygon_data import get_client, BASE

INDICES = {"SPX", "VIX", "NDX", "RUT", "XSP", "OEX", "DJX", "VVIX", "XEO", "NDXP", "SPXW"}


def _v(stmt, key):
    try:
        return stmt[key]["value"]
    except (KeyError, TypeError):
        return None


def main():
    uni = pd.read_csv(ROOT / "data" / "vrp_universe.csv")["ticker"].tolist()
    tickers = [t for t in uni if t not in INDICES]
    c = get_client()
    if not c.available():
        sys.exit("POLYGON_API_KEY not set")
    print(f"pulling quarterly financials for {len(tickers)} names...", flush=True)
    rows, fails = [], []
    for i, t in enumerate(tickers):
        out = c._get(f"{BASE}/vX/reference/financials?ticker={t}&timeframe=quarterly"
                     f"&limit=28&sort=filing_date&order=desc")
        if out is None or "error" in (out or {}) or not out.get("results"):
            fails.append(t)
        else:
            for r in out["results"]:
                fin = r.get("financials", {})
                inc, bs = fin.get("income_statement", {}), fin.get("balance_sheet", {})
                rows.append({
                    "ticker": t, "filing_date": r.get("filing_date"),
                    "period": f"{r.get('fiscal_year')}{r.get('fiscal_period')}",
                    "eps": _v(inc, "basic_earnings_per_share"),
                    "ni": _v(inc, "net_income_loss"),
                    "rev": _v(inc, "revenues"),
                    "gp": _v(inc, "gross_profit"),
                    "equity": _v(bs, "equity"),
                })
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(tickers)}  ({len(rows)} rows, {len(fails)} fail)", flush=True)
    df = pd.DataFrame(rows).dropna(subset=["filing_date"])
    df["filing_date"] = pd.to_datetime(df["filing_date"])
    df = df.drop_duplicates(["ticker", "period"]).sort_values(["ticker", "filing_date"])
    out = ROOT / "data" / "fundamentals_raw.parquet"
    df.to_parquet(out)
    print(f"\nDONE: {df['ticker'].nunique()} names, {len(df)} quarterly rows -> {out}")
    print(f"failed ({len(fails)}): {fails[:15]}{'...' if len(fails)>15 else ''}")


if __name__ == "__main__":
    main()
