#!/usr/bin/env python3
"""RVOL shadow universe — daily observation logger (K17 follow-through,
user-signed 2026-07-16). NO trading impact: logs what the market-wide
opening-RVOL top-20 was today, alongside the static watchlist's own RVOLs,
building the evidence base for a future (pre-registered) watchlist decision.

Commissioning pre-mortem: silent-failure modes = Polygon outage (grouped
pull fails -> logged error line + no output file -> manifest alarm),
holiday (grouped empty -> writes holiday marker), candidate 5m pulls
partially failing (coverage count in output). Data is 15-min delayed at
source — irrelevant for after-close logging. Job runs 14:10 PT (launchd
weekdays).

Pacing history: on 2026-07-16 Polygon throttled to ~5 req/min, so ~1,900
candidate pulls took ~6.5h. As of 2026-09-13 that limit is gone (8/8 rapid
calls 200) and src/data/polygon_data._get backs off on 429/5xx itself, so
the per-request spacing is a small constant (REQ_SLEEP) and a full scan
takes minutes. Coverage accounting (2026-09-13): on 2026-07-29 a 429 storm
silently dropped 591 of 1,780 candidates — the top-20 was ranked on 67% of
the universe and nothing alarmed. Every run now writes candidates_total /
ranked_total / coverage_pct and flags ranking_incomplete (+ ntfy) below the
pre-declared COVERAGE_MIN_PCT; the record is still written and the sleeve
carries the flag so the gate reader can exclude the session.

Run: .venv/bin/python scripts/rvol_shadow_scan.py  (launchd 14:10 weekdays)
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.polygon_data import get_client  # noqa: E402

RAW = ROOT / "data" / "research_orb" / "grouped"
OUT_DIR = ROOT / "logs" / "observation"

# Per-request pacing for the candidate 5m pulls (one-line tunable). Only
# spaces the steady-state stream — throttling (429) and 5xx are handled by
# polygon_data._get's exponential backoff, so this no longer needs to sit
# under any req/min cap (the ~5 req/min limit measured 07-16 is gone as of
# 2026-09-13: 8/8 rapid calls returned 200).
REQ_SLEEP = 0.25

# PRE-DECLARED coverage threshold (2026-09-13). Mirrors the A2 amendment
# draft's hygiene rule (docs/rvol_squeeze_build/A2_amendment_draft.md:
# "sessions with scan_errors > 10% of candidates are flagged in reporting
# and EXCLUDED from the gate evaluation"): coverage_pct = 100 x
# (candidates_total - scan_errors) / candidates_total, and a run below 90%
# is stamped ranking_incomplete. 2026-07-29 (591/1,780 errors) = 66.8%.
# The flag is applied by READERS at evaluation time; the scan still writes
# the record and the sleeve still replays it.
COVERAGE_MIN_PCT = 90.0


def rank_candidates(client, tickers, today: str, start: str) -> tuple[list, dict]:
    """Step 3 — opening RVOL per candidate (today's 5m 09:30 bar / prior-14
    same-slot mean). The ranking math is UNCHANGED from the 07-16 version;
    2026-09-13 only added error accounting (returned as the stats dict) and
    per-request pacing. scan_errors keeps its original meaning (pull returned
    no rows) for ledger continuity; scan_errors_http counts the subset that
    came back as an explicit error shape, with an http-status histogram."""
    results, errors = [], 0
    errors_http, statuses = 0, {}
    n = len(tickers)
    for i, tick in enumerate(tickers, 1):
        url = (f"https://api.polygon.io/v2/aggs/ticker/{tick}/range/5/minute/"
               f"{start}/{today}?adjusted=true&sort=asc&limit=50000")
        out = client._get(url)
        time.sleep(REQ_SLEEP)
        if isinstance(out, dict) and "error" in out:
            errors_http += 1
            st = str(out.get("http_status"))
            statuses[st] = statuses.get(st, 0) + 1
        rws = (out or {}).get("results") or []
        if i % 200 == 0:
            print(f"  {i}/{n} (errors so far: {errors})", flush=True)
        if not rws:
            errors += 1
            continue
        df = pd.DataFrame(rws)
        ts = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("US/Eastern")
        df["d"] = ts.dt.strftime("%Y-%m-%d")
        df["hm"] = ts.dt.strftime("%H:%M")
        b930 = df[df["hm"] == "09:30"].set_index("d")["v"]
        if today not in b930.index:
            continue
        prior = b930[b930.index < today].tail(14)
        if len(prior) < 14 or prior.mean() <= 0:
            continue
        results.append({"ticker": tick, "rvol": round(float(b930.loc[today] / prior.mean()), 2),
                        "v930": int(b930.loc[today])})
    results.sort(key=lambda x: -x["rvol"])
    return results, {"scan_errors": errors, "scan_errors_http": errors_http,
                     "scan_error_statuses": statuses}


def build_payload(today: str, candidates_total: int, results: list, stats: dict,
                  watch: list) -> dict:
    """The single-line output record. Original keys (candidates_scanned,
    scan_errors, top20, watchlist_*) unchanged; 2026-09-13 keys are ADDITIVE:
    candidates_total, ranked_total, coverage_pct, scan_errors_http,
    scan_error_statuses, req_sleep_s, and ranking_incomplete (only when
    coverage_pct < COVERAGE_MIN_PCT)."""
    total = int(candidates_total)
    errors = int(stats.get("scan_errors", 0))
    coverage = round(100.0 * (total - errors) / total, 1) if total > 0 else 0.0
    payload = {
        "date": today, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidates_scanned": total, "scan_errors": errors,
        "top20": results[:20],
        "watchlist_rvols": {r["ticker"]: r["rvol"] for r in results
                            if r["ticker"] in watch},
        "watchlist_in_top20": [r["ticker"] for r in results[:20] if r["ticker"] in watch],
        "note": "observation-only; source 15-min delayed (after-close job)",
        # additive coverage evidence (2026-09-13)
        "candidates_total": total,
        "ranked_total": len(results),
        "coverage_pct": coverage,
        "scan_errors_http": int(stats.get("scan_errors_http", 0)),
        "scan_error_statuses": dict(stats.get("scan_error_statuses", {}) or {}),
        "req_sleep_s": REQ_SLEEP,
    }
    # Decide the flag on the UNROUNDED share (errors > 10% of candidates), not
    # on the 1-dp stored value: 178/1779 = 10.006% rounds to coverage 90.0 yet
    # is over the A2 line and must be flagged; exactly 10% (100/1000) is not.
    if total == 0 or errors * 10 > total:
        payload["ranking_incomplete"] = True
    return payload


def alert_if_incomplete(payload: dict) -> bool:
    """ONE ntfy line when the run is stamped ranking_incomplete. The record
    is written regardless; this only makes the degraded ranking visible."""
    if not payload.get("ranking_incomplete"):
        return False
    try:
        from src.engine.notify import send
        send("RVOL scan ranking INCOMPLETE",
             f"{payload.get('date')}: coverage {payload.get('coverage_pct')}% "
             f"< {COVERAGE_MIN_PCT}% ({payload.get('scan_errors')}/"
             f"{payload.get('candidates_total')} candidate pulls returned no "
             f"rows; {payload.get('scan_errors_http')} explicit errors, "
             f"statuses {payload.get('scan_error_statuses')}). Top-20 written "
             f"but flagged ranking_incomplete — the sleeve stamps tonight's "
             f"record; excluded from gate evaluation per the A2 hygiene rule.",
             priority="high", tags="warning")
    except Exception:
        pass
    return True


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    client = get_client()
    today = args.date
    out_path = OUT_DIR / f"rvol_shadow-{today}.json"
    # ── Laptop-state hardening (2026-08-02) — root cause of the 3 lost
    # forward-test sessions (07-27/30/31): a wake-coalesced launchd fire ran
    # this scan at the WRONG time with date.today(), got an empty grouped
    # response, and wrote a no-data marker that then BLOCKED the legitimate
    # 14:10 run via the exists() skip. Three rules now:
    #  1. only a file WITH top20 counts as "already logged" — failure/premature
    #     markers are re-attempted and overwritten (un-poisons the day);
    #  2. refuse to scan-or-write for TODAY before 13:15 PT (session not
    #     complete -> a premature fire exits silently and leaves the day clean);
    #  3. empty grouped after-hours retries 3x over ~10 min, then writes a
    #     marker that distinguishes holiday (quiet) from scan_failed (LOUD
    #     ntfy — a forward-test session is being lost RIGHT NOW).
    if out_path.exists():
        try:
            prior = json.loads(out_path.read_text())
        except Exception:
            prior = {}
        if prior.get("top20"):
            print("already logged today")
            return 0
        print(f"existing file for {today} is a marker ({list(prior.keys())}) — re-attempting")
    if today == date.today().isoformat():
        now_pt = datetime.now()
        if (now_pt.hour, now_pt.minute) < (13, 15):
            print(f"refusing: {today} session not complete at {now_pt:%H:%M} PT "
                  "(wake-coalesced early fire?) — writing NOTHING; the 14:10 "
                  "run or travel_catchup will scan when the day is done")
            return 0
    # 1. today's grouped daily (also extends the research store)
    url = (f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
           f"{today}?adjusted=true")
    rows = []
    for attempt in range(1, 4):
        out = client._get(url) or {}
        if "error" in out:   # visible, never silent (2026-09-13)
            print(f"grouped pull error: status={out.get('http_status')} "
                  f"retries={out.get('retries')} {str(out['error'])[:120]}", flush=True)
        rows = out.get("results") or []
        if rows:
            break
        print(f"grouped empty (attempt {attempt}/3)")
        if attempt < 3:
            time.sleep(300)
    if not rows:
        # holiday or a real scan failure? distinguish; only holidays are quiet
        is_holiday = False
        try:
            from src.trendbook.executor import _holidays
            is_holiday = today in _holidays()
        except Exception:
            pass
        if is_holiday:
            out_path.write_text(json.dumps({"date": today, "holiday_or_no_data": True}))
            print("market holiday — quiet marker written")
        else:
            out_path.write_text(json.dumps(
                {"date": today, "scan_failed": True, "attempts": 3,
                 "at": datetime.now().isoformat(timespec="seconds")}))
            print("SCAN FAILED after 3 attempts — session will be LOST; alerting")
            try:
                from src.engine.notify import send
                send("RVOL scan FAILED — forward-test session being lost",
                     f"{today}: grouped-daily empty after 3 attempts and not a "
                     "holiday. No live selection captured; this session cannot "
                     "be backfilled. Investigate Polygon/network now.",
                     priority="high", tags="rotating_light")
            except Exception:
                pass
        return 0
    RAW.mkdir(parents=True, exist_ok=True)
    gdf = pd.DataFrame(rows)[["T", "o", "h", "l", "c", "v"]].rename(
        columns={"T": "ticker", "o": "open", "h": "high", "l": "low",
                 "c": "close", "v": "volume"})
    gdf.to_parquet(RAW / f"{today}.parquet")

    # 2. point-in-time filters from the trailing store (ex-ante convention)
    hist = []
    files = sorted(RAW.glob("2*.parquet"))[-16:]
    for f in files:
        d = pd.read_parquet(f)
        d["date"] = f.stem
        hist.append(d)
    h = pd.concat(hist, ignore_index=True).sort_values(["ticker", "date"])
    g = h.groupby("ticker", sort=False)
    prev_close = g["close"].shift(1)
    tr = pd.concat([h["high"] - h["low"], (h["high"] - prev_close).abs(),
                    (h["low"] - prev_close).abs()], axis=1).max(axis=1)
    h["atr14"] = tr.groupby(h["ticker"]).transform(lambda s: s.rolling(14).mean().shift(1))
    h["avgvol14"] = g["volume"].transform(lambda s: s.rolling(14).mean().shift(1))
    cand = h[(h["date"] == today) & (h["open"] >= 5.0)
             & (h["avgvol14"] >= 1_000_000) & (h["atr14"] > 0.50)]
    print(f"{len(cand)} filter-passing candidates", flush=True)

    # 3. opening RVOL per candidate (today's 5m 09:30 bar / prior-14 same-slot)
    start = (date.today() - timedelta(days=30)).isoformat()
    results, stats = rank_candidates(client, list(cand["ticker"]), today, start)
    watch = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())["watchlist"]["default"]
    payload = build_payload(today, len(cand), results, stats, watch)
    out_path.write_text(json.dumps(payload))   # single line (manifest parses line-wise)
    print(f"coverage {payload['coverage_pct']}% ({payload['ranked_total']} ranked, "
          f"{payload['scan_errors']} pull errors of {payload['candidates_total']})"
          + (" — RANKING INCOMPLETE" if payload.get("ranking_incomplete") else ""))
    alert_if_incomplete(payload)
    print(f"top-5 today: {[(r['ticker'], r['rvol']) for r in results[:5]]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
