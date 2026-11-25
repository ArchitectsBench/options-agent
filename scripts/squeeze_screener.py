"""Daily TTM-squeeze setup screener — MANUAL DECISION SUPPORT ONLY.

Scans the Polygon grouped daily store (current through last close) for
squeeze setups using the intake-#5 verified primitives:
  - tier states: BB(20,2 SMA) upper vs Keltner upper (SMA20 + k*EMA(TR,20)),
    k = 1.0 HIGH / 1.5 MID / 2.0 LOW (HIGH = tightest compression)
  - days in mid-tier squeeze; EARLY-IN flag (delta_mid rising, still <= 0)
  - FIRED today (mid-tier released after >= 6 days on) + momentum direction
  - context: momentum histogram slope, EMA 8/13/21 stack, ADX(14), RVOL,
    dollar volume

HONESTY STAMP: our 336-cell registered test (intake #5) found NO systematic
daily-bar edge in these setups. This screener exists so a human can apply
discretion the backtest cannot. Output is NOT a recommendation.

Usage:
  .venv/bin/python scripts/squeeze_screener.py                # liquid universe
  .venv/bin/python scripts/squeeze_screener.py --min-dv 25e6  # tighter
  .venv/bin/python scripts/squeeze_screener.py --tickers AAPL NVDA TSLA
Output: console tables + logs/screener/squeeze-YYYY-MM-DD.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
GROUPED = ROOT / "data" / "research_orb" / "grouped"
LOOKBACK_FILES = 150
STAMP = "UNVALIDATED — MANUAL DECISION SUPPORT ONLY (intake #5: no systematic daily edge)"


def load_panel(min_dv: float, tickers: list[str] | None) -> pd.DataFrame:
    files = sorted(GROUPED.glob("*.parquet"))[-LOOKBACK_FILES:]
    frames = []
    for f in files:
        d = pd.read_parquet(f)
        d = d.rename(columns={"open": "o", "high": "h", "low": "l",
                              "close": "c", "volume": "v"})
        d["date"] = f.stem
        frames.append(d[["ticker", "date", "o", "h", "l", "c", "v"]])
    df = pd.concat(frames, ignore_index=True)
    if tickers:
        df = df[df["ticker"].isin({t.upper() for t in tickers})]
    else:
        last = df[df["date"] == df["date"].max()]
        keep = set(last.loc[(last["c"] * last["v"] >= min_dv)
                            & (last["c"] >= 5), "ticker"])
        df = df[df["ticker"].isin(keep)]
    return df.sort_values(["ticker", "date"])


def screen_ticker(g: pd.DataFrame) -> dict | None:
    n = len(g)
    if n < 60:
        return None
    h, l, c, v = (g[k].to_numpy(float) for k in ("h", "l", "c", "v"))
    s = pd.Series(c)
    sma20 = s.rolling(20).mean()
    sd20 = s.rolling(20).std()
    bb_up = (sma20 + 2 * sd20).to_numpy()
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_e = pd.Series(tr).ewm(span=20, adjust=False).mean().to_numpy()
    sma20a = sma20.to_numpy()
    d_high = bb_up - (sma20a + 1.0 * atr_e)
    d_mid = bb_up - (sma20a + 1.5 * atr_e)
    d_low = bb_up - (sma20a + 2.0 * atr_e)
    hh20 = pd.Series(h).rolling(20).max()
    ll20 = pd.Series(l).rolling(20).min()
    ema20 = s.ewm(span=20, adjust=False).mean()
    dev = s - ((hh20 + ll20) / 2 + ema20) / 2
    x = np.arange(20); xc = x - x.mean(); denom = float((xc ** 2).sum())
    mom = (dev.rolling(20).mean() + dev.rolling(20).apply(
        lambda w: float((xc * (w - w.mean())).sum()) / denom, raw=True) * xc[-1]
    ).to_numpy()
    on_mid = d_mid <= 0
    run = pd.Series(on_mid.astype(float)).groupby(
        (~pd.Series(on_mid)).cumsum()).cumsum().to_numpy()
    d_mid_ema7 = pd.Series(d_mid).ewm(span=7, adjust=False).mean().to_numpy()
    ema8 = s.ewm(span=8, adjust=False).mean().to_numpy()
    ema13 = s.ewm(span=13, adjust=False).mean().to_numpy()
    ema21 = s.ewm(span=21, adjust=False).mean().to_numpy()
    # ADX(14)
    up = np.diff(h, prepend=h[0]); dn = -np.diff(l, prepend=l[0])
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    wa = lambda arr: pd.Series(arr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    atr14v = wa(tr)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * wa(pdm) / atr14v
        mdi = 100 * wa(mdm) / atr14v
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    adx = wa(np.nan_to_num(dx))
    dv20 = pd.Series(c * v).rolling(20).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        rvol = (c * v) / dv20
    i = n - 1
    tier = ("HIGH" if d_high[i] <= 0 else "MID" if d_mid[i] <= 0
            else "LOW" if d_low[i] <= 0 else None)
    prev_on = on_mid[i - 1]
    fired = (not on_mid[i]) and prev_on and run[i - 1] >= 6
    early = (d_mid[i] > d_mid_ema7[i]) and (d_mid[i] <= 0)
    if tier is None and not fired:
        return None
    mom_dir = "LONG" if mom[i] > 0 else "SHORT"
    mom_slope = "rising" if mom[i] > mom[i - 1] else "falling"
    stack = ("bull" if ema8[i] > ema13[i] > ema21[i]
             else "bear" if ema8[i] < ema13[i] < ema21[i] else "mixed")
    return {
        "ticker": g["ticker"].iloc[0], "close": round(c[i], 2),
        "state": "FIRED-" + mom_dir if fired else f"SQUEEZE-{tier}",
        "days_in_sqz": int(run[i]) if on_mid[i] else int(run[i - 1]),
        "early_in": bool(early), "mom": round(float(mom[i]), 3),
        "mom_slope": mom_slope, "ema_stack": stack,
        "adx": round(float(adx[i]), 1), "rvol": round(float(rvol[i]), 2),
        "dollar_vol_m": round(float(c[i] * v[i] / 1e6), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-dv", type=float, default=10e6)
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()
    df = load_panel(args.min_dv, args.tickers)
    day = df["date"].max()
    rows = [r for _, g in df.groupby("ticker") if (r := screen_ticker(g))]
    out = pd.DataFrame(rows)
    print(f"\n=== SQUEEZE SCREENER {day} ===\n{STAMP}\n")
    if out.empty:
        print("no setups found")
        return
    fired = out[out["state"].str.startswith("FIRED")].sort_values(
        "rvol", ascending=False)
    insq = out[~out["state"].str.startswith("FIRED")]
    # rank in-squeeze: tightest tier first, then longest compression, early-in
    tier_rank = insq["state"].map({"SQUEEZE-HIGH": 0, "SQUEEZE-MID": 1,
                                   "SQUEEZE-LOW": 2})
    insq = insq.assign(_t=tier_rank).sort_values(
        ["_t", "early_in", "days_in_sqz"], ascending=[True, False, False]
    ).drop(columns="_t")
    if not fired.empty:
        print(f"--- FIRED TODAY ({len(fired)}) ---")
        print(fired.head(args.top).to_string(index=False))
    print(f"\n--- IN SQUEEZE ({len(insq)}; tightest first) ---")
    print(insq.head(args.top).to_string(index=False))
    outdir = ROOT / "logs" / "screener"
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / f"squeeze-{day}.csv"
    out.to_csv(dest, index=False)
    print(f"\nfull list ({len(out)} names) -> {dest}")


if __name__ == "__main__":
    main()
