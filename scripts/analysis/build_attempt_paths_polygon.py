"""Rebuild the SCALP attempt-path cache from POLYGON option minute aggregates.

Same output shape as build_attempt_paths.py ({'attempts','paths','vwap'}), so the
v2 counterfactual and the grid scripts consume it unchanged. Built 2026-09-24
because ThetaData lapsed to the free tier (403 on option history) and the
ThetaData-built cache was lost with a temp folder.

RULER CAVEAT — read before trusting absolute numbers: Polygon minute bars are
built from TRADE PRINTS, not NBBO quotes. A leg that did not trade in a minute
is forward-filled from its last print, and a spread's net is the sum of legs
that may have printed at different moments. It is a noisier ruler than
ThetaData's NBBO mids, most on thin contracts. Use it to RANK exit rules on one
common ruler; calibrate its baseline against the ThetaData replay before
quoting levels. Read-only w.r.t. engine state. UNVALIDATED — PAPER ONLY.

Run: .venv/bin/python scripts/analysis/build_attempt_paths_polygon.py data/cache/attempt_paths_polygon.pkl
"""
import glob
import json
import pickle
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
from enrich_trades_thetadata import pt_to_et_minute  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = Path(sys.argv[1])


def _key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("POLYGON_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("POLYGON_API_KEY missing from .env")


KEY = _key()


def attempts_from_logs():
    attempts, vwap = [], {}
    for f in sorted(glob.glob(str(ROOT / "logs/observation/2026-*.jsonl"))):
        day = Path(f).name[:-6]
        if not day[:4].isdigit() or len(day) != 10:
            continue
        for line in open(f):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = r.get("ts")
            if ts:
                et = pt_to_et_minute(ts)
                for tk, s in (r.get("signals") or {}).items():
                    if isinstance(s, dict) and isinstance(s.get("vwap_dist_atr"), (int, float)):
                        vwap.setdefault((day, tk), []).append((et, float(s["vwap_dist_atr"])))
            for a in (r.get("paper_execution_tests") or []):
                t = a.get("trade") or {}
                if t.get("legs") and all("bid" in x and "ask" in x for x in t["legs"]):
                    attempts.append({"day": day, "ts": a.get("ts") or ts, "rec_id": a.get("rec_id"),
                                     "ticker": a.get("ticker"), "strategy": a.get("strategy"),
                                     "mode": a.get("mode"), "qty": a.get("quantity"), "trade": t})
    for k in vwap:
        vwap[k].sort()
    return attempts, vwap


PACE_S = 20                 # 3 requests/minute: our Polygon key allows ~5/min and
                            # production jobs share it — leave them headroom
QUIET_PT = [("21:25", "21:45"), ("23:25", "23:50")]   # rvolrank 21:30, rvolexecprep 23:30
HARD_STOP_PT = ("05:30", "06:00")   # stop before the open: the RVOL daemon + 06:20
                                     # restart need the key; progress is kept, rerun resumes
LEG_CACHE = ROOT / "data" / "cache" / "polygon_legs.jsonl"
PT = ZoneInfo("America/Los_Angeles")


def _now_hm() -> str:
    return datetime.now(PT).strftime("%H:%M")


def _wait_for_quiet_windows() -> None:
    while any(a <= _now_hm() < b for a, b in QUIET_PT):
        time.sleep(30)
    a, b = HARD_STOP_PT
    if a <= _now_hm() < b:
        print(f"  HARD STOP {_now_hm()} PT — before the open; rerun to resume", flush=True)
        raise SystemExit(3)


def leg_minutes(symbol: str, day: str) -> dict[str, float] | None:
    """{'HH:MM' ET -> close} for one option leg on one day, or None if Polygon has
    no bars. A 429 is NEVER recorded as 'no data' — it waits and retries, because
    a silent None would punch a hole in the cache."""
    url = (f"https://api.polygon.io/v2/aggs/ticker/O:{symbol}/range/1/minute/{day}/{day}"
           f"?adjusted=true&sort=asc&limit=50000&apiKey={KEY}")
    for attempt in range(30):
        _wait_for_quiet_windows()
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = json.loads(r.read().decode())
            out = {}
            for b in body.get("results") or []:
                hm = datetime.fromtimestamp(b["t"] / 1000, tz=ET).strftime("%H:%M")
                if "09:30" <= hm <= "16:00":
                    out[hm] = float(b["c"])
            return out or None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(70)
                continue
            return None
        except Exception:  # noqa: BLE001
            time.sleep(10 + 5 * attempt)
    raise RuntimeError(f"gave up on {symbol} {day} after 30 tries — not recording a fake 'no data'")


def net_path(legs, per_leg: dict) -> list[tuple[str, float]] | None:
    """Forward-filled net value per minute, from the first minute every leg has printed."""
    series = [per_leg.get(l["symbol"]) for l in legs]
    if any(s is None for s in series):
        return None
    minutes = sorted(set().union(*[s.keys() for s in series]))
    last = [None] * len(legs); out = []
    for hm in minutes:
        for i, s in enumerate(series):
            if hm in s:
                last[i] = s[hm]
        if None in last:
            continue
        net = sum(p if l["action"].startswith("buy") else -p for p, l in zip(last, legs))
        out.append((hm, round(net, 4)))
    return out or None


def main():
    attempts, vwap = attempts_from_logs()
    print(f"attempts={len(attempts)} vwap series={len(vwap)}", flush=True)
    # the grids never replay an entry under the $1 net-debit floor — don't spend
    # rate-limited requests on those legs
    need = sorted({(l["symbol"], a["day"]) for a in attempts
                   if float(a["trade"].get("net_debit") or 0) >= 1.0 for l in a["trade"]["legs"]})
    legdata = {}
    if LEG_CACHE.exists():                       # resume
        for line in LEG_CACHE.read_text().splitlines():
            r = json.loads(line); legdata[(r["sym"], r["day"])] = r["bars"]
    todo = [sd for sd in need if sd not in legdata]
    print(f"leg-days needed {len(need)}; already cached {len(need)-len(todo)}; to fetch {len(todo)} "
          f"(~{len(todo)*PACE_S/3600:.1f} h at {3600//PACE_S}/h)", flush=True)
    LEG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(LEG_CACHE, "a") as fh:
        for i, (sym, day) in enumerate(todo, 1):
            t0 = time.time()
            bars = leg_minutes(sym, day)
            legdata[(sym, day)] = bars
            fh.write(json.dumps({"sym": sym, "day": day, "bars": bars}) + "\n"); fh.flush()
            if i % 50 == 0:
                print(f"  fetched {i}/{len(todo)} at {_now_hm()} PT", flush=True)
            time.sleep(max(0.0, PACE_S - (time.time() - t0)))
    paths = {}
    for a in attempts:
        legs = a["trade"]["legs"]
        key = (tuple(x["symbol"] for x in legs), tuple(x["action"] for x in legs), a["day"])
        if key in paths:
            continue
        paths[key] = net_path(legs, {l["symbol"]: legdata.get((l["symbol"], a["day"])) for l in legs})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pickle.dump({"attempts": attempts, "paths": paths, "vwap": vwap,
                 "source": "polygon_minute_aggs_trade_prints", "built": datetime.now().isoformat()},
                open(OUT, "wb"))
    ok = sum(1 for v in paths.values() if v)
    print(f"DONE {len(paths)} paths ({ok} non-empty, {len(paths)-ok} no data) -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
