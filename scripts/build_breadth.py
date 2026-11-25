#!/usr/bin/env python3
"""Build the daily market-breadth panel from Polygon grouped-daily files.

Family F3 (analysis/discovery/IDEAS.md §P4). This is pure DATA CONSTRUCTION,
not mining — per the task brief it MAY read all available dates, including
2025+ (the Polygon holdout per analysis/discovery/windows.py). Nothing here
enforces the discovery/holdout split; that guard lives in the SCREEN script
(analysis/discovery/f3_breadth_screen.py), which must filter this dataset's
output down to the discovery window before touching it.

SOURCE: data/research_orb/grouped/*.parquet — one file per trading day
(name YYYY-MM-DD.parquet), the full US-stock daily OHLCV Polygon grouped-daily
bar set (~10-12k tickers/day). Non-trading days are marked with a same-named
`.empty` file (no `.parquet`) and are skipped — 105 such markers found across
the store.

OUTPUT: data/breadth/daily.parquet, one row per trading date, columns:
  date, n_names, pct_adv, pct_decl, upvol_share, pct_above_20dma,
  nh_20d_pct, nl_20d_pct, ad_line, breadth_thrust_10d
  (+ informational: n_classifiable, n_eligible_20d, n_adv, n_decl)

DEFINITIONS (all per-ticker rolling/prior stats are built from that ticker's
own FULL row history in the store, unfiltered by the day's activity test —
a name's price history should not gap just because one day it had volume==0;
the activity/eligibility filters below only gate which names CONTRIBUTE to a
given day's cross-sectional aggregate, not which rows feed the rolling calcs):

- active(day) = volume > 0 AND close >= 1.0. n_names = count of active names
  that day. This is the base population for pct_adv/pct_decl/upvol_share.
- classifiable(day) = active(day) AND a prior-close exists for that ticker
  (i.e. not its first appearance in the store). pct_adv/pct_decl are computed
  over this population (advancers+decliners+unchanged == classifiable; ties
  are counted in neither pct_adv nor pct_decl, so they need not sum to 1).
- upvol_share = sum(volume where advancing) / sum(volume) over active(day).
- 20-day stats (pct_above_20dma, nh_20d_pct, nl_20d_pct): the reference
  (20-session MA / 20-session high / 20-session low) is built from the PRIOR
  20 sessions of that ticker's history, EXCLUDING today, then today's close
  is compared against it. This means a name needs 21 days of history
  (20 prior + today) before it can contribute — the eligibility mask used
  for all three of these stats. Denominator = active(day) AND eligible_20d.
  KNOWN APPROXIMATION: the "prior 20 sessions" are the ticker's prior 20
  ROWS in the store, not 20 prior CALENDAR trading days — if a name is
  briefly absent from a grouped file (halt / thin listing gap), its window
  silently bridges the gap. This affects a small minority of names; large
  liquid names trade every session.
- ad_line = cumulative sum of (n_adv - n_decl) counts across the full output
  range (informational; standard advance/decline line).
- breadth_thrust_10d = 10-session trailing mean of pct_adv (Zweig-style
  breadth thrust input; NOT the same as the classic Zweig thrust ratio, just
  the input the screen script uses).

Names appear/disappear (new listings, delistings) — handled by tracking each
ticker's OWN row sequence (cumcount) rather than assuming a fixed universe.

Run: .venv/bin/python scripts/build_breadth.py
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
GROUPED = ROOT / "data" / "research_orb" / "grouped"
OUT_DIR = ROOT / "data" / "breadth"
OUT_PATH = OUT_DIR / "daily.parquet"

MIN_CLOSE = 1.0
MA_WINDOW = 20
THRUST_WINDOW = 10
MIN_HISTORY_DAYS = MA_WINDOW + 1   # 20 prior + today


def load_panel() -> pd.DataFrame:
    files = sorted(GROUPED.glob("*.parquet"))
    empties = sorted(GROUPED.glob("*.empty"))
    print(f"grouped store: {len(files)} trading-day parquet files, "
          f"{len(empties)} holiday/missing markers skipped", flush=True)
    frames = []
    for i, f in enumerate(files, 1):
        df = pd.read_parquet(f, columns=["ticker", "close", "volume"])
        df["date"] = f.stem
        frames.append(df)
        if i % 200 == 0:
            print(f"  read {i}/{len(files)} ({f.stem})", flush=True)
    m = pd.concat(frames, ignore_index=True)
    before = len(m)
    m = m.drop_duplicates(subset=["ticker", "date"], keep="last")
    if len(m) != before:
        print(f"  dropped {before - len(m)} duplicate ticker/date rows", flush=True)
    print(f"panel assembled: {len(m):,} ticker-days, {m['ticker'].nunique():,} "
          f"distinct tickers, {m['date'].nunique():,} distinct dates "
          f"({m['date'].min()}..{m['date'].max()})", flush=True)
    return m


def build_ticker_history(m: pd.DataFrame) -> pd.DataFrame:
    m = m.sort_values(["ticker", "date"]).reset_index(drop=True)
    m["active"] = (m["volume"] > 0) & (m["close"] >= MIN_CLOSE)

    g = m.groupby("ticker", sort=False)
    m["prior_close"] = g["close"].shift(1)

    # trailing-20 reference stats, EXCLUDING today (today = the 21st obs)
    m["ma20_prior"] = g["close"].transform(
        lambda s: s.shift(1).rolling(MA_WINDOW, min_periods=MA_WINDOW).mean())
    m["hi20_prior"] = g["close"].transform(
        lambda s: s.shift(1).rolling(MA_WINDOW, min_periods=MA_WINDOW).max())
    m["lo20_prior"] = g["close"].transform(
        lambda s: s.shift(1).rolling(MA_WINDOW, min_periods=MA_WINDOW).min())

    m["eligible_20d"] = m["ma20_prior"].notna()
    m["above_20dma"] = m["eligible_20d"] & (m["close"] > m["ma20_prior"])
    m["nh_20d"] = m["eligible_20d"] & (m["close"] > m["hi20_prior"])
    m["nl_20d"] = m["eligible_20d"] & (m["close"] < m["lo20_prior"])

    m["classifiable"] = m["active"] & m["prior_close"].notna()
    m["is_adv"] = m["classifiable"] & (m["close"] > m["prior_close"])
    m["is_decl"] = m["classifiable"] & (m["close"] < m["prior_close"])
    return m


def aggregate_daily(m: pd.DataFrame, skips: dict) -> pd.DataFrame:
    rows = []
    dates = sorted(m["date"].unique())
    print(f"aggregating {len(dates)} dates cross-sectionally", flush=True)
    for i, d in enumerate(dates, 1):
        day = m[m["date"] == d]
        active = day[day["active"]]
        classifiable = day[day["classifiable"]]
        elig = day[day["eligible_20d"] & day["active"]]

        n_names = len(active)
        n_class = len(classifiable)
        n_elig = len(elig)
        n_adv = int(classifiable["is_adv"].sum())
        n_decl = int(classifiable["is_decl"].sum())

        total_vol = float(active["volume"].sum())
        up_vol = float(active.loc[active["is_adv"], "volume"].sum())

        rows.append(dict(
            date=d,
            n_names=n_names,
            pct_adv=(n_adv / n_class) if n_class else np.nan,
            pct_decl=(n_decl / n_class) if n_class else np.nan,
            upvol_share=(up_vol / total_vol) if total_vol > 0 else np.nan,
            pct_above_20dma=(int(elig["above_20dma"].sum()) / n_elig) if n_elig else np.nan,
            nh_20d_pct=(int(elig["nh_20d"].sum()) / n_elig) if n_elig else np.nan,
            nl_20d_pct=(int(elig["nl_20d"].sum()) / n_elig) if n_elig else np.nan,
            n_classifiable=n_class,
            n_eligible_20d=n_elig,
            n_adv=n_adv,
            n_decl=n_decl,
        ))
        if i % 200 == 0:
            print(f"  day {i}/{len(dates)} ({d})", flush=True)

    daily = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    daily["ad_line"] = (daily["n_adv"] - daily["n_decl"]).cumsum()
    daily["breadth_thrust_10d"] = daily["pct_adv"].rolling(
        THRUST_WINDOW, min_periods=THRUST_WINDOW).mean()

    skips["dates_zero_classifiable"] = int((daily["n_classifiable"] == 0).sum())
    skips["dates_zero_eligible_20d"] = int((daily["n_eligible_20d"] == 0).sum())
    return daily


def report_contributing_names(m: pd.DataFrame) -> None:
    m = m.copy()
    m["year"] = m["date"].str[:4]
    m["eligible_20d_and_active"] = m["eligible_20d"] & m["active"]
    per_year = m.groupby("year").agg(
        avg_active=("active", "sum"),
        avg_eligible_20d=("eligible_20d_and_active", "sum"),
        n_days=("date", "nunique"),
    )
    # convert sums to per-day averages
    per_year["avg_active_per_day"] = per_year["avg_active"] / per_year["n_days"]
    per_year["avg_eligible_20d_per_day"] = per_year["avg_eligible_20d"] / per_year["n_days"]
    print("\nper-year average contributing-name counts:")
    print("  year   n_days  avg_active_names  avg_eligible_20d_names")
    for y, r in per_year.iterrows():
        print(f"  {y}   {int(r['n_days']):>4}    {r['avg_active_per_day']:>10.0f}"
              f"       {r['avg_eligible_20d_per_day']:>10.0f}")


def main() -> int:
    t0 = time.time()
    m = load_panel()
    m = build_ticker_history(m)
    print(f"per-ticker history built in {time.time() - t0:.0f}s", flush=True)

    skips: dict = {}
    daily = aggregate_daily(m, skips)
    print(f"aggregation done in {time.time() - t0:.0f}s", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(OUT_PATH, index=False)
    print(f"\nwrote {OUT_PATH} ({len(daily)} rows, "
          f"{daily['date'].min()}..{daily['date'].max()})", flush=True)
    print(f"coverage skips: {skips}", flush=True)

    report_contributing_names(m)

    print(f"\nsample rows:\n{daily.head(3)}\n...\n{daily.tail(3)}", flush=True)
    print(f"\ntotal runtime: {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
