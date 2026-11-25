#!/usr/bin/env python3
"""Measure the Polygon whole-market snapshot's DELAY vs the wall clock.
One call. Records: wall time (ET), the newest `updated` stamp across tickers,
SPY day o/h/l/c/v and last-minute bar time, and the implied delay in seconds.
Purpose (2026-09-13): decide whether the RVOL-RANK executable window is
09:35 (real-time) or ~09:50 (15-min delayed). Run during the open, e.g.
09:36 and 09:51 ET on 2026-09-14. Appends to logs/ops/polygon_snapshot_probe.jsonl.
Read-only w.r.t. every evidence surface; costs one API call.
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import urllib.request
ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
OUT = ROOT / "logs" / "ops" / "polygon_snapshot_probe.jsonl"

def key() -> str:
    for l in (ROOT / ".env").read_text().splitlines():
        if l.startswith("POLYGON_API_KEY="):
            return l.split("=", 1)[1].strip()
    raise SystemExit("no POLYGON_API_KEY")

def main() -> int:
    t0 = time.time()
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers?apiKey={key()}"
    with urllib.request.urlopen(url, timeout=30) as r:
        d = json.load(r)
    wall = datetime.now(timezone.utc)
    tk = d.get("tickers") or []
    newest_ns = max((int(x.get("updated") or 0) for x in tk), default=0)
    newest = datetime.fromtimestamp(newest_ns / 1e9, tz=timezone.utc) if newest_ns else None
    spy = next((x for x in tk if x.get("ticker") == "SPY"), {})
    mn = spy.get("min") or {}
    rec = {"wall_et": wall.astimezone(ET).isoformat(timespec="seconds"),
           "n_tickers": len(tk), "status": d.get("status"),
           "newest_updated_et": newest.astimezone(ET).isoformat(timespec="seconds") if newest else None,
           "implied_delay_sec": round((wall - newest).total_seconds(), 1) if newest else None,
           "spy_day": spy.get("day"), "spy_min_t_et": (datetime.fromtimestamp(mn["t"] / 1e3, tz=ET).isoformat(timespec="seconds") if mn.get("t") else None),
           "spy_min_av": mn.get("av"), "elapsed_sec": round(time.time() - t0, 2)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # double duty (2026-09-13): keep the RAW payload so the intraday rank can be
    # reconstructed offline and reconciled against the nightly logged top-20
    # (the exec lane's kill criterion: overlap >= 18/20 before 10:00 ET)
    import gzip
    snapdir = ROOT / "logs" / "ops" / "snapshots"; snapdir.mkdir(parents=True, exist_ok=True)
    raw_path = snapdir / f"{wall.astimezone(ET):%Y%m%dT%H%M%S}.json.gz"
    with gzip.open(raw_path, "wt") as g:
        json.dump(d, g)
    rec["raw_payload"] = str(raw_path.relative_to(ROOT))
    with OUT.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))
    return 0

if __name__ == "__main__":
    sys.exit(main())
