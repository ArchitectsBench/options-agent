#!/usr/bin/env python3
"""Nightly prep for the RVOL-EXEC lane (spec §14). UNVALIDATED — PAPER ONLY.

Reads data/research_orb/grouped/*.parquet, bars5m_fresh/<date>/, the bars5m
store and the lane's own v930 cache; writes data/lane_rvol/prep/<D>.parquet
+ <D>.meta.json. The Polygon API is used ONLY with --allow-api (production);
tests inject a client and assert zero calls without the flag.

    scripts/rvol_exec_prep.py --for 2026-09-14 [--allow-api] [--force]

Idempotent: a fresh prep for the target day -> exit 0 without rebuilding
(unless --force).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.rvol_exec.paths as paths  # noqa: E402
from src.rvol_exec.constants import ET, STAMP  # noqa: E402
from src.rvol_exec.prep import build_prep, freshness, is_trading_day, read_meta  # noqa: E402


def next_trading_day(d: date) -> date:
    n = d + timedelta(days=1)
    while not is_trading_day(n):
        n += timedelta(days=1)
    return n


def main(argv=None, api=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--for", dest="for_day", default=None, help="target session (default: next trading day)")
    ap.add_argument("--allow-api", action="store_true", help="allow Polygon pulls for missing baselines")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--api-sleep", type=float, default=0.0)
    a = ap.parse_args(argv)
    target = a.for_day or next_trading_day(datetime.now(ET).date()).isoformat()
    meta = read_meta(paths.PREP_DIR, target)
    ok, why = freshness(meta, target)
    if ok and not a.force:
        print(f"[{STAMP}] prep for {target} already fresh ({why}); exit 0")
        return 0
    if a.allow_api and api is None:
        from src.rvol_exec.snapshot import PolygonSnapshotClient
        api = PolygonSnapshotClient()
    df, meta = build_prep(target, api=api, allow_api=a.allow_api, api_sleep_s=a.api_sleep)
    ok, why = freshness(meta, target)
    print(f"[{STAMP}] prep {target}: candidates={meta['n_candidates']} rankable={meta['n_rankable']} "
          f"api_calls={meta['api_calls']} sources={json.dumps(meta['sources'])} fresh={ok} ({why})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
