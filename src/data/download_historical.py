"""Download and cache historical OHLCV bars as parquet.

Intervals (yfinance limits):
  1d:  2 years           -> {TICKER}.parquet
  1h:  730 days max      -> {TICKER}_1h.parquet   (primary intraday backtest data)
  30m: 60 days max       -> {TICKER}_30m.parquet  (signal timing validation)
  5m:  60 days max       -> {TICKER}_5m.parquet   (signal timing validation)
"""

from pathlib import Path

import yfinance as yf

TICKERS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data" / "historical"

# (interval, period, filename suffix)
INTERVALS = [
    ("1d", "2y", ""),
    ("1h", "730d", "_1h"),
    ("30m", "60d", "_30m"),
    ("5m", "60d", "_5m"),
]


def download(ticker: str, interval: str, period: str, suffix: str) -> str:
    df = yf.download(ticker, period=period, interval=interval, auto_adjust=False, progress=False)
    if df.empty:
        return f"{ticker} {interval}: no data returned"
    # yfinance returns MultiIndex columns for single tickers in newer versions
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    path = OUTPUT_DIR / f"{ticker}{suffix}.parquet"
    df.to_parquet(path)
    start, end = df.index.min(), df.index.max()
    return f"{ticker} {interval}: {len(df)} bars, {start} to {end}"


def download_all():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for interval, period, suffix in INTERVALS:
        print(f"── {interval} bars (period={period}) ──")
        for ticker in TICKERS:
            print(f"  {download(ticker, interval, period, suffix)}")


if __name__ == "__main__":
    download_all()
