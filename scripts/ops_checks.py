"""Deterministic nightly ops checks — facts only, no judgment.

Emits one JSON document the nightly-ops agent interprets. Every section is
independently fault-isolated: a failing check reports its error string
instead of crashing the run.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── IBKR clientId allocation (single source of truth, 2026-09-29) ────────────
# ib_async scopes a gateway session per clientId; two components sharing one id
# means the second gets Error 326 and then a TimeoutError. Several components
# also retry on a +10/+20/+30 SHADOW family, so an id is only free if its whole
# family is free. Current allocation:
#
#   17              scripts/ibkr_smoke.py           (ad-hoc smoke test)
#   23 /33/43/53    src/trendbook/executor.py       (CLIENT_ID + family)
#   42 /52/62/72    src/data/ibkr_orders.py         (DEFAULT_CLIENT_ID + family)
#                     -> the live options engine, and src/api/reconcile.py in
#                        the cockpit process (both via get_order_client())
#   61              src/rvol_exec/constants.py      (RVOL-EXEC lane, NO family)
#   161             scripts/ops/rvol_exec_go_nogo.py (probes)
#   91 /101/111/121 THIS FILE                       (nightly ops checks)
#
# Why 91: the nightly checks run at ~18:40 PT while the engine still holds 42
# (it re-activates at 13:10 post-close and keeps the socket into the evening).
# Sharing 42 is exactly what broke checks-2026-09-{21,22,23,25,28}.err — every
# one ends in `Error 326 ... client id is already in use` -> TimeoutError, so
# position_reconcile lost its broker fields on five consecutive nights.
OPS_CHECKS_CLIENT_ID = 91


def section(fn):
    try:
        return fn()
    except Exception as exc:
        return {"check_error": str(exc)}


def agent_state():
    state = json.loads((ROOT / "data" / "agent_state.json").read_text())
    pid = state.get("pid")
    alive = False
    if pid:
        try:
            os.kill(int(pid), 0)
            alive = True
        except OSError:
            pass
    return {"status": state.get("status"), "pid": pid, "alive": alive,
            "cycle_count": state.get("cycle_count"), "last_cycle": state.get("last_cycle"),
            "health_fail_streak": state.get("health_fail_streak", 0)}


def log_errors():
    lines = (ROOT / "logs" / "agent.log").read_text().splitlines()
    today = datetime.now().strftime("%Y-%m-%d")
    errs = [l for l in lines if l.startswith(today) and ("ERROR" in l or "cycle failed" in l)]
    warns = [l for l in lines if l.startswith(today) and "WARNING" in l]
    return {"errors_today": len(errs), "warnings_today": len(warns),
            "samples": (errs + warns)[-5:]}


def observation(date):
    path = ROOT / "logs" / "observation" / f"{date}.jsonl"
    if not path.exists():
        return {"exists": False}
    records = [json.loads(l) for l in path.read_text().splitlines()]
    rec_status = {}
    for r in records:
        for e in r.get("recommendations", []):
            key = ("posted" if "rec_id" in e else "vetoed" if "vetoed" in e
                   else str(e.get("skipped")))
            rec_status[key] = rec_status.get(key, 0) + 1
    return {"exists": True, "cycles": len(records),
            "first_ts": records[0]["ts"] if records else None,
            "last_ts": records[-1]["ts"] if records else None,
            "candidate_decisions": rec_status,
            "degraded_cycles": sum(1 for r in records if r.get("regime", {}).get("degraded"))}


def fills(date):
    path = ROOT / "logs" / "observation" / "fills.jsonl"
    if not path.exists():
        return {"exists": False}
    events = [json.loads(l) for l in path.read_text().splitlines()]
    today = [e for e in events if e.get("ts", "").startswith(date) and not e.get("deprecated")]
    rts = [e for e in today if e.get("event") in ("paper_exit", "round_trip") and
           (e.get("round_trip") or e.get("event") == "round_trip")]
    # L16: aggregate pnl_total (per-contract x qty), never bare per-contract —
    # summing pnl_per_contract under-reported every day with qty > 1 fills.
    pnls = [e["pnl_total"] if e.get("pnl_total") is not None
            else (e.get("pnl_per_contract") or 0) * int(e.get("quantity") or 1)
            for e in rts
            if e.get("pnl_total") is not None or e.get("pnl_per_contract") is not None]
    # 2026-10-15: a partial entry books as `partial_fill_booked` and never also
    # as `paper_fill` (verified mutually exclusive, 17 of 17 rows all-time), so
    # counting only `paper_fill` under-reported every day with a partial — e.g.
    # 10-07 read 1 entry against 2. `partial_fill_detected` is the detection
    # stage, not a booking, and is deliberately NOT counted.
    ENTRY_EVENTS = ("paper_fill", "partial_fill_booked")
    return {"events_today": len(today),
            "entries_today": sum(1 for e in today if e["event"] in ENTRY_EVENTS),
            "partial_entries_today": sum(
                1 for e in today if e["event"] == "partial_fill_booked"),
            "round_trips_today": len(rts),
            "expired_entries_today": sum(1 for e in today if e["event"] == "entry_expired"),
            "realized_pnl_today": round(sum(pnls), 2) if pnls else 0.0,
            "exit_reasons": sorted({e.get("exit_reason") or e.get("close_reason", "")[:20] for e in rts})}


def book():
    # LANE REPOINT (2026-08-10): read the LANE books, not the retired shared
    # ones — this reported "0 pending / 0 open" against a live book.
    from src.engine.paper_trader import load_positions, _load, PENDING_FILE
    orders = _load(PENDING_FILE)
    positions = load_positions()
    return {"pending_orders": len(orders), "open_positions": len(positions),
            "open_position_tickers": [p.get("ticker") for p in positions]}


def labels(date):
    path = ROOT / "logs" / "observation" / f"labels-{date}.jsonl"
    if not path.exists():
        return {"exists": False}
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    cands = [r for r in rows if r["kind"] == "candidate" and r.get("cf_eod") is not None]
    by_status = {}
    for c in cands:
        by_status.setdefault(c["status"], []).append(c["cf_eod"])
    return {"exists": True, "snapshots": sum(1 for r in rows if r["kind"] == "snapshot"),
            "candidates": len(cands),
            "mean_cf_eod_by_status": {k: round(sum(v) / len(v), 5) for k, v in by_status.items()}}


def calibration():
    path = ROOT / "logs" / "research" / "calibration_history.jsonl"
    if not path.exists():
        return {"exists": False}
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    return {"latest": rows[-1], "runs": len(rows)}


def batch_verdicts(date):
    path = ROOT / "logs" / "observation" / f"batch_verdicts-{date}.jsonl"
    if not path.exists():
        return {"exists": False}
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    ok = [r for r in rows if r.get("verdict")]
    return {"exists": True, "n": len(rows), "parsed": len(ok),
            "approvals": sum(1 for r in ok if r["verdict"].get("approve"))}


def iv_coverage(date):
    import yaml
    expected = len(yaml.safe_load((ROOT / "config" / "config.yaml").read_text())["watchlist"]["default"])
    iv_dir = ROOT / "data" / "iv_history"
    have = 0
    for p in iv_dir.glob("*.csv"):
        if date in p.read_text():
            have += 1
    return {"tickers_with_today_row": have, "expected": expected}


def claude_budget():
    path = ROOT / "data" / "claude_budget.json"
    if not path.exists():
        return {"exists": False}
    b = json.loads(path.read_text())
    return {"date": b.get("date"), "calls": b.get("calls"),
            "verdicts_stored": len(b.get("verdicts", {}))}


def launchd_tails():
    out = {}
    for name in ["launchd_labeler", "launchd_batchreview", "launchd_ivsnapshot", "launchd_snapshot"]:
        p = ROOT / "logs" / f"{name}.log"
        out[name] = p.read_text().splitlines()[-3:] if p.exists() else "no log yet"
    return out


def store_freshness():
    import pandas as pd
    out = {}
    for suffix in ["_5m", "_1h", ""]:
        p = ROOT / "data" / "historical" / f"SPY{suffix}.parquet"
        if p.exists():
            out[suffix or "_1d"] = str(pd.read_parquet(p).index.max())
    return out


def fd_count():
    import subprocess
    state = json.loads((ROOT / "data" / "agent_state.json").read_text())
    pid = state.get("pid")
    alive = False
    if pid:
        try:
            os.kill(int(pid), 0)
            alive = True
        except OSError:
            pass
    out = {"pid": pid, "alive": alive, "fd_count": None, "maxfiles_soft_limit": None}
    if alive:
        lsof = subprocess.run(["lsof", "-p", str(pid)], capture_output=True,
                              text=True, timeout=60)
        lines = lsof.stdout.splitlines()
        out["fd_count"] = max(len(lines) - 1, 0)  # minus header row
    # read the SERVICE's own limit (the plist sets NumberOfFiles=16384); the
    # global `launchctl limit maxfiles` (256) misled the 07-10 weekly review
    # into a false L9-regression alarm after the 07-09 restart.
    svc = subprocess.run(["launchctl", "print",
                          f"gui/{os.getuid()}/com.optionsagent.trader"],
                         capture_output=True, text=True, timeout=10)
    for line in svc.stdout.splitlines():
        if "maxfiles (soft)" in line:
            out["maxfiles_soft_limit"] = line.split("=>")[-1].strip()
            break
    else:
        lim = subprocess.run(["launchctl", "limit", "maxfiles"], capture_output=True,
                             text=True, timeout=10)
        parts = lim.stdout.split()
        if len(parts) >= 2 and parts[0] == "maxfiles":
            out["maxfiles_soft_limit"] = parts[1] + " (global fallback)"
    return out


def store_freshness_alert():
    import numpy as np
    import pandas as pd
    hist = ROOT / "data" / "historical"
    timeframes = {"_5m": "5m", "_30m": "30m", "_1h": "1h", "": "1d"}
    expected = np.busday_offset(datetime.now().date(), 0, roll="backward")
    out = {"last_expected_trading_day": str(expected), "timeframes": {},
           "any_stale": False}
    for p in sorted(hist.glob("*.parquet")):
        stem = p.stem
        tf = "1d"
        for suffix, name in timeframes.items():
            if suffix and stem.endswith(suffix):
                tf = name
                break
        latest = pd.read_parquet(p, columns=[]).index.max()  # index only
        cur = out["timeframes"].get(tf)
        if cur is None or latest > cur["_max"]:
            out["timeframes"][tf] = {"_max": latest, "freshest_file": p.name}
    for tf, info in out["timeframes"].items():
        freshest = pd.Timestamp(info.pop("_max")).date()
        behind = int(np.busday_count(freshest, expected))
        # tightened 3 -> 1 (gap analysis 2026-07-09): a permanent 2-day lag was
        # silently degrading research counterfactuals without ever alarming.
        # behind == 1 is normal (today's bars land after the close refresh).
        info.update({"freshest_bar_date": str(freshest),
                     "trading_days_behind": behind, "stale": behind > 1})
        out["any_stale"] = out["any_stale"] or info["stale"]
    return out


def persistent_cloud_locks(err_dir=None, nights=3):
    """A Drive-locked path is EXCLUDED and the night continues (2026-10-20
    design: one stuck file must not cost the whole offsite copy). That absorbs
    a TRANSIENT lock correctly but says nothing about the same path stuck for
    DAYS: data/trendbook/state.json was missing from the cloud copy on five
    consecutive nights (10-21..10-25) while every receipt read a reassuring
    PARTIAL(1 locked), so a permanent offsite hole looked identical to a
    one-night blip. Count the trailing streak per path and name anything locked
    `nights` nights running (threshold pre-stated at 3). Reporting only —
    exclude-and-continue and the exit code are deliberately unchanged.

    `since` is the OLDEST night with evidence, not proven onset: only ~6 days of
    `.err` receipts survive on disk, and the real streak was already running on
    the oldest one (2026-10-20), so a streak that equals `nights_seen` is a
    lower bound and the true onset is unknowable from here."""
    import re

    lock_re = re.compile(
        r"error:\s*(?P<path>.+?):\s*mmap:\s*Resource deadlock avoided")
    name_re = re.compile(r"backup_cloud-(\d{4}-\d{2}-\d{2})\.err\Z")
    # Sidecar lines (backup_research_record.sh:106/109/112). The source capture
    # is non-greedy and the destination unconstrained so the dated fallback
    # (`-> state.json.sidecar-2026-11-06 ok`) reads the same as the plain one.
    sc_ok_re = re.compile(
        r"^sidecar:\s*(?P<path>.+?)\s*->\s*\S+\s+ok\s*$", re.MULTILINE)
    sc_fail_re = re.compile(
        r"^sidecar:\s*(?P<path>.+?)\s+FAILED", re.MULTILINE)
    d = Path(err_dir) if err_dir else ROOT / "logs" / "ops"
    per_night = {}
    for f in sorted(d.glob("backup_cloud-*.err")):
        m = name_re.match(f.name)
        if not m:
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        per_night[m.group(1)] = {
            "locked": {mm.group("path") for mm in lock_re.finditer(text)},
            "ok": {mm.group("path") for mm in sc_ok_re.finditer(text)},
            "failed": {mm.group("path") for mm in sc_fail_re.finditer(text)},
        }

    # A night the cloud leg ran is a night that has an .err file (it is written
    # even when empty — 2026-10-20 is 0 B), so the streak walks the dates that
    # are present, newest first. A night with no file is not evidence either way.
    dates = sorted(per_night, reverse=True)
    if not dates:
        return {"nights_threshold": nights, "nights_seen": 0,
                "persistent": [], "offsite_holes": [], "alert": False}
    latest = dates[0]
    persistent = []
    holes = []
    # A locked path is only an offsite HOLE if the content did not reach the
    # cloud another way. Since 2026-11-02 the writer copies each stuck path to
    # `<path>.sidecar`, so "locked N nights running" stopped meaning "missing
    # offsite" — the first rsync still fails every night, forever. Two branches,
    # agreed before the data (evening 2026-11-06 design block in the queue):
    #   alert = (streak >= nights AND the latest night has no sidecar-ok)
    #           OR (the latest night has a sidecar FAILED for that path)
    # A FAILED sidecar alerts IMMEDIATELY because it is affirmative evidence of
    # a hole. An ABSENT sidecar line keeps the 3-night threshold: it is the
    # normal reading when the stuck path is not in staging at all
    # (backup_research_record.sh `[ -f "$STAGING/$stuck" ] || continue`), and it
    # is also what every pre-sidecar night looks like.
    for p in sorted({q for s in per_night.values() for q in s["locked"]}):
        n = 0
        for date in dates:
            if p not in per_night[date]["locked"]:
                break  # a clean night ends the streak: the lock released
            n += 1
        if p in per_night[latest]["failed"]:
            sidecar = "failed"
        elif p in per_night[latest]["ok"]:
            sidecar = "ok"
        else:
            sidecar = "absent"
        if n >= nights:
            persistent.append({"path": p, "nights": n, "latest": latest,
                               "since": dates[n - 1], "sidecar_latest": sidecar})
        if sidecar == "failed" or (n >= nights and sidecar != "ok"):
            holes.append({"path": p, "nights": n, "sidecar_latest": sidecar})
    return {"nights_threshold": nights, "nights_seen": len(dates),
            "persistent": persistent, "offsite_holes": holes,
            "alert": bool(holes)}


def backup_freshness():
    """Gap-analysis item 2 (2026-07-09; two-stage 2026-07-10 after TCC denied
    launchd writes to CloudStorage). STALE (alarm) = staging failed or > 36h old
    — the local safety copy is non-negotiable. cloud_ok is reported separately:
    off-machine durability, satisfied by stage-2 rsync OR the Drive app syncing
    the staging folder (then stage-2 'FAILED' under launchd is expected)."""
    out = {"receipt": None, "hours_since_backup": None,
           "stale": None, "cloud_ok": None,
           "cloud_locks": persistent_cloud_locks()}
    receipt = ROOT / "logs" / "ops" / "last_backup.json"
    if receipt.exists():
        r = json.loads(receipt.read_text())
        out["receipt"] = r
        age_h = (datetime.now() - datetime.fromisoformat(r["ts"])).total_seconds() / 3600
        out["hours_since_backup"] = round(age_h, 1)
        staging_ok = r.get("staging", r.get("status")) == "ok"
        out["stale"] = (not staging_ok) or age_h > 36
        cloud_dir = r.get("cloud_dir") or r.get("dest")
        if cloud_dir:
            try:
                mtime = datetime.fromtimestamp(
                    (Path(cloud_dir) / "last_backup.json").stat().st_mtime)
                out["cloud_ok"] = (datetime.now() - mtime).total_seconds() < 48 * 3600
            except OSError:
                out["cloud_ok"] = False
    else:
        out["stale"] = True
    return out


def stale_code():
    """Lesson L14 (extended 2026-07-14): a long-running process runs the code
    it was STARTED with; a commit does not take effect until restart. Check
    EVERY long-running consumer — the trader (reads src/) AND the cockpit API
    (imports src/ + scripts/; found running 4-day-old evidence math after the
    07-11 window resets because only the trader was ever restarted)."""
    import subprocess
    from datetime import datetime

    def _start_epoch(pid):
        ls = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=10).stdout.strip()
        if not ls:
            return None, None
        return ls, datetime.strptime(" ".join(ls.split()), "%a %b %d %H:%M:%S %Y").timestamp()

    def _commit_epoch(*paths):
        ct = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%ct", "--", *paths],
                            capture_output=True, text=True, timeout=10).stdout.strip()
        return int(ct) if ct else 0

    state = json.loads((ROOT / "data" / "agent_state.json").read_text())
    pg = subprocess.run(["pgrep", "-f", "uvicorn src.api.server"],
                        capture_output=True, text=True, timeout=10).stdout.split()
    procs = {"trader": (state.get("pid"), ("src",)),
             "cockpitapi": (int(pg[0]) if pg else None, ("src", "scripts"))}
    out = {"stale": False}
    for name, (pid, paths) in procs.items():
        row = {"pid": pid, "proc_started": None, "latest_code_commit": None, "stale": None}
        if pid:
            ls, proc_epoch = _start_epoch(pid)
            commit_epoch = _commit_epoch(*paths)
            row["proc_started"] = ls
            row["latest_code_commit"] = (datetime.fromtimestamp(commit_epoch)
                                         .isoformat(timespec="seconds") if commit_epoch else None)
            if proc_epoch:
                row["stale"] = bool(commit_epoch) and proc_epoch < commit_epoch
                out["stale"] = out["stale"] or row["stale"]
        out[name] = row
    return out


def forward_acceptance():
    sys.path.insert(0, str(ROOT / "scripts"))
    from forward_acceptance_check import evaluate
    return evaluate(ROOT)


def _expectation_target_day(due_after: str, now=None):
    """The trading day this expectation is asserted against: today once
    matured (past due_after on a weekday), else the previous weekday."""
    import numpy as np
    now = now or datetime.now()
    today = now.date()
    is_bday = bool(np.is_busday(today))
    matured = now.strftime("%H:%M") >= due_after
    if is_bday and matured:
        return str(today)
    base = today if is_bday else np.busday_offset(today, 0, roll="backward")
    return str(np.busday_offset(base, -1, roll="backward")) if is_bday \
        else str(np.busday_offset(today, 0, roll="backward"))


def evaluate_expectation(entry: dict, root, now=None) -> dict:
    """Pure engine for one manifest entry — returns {name, target_day, ok, detail}."""
    day = _expectation_target_day(entry.get("due_after", "00:00"), now)
    path = root / entry["path"].format(date=day)
    kind = entry.get("type", "jsonl_min_rows")
    out = {"name": entry["name"], "target_day": day, "ok": False, "detail": None}
    try:
        if kind == "ledger_row_for_date":
            where = entry.get("where") or {}
            rows = 0
            for l in path.read_text().splitlines():
                r = json.loads(l)
                if (r.get(entry["date_field"], "")[:10] == day
                        and all(r.get(k) == v for k, v in where.items())):
                    rows += 1
            out["ok"] = rows >= 1
            out["detail"] = f"{rows} rows for {day}"
        elif kind == "rvol_shadow_scan":
            # The shadow scan's failure mode is a well-formed 87-byte MARKER
            # ({date, scan_failed, attempts, at}), not a missing or empty file,
            # so `min_rows` is satisfied by the failure itself — it scored
            # 10-23/10-26/10-27 ok while the sleeve paged `no_top20`. Assert on
            # the PAYLOAD instead: scan_failed, or a short/absent top20.
            doc = json.loads(path.read_text())
            if not isinstance(doc, dict):
                out["detail"] = f"not a JSON object ({type(doc).__name__})"
            else:
                top = doc.get("top20")
                n = len(top) if isinstance(top, list) else 0
                need = entry.get("min_names", 1)
                if doc.get("scan_failed"):
                    out["detail"] = ("SCAN FAILED after "
                                     f"{doc.get('attempts')} attempts — "
                                     f"failure marker only, {n} ranked names")
                elif n < need:
                    out["detail"] = f"top20 has {n} names (need {need})"
                else:
                    out["ok"] = True
                    out["detail"] = f"top20 has {n} names"
        elif kind == "exit_mark_series_ratio":
            exits = [json.loads(l) for l in path.read_text().splitlines()
                     if '"paper_exit"' in l]
            exits = [e for e in exits if str(e.get("ts", "")).startswith(day)]
            if not exits:
                out["ok"] = True  # no exits that day — nothing to cover
                out["detail"] = "no exits"
            else:
                with_path = sum(1 for e in exits
                                if (e.get("round_trip") or {}).get("mark_series"))
                ratio = with_path / len(exits)
                out["ok"] = ratio >= entry["min_ratio"]
                out["detail"] = f"{with_path}/{len(exits)} exits pathed ({ratio:.0%})"
        else:  # jsonl_min_rows
            n = 0
            where = entry.get("where") or {}
            for l in path.read_text().splitlines():
                r = json.loads(l)
                if r.get("dry_run_machinery_test"):
                    continue
                if all(r.get(k) == v for k, v in where.items()):
                    n += 1
            out["ok"] = n >= entry["min_rows"]
            out["detail"] = f"{n} rows (need {entry['min_rows']})"
    except FileNotFoundError:
        out["detail"] = "FILE MISSING"
    except Exception as exc:
        out["detail"] = f"check error: {exc}"
    return out


def collection_health():
    """Pillar 1 (2026-07-14): every evidence stream declares its healthy-day
    output in config/collection_expectations.yaml; silence becomes an alarm."""
    import yaml
    manifest = yaml.safe_load(
        (ROOT / "config" / "collection_expectations.yaml").read_text())
    results = [evaluate_expectation(e, ROOT) for e in manifest["expectations"]]
    return {"all_ok": all(r["ok"] for r in results),
            "failing": [r for r in results if not r["ok"]],
            "results": results}


def api_sections_healthy():
    """Pillar 3: the cockpit's fault isolation keeps the page up when a
    section throws — this check makes the swallowed error un-ignorable."""
    import urllib.request
    out = {"reachable": False, "error_sections": []}
    try:
        for ep in ("clocks", "feed", "sleeves"):
            with urllib.request.urlopen(f"http://127.0.0.1:8421/api/{ep}", timeout=10) as r:
                data = json.loads(r.read())
            out["reachable"] = True
            for k, v in data.items():
                if isinstance(v, dict) and "error" in v:
                    out["error_sections"].append(f"{ep}.{k}: {str(v['error'])[:120]}")
    except Exception as exc:
        out["unreachable_reason"] = str(exc)[:120]
    out["healthy"] = out["reachable"] and not out["error_sections"]
    return out


def broker_canary():
    """Pillar 5: trend the sandbox's failure modes instead of meeting them
    one anecdote at a time — order rejects, backend 500s, fill-guard hits."""
    import numpy as np
    days = [str(np.busday_offset(datetime.now().date(), -i, roll="backward"))
            for i in range(5)][::-1]
    log = (ROOT / "logs" / "agent.log")
    lines = log.read_text().splitlines()[-20000:] if log.exists() else []
    fills = [json.loads(l) for l in
             (ROOT / "logs" / "observation" / "fills.jsonl").read_text().splitlines()]
    out = {}
    for d in days:
        dl = [l for l in lines if l.startswith(d)]
        out[d] = {
            "order_rejected": sum("ended: rejected" in l for l in dl),
            "close_rejected_500": sum("paper close rejected" in l for l in dl),
            "fill_guard_hits": sum(1 for f in fills
                                   if str(f.get("ts", "")).startswith(d) and f.get("fill_guard")),
            "entry_expired": sum(1 for f in fills
                                 if str(f.get("ts", "")).startswith(d)
                                 and f.get("event") == "entry_expired"),
        }
    today = out.get(days[-1], {})
    return {"by_day": out,
            "today_total": sum(today.values()),
            "note": "rising trend = broker reliability case for the IBKR migration"}


def position_reconcile():
    """L20: two-sided book-vs-broker check. adopt_foreign=false already
    ignores broker-only positions (user's manual trades — out of scope);
    this closes the OTHER side: book positions whose legs the broker no
    longer holds (2026-07-14: sandbox silently deleted two overnight
    positions; discovered only via a 59-reject afternoon)."""
    # BROKER RESOLUTION (2026-08-10): was hardcoded to the Tradier sandbox
    # while the configured broker is ibkr_paper — see src/api/reconcile.py.
    # CLIENTID COLLISION FIX (2026-09-29): get_order_client() hands back the
    # engine's IbkrOrderClient defaults — clientId 42 — which the live engine
    # already holds all evening, so this check timed out every night since
    # 09-21. Build our OWN read-only client on the dedicated id instead, and
    # release it afterwards. Under pytest we keep the factory path untouched:
    # order_client.py:123-132 deliberately returns a Tradier stub there, and
    # instantiating IbkrOrderClient unconditionally would aim the suite at the
    # live gateway (the 2026-08-05 hermeticity failure).
    from src.data.order_client import get_order_client, _resolve_broker
    _hermetic = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
    _own_client = False
    if not _hermetic and _resolve_broker() == "ibkr_paper":
        from src.data.ibkr_orders import IbkrOrderClient
        client = IbkrOrderClient(client_id=OPS_CHECKS_CLIENT_ID)
        _own_client = True
    else:
        client = get_order_client()
    out = {"broker_reachable": False, "diverged": [], "broker_lost_flags": [],
           "client_id": getattr(client, "client_id", None)}
    from src.engine.paper_trader import load_positions   # LANE REPOINT 2026-08-10
    book = load_positions()
    out["broker_lost_flags"] = [p["rec_id"] for p in book if p.get("broker_lost")]
    try:
        # REACHABLE MUST MEAN VERIFIED (2026-08-22). This marked reachable on
        # any non-exception, and get_positions() returned [] when the client
        # was unavailable — so a dead broker read as healthy for three straight
        # sessions. A failed read is UNKNOWN, never "reachable and empty".
        raw = client.get_positions()
        if raw is None:
            out["broker_reachable"] = False
            out["error"] = "broker position read FAILED (not an empty account)"
            return out
        broker = {p["symbol"]: float(p["quantity"]) for p in raw}
        out["broker_reachable"] = True
    except Exception as exc:
        out["error"] = str(exc)[:120]
        return out
    finally:
        # release OUR socket so id 91 is free for the next run; never touch the
        # shared singleton the factory owns.
        if _own_client:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    for p in book:
        for leg in p["legs"]:
            want = p.get("quantity", 1) * (1 if leg["action"] == "buy_to_open" else -1)
            have = broker.get(leg["symbol"], 0.0)
            if want != have:
                out["diverged"].append(
                    {"rec_id": p["rec_id"], "leg": leg["symbol"],
                     "book": want, "broker": have})
    out["ok"] = out["broker_reachable"] and not out["diverged"] \
        and not out["broker_lost_flags"]
    return out


def gate_learning():
    """The self-learning loop's cumulative scoreboard (user 2026-07-15):
    Claude verdict categories vs gate-free twins at market truth. Accumulates
    only — rule/prompt changes require a pre-registered proposal at n>=30."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import gate_learning as gl
    ready = gl.readiness()
    return {"scoreboard": gl.scoreboard(), "readiness": ready,
            "proposal_warranted": [k for k, v in ready.items()
                                   if v.get("proposal_warranted")]}


def dcexecq():
    """DC execution-quality collection health (user 2026-07-14: 'make sure
    that measurement effort continuous daily') — per-cell cumulative sample
    counts vs the >=40/cell evaluation gate, last run date, last skips."""
    led = ROOT / "logs" / "dcexecq"
    files = sorted(led.glob("2026-*.jsonl")) if led.exists() else []
    out = {"last_run": files[-1].stem if files else None, "days_collected": len(files),
           "cells": {}, "last_skips": None}
    for f in files:
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if r.get("dry_run_machinery_test"):
                continue
            c = out["cells"].setdefault(r["struct"], {"n": 0, "errors": 0})
            c["n"] += 1
            if (r.get("summary") or {}).get("error"):
                c["errors"] += 1
    log = ROOT / "logs" / "launchd_dcexecq.log"
    if log.exists():
        skips = [l for l in log.read_text().splitlines() if "skips=" in l]
        out["last_skips"] = skips[-1].split("skips=")[-1] if skips else None
    return out


def scalp_forward():
    """SCALP-candidate gate progress (user 2026-07-14: periodic positioning
    check toward mainstream promotion) — same daily visibility as the funnel."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import scalp_forward_check as sfc
    res = sfc.evaluate()
    # SCALP-v2 (2026-09-12): the nightly sweep is the only thing that runs the
    # trackers unattended; before this the v2 tracker had NO production caller
    try:
        res["v2"] = sfc.evaluate(v2=True)
    except Exception as exc:  # noqa: BLE001
        res["v2"] = {"error": f"{type(exc).__name__}: {exc}"}
    # SCALP-v3 (activated 2026-09-25; O-32 open item 2): the nightly report must
    # carry the LIVE window, or v3's first sessions are invisible exactly as
    # v2's were on 09-14.
    try:
        res["v3"] = sfc.evaluate(v3=True)
    except Exception as exc:  # noqa: BLE001
        res["v3"] = {"error": f"{type(exc).__name__}: {exc}"}

    def _shape(r):
        return {"window_start": r.get("window_start"), "n_trades": r.get("n_trades"),
                "total_pnl": r.get("total_pnl"), "excluded": r.get("excluded"),
                "checks": {k: {"value": v.get("value"), "pass": v.get("pass")}
                           for k, v in (r.get("checks") or {}).items()},
                "all_pass": r.get("all_pass"), "error": r.get("error")}
    out = _shape(res)
    # 2026-09-14: res["v2"] was computed above and then DROPPED from the returned
    # dict — the nightly checks JSON never carried a v2 line, so the v2 window's
    # first sessions were invisible to the nightly report. Both trackers now ship,
    # plus the v1->v2 mechanics telemetry (fill rate, cost, concentration, truth).
    out["v2"] = _shape(res["v2"])
    out["v3"] = _shape(res["v3"])
    out["active_candidate"] = sfc.active_candidate()
    out["v2_mechanics"] = section(_v2_mechanics_summary)
    return out


def _v2_mechanics_summary():
    import scalp_v2_mechanics as svm
    import scalp_forward_check as sfc
    import forward_acceptance_check as fac
    c = sfc.load_active_criteria()          # v3 while its window is open (O-32)
    enrichment = fac.load_enrichment(ROOT) if c.get("scoring") == "market_mid_v1" else {}
    today = datetime.now().strftime("%Y-%m-%d")
    r = svm.mechanics(ROOT / c["scope"]["ledger"], c,
                      lambda e: fac.market_truth_pnl(e, enrichment), today=today)
    today_row, latest = _pick_day(r["days"], today)
    return {"summary": svm.summary_line(r), "cumulative": r["cumulative"],
            "as_of": today, "today": today_row, "latest_active_day": latest,
            "predictions": r["predictions"]}


def _pick_day(days, today):
    """Split the per-day rows into (today's row, the latest ACTIVE row).

    The old single `today` key was `days[-1]`, i.e. the last day the ledger
    moved — it read 2026-10-08 every night from 10-08 to 11-04, which a run
    could mistake for today's mechanics. `today` is now None on a day with no
    activity; the old value keeps its usefulness under an honest name.
    Day `date` values are strings (see the stale-pending sum in
    scalp_v2_mechanics.mechanics), so an equality match is correct here.
    """
    if not days:
        return None, None
    return next((d for d in days if d.get("date") == today), None), days[-1]


def trendexec():
    """Track C IBKR paper executor vs shadow manager (criteria frozen
    2026-07-06: unexplained_position_diffs must be 0). Checks (a) the morning
    exec record exists on trading days once 07:05 PT has passed, (b) for the
    latest date holding BOTH an exec and a daily record, the executor's
    targets equal the shadow's independently-computed targets bit-for-bit,
    (c) the exec record carries no unexplained diffs."""
    led = ROOT / "logs" / "trendbook" / "ledger.jsonl"
    out = {"ok": True, "present_today": None, "targets_match": None,
           "diffs": [], "compared_date": None}
    if not led.exists():
        return {"ok": False, "error": "no trendbook ledger"}
    execs, dailies, skips = {}, {}, {}
    for line in led.read_text().splitlines():
        r = json.loads(line)
        if r.get("record") == "exec":
            execs[r["date"]] = r
        elif r.get("record") == "daily":
            dailies[r["date"]] = r
        elif r.get("record") == "exec_skipped":
            skips[r["date"]] = r.get("reason")
    today = datetime.now().strftime("%Y-%m-%d")
    now_hm = datetime.now().strftime("%H:%M")
    import yaml as _y
    hol = set((_y.safe_load((ROOT / "config" / "config.yaml").read_text())
               ["agent"].get("holidays_2026", [])))
    is_trading = datetime.now().weekday() < 5 and today not in hol
    if is_trading and now_hm >= "07:05":
        if not execs and not skips:
            out["present_today"] = None    # commissioning: no first run yet
            out["note"] = "no exec records yet — first run pending"
        else:
            out["present_today"] = today in execs
            if not out["present_today"]:
                out["ok"] = False
                out["skip_reason"] = skips.get(today)
    common = sorted(set(execs) & set(dailies))
    if common:
        d = common[-1]
        out["compared_date"] = d
        ex, dy = execs[d], dailies[d]
        shadow_targets = {c: v["target"] for c, v in dy["legs"].items()}
        out["targets_match"] = ex["targets"] == shadow_targets
        out["diffs"] = ex.get("intended_vs_executed_diffs", [])
        if not out["targets_match"] or out["diffs"]:
            out["ok"] = False
            if not out["targets_match"]:
                out["target_mismatch"] = {
                    c: {"exec": ex["targets"].get(c), "shadow": shadow_targets.get(c)}
                    for c in set(ex["targets"]) | set(shadow_targets)
                    if ex["targets"].get(c) != shadow_targets.get(c)}
    return out


DECAY_TRIPWIRE_THRESHOLD = -150.0  # PROVISIONAL, purely descriptive — not a
                                    # rule/gate change; flags a lane worth a
                                    # human look, same spirit as the other
                                    # flags in this file (item 4, 2026-07-19
                                    # instrumentation batch, see
                                    # docs/external_edge_map.md)


def _enriched_last_row_per_rec() -> list[dict]:
    """logs/observation/enriched_trades_v1.jsonl, last row per rec_id wins
    (re-enrichment can flip a row from no_market_data -> ok) — same pattern
    as gate_learning.market_truth_map(). Order preserved by each rec_id's
    FIRST appearance in the file, which tracks the chronological order
    enrich_trades_thetadata.py processes exits in (it reads fills.jsonl
    top-to-bottom); re-enriched rows update values in place without moving
    position."""
    path = ROOT / "logs" / "observation" / "enriched_trades_v1.jsonl"
    if not path.exists():
        return []
    order, last = [], {}
    for line in path.read_text().splitlines():
        r = json.loads(line)
        rid = r.get("rec_id")
        if rid is None:
            continue
        if rid not in last:
            order.append(rid)
        last[rid] = r
    return [last[rid] for rid in order]


def decay_tripwire():
    """Item 4 of the 2026-07-19 observation-instrumentation batch. Rolling
    mean true_pnl_mid (market-truth P&L, L21 ThetaData enrichment) of the
    last 20 FUNNEL and last 20 SCALP round trips, scored independently per
    the standing SCALP-is-its-own-stream rule. Flags {lane, breach: true}
    when a lane's rolling mean falls below DECAY_TRIPWIRE_THRESHOLD — a
    provisional descriptive threshold, not an evidence rule or gate."""
    rows = _enriched_last_row_per_rec()
    ok = [r for r in rows if r.get("status") == "ok" and r.get("true_pnl_mid") is not None]
    out = {}
    for lane in ("funnel", "scalp"):
        lane_rows = [r for r in ok if r.get("sleeve") == lane][-20:]
        pnls = [r["true_pnl_mid"] for r in lane_rows]
        mean = round(sum(pnls) / len(pnls), 2) if pnls else None
        out[lane] = {
            "lane": lane,
            "n": len(pnls),
            "rolling_mean_true_pnl_mid": mean,
            "threshold": DECAY_TRIPWIRE_THRESHOLD,
            "breach": bool(mean is not None and mean < DECAY_TRIPWIRE_THRESHOLD),
        }
    return out


def main():
    date = datetime.now().strftime("%Y-%m-%d")
    checks = {
        "date": date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "agent": section(agent_state),
        "log_errors": section(log_errors),
        "observation": section(lambda: observation(date)),
        "fills": section(lambda: fills(date)),
        "book": section(book),
        "labels": section(lambda: labels(date)),
        "calibration": section(calibration),
        "batch_verdicts": section(lambda: batch_verdicts(date)),
        "iv_coverage": section(lambda: iv_coverage(date)),
        "claude_budget": section(claude_budget),
        "launchd": section(launchd_tails),
        "store_freshness": section(store_freshness),
        "fd_count": section(fd_count),
        "stale_code": section(stale_code),
        "backup_freshness": section(backup_freshness),
        "store_freshness_alert": section(store_freshness_alert),
        "forward_acceptance": section(forward_acceptance),
        "scalp_forward": section(scalp_forward),
        "dcexecq": section(dcexecq),
        "collection_health": section(collection_health),
        "api_sections_healthy": section(api_sections_healthy),
        "broker_canary": section(broker_canary),
        "position_reconcile": section(position_reconcile),
        "gate_learning": section(gate_learning),
        "trendexec": section(trendexec),
        "decay_tripwire": section(decay_tripwire),
    }
    print(json.dumps(checks, indent=1, default=str))


if __name__ == "__main__":
    main()
