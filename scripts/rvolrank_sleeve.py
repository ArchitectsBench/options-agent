#!/usr/bin/env python3
"""RVOL-RANK virtual sleeve — nightly after-the-fact ORB simulation.

Criteria FROZEN in config/rvolrank_forward_criteria.yaml (2026-07-18).
Construction follows analysis/intake/orb_rvol/redesign_prereg.md (K18):
rank-priority allocation, 10 slots x 0.1% risk, 20% ATR(14) hard stop,
no partial banking (hard stop or 15:55 close only), 09:35-10:25 triggers,
costs = 5bps slippage + $0.005/share/side + 1%-of-opening-volume cap +
10%-of-stop cancel guardrail. Two lanes nightly, both from FRESH Polygon
pulls (K20 anchoring cannot apply):
  selection = day's rvolshadow top-20 (logs/observation/rvol_shadow-*.json)
  control   = day's top-20 by dollar volume (grouped daily endpoint)

Every output stamped UNVALIDATED — PAPER ONLY. This is a simulation on
after-close data under frozen rules — a forward test of the RANKING signal,
not live execution.

Run: .venv/bin/python scripts/rvolrank_sleeve.py [--date YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.data.polygon_data import RETRYABLE_STATUS, get_client  # noqa: E402
from src.util.atomic import read_jsonl  # noqa: E402

STAMP = "UNVALIDATED — PAPER ONLY"
OBS_DIR = ROOT / "logs" / "observation"
LEDGER = OBS_DIR / "rvolrank_sleeve.jsonl"
# Lost-night alert dedup marker (2026-09-13): one line per (date, cause)
# already paged, so the hourly catch-up sweep re-running a lost date does
# not page every hour. Runtime state of THIS script only; tests redirect it.
ALERT_LOG = OBS_DIR / "rvolrank_sleeve_alerts.jsonl"
FRESH = ROOT / "data" / "research_orb" / "bars5m_fresh"
SLIP = 5 / 10_000
COMMISSION = 0.005
SLOTS = 10                      # redesign_prereg.md construction
RISK_USD = 50.0                 # 0.1% of $50k reference per slot
STOP_ATR_FRAC = 0.20            # K18b

# item 3 (2026-07-23): cost-stress tiers, frozen in the forward-criteria doc.
import yaml as _yaml  # noqa: E402
_CRIT = _yaml.safe_load(
    (ROOT / "config" / "rvolrank_forward_criteria.yaml").read_text())
COST_TIERS = (_CRIT.get("amendment_a1_2026_07_23", {}) or {}).get("cost_tiers", {})
COST_TIERS = {k: v for k, v in COST_TIERS.items() if isinstance(v, dict)}
# Sharadar store (read-only, cross-repo) for cap/ADV/earnings measurement.
SHARADAR = Path("/Users/you/projects/strategy-chase/data/sharadar")
# Per-request pacing (one-line tunable). History: night-1 defect (2026-07-20)
# — 1.5s blew the then ~5 req/min cap after ~8 names and every later request
# 429'd, MASKED as "no_bars" (see the superseded rec); the fix was 13s. As of
# 2026-09-13 that req/min limit is gone (8/8 rapid calls 200) and
# polygon_data._get retries 429/5xx with exponential backoff + jitter and
# returns a distinguishable error shape, so a throttled name can no longer
# be recorded as missing data — it lands in the skip list WITH its status.
# 13s x ~80 calls (~17 min/night, ~7h for a catch-up sweep) was stale pacing.
REQ_SLEEP = 0.25
# Outer second chance after _get's own backoff window (~15s) is exhausted on
# a TRANSIENT failure (429/5xx/network); non-transient 4xx never re-tries.
OUTER_RETRY_SLEEP_S = 20.0
# Visibility (2026-09-13): the last exhausted error shape per (ticker, stage)
# so run_lane can name WHY a skip happened ("no_bars:http_429"), instead of
# the silent "no_bars" that masked night-1. Cleared on a successful pull.
PULL_ERRORS: dict[tuple[str, str], dict] = {}


def polygon_grouped(client, day: str) -> pd.DataFrame:
    url = (f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
           f"{day}?adjusted=true")
    rows = (client._get(url) or {}).get("results") or []
    return pd.DataFrame(rows)


def _get_retry(client, url: str, tries: int = 3) -> dict:
    """_get with an outer re-attempt on TRANSIENT failures — a rate-limited
    request must never be silently recorded as missing data (night-1 lesson).
    polygon_data._get (2026-09-13) already backs off internally (429/5xx/
    network, up to 5 tries, Retry-After honoured) and returns a distinguishable
    {"error", "http_status", "retries"} shape; this wrapper adds a short second
    chance for an outage that outlasts that window and prints every exhausted
    error WITH its status. A non-transient 4xx (403/404) returns at once."""
    out: dict = {}
    for i in range(tries):
        out = client._get(url) or {}
        if "error" not in out:
            return out
        status = out.get("http_status")
        print(f"[rvolrank] API error (try {i+1}/{tries}) status={status} "
              f"retries={out.get('retries')}: {str(out['error'])[:120]}",
              flush=True)
        transient = status is None or status in RETRYABLE_STATUS
        if not transient or i >= tries - 1:
            break
        time.sleep(OUTER_RETRY_SLEEP_S)
    return out


def _note_error(t: str, stage: str, out: dict) -> None:
    """Keep the exhausted error shape for skip-reason visibility."""
    if "error" in out:
        PULL_ERRORS[(t, stage)] = {"http_status": out.get("http_status"),
                                   "retries": out.get("retries"),
                                   "error": str(out.get("error"))[:120]}
    else:
        PULL_ERRORS.pop((t, stage), None)


def _skip_reason(base: str, t: str, stage: str) -> str:
    """'no_bars' / 'no_atr' UNCHANGED when the pull simply returned no rows;
    ':http_<status>' or ':net_error' appended when it failed after retries."""
    err = PULL_ERRORS.get((t, stage))
    if not err:
        return base
    st = err.get("http_status")
    return f"{base}:http_{st}" if st is not None else f"{base}:net_error"


def pull_5m(client, t: str, day: str) -> pd.DataFrame | None:
    url = (f"https://api.polygon.io/v2/aggs/ticker/{t}/range/5/minute/"
           f"{day}/{day}?adjusted=true&sort=asc&limit=50000")
    out = _get_retry(client, url)
    _note_error(t, "bars", out)
    rows = out.get("results") or []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    ts = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("US/Eastern")
    df["hm"] = ts.dt.strftime("%H:%M")
    df = df[(df["hm"] >= "09:30") & (df["hm"] < "16:00")]
    return df[["hm", "o", "h", "l", "c", "v"]].reset_index(drop=True)


def atr14(client, t: str, day: str) -> float | None:
    d1 = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    d0 = (date.fromisoformat(day) - timedelta(days=45)).isoformat()
    url = (f"https://api.polygon.io/v2/aggs/ticker/{t}/range/1/day/"
           f"{d0}/{d1}?adjusted=true&sort=asc&limit=120")
    out = _get_retry(client, url)
    _note_error(t, "atr", out)
    rows = out.get("results") or []
    if len(rows) < 15:
        return None
    df = pd.DataFrame(rows).tail(15)
    pc = df["c"].shift(1)
    tr = pd.concat([df["h"] - df["l"], (df["h"] - pc).abs(),
                    (df["l"] - pc).abs()], axis=1).max(axis=1)
    v = tr.tail(14).mean()
    return float(v) if pd.notna(v) else None


def replay_k18(bars: pd.DataFrame, atr: float, entry_start: str = "09:35") -> dict | None:
    """orb_backtest.replay_day with the K18 20%-ATR stop, mode-A exits.
    Logic kept line-compatible with the frozen study code.

    entry_start (added 2026-07-23, executability fork): "09:35" is the
    IDEALIZED lane (rank from the 09:30 bar acted on immediately — requires
    real-time stock data we do NOT have). "09:45" is the EXECUTABLE lane
    that skips the 09:35-09:44 triggers unreachable under our 15-min-delayed
    stock feed (Polygon; IBKR stock API not entitled per 2026-07-22 probe).
    The 09:45 lane is the gate-eligible PRIMARY; 09:35 is a delay-cost only
    measurement. See config/rvolrank_forward_criteria.yaml amendment A1."""
    b = bars.set_index("hm")
    if "09:30" not in b.index:
        return None
    orb = b.loc["09:30"]
    if isinstance(orb, pd.DataFrame):
        orb = orb.iloc[0]
    rng = float(orb["h"] - orb["l"])
    body = abs(float(orb["c"] - orb["o"]))
    if rng <= 0 or body < 0.10 * rng:
        return None
    long_side = orb["c"] > orb["o"]
    level = float(orb["h"]) + 0.01 if long_side else float(orb["l"]) - 0.01
    stop_dist = STOP_ATR_FRAC * atr
    if stop_dist <= 0:
        return None
    sig = b[(b.index >= entry_start) & (b.index <= "10:25")]
    raw_fill = fill_t = None
    fill_bar_hl = None                                  # item 3: spread proxy
    for tt, row in sig.iterrows():
        crossed = row["h"] >= level if long_side else row["l"] <= level
        if not crossed:
            continue
        raw = max(level, float(row["o"])) if long_side else min(level, float(row["o"]))
        if abs(raw - level) > 0.10 * stop_dist:
            return None                                # cancel guardrail
        raw_fill = raw
        fill_bar_hl = float(row["h"] - row["l"])
        fill_t = tt
        break
    if raw_fill is None:
        return None
    fill = raw_fill * (1 + SLIP) if long_side else raw_fill * (1 - SLIP)
    hard = fill - stop_dist if long_side else fill + stop_dist
    live = b[b.index > fill_t]
    raw_exit, reason = None, "eod"
    for tt, row in live.iterrows():
        if tt >= "15:55":
            raw_exit = float(row["c"]); break
        hit = row["l"] <= hard if long_side else row["h"] >= hard
        if hit:
            raw_exit = min(hard, float(row["o"])) if long_side else max(hard, float(row["o"]))
            reason = "stop"
            break
    if raw_exit is None:
        raw_exit = float(live["c"].iloc[-1]) if len(live) else raw_fill
    v930 = float(orb["v"])

    # cost tiers (item 3, frozen in config): base tier reproduces the
    # original inline model exactly; 2x/3x scale slippage + per-share.
    def net_r(slip, per_share):
        f = raw_fill * (1 + slip) if long_side else raw_fill * (1 - slip)
        x = raw_exit * (1 - slip) if long_side else raw_exit * (1 + slip)
        gross = (x - f) if long_side else (f - x)
        return round((gross - 2 * per_share) / stop_dist, 4)
    # r_tiers = FULLY-costed (slippage + per-share commission) per tier, for
    # the cost-stress gate. Primary "r" keeps the ORIGINAL definition
    # (net of base slippage, gross of commission) for continuity with the
    # existing sessions — do NOT change it.
    r_tiers = {name: net_r(t["slippage_bps"] / 10000.0, t["per_share_side"])
               for name, t in COST_TIERS.items()}
    base_gross = (raw_exit * (1 - SLIP) - raw_fill * (1 + SLIP)) if long_side \
        else (raw_fill * (1 - SLIP) - raw_exit * (1 + SLIP))
    return {"fill_t": fill_t, "r": round(base_gross / stop_dist, 4),
            "r_tiers": r_tiers,
            "pnl_per_share": round(base_gross - 2 * COMMISSION, 4),
            "spread_proxy": round(fill_bar_hl, 4) if fill_bar_hl is not None else None,
            "stop_dist": stop_dist, "open_vol": v930,
            "side": "L" if long_side else "S", "reason": reason}


def run_lane(client, day, lane, names, cache):
    # two entry-window variants per the executability fork (2026-07-23):
    # "09:35" idealized (delay-cost measurement), "09:45" executable (gate
    # primary). Both replayed from the same cached bars — no extra pulls.
    out = {"09:35": [], "09:45": []}
    skips = []
    for rank, t in enumerate(names, 1):
        if t not in cache:
            time.sleep(REQ_SLEEP)
            cache[t] = pull_5m(client, t, day)
            if cache[t] is not None:
                fd = FRESH / day
                fd.mkdir(parents=True, exist_ok=True)
                cache[t].to_parquet(fd / f"{t}.parquet")
        bars = cache[t]
        if bars is None or bars.empty:
            skips.append((t, _skip_reason("no_bars", t, "bars"))); continue
        time.sleep(REQ_SLEEP)
        a = atr14(client, t, day)
        if not a:
            skips.append((t, _skip_reason("no_atr", t, "atr"))); continue
        for es in ("09:35", "09:45"):
            tr = replay_k18(bars, a, entry_start=es)
            if tr is None:
                continue
            shares = min(RISK_USD / tr["stop_dist"], 0.01 * tr["open_vol"])
            tr.update({"ticker": t, "rank": rank, "lane": lane,
                       "entry_window": es, "shares": round(shares, 1),
                       "pnl": round(tr["pnl_per_share"] * shares, 2)})
            out[es].append(tr)
    for es in out:
        out[es] = sorted(out[es], key=lambda x: x["rank"])[:SLOTS]
    return out, skips


def name_metadata(day: str, names: list, grouped, cache: dict) -> dict:
    """Items 4-5 (measurement only): per-name earnings-adjacency, overnight
    gap %, halt proxy, market cap, avg daily $-volume. Sharadar cap/earnings
    coverage is partial (its universe) — coverage is reported, not assumed."""
    md = {}
    # Sharadar earnings (SF1 datekey) + market cap (DAILY), read-only
    edates = {}
    caps = {}
    try:
        import pandas as _pd
        sf = _pd.read_parquet(SHARADAR / "sf1_arq.parquet", columns=["ticker", "datekey"])
        sf = sf[sf["ticker"].isin(names)]
        sf["datekey"] = _pd.to_datetime(sf["datekey"])
        d0 = _pd.to_datetime(day)
        for t, grp in sf.groupby("ticker"):
            edates[t] = int((grp["datekey"] - d0).abs().dt.days.min())
        dly = _pd.read_parquet(SHARADAR / "daily" / "year=2026" / "part.parquet")
        dly = dly[dly["ticker"].isin(names)].sort_values("date")
        for t, grp in dly.groupby("ticker"):
            caps[t] = float(grp["marketcap"].iloc[-1]) * 1e6  # Sharadar $M
    except Exception as ex:
        md["_sharadar_error"] = str(ex)[:80]
    # grouped store for gap % and ADV $
    gmap = {}
    try:
        files = sorted((ROOT / "data" / "research_orb" / "grouped").glob("2*.parquet"))[-16:]
        import pandas as _pd
        h = _pd.concat([_pd.read_parquet(f).assign(date=f.stem) for f in files])
        for t, grp in h[h["ticker"].isin(names)].groupby("ticker"):
            grp = grp.sort_values("date")
            prev_close = grp["close"].shift(1)
            adv = float((grp["close"] * grp["volume"]).tail(14).mean())
            row = grp[grp["date"] == day]
            gap = (float(row["open"].iloc[0]) / float(prev_close[grp["date"] == day].iloc[0]) - 1) \
                if len(row) and grp["date"].tolist().count(day) and prev_close[grp["date"] == day].notna().any() else None
            gmap[t] = {"adv_dollar": adv, "gap_pct": round(gap * 100, 2) if gap is not None else None}
    except Exception:
        pass
    for t in names:
        bars = cache.get(t)
        halt = None
        if bars is not None and not bars.empty:
            rth = bars[(bars["hm"] >= "09:30") & (bars["hm"] < "16:00")]
            halt = bool(len(rth) < 70)  # <70 of ~78 RTH 5m bars ⇒ possible halt
        g = gmap.get(t, {})
        md[t] = {"days_to_earnings": edates.get(t), "marketcap": caps.get(t),
                 "adv_dollar": g.get("adv_dollar"), "gap_pct": g.get("gap_pct"),
                 "halt_proxy": halt}
    return md


def stamp_shadow_quality(rec: dict, shadow: dict) -> dict:
    """ADDITIVE (2026-09-13): carry the shadow scan's coverage evidence onto
    the sleeve record. The replay itself is unchanged — the gate's hygiene
    rule (A2 draft: scan_errors > 10% of candidates -> session flagged and
    EXCLUDED from evaluation) is applied by the READER at evaluation time,
    never here. coverage_pct is stamped whenever the shadow carries it;
    ranking_incomplete only when the scan set it."""
    if shadow.get("coverage_pct") is not None:
        rec["coverage_pct"] = shadow["coverage_pct"]
    if shadow.get("ranking_incomplete"):
        rec["ranking_incomplete"] = True
    return rec


def _is_non_trading_day(day: str) -> bool:
    try:
        if date.fromisoformat(day).weekday() >= 5:
            return True
    except ValueError:
        return False
    try:
        from src.trendbook.executor import _holidays
        return day in _holidays()
    except Exception:
        return False


def page_lost_night(day: str, cause: str, detail: str = "") -> bool:
    """Lost nights must page (2026-09-13: 08-25 and 09-02 'grouped daily
    unavailable' alarmed nothing). ONE ntfy line per (date, cause): the hourly
    catch-up sweep re-runs every shadow file of the last 14 days, so without
    dedup a lost date would page hourly for two weeks (the 09-08..09-12
    Labor-Day sweep incident). Quiet on weekends/holidays — a holiday marker
    is not a lost night. Returns True iff a page was sent."""
    if _is_non_trading_day(day):
        print(f"[rvolrank] {day} {cause}: non-trading day — quiet")
        return False
    if any((r.get("date"), r.get("cause")) == (day, cause) for r in read_jsonl(ALERT_LOG)):
        print(f"[rvolrank] {day} {cause}: already paged — quiet")
        return False
    print(f"[rvolrank] {day} {cause}: session LOST — paging")
    try:
        from src.engine.notify import send
        send("RVOL-RANK session LOST",
             f"{day}: {cause}. {detail} No sleeve record for this session; "
             f"it cannot be backfilled. Investigate Polygon/scan now.\n{STAMP}",
             priority="high", tags="rotating_light")
    except Exception as exc:
        print(f"[rvolrank] page failed: {exc}")
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a") as f:
        f.write(json.dumps({"date": day, "cause": cause,
                            "at": datetime.now().isoformat(timespec="seconds")}) + "\n")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--supersede", action="store_true",
                    help="append a corrected record for a date that already "
                         "has one (defect repair; ledger stays append-only, "
                         "readers take the LAST record per date)")
    args = ap.parse_args()
    day = args.date or date.today().isoformat()
    ledger = (LEDGER.with_name("rvolrank_sleeve_DRYRUN.jsonl")
              if args.dry_run else LEDGER)
    if (not args.supersede) and any(
            r.get("date") == day for r in read_jsonl(ledger)):
        print(f"{day} already in {ledger.name}; refusing double-write")
        return 0
    # Exit codes below are UNCHANGED (1 = not replayable; the catch-up sweep
    # relies on them); 2026-09-13 only added the page on each lost-night cause.
    # A --dry-run never pages and never writes the dedup marker (it would
    # otherwise silence the real 21:30 run for that date).
    def _page(cause: str, detail: str) -> bool:
        if args.dry_run:
            print(f"[rvolrank] (dry-run) would page {day} {cause}: {detail}")
            return False
        return page_lost_night(day, cause, detail)

    shadow_f = OBS_DIR / f"rvol_shadow-{day}.json"
    if not shadow_f.exists():
        print(f"no rvol_shadow output for {day} — market holiday or scan failure")
        _page("no_shadow_file", "The 14:10 shadow scan wrote nothing for this date.")
        return 1
    shadow = json.loads(shadow_f.read_text())
    if not shadow.get("top20"):
        # scan ran but produced no selection (holiday_or_no_data — e.g. feeds
        # cold at scan time). No live selection exists -> session is MISSED,
        # not backfillable; exit gracefully instead of tracebacking (2026-08-01).
        print(f"{day}: shadow output has no top20 ({list(shadow.keys())}) — "
              "no live selection captured; session missed, nothing to replay")
        _page("no_top20", f"Shadow file is a marker ({sorted(shadow.keys())}).")
        return 1
    sel_names = [x["ticker"] for x in shadow["top20"]]
    client = get_client()
    grouped = polygon_grouped(client, day)
    if grouped.empty:
        print("grouped daily unavailable")
        _page("grouped_unavailable",
              "Polygon grouped-daily returned no rows for the control lane.")
        return 1
    grouped["dv"] = grouped["c"] * grouped["v"]
    ctl_names = grouped.sort_values("dv", ascending=False).head(20)["T"].tolist()
    cache: dict = {}
    sel, sel_skips = run_lane(client, day, "selection", sel_names, cache)
    ctl, ctl_skips = run_lane(client, day, "control", ctl_names, cache)

    def summ(trs, skips):
        return {"trades": trs, "skips": skips, "n": len(trs),
                "pnl": round(sum(t["pnl"] for t in trs), 2),
                "mean_R": round(sum(t["r"] for t in trs) / len(trs), 4) if trs else None}

    rec = {"date": day, "stamp": STAMP,
           "generated": datetime.now().isoformat(timespec="seconds"),
           "criteria": "config/rvolrank_forward_criteria.yaml",
           **({"supersedes_earlier_record": True,
               "supersede_reason": "night-1 rate-limit defect (429s masked "
                                   "as no_bars)"} if args.supersede else {}),
           # 09:35 = IDEALIZED (delay-cost measurement); 09:45 = EXECUTABLE
           # (GATE PRIMARY). See forward-criteria amendment A1 (2026-07-23).
           "gate_primary_lane": "selection_exec (09:45)",
           "selection": summ(sel["09:35"], sel_skips),
           "control": summ(ctl["09:35"], ctl_skips),
           "selection_exec": summ(sel["09:45"], sel_skips),
           "control_exec": summ(ctl["09:45"], ctl_skips),
           # items 4-5 (measurement only): per-name earnings/gap/halt/cap/ADV
           "metadata": name_metadata(day, sel_names, grouped, cache)}
    # additive (2026-09-13): shadow coverage evidence — hygiene applied by readers
    stamp_shadow_quality(rec, shadow)

    # ITEM B — binding-constraint run-rate over GATE-VALID exec sessions.
    valid = []
    for r in read_jsonl(ledger):
        if r.get("date", "") >= "2026-07-20":  # window_start; pre-window excluded
            valid.append(r)
    byd = {r["date"]: r for r in valid}
    byd[day] = rec
    exec_trades = sum(v.get("selection_exec", {}).get("n", 0) for v in byd.values())
    nsess = len(byd)
    rate = exec_trades / nsess if nsess else 0
    proj = round((400 - exec_trades) / rate) if rate > 0 and exec_trades < 400 else 0
    rec["binding_constraint"] = {"exec_selection_trades_cum": exec_trades,
                                 "gate_valid_sessions": nsess,
                                 "trades_per_session_run_rate": round(rate, 1),
                                 "projected_sessions_to_400": proj,
                                 "hard_min": 400}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    se, ce = rec["selection_exec"], rec["control_exec"]
    print(f"[{STAMP}] {day} EXEC(09:45) sel n={se['n']} meanR={se['mean_R']} "
          f"pnl={se['pnl']} | ctl n={ce['n']} meanR={ce['mean_R']} pnl={ce['pnl']} "
          f"|| IDEAL(09:35) sel meanR={rec['selection']['mean_R']} "
          f"|| exec-cum {exec_trades}/400 @ {round(rate,1)}/sess -> "
          f"~{proj} more sessions | skips sel={len(sel_skips)} ctl={len(ctl_skips)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
