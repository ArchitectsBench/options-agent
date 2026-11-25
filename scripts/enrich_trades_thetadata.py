#!/usr/bin/env python3
"""T2a — market-truth enrichment for EVERY executed round trip, FUNNEL and
SCALP equally (standing rule 2026-07-15: both streams, always).

For each paper_exit in the post-06-25 ledger, pulls minute NBBO for the
trade's EXACT contracts (ThetaData) and writes an append-only enrichment row:
  - true minute mid path of the structure (entry day .. exit day)
  - true entry/exit mids at the booked fill minutes
  - market MFE/MAE (1-min granularity vs the live 5-min marks)
  - sandbox divergence: booked fill price vs contemporaneous market mid
    (the L17 fantasy-fill audit, run systematically over history)

Never modifies existing records (L15 spirit): output is
logs/observation/enriched_trades_v1.jsonl, keyed by rec_id + exit order id;
re-runs skip already-enriched round trips.

Run: .venv/bin/python scripts/enrich_trades_thetadata.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import thetadata as td  # noqa: E402
from src.util.atomic import read_jsonl  # noqa: E402
# SLEEVE TAG (2026-08-10): route through the canonical classifier, which
# accepts BOTH marker eras. This file hardcoded '-TEST-', so every rec_id
# minted after the 2026-08-08 forward-only rename was tagged 'funnel' — a
# mislabel in the TRUTH BASIS itself, which is what the gates read.
from src.engine.paper_trader import is_scalp  # noqa: E402

LEDGER = ROOT / "logs" / "observation" / "fills.jsonl"
import glob as _glob


def fallback_legs_map() -> dict:
    """rec_id -> [{'symbol','side'}] from rec/observation trade specs — covers
    single-leg fills whose ledger legs list is empty (2026-07-15 coverage fix:
    the 0DTE sleeve was landing in no_market_data)."""
    out = {}
    recs = ROOT / "logs" / "recommendations.jsonl"
    for r in read_jsonl(recs):
        legs = ((r.get("trade") or {}).get("legs")) or []
        if r.get("rec_id") and legs:
            out[r["rec_id"]] = [{"symbol": l["symbol"], "side": l["action"]}
                                for l in legs if l.get("symbol")]
    for f in _glob.glob(str(ROOT / "logs/observation/2026-*.jsonl")):
        for r in read_jsonl(Path(f)):
            for t in (r.get("paper_execution_tests") or []):
                legs = ((t.get("trade") or {}).get("legs")) or []
                if t.get("rec_id") and legs and t["rec_id"] not in out:
                    out[t["rec_id"]] = [{"symbol": l["symbol"], "side": l["action"]}
                                        for l in legs if l.get("symbol")]
    return out
OUT = ROOT / "logs" / "observation" / "enriched_trades_v1.jsonl"
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def parse_occ(sym: str):
    m = OCC.match(sym)
    if not m:
        return None
    root, ymd, right, strike = m.groups()
    return {"symbol": root, "expiration": f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}",
            "right": right, "strike": int(strike) / 1000}


def pt_to_et_minute(ts: str) -> str:
    """Ledger local (PT, naive) or broker UTC (any aware form) -> ET HH:MM.

    R10 DST fix: the broker-UTC branch converts through the real
    America/New_York tz database, not a hardcoded -4h. The fixed offset was
    EDT-only and produced a one-hour error for every UTC timestamp from the
    Nov DST rollover onward (EST is UTC-5). The PT-naive branch stays +3h: PT
    and ET observe DST simultaneously, so that offset is constant year-round.

    TZ-AWARE FIX (2026-08-10): the aware branch keyed on a literal 'Z'
    suffix, but IBKR stamps '+00:00' — aware UTC without the Z. Those fell
    into the naive-PT branch (+3h on top of UTC), landing every IBKR entry at
    ~17:00 ET, AFTER THE CLOSE. The enricher then took the end-of-day mid as
    the "entry" (an expired 0DTE quotes ~$0.015 — a fabricated bargain) and
    the inverted entry->exit window produced an empty path that was STILL
    scored status="ok": 57 fabricated truth scores, +$4,412 of phantom P&L,
    75% of the IBKR era. Branch on tzinfo, not on string suffix."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(_ET)
    else:
        dt = dt + timedelta(hours=3)
    return dt.strftime("%H:%M")


def structure_path(legs: list[dict], dates: list[str]) -> pd.DataFrame | None:
    """Per-minute signed net mid across the trade's dates; None if a leg dry."""
    frames = []
    for i, leg in enumerate(legs):
        occ = parse_occ(leg["symbol"])
        if occ is None:
            return None
        sign = 1 if leg["side"].startswith("buy") else -1
        parts = []
        for d in dates:
            q = td.option_quotes(occ["symbol"], occ["expiration"], occ["strike"],
                                 occ["right"], d)
            if q is None or q.empty:
                continue
            q = q[(q["bid"] > 0) & (q["ask"] >= q["bid"])]
            if q.empty:
                continue
            q = q.assign(key=d + "T" + q["timestamp"].str[11:16],
                         mid=(q["bid"] + q["ask"]) / 2)
            parts.append(q.set_index("key")[["mid"]].rename(columns={"mid": f"m{i}"})
                         .assign(**{f"s{i}": sign}))
        if not parts:
            return None
        frames.append(pd.concat(parts))
    m = pd.concat(frames, axis=1, join="inner")
    if m.empty:
        return None
    m["net_mid"] = sum(m[f"m{i}"] * m[f"s{i}"] for i in range(len(legs)))
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo-no-data", action="store_true",
                    help="re-attempt rows previously written as no_market_data "
                         "(last row per rec_id wins at read time)")
    ap.add_argument("--through", metavar="YYYY-MM-DD", default=None,
                    help="only enrich round trips whose exit date is on or "
                         "before this day; use to finalize past sessions "
                         "without writing premature rows for a still-open day")
    args = ap.parse_args()
    if not td.ensure_terminal():
        print("terminal unavailable"); return 1

    done = set()
    for r in read_jsonl(OUT):
        if r.get("status") == "ok" or not args.redo_no_data:
            done.add(r["rec_id"])
    fallback = fallback_legs_map()

    exits = [e for e in read_jsonl(LEDGER) if e.get("event") == "paper_exit"]
    todo = [e for e in exits if e["rec_id"] not in done]
    if args.through:
        todo = [e for e in todo if e["ts"][:10] <= args.through]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(exits)} round trips, {len(done)} already enriched, "
          f"{len(todo)} to do", flush=True)

    n_ok = n_skip = 0
    with OUT.open("a") as f:
        for i, e in enumerate(todo, 1):
            rt = e.get("round_trip") or {}
            legs = rt.get("entry_legs") or fallback.get(e["rec_id"]) or []
            entered = rt.get("entry_filled_at") or e.get("ts")
            entry_date = str(pd.Timestamp(entered.rstrip("Z")).date()) if entered else e["ts"][:10]
            exit_date = e["ts"][:10]
            dates = sorted({entry_date, exit_date})
            path = structure_path(legs, dates) if legs else None
            if path is None:
                n_skip += 1
                f.write(json.dumps({
                    "rec_id": e["rec_id"], "enrich_v": 1, "status": "no_market_data",
                    "enriched_at": datetime.now().isoformat(timespec="seconds"),
                    "sleeve": "scalp" if is_scalp(e["rec_id"]) else "funnel",
                    "ticker": e.get("ticker")}) + "\n")
                continue
            qty = int(e.get("quantity", 1))
            entry_key = entry_date + "T" + pt_to_et_minute(entered)
            exit_key = exit_date + "T" + pt_to_et_minute(e["ts"])
            def at(key):
                sub = path[path.index <= key]
                return float(sub["net_mid"].iloc[-1]) if len(sub) else None
            tm_entry, tm_exit = at(entry_key), at(exit_key)
            booked_entry = e.get("entry_price")
            booked_exit = e.get("exit_price")
            sleeve = "scalp" if is_scalp(e["rec_id"]) else "funnel"
            live = path[(path.index >= entry_key) & (path.index <= exit_key)]["net_mid"]
            # FAIL CLOSED (2026-08-10): an inverted window or an empty path is
            # a MEASUREMENT REFUSAL, not a score. The tz defect above produced
            # entry_key > exit_key, len(live) == 0 — and the row was still
            # written status="ok" with a fabricated true_pnl_mid, which the
            # frozen gates then consumed as evidence. A truth ruler must
            # refuse loudly when it cannot measure; gates already treat any
            # non-"ok" status as excluded, so this is safe downstream.
            if entry_key > exit_key or not len(live) or tm_entry is None or tm_exit is None:
                n_skip += 1
                f.write(json.dumps({
                    "rec_id": e["rec_id"], "enrich_v": 1, "status": "window_error",
                    "enriched_at": datetime.now().isoformat(timespec="seconds"),
                    "sleeve": sleeve, "ticker": e.get("ticker"),
                    "detail": {"entry_key": entry_key, "exit_key": exit_key,
                               "n_path_minutes": int(len(live)),
                               "tm_entry": tm_entry, "tm_exit": tm_exit}}) + "\n")
                continue
            row = {
                "rec_id": e["rec_id"], "ticker": e.get("ticker"), "sleeve": sleeve,
                "enrich_v": 1, "status": "ok",
                "enriched_at": datetime.now().isoformat(timespec="seconds"),
                "exit_reason": e.get("exit_reason"), "quantity": qty,
                "booked_entry": booked_entry, "booked_exit": booked_exit,
                "booked_pnl": e.get("pnl_total"),
                "true_mid_entry": tm_entry, "true_mid_exit": tm_exit,
                "true_pnl_mid": (round((tm_exit - tm_entry) * 100 * qty, 2)
                                 if tm_entry is not None and tm_exit is not None else None),
                "entry_divergence": (round(booked_entry - tm_entry, 3)
                                     if tm_entry is not None and booked_entry is not None else None),
                "exit_divergence": (round(booked_exit - tm_exit, 3)
                                    if tm_exit is not None and booked_exit is not None else None),
                "mfe_mid_1m": round(float(live.max()), 3) if len(live) else None,
                "mae_mid_1m": round(float(live.min()), 3) if len(live) else None,
                "n_path_minutes": int(len(live)),
                "path_1m": [[k, round(v, 3)] for k, v in live.items()][:500],
            }
            f.write(json.dumps(row) + "\n")
            n_ok += 1
            if i % 20 == 0:
                print(f"  {i}/{len(todo)} ({n_ok} ok, {n_skip} no-data)", flush=True)
    print(f"done: {n_ok} enriched, {n_skip} without market data -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
