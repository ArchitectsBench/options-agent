"""/api/watchlist-squeeze builder (2026-09-13) — pure reader of the buy-zone
radar's state file (scripts/watch_buyzone.py :: data/watchlist_squeeze.json).

`stamp` and `rows` are byte-identical to the inline endpoint this replaced.
Additive ops-visibility fields (the UI pass that renders them is separate):
  last_run_at  state-file mtime, local ISO seconds (None if missing)
  data_asof    max `last_seen` across entries — the grouped-daily session the
               radar last evaluated (None if empty/missing)
  stale        True when last_run_at is older than the most recent weekday
               session close (13:00 local) that matured at least STALE_GRACE
               ago, or the state file is missing. 09-08..09-12 the radar's
               state froze for five sessions and nothing on the cockpit said
               so.
Additive outcome-ledger fields (2026-09-13, scripts/radar_outcomes.py):
  outcomes_recorded  distinct state instances with a +10-session outcome row
                     in logs/alerts/outcomes.jsonl (fully matured)
  m5_n_long / m5_n_short  the pre-registered M5 query's per-side n, on the
                     PRE-REGISTERED population (docs/watchlist_known_
                     asymmetries.md): COMPLETED instances — a `zone_entered`
                     anchor followed by a terminal `invalidated` / `removed`
                     event — whose anchor `zone_dir` is long / short, read
                     from the event log (archive + hot, fixture excluded),
                     NOT from the outcome rows. The SAME function
                     (`m5_population_counts`) feeds scripts/radar_m5_query.py's
                     >= 30-per-side guard, so the cockpit and the query can
                     never disagree on n.
  m5_guard_met       both sides >= M5_MIN_PER_SIDE. A count, not a reading:
                     the rule itself is evaluated and printed only by
                     scripts/radar_m5_query.py.
  no_claim_stamp     the literal NO EDGE CLAIM line the panel renders.
All four are None-safe: a malformed ledger / event line is skipped and any
other failure yields zeros + the stamp, so an additive field can never break
`stamp` / `rows` / `last_run_at` / `data_asof` / `stale`.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data" / "watchlist_squeeze.json"
OUTCOMES = ROOT / "logs" / "alerts" / "outcomes.jsonl"
EVENTS_HOT = ROOT / "logs" / "alerts" / "events.jsonl"
ARCHIVE_DIR = ROOT / "logs" / "alerts" / "archive"
RESULTS = ROOT / "analysis" / "squeeze_buyzone" / "results.json"
STAMP = "UNVALIDATED — MANUAL DECISION SUPPORT ONLY"
# The M5 guard (docs/watchlist_known_asymmetries.md): no side-vs-side reading
# below this many COMPLETED instances PER SIDE. Frozen with the pre-registration.
M5_MIN_PER_SIDE = 30
M5_HORIZON = 10
# "zone_entered -> terminal event": the terminal transitions of the radar's
# event schema (wl_event_v1). Mirrors scripts/radar_outcomes.TERMINAL_TRANSITIONS.
M5_TERMINAL = ("invalidated", "removed")
# The synthetic fixture (OLD / OLD-x rows, no schema stamp) is never counted —
# the same exclusions as scripts/radar_outcomes.is_fixture.
FIXTURE_TICKERS = {"OLD"}
FIXTURE_ID_PREFIX = "OLD-x"
NO_CLAIM_STAMP = ("exploratory, close-to-close, no costs — NO EDGE CLAIM "
                  f"(M5 needs >= {M5_MIN_PER_SIDE}/side)")
# Regular-session close in local (machine) time — mirrors config.yaml
# agent.market_hours.end (13:00 America/Los_Angeles). Hardcoded on purpose:
# this reader must not import engine modules.
SESSION_CLOSE = time(13, 0)
# Grace after a close before a run that predates it reads stale: the hourly
# travel_catchup sweep (StartInterval 3600, phase drifts) plus radar runtime.
# Without it every healthy weekday read stale from 13:00 until the next sweep
# — a flicker that trains people to ignore the field. Applied as
# last_session_close(now - STALE_GRACE), which is MONOTONIC: a run that read
# stale on Saturday cannot read fresh on Monday afternoon.
STALE_GRACE = timedelta(minutes=90)


def last_session_close(now: datetime | None = None) -> datetime:
    """Most recent weekday close at or before `now`: today's close once past
    SESSION_CLOSE on a weekday, else the previous weekday's close. Weekend-
    aware only — US holidays are not modelled, the same approximation
    config/collection_expectations.yaml documents."""
    now = now or datetime.now()
    d = now.date()
    close = datetime.combine(d, SESSION_CLOSE)
    if d.weekday() >= 5 or now < close:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        close = datetime.combine(d, SESSION_CLOSE)
    return close


def is_stale(last_run_at: datetime | None, now: datetime | None = None) -> bool:
    """A radar run that predates the last weekday close which matured
    STALE_GRACE ago is stale; no run at all is stale."""
    now = now or datetime.now()
    return last_run_at is None or last_run_at < last_session_close(now - STALE_GRACE)


def _jsonl_dicts(path: Path):
    """Dict rows of a jsonl file, tolerant of everything: a missing or
    unreadable file yields nothing, a bad line or a non-dict line is skipped."""
    try:
        text = Path(path).read_text()
    except Exception:
        return
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if isinstance(r, dict):
            yield r


def outcome_counts(outcomes_path: Path | None = None) -> dict:
    """`outcomes_recorded` over logs/alerts/outcomes.jsonl (wl_outcome_v1
    rows, one per (instance, horizon)) = distinct instances with a
    horizon-M5_HORIZON row. Stdlib only; a malformed row is skipped, any
    other failure yields 0 — never an exception."""
    p = Path(outcomes_path) if outcomes_path is not None else OUTCOMES
    matured: set[str] = set()
    try:
        for r in _jsonl_dicts(p):
            try:
                if r.get("schema") != "wl_outcome_v1" or int(r.get("horizon")) != M5_HORIZON:
                    continue
                iid = r.get("state_instance_id")
                if iid:
                    matured.add(str(iid))
            except Exception:
                continue
    except Exception:
        matured = set()
    return {"outcomes_recorded": len(matured)}


def _event_sort_key(r: dict) -> tuple:
    return (str(r.get("data_asof", "")), str(r.get("detected_at", "")))


def m5_population_counts(events_hot: Path | None = None,
                         archive_dir: Path | None = None) -> dict:
    """The pre-registered M5 population, per side, from the event log alone
    (archive + hot, fixture excluded): instances whose earliest `zone_entered`
    row (by data_asof, detected_at — the anchor) has zone_dir long / short
    AND which have a terminal event (M5_TERMINAL). Stdlib only; malformed
    lines are skipped, any other failure yields zeros. Mirrors
    scripts/radar_outcomes.instances() — radar_m5_query asserts the two agree."""
    hot = Path(events_hot) if events_hot is not None else EVENTS_HOT
    adir = Path(archive_dir) if archive_dir is not None else ARCHIVE_DIR
    anchors: dict[str, dict] = {}
    completed: set[str] = set()
    try:
        files = sorted(adir.glob("*.jsonl")) if adir.is_dir() else []
        files.append(hot)
        for f in files:
            for r in _jsonl_dicts(f):
                try:
                    if r.get("schema") != "wl_event_v1":
                        continue
                    if str(r.get("ticker", "")) in FIXTURE_TICKERS:
                        continue
                    iid = r.get("state_instance_id")
                    if not iid or str(iid).startswith(FIXTURE_ID_PREFIX):
                        continue
                    iid = str(iid)
                    tr = r.get("transition")
                    if tr == "zone_entered":
                        cur = anchors.get(iid)
                        if cur is None or _event_sort_key(r) < _event_sort_key(cur):
                            anchors[iid] = r
                    elif tr in M5_TERMINAL:
                        completed.add(iid)
                except Exception:
                    continue
    except Exception:
        anchors, completed = {}, set()
    n = {"long": 0, "short": 0}
    for iid, a in anchors.items():
        side = a.get("zone_dir")
        if iid in completed and side in n:
            n[side] += 1
    return {"m5_n_long": n["long"], "m5_n_short": n["short"],
            "m5_guard_met": n["long"] >= M5_MIN_PER_SIDE and n["short"] >= M5_MIN_PER_SIDE}


def ledger_fields(outcomes_path: Path | None = None, events_hot: Path | None = None,
                  archive_dir: Path | None = None) -> dict:
    """The additive outcome-ledger fields, with a top-level fallback: on ANY
    failure the fields are zeros + the stamp, never an exception."""
    try:
        return {**outcome_counts(outcomes_path),
                **m5_population_counts(events_hot, archive_dir),
                "no_claim_stamp": NO_CLAIM_STAMP}
    except Exception:
        return {"outcomes_recorded": 0, "m5_n_long": 0, "m5_n_short": 0,
                "m5_guard_met": False, "no_claim_stamp": NO_CLAIM_STAMP}


def backtest_verdict(results_path: Path | None = None) -> dict:
    """The pre-registered offline backtest's VERDICT, read from the frozen
    result file (analysis/squeeze_buyzone/results.json, prereg SQZBZ-2026-09-13).

    The cockpit must never carry a hardcoded verdict: a reader that states
    "FAIL" in its own source keeps saying it after the file changes. This
    returns None when the file is absent or malformed, and the panel then
    says nothing rather than something stale."""
    p = Path(results_path) if results_path is not None else RESULTS
    try:
        d = json.loads(p.read_text())
        s = d.get("summary") or {}
        dec = s.get("decision") or {}
        v = s.get("verdict") or dec.get("verdict")
        if not v:
            return {}
        return {"backtest": {
            "prereg_id": d.get("prereg_id"),
            "verdict": str(v),
            "consequence": dec.get("consequence"),
            "gate_t": dec.get("gate_t"),
            "bar_t": dec.get("bar_t"),
            "n_treated": dec.get("n_treated_long_valid"),
            "sessions": dec.get("sessions_with_treated_long"),
            "as_of": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        }}
    except Exception:
        return {}


def build_watchlist_squeeze(state_path: Path | None = None,
                            now: datetime | None = None,
                            outcomes_path: Path | None = None,
                            events_hot: Path | None = None,
                            archive_dir: Path | None = None,
                            results_path: Path | None = None) -> dict:
    f = Path(state_path) if state_path is not None else STATE
    state = json.loads(f.read_text()) if f.exists() else {}
    rows = [{"ticker": t, **e} for t, e in state.items()]
    rows.sort(key=lambda r: (not r.get("in_zone", False),
                             r.get("dist_atr", 99)))
    last_run = datetime.fromtimestamp(f.stat().st_mtime) if f.exists() else None
    asof = [str(e.get("last_seen")) for e in state.values() if e.get("last_seen")]
    return {"stamp": STAMP,
            "rows": rows,
            "last_run_at": last_run.isoformat(timespec="seconds") if last_run else None,
            "data_asof": max(asof) if asof else None,
            "stale": is_stale(last_run, now),
            **ledger_fields(outcomes_path, events_hot, archive_dir),
            **backtest_verdict(results_path)}
