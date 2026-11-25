"""RVOL-surge x multi-timeframe squeeze screener — MANUAL DECISION SUPPORT.

Combines today's RVOL top-20 (early volume vs own 14-day normal, from
rvol_shadow) with TTM squeeze state on 1d/2d/3d/4d/weekly bars resampled
from the grouped daily store. Same honesty stamp as squeeze_screener.py:
no systematic edge claim — this is a watchlist builder for human judgment.

Usage: .venv/bin/python scripts/rvol_squeeze_screener.py [--date YYYY-MM-DD]
       [--min-rvol 3.0]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
GROUPED = ROOT / "data" / "research_orb" / "grouped"
STAMP = "UNVALIDATED — MANUAL DECISION SUPPORT ONLY"
TFS = {"1d": 1, "2d": 2, "3d": 3, "4d": 4, "W": 5}


def load_history(tickers: set[str], n_files: int = 220) -> pd.DataFrame:
    frames = []
    for f in sorted(GROUPED.glob("*.parquet"))[-n_files:]:
        d = pd.read_parquet(f)
        d = d[d["ticker"].isin(tickers)]
        d["date"] = f.stem
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["ticker", "date"])


def squeeze_state(h, l, c) -> tuple[bool, int]:
    """(mid-tier squeeze ON at last bar, run length). Needs >= 25 bars."""
    n = len(c)
    if n < 25:
        return False, 0
    s = pd.Series(c)
    sma20 = s.rolling(20).mean()
    sd20 = s.rolling(20).std()
    bb_up = (sma20 + 2 * sd20).to_numpy()
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_e = pd.Series(tr).ewm(span=20, adjust=False).mean().to_numpy()
    d_mid = bb_up - (sma20.to_numpy() + 1.5 * atr_e)
    on = d_mid <= 0
    if not on[-1]:
        return False, 0
    run = 0
    for i in range(n - 1, -1, -1):
        if on[i]:
            run += 1
        else:
            break
    return True, run


def resample(g: pd.DataFrame, k: int, weekly: bool) -> tuple:
    if weekly:
        w = g.set_index("date").resample("W-FRI").agg(
            {"high": "max", "low": "min", "close": "last"}).dropna()
        return w["high"].to_numpy(), w["low"].to_numpy(), w["close"].to_numpy()
    n = len(g) // k * k
    gg = g.iloc[len(g) - n:]
    hh = gg["high"].to_numpy().reshape(-1, k).max(axis=1)
    ll = gg["low"].to_numpy().reshape(-1, k).min(axis=1)
    cc = gg["close"].to_numpy().reshape(-1, k)[:, -1]
    return hh, ll, cc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--min-rvol", type=float, default=3.0)
    args = ap.parse_args()
    day = args.date or sorted(GROUPED.glob("*.parquet"))[-1].stem
    shadow = json.loads((ROOT / "logs" / "observation" /
                         f"rvol_shadow-{day}.json").read_text())
    picks = [x for x in shadow["top20"] if x["rvol"] >= args.min_rvol]
    tickers = {x["ticker"] for x in picks}
    hist = load_history(tickers)
    print(f"\n=== RVOL-SURGE x SQUEEZE SCREENER {day} ===\n{STAMP}\n")
    rows = []
    for x in picks:
        t = x["ticker"]
        g = hist[hist["ticker"] == t]
        if len(g) < 30:
            continue
        states = {}
        for tf, k in TFS.items():
            h, l, c = resample(g, k, weekly=(tf == "W"))
            on, run = squeeze_state(h, l, c)
            states[tf] = f"ON({run})" if on else "-"
        n_on = sum(1 for v in states.values() if v != "-")
        c_last = float(g["close"].iloc[-1])
        rows.append({"ticker": t, "rvol": x["rvol"], "close": round(c_last, 2),
                     **states, "tfs_in_squeeze": n_on})
    out = pd.DataFrame(rows).sort_values(
        ["tfs_in_squeeze", "rvol"], ascending=[False, False])
    print(out.to_string(index=False))
    print(f"\n(ON(n) = mid-tier squeeze active for n bars on that timeframe; "
          f"more timeframes ON + high RVOL = compressed name having its loud day)")


if __name__ == "__main__":
    main()
