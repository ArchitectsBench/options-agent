"""Intraday data accumulation job (run weekly via launchd, idempotent).

Downloads 5m/30m (60d), 1h (730d), and 1d (2y) bars for the full default
watchlist + ^VIX and MERGES them into the existing parquet store:
(1h/1d added 2026-06-11 — previously only the original 5 tickers had
1h/1d history and nothing refreshed it; Track A's primary backtest
timeframe was rotting.)
  - dedup on timestamp (new data wins for overlapping bars)
  - safe to run any day: fills whatever the 60-day window covers, never
    duplicates, never truncates existing history
Run weekly and the store grows past yfinance's 60-day limit indefinitely.

Appends a summary line per file to logs/data_snapshots.log.
"""

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "data" / "historical"
LOG_FILE = ROOT / "logs" / "data_snapshots.log"

# single source of truth: config watchlist + VIX
import yaml
_cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
WATCHLIST = _cfg["watchlist"]["default"] + ["^VIX"]
INTERVALS = [("5m", "60d"), ("30m", "60d"), ("1h", "730d"), ("1d", "2y")]


def merge_snapshot(ticker: str, interval: str, period: str) -> str:
    # daily files use the bare {TICKER}.parquet name (Track A convention)
    suffix = "" if interval == "1d" else f"_{interval}"
    fname = f"{ticker.lstrip('^')}{suffix}.parquet"
    path = OUTPUT_DIR / fname
    try:
        new = yf.download(ticker, period=period, interval=interval,
                          auto_adjust=False, progress=False)
    except Exception as exc:
        return f"{fname}: DOWNLOAD FAILED ({exc}) — existing data untouched"
    if new.empty:
        return f"{fname}: no data returned — existing data untouched"
    if hasattr(new.columns, "nlevels") and new.columns.nlevels > 1:
        new.columns = new.columns.get_level_values(0)

    if path.exists():
        old = pd.read_parquet(path)
        before = len(old)
        combined = pd.concat([old, new])
        # keep='last' → freshest download wins for overlapping timestamps
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        before = 0
        combined = new[~new.index.duplicated(keep="last")].sort_index()

    combined.to_parquet(path)
    added = len(combined) - before
    return (f"{fname}: +{added} bars (now {len(combined)}, "
            f"{combined.index.min()} to {combined.index.max()})")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"=== snapshot {stamp} ==="]
    failures = 0
    for ticker in WATCHLIST:
        for interval, period in INTERVALS:
            line = merge_snapshot(ticker, interval, period)
            failures += ("FAILED" in line) or ("no data" in line)
            lines.append(line)
            print(line)
    lines.append(f"=== done: {failures} failures ===")
    with open(LOG_FILE, "a") as fh:
        fh.write("\n".join(lines) + "\n")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
