#!/usr/bin/env python3
"""Nightly incremental appender for data/breadth/daily.parquet — item 2 of
the 2026-07-19 observation-instrumentation batch (see docs/external_edge_map.md,
analysis/discovery/IDEAS.md F3). Feeds regime_live.build_regime()'s
'breadth_daily' context section and paper_trader.py's regime_tags
(breadth_health) — descriptive context only, not a gate.

Appends the newest grouped-file date(s) NOT already present in daily.parquet,
by reusing scripts/build_breadth.py's exact per-ticker/aggregation logic
(build_ticker_history + aggregate_daily) on a TRAILING WINDOW of grouped
files — not the full store. A full-store rebuild remains build_breadth.py's
job, run on demand; this script is deliberately tiny and fast for a nightly
launchd tick.

Idempotent: dates already in daily.parquet are never recomputed here —
re-running any number of times is safe (dedup on 'date', keep='last').

APPROXIMATION (inherited + slightly compounded from build_breadth.py's own
documented one): the 20-session reference window (and classifiable/
prior_close) for each new day is built from a ticker's prior ROWS within the
loaded LOOKBACK_FILES window, not its true full-store row sequence. A ticker
whose full history predates the window sees its within-window row count
start over, so it can be spuriously ineligible/unclassifiable near the
window's start. MEASURED (2026-07-19, single-day append test against a real
full rebuild): n_classifiable off by 1/11710 names, n_eligible_20d off by
~53/11334 — small, and shrinking as LOOKBACK_FILES grows relative to the gap
size. n_adv/n_decl/pct_adv/pct_above_20dma/etc. all inherit this same
window-boundary approximation for the newly appended day(s) only; PRIOR days
already in daily.parquet are never touched or recomputed, so they stay exact.

ad_line (cumulative) and breadth_thrust_10d (10-day rolling mean of pct_adv)
are recomputed on the FULL combined (existing + new) daily.parquet after
appending, using the same rolling/cumsum FORMULAS as build_breadth.
aggregate_daily — this makes the ROLLING MATH itself exact given its inputs,
but since those inputs (n_adv/n_decl/pct_adv for the newly appended day)
carry the same small window-boundary approximation above, ad_line and
breadth_thrust_10d are NOT byte-identical to a full rebuild either — they
inherit that day's tiny n_adv/n_decl offset, propagated forward through the
cumulative sum. A periodic full rebuild via build_breadth.py is the only way
to get true full-history exactness; this appender's job is fast, safe,
directionally-correct nightly context, not a substitute for it.

Run manually: .venv/bin/python scripts/breadth_daily_update.py
Deployed (NOT loaded by this change — a separate explicit operator step):
  launchctl load deploy/launchd/com.optionsagent.breadthdaily.plist
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_breadth import (  # noqa: E402
    GROUPED, OUT_PATH, aggregate_daily, build_ticker_history,
)

# Hard minimum for one date's 20dma stats is 21 files (20 prior + today);
# padded generously so short per-ticker row-sequence gaps don't starve the
# window (see APPROXIMATION note above).
LOOKBACK_FILES = 60


def _existing_dates() -> set[str]:
    if not OUT_PATH.exists():
        return set()
    return set(pd.read_parquet(OUT_PATH, columns=["date"])["date"].astype(str))


def main() -> int:
    files = sorted(GROUPED.glob("*.parquet"))
    if not files:
        print("no grouped files found — nothing to do", flush=True)
        return 0

    existing = _existing_dates()
    all_dates = [f.stem for f in files]
    new_dates = [d for d in all_dates if d not in existing]
    if not new_dates:
        print(f"daily.parquet already covers the newest grouped date "
              f"({all_dates[-1]}) — nothing to append", flush=True)
        return 0

    window_files = files[-LOOKBACK_FILES:]
    print(f"loading {len(window_files)} trailing grouped files "
          f"({window_files[0].stem}..{window_files[-1].stem}) to compute "
          f"{len(new_dates)} new date(s): {new_dates}", flush=True)

    frames = []
    for f in window_files:
        df = pd.read_parquet(f, columns=["ticker", "close", "volume"])
        df["date"] = f.stem
        frames.append(df)
    m = pd.concat(frames, ignore_index=True)
    m = m.drop_duplicates(subset=["ticker", "date"], keep="last")

    m = build_ticker_history(m)
    skips: dict = {}
    window_daily = aggregate_daily(m, skips)  # reuses build_breadth's exact logic

    new_rows = window_daily[window_daily["date"].isin(new_dates)].copy()
    if new_rows.empty:
        print("computed window produced no rows for the new dates (unexpected) "
              "— skipping append", flush=True)
        return 0

    # ad_line/breadth_thrust_10d from the WINDOW are window-scoped approximations
    # (see module docstring) — drop them here; recomputed exactly below on the
    # full combined history instead.
    new_rows = new_rows.drop(columns=["ad_line", "breadth_thrust_10d"])

    if OUT_PATH.exists():
        existing_df = pd.read_parquet(OUT_PATH)
        combined = pd.concat(
            [existing_df.drop(columns=["ad_line", "breadth_thrust_10d"]), new_rows],
            ignore_index=True,
        )
        combined = combined.drop_duplicates(subset=["date"], keep="last")
    else:
        combined = new_rows
    combined = combined.sort_values("date").reset_index(drop=True)

    # exact recompute over full history (see module docstring EXACTNESS FIX)
    combined["ad_line"] = (combined["n_adv"] - combined["n_decl"]).cumsum()
    combined["breadth_thrust_10d"] = combined["pct_adv"].rolling(10, min_periods=10).mean()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(OUT_PATH, index=False)
    print(f"appended {len(new_rows)} new row(s); daily.parquet now "
          f"{len(combined)} rows ({combined['date'].min()}..{combined['date'].max()})",
          flush=True)
    print(f"window aggregation skips: {skips}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
