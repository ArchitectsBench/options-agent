"""Durable bars5m refresh keyed on TRADED (ticker, day) — closes the no_path gap
at the source so it can't regrow.

The pre-gate's drift metric reads underlying_path(ticker, day), which resolves
bars5m_fresh/{day}/ then the bars5m/ full-history store. Both stores are populated
by LIQUIDITY universes (rvolrank sleeve top-20s; backfill top-200 dollar-volume) —
neither is keyed on what the FUNNEL/SCALP lanes actually traded. So a traded name
outside both universes (SLV, XLE, TLT, UBER, …) has no path, and the pre-gate falls
back to the age heuristic (age_fallback_no_path) instead of measuring real drift.

This step iterates every (ticker, day) with an `ok` enriched trade and, for any pair
where underlying_path() returns nothing, pulls 5-min bars from Polygon into
bars5m_fresh/{day}/. underlying_path() is BOTH the coverage oracle and the pre-gate's
reader, so a pair this script leaves covered is exactly a pair the pre-gate can then
measure. Idempotent: pairs already covered are skipped (no Polygon call). Report-only
by default; --write performs the pulls.

Run nightly after rvolrank_sleeve. Spec: item 2 durable half (2026-07-25).
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
eng = importlib.import_module("post_mortem_engine")
sleeve = importlib.import_module("rvolrank_sleeve")   # reuse pull_5m + REQ_SLEEP
from src.data.polygon_data import get_client  # noqa: E402

FRESH = ROOT / "data" / "research_orb" / "bars5m_fresh"
ENRICHED = ROOT / "logs" / "observation" / "enriched_trades_v1.jsonl"


def traded_pairs() -> set[tuple[str, str]]:
    """(ticker, day) for every `ok` enriched trade. rec_id day-parse mirrors the
    pm/nightly convention (TEST=YYYYMMDD prefix, funnel=YYYY-MM-DD prefix)."""
    pairs = set()
    for line in ENRICHED.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("status") != "ok" or not r.get("ticker"):
            continue
        rid = r["rec_id"]
        day = rid[:8] if "-TEST-" in rid else rid[:10]
        if "-TEST-" in rid:
            day = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
        pairs.add((r["ticker"], day))
    return pairs


def covered(ticker: str, day: str) -> bool:
    """Coverage == the pre-gate's own reader returns a usable path."""
    p = eng.underlying_path(ticker, day)
    return isinstance(p, list) and len(p) > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="pull missing pairs (else report-only)")
    ap.add_argument("--max-minutes", type=float, default=60)
    args = ap.parse_args()

    pairs = sorted(traded_pairs())
    missing = [(t, d) for t, d in pairs if not covered(t, d)]
    print(f"traded (ticker,day): {len(pairs)} | already covered: {len(pairs)-len(missing)} "
          f"| no_path: {len(missing)}")
    if not missing:
        print("nothing to refresh — store fully covers the traded universe.")
        return 0
    by_tk = Counter(t for t, _ in missing)
    print(f"no_path tickers: {dict(by_tk)}")
    if not args.write:
        print("(report-only; re-run with --write to pull)")
        return 0

    client = get_client()
    t0 = time.time()
    pulled = failed = 0
    for t, d in missing:
        if (time.time() - t0) / 60 > args.max_minutes:
            print("time box reached"); break
        time.sleep(sleeve.REQ_SLEEP)
        try:
            df = sleeve.pull_5m(client, t, d)
        except Exception as exc:
            failed += 1; print(f"  {t} {d}: FAIL {str(exc)[:80]}"); continue
        if df is None or df.empty:
            failed += 1; print(f"  {t} {d}: no_bars"); continue
        fd = FRESH / d
        fd.mkdir(parents=True, exist_ok=True)
        tmp = fd / f"{t}.parquet.tmp"
        df.to_parquet(tmp); tmp.replace(fd / f"{t}.parquet")
        pulled += 1
    still = sum(1 for t, d in missing if not covered(t, d))
    print(f"pulled: {pulled} | failed: {failed} | still no_path after refresh: {still}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
