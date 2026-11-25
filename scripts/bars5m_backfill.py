#!/usr/bin/env python3
"""bars5m full-lifetime backfill — the K20 repair (user-approved 2026-07-18).

Re-pulls FULL-SPAN 5-minute bars (first-candidacy minus 45d .. today) for
every ticker in daily_candidates.parquet plus the daily top-200-dollar-volume
union from the grouped store — the population whose truncated coverage
distorted every selection-vs-control comparison (K20; K17 audit).

Design: resumable (state json), throttled (REQ_SLEEP), time-boxed
(--max-minutes / hard stop at --until), atomic per-ticker replace (tmp ->
rename) writing the SAME schema as the existing store (date, hm, o,h,l,c,v)
so all existing readers keep working. Priority: liquidity-union names first
(they fix the control), then candidates by frequency.

Run: .venv/bin/python scripts/bars5m_backfill.py --max-minutes 330
Nightly: launchd com.optionsagent.bars5mfill 22:30, until 06:00.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.data.polygon_data import get_client  # noqa: E402

BARS = ROOT / "data" / "research_orb" / "bars5m"
CAND = ROOT / "data" / "research_orb" / "daily_candidates.parquet"
GROUPED = str(ROOT / "data" / "research_orb" / "grouped" / "2*.parquet")
STATE = ROOT / "data" / "research_orb" / "bars5m_backfill_state.json"
REQ_SLEEP = float(__import__("os").environ.get("BF_SLEEP", "12"))  # tier-dependent


def build_plan() -> dict:
    cand = pd.read_parquet(CAND, columns=["date", "ticker"])
    spans = cand.groupby("ticker").date.agg(["min", "max", "count"])
    con = duckdb.connect()
    liq = con.execute(f"""
      WITH r AS (SELECT ticker, close*volume dv,
                        row_number() OVER (PARTITION BY filename ORDER BY close*volume DESC) rk
                 FROM read_parquet('{GROUPED}', union_by_name=true, filename=true))
      SELECT DISTINCT ticker FROM r WHERE rk <= 200
    """).df()["ticker"].tolist()
    liq = [t for t in liq if isinstance(t, str) and t.isalpha()]
    order, seen = [], set()
    for t in liq:                                # control-critical first
        if t in seen:
            continue
        seen.add(t)
        if t in spans.index:
            order.append(t)
        else:                                    # liquidity name never a
            order.append(t)                      # candidate: full-window span
    for t in spans.sort_values("count", ascending=False).index:
        if t not in seen:
            seen.add(t); order.append(t)
    plan = {}
    for t in order:
        if t in spans.index:
            d0 = (pd.Timestamp(spans.loc[t, "min"]) - timedelta(days=45)).date()
        else:
            d0 = date(2021, 5, 1)
        plan[t] = str(d0)
    return {"order": order, "start": plan}


def pull_full(client, t: str, d0: str) -> pd.DataFrame | None:
    """Paginate until an EMPTY page. The original store's anchoring bug (K20)
    was the `len(rows) < 50000 -> done` heuristic: Polygon pages aggs
    responses at ~5-10k rows well below the limit, so page 1 looked final
    and every ticker froze ~90-160 days after its span start."""
    frames, start, d1 = [], d0, date.today().isoformat()
    while start <= d1:
        url = (f"https://api.polygon.io/v2/aggs/ticker/{t}/range/5/minute/"
               f"{start}/{d1}?adjusted=true&sort=asc&limit=50000")
        out = client._get(url)
        rows = (out or {}).get("results") or []
        time.sleep(REQ_SLEEP)
        if not rows:
            break
        df = pd.DataFrame(rows)
        frames.append(df)
        last = pd.Timestamp(df["t"].iloc[-1], unit="ms", tz="US/Eastern")
        nxt = (last + pd.Timedelta(days=1)).date().isoformat()
        if nxt <= start:
            break
        start = nxt
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("US/Eastern")
    df["date"] = ts.dt.strftime("%Y-%m-%d")
    df["hm"] = ts.dt.strftime("%H:%M")
    df = df[(df["hm"] >= "09:30") & (df["hm"] < "16:00")]
    return df[["date", "hm", "o", "h", "l", "c", "v"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=330)
    ap.add_argument("--until", default=None, help="hard stop local HH:MM")
    args = ap.parse_args()
    t0 = time.time()
    deadline = None
    if args.until:
        from datetime import datetime as _dt, timedelta as _td
        now = _dt.now()
        deadline = now.replace(hour=int(args.until[:2]),
                               minute=int(args.until[3:5]), second=0)
        if deadline <= now:                 # overnight crossing (22:45->06:00)
            deadline += _td(days=1)

    if STATE.exists():
        st = json.loads(STATE.read_text())
    else:
        print("building plan (one-time)...", flush=True)
        st = build_plan()
        st["done"], st["failed"] = {}, {}
        STATE.write_text(json.dumps(st))
    client = get_client()
    todo = [t for t in st["order"] if t not in st["done"] and t not in st["failed"]]
    print(f"[{datetime.now():%H:%M}] backfill: {len(st['done'])} done, "
          f"{len(st['failed'])} failed, {len(todo)} to go", flush=True)
    n_this = 0
    for t in todo:
        if (time.time() - t0) / 60 > args.max_minutes:
            print("time box reached", flush=True); break
        if deadline and datetime.now() >= deadline:
            print("hard stop reached", flush=True); break
        try:
            df = pull_full(client, t, st["start"].get(t, "2021-05-01"))
        except Exception as exc:
            st["failed"][t] = str(exc)[:120]
            STATE.write_text(json.dumps(st)); continue
        if df is None or df.empty:
            st["failed"][t] = "no_data"
        else:
            tmp = BARS / f"{t}.parquet.tmp"
            df.to_parquet(tmp)
            tmp.replace(BARS / f"{t}.parquet")   # atomic swap, full history
            st["done"][t] = {"rows": len(df), "d0": df["date"].iloc[0],
                             "d1": df["date"].iloc[-1]}
        n_this += 1
        if n_this % 20 == 0:
            STATE.write_text(json.dumps(st))
            print(f"  {n_this} tickers this run ({len(st['done'])} total done)",
                  flush=True)
    STATE.write_text(json.dumps(st))
    print(f"[{datetime.now():%H:%M}] run end: +{n_this} this run, "
          f"{len(st['done'])} done / {len(st['order'])} planned", flush=True)


if __name__ == "__main__":
    main()
