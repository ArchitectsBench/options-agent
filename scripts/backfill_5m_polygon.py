"""Backfill clean 5m history from Polygon for the squeeze experiment (#17).

Single-source (Polygon only), REGULAR-HOURS-ONLY (09:30-16:00 ET) — NOT
merged into the live data/historical/_5m store (which is mixed-source
Tradier/yfinance and would Frankenstein the bar conventions). Writes a
dedicated clean research store: data/research5m/{ticker}.parquet.

5 years available (2021+) — spans the 2022 bear market, giving the regime
diversity the daily squeeze experiment lacked (its main confound was a
2-year bull sample).

Usage: python scripts/backfill_5m_polygon.py [--years 5]
"""

import argparse
import sys
from datetime import date, time, timedelta
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.polygon_data import get_client

OUT = ROOT / "data" / "research5m"
RTH_START, RTH_END = time(9, 30), time(16, 0)


def regular_hours(df: pd.DataFrame) -> pd.DataFrame:
    t = df.index.time
    return df[(t >= RTH_START) & (t < RTH_END)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    args = ap.parse_args()

    client = get_client()
    if not client.available():
        print("POLYGON_API_KEY not set")
        return

    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    tickers = sorted(set(cfg["watchlist"]["default"]) | {"SPY"})  # SPY for relative strength
    OUT.mkdir(parents=True, exist_ok=True)
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=int(args.years * 365))).isoformat()
    print(f"backfilling {len(tickers)} tickers, 5m regular-hours, {start}..{end}\n")

    for t in tickers:
        df = client.fetch_bars(t, "5m", start, end)
        if df is None or df.empty:
            print(f"  {t}: no data")
            continue
        rh = regular_hours(df).sort_index()
        rh.to_parquet(OUT / f"{t}.parquet")
        sessions = rh.index.normalize().nunique()
        print(f"  {t}: {len(rh)} bars, {sessions} sessions, "
              f"{rh.index.min().date()}..{rh.index.max().date()}")
    print(f"\ndone -> {OUT}")


if __name__ == "__main__":
    main()
