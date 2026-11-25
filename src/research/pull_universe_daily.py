"""Pull daily bars for the full ~484-name liquid universe (Polygon) and build a
close-price panel for proper cross-sectional strategies. One call per ticker.

  python src/research/pull_universe_daily.py
Output: data/universe_daily.parquet  (dates x tickers, adjusted close)
"""
import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.data.polygon_data import get_client

# pure index symbols have no stock aggregates on Polygon — drop them
INDICES = {"SPX", "VIX", "NDX", "RUT", "XSP", "OEX", "DJX", "VVIX", "XEO", "NDXP", "SPXW"}
START = "2020-01-01"


def main():
    uni = pd.read_csv(ROOT / "data" / "vrp_universe.csv")["ticker"].tolist()
    tickers = [t for t in uni if t not in INDICES]
    client = get_client()
    if not client.available():
        sys.exit("POLYGON_API_KEY not set")
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    print(f"pulling {len(tickers)} names daily {START}..{end} ...", flush=True)
    closes, fails = {}, []
    for i, t in enumerate(tickers):
        try:
            df = client.fetch_bars(t, "1d", START, end)
        except Exception:
            df = None
        if df is None or df.empty:
            fails.append(t)
        else:
            closes[t] = df["Close"]
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(tickers)}  ({len(closes)} ok, {len(fails)} fail)", flush=True)
    panel = pd.DataFrame(closes).sort_index()
    out = ROOT / "data" / "universe_daily.parquet"
    panel.to_parquet(out)
    print(f"\nDONE: {panel.shape[1]} names, {len(panel)} sessions, "
          f"{panel.index.min().date()}..{panel.index.max().date()} -> {out}")
    if fails:
        print(f"failed ({len(fails)}): {fails[:20]}{'...' if len(fails)>20 else ''}")


if __name__ == "__main__":
    main()
