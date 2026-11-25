"""Buy-zone radar scheduling + ops visibility (2026-09-13).

2026-09-08..09-12 the radar (scripts/watch_buyzone.py) was silent for five
sessions and nothing alarmed: it ran ONLY as the last line of the hourly
travel_catchup sweep, its '[buyzone-alerts]' summary was piped through
`| tail -1` (discarded), config/collection_expectations.yaml had no entry for
it, and /api/watchlist-squeeze carried no as-of stamp. These tests pin the
four fixes, plus the single-instance run lock (two launchd runners can now
overlap). Everything runs against tmp_path (O-20 seal); the radar's _run()
body is NEVER invoked here — it calls Polygon and yfinance — and main() is
only ever entered with the lock held elsewhere or _run monkeypatched.

Run: .venv/bin/python -m pytest -q tests/test_radar_ops.py
"""
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.api.watchlist_squeeze import (  # noqa: E402
    STAMP, build_watchlist_squeeze, is_stale, last_session_close)

NIGHTLY = ROOT / "scripts" / "nightly_ops.sh"
SWEEP = ROOT / "scripts" / "travel_catchup.sh"
MANIFEST = ROOT / "config" / "collection_expectations.yaml"

FRI_EVE = datetime(2026, 9, 11, 18, 45)     # Friday, after the nightly chain
SAT = datetime(2026, 9, 12, 10, 0)
SUN = datetime(2026, 9, 13, 2, 30)
MON_AM = datetime(2026, 9, 14, 9, 0)
MON_PM = datetime(2026, 9, 14, 13, 1)        # first minute after Monday's close (inside the grace)
MON_LATE = datetime(2026, 9, 14, 15, 0)      # Monday's close matured > STALE_GRACE ago


def _state_file(tmp_path, mtime: datetime, entries: dict | None = None) -> Path:
    f = tmp_path / "watchlist_squeeze.json"
    if entries is None:
        entries = {
            "LULU": {"added": "2026-09-04", "last_seen": "2026-09-04",
                     "in_zone": False, "dist_atr": 2.4, "state": "WATCH"},
            "IOT": {"added": "2026-09-11", "last_seen": "2026-09-11",
                    "in_zone": True, "dist_atr": 0.3, "state": "COILED-LONG"},
            "AMBA": {"added": "2026-09-11", "last_seen": "2026-09-11",
                     "in_zone": True, "dist_atr": 0.5, "state": "SHORT-ZONE"},
        }
    f.write_text(json.dumps(entries, indent=1))
    os.utime(f, (mtime.timestamp(), mtime.timestamp()))
    return f


# ── 3. /api/watchlist-squeeze additive fields ────────────────────────────────

def test_api_additive_fields_from_a_tmp_state_file(tmp_path):
    f = _state_file(tmp_path, FRI_EVE)
    # results_path pinned at a tmp absent file: without it this reads the REAL
    # analysis/squeeze_buyzone/results.json and the key set below depends on
    # whether a backtest has been run (see tests/test_api_research_fields.py).
    out = build_watchlist_squeeze(state_path=f, now=SUN,
                                  results_path=tmp_path / "absent-results.json")
    # additive fields
    assert out["last_run_at"] == "2026-09-11T18:45:00"
    assert out["data_asof"] == "2026-09-11"            # max last_seen, not min
    assert out["stale"] is False                        # Fri-evening run, Sunday now
    # existing contract untouched: stamp text + rows sorted in-zone first, then dist_atr
    assert out["stamp"] == STAMP == "UNVALIDATED — MANUAL DECISION SUPPORT ONLY"
    assert [r["ticker"] for r in out["rows"]] == ["IOT", "AMBA", "LULU"]
    assert all("ticker" in r and "state" in r for r in out["rows"])
    # + the outcome-ledger fields (2026-09-13; pinned in tests/test_radar_outcomes.py)
    assert set(out) == {"stamp", "rows", "last_run_at", "data_asof", "stale",
                        "outcomes_recorded", "m5_n_long", "m5_n_short", "m5_guard_met",
                        "no_claim_stamp"}


def test_api_missing_state_file_is_stale_not_an_error(tmp_path):
    out = build_watchlist_squeeze(state_path=tmp_path / "absent.json", now=SUN)
    assert out["rows"] == [] and out["stamp"] == STAMP
    assert out["last_run_at"] is None and out["data_asof"] is None
    assert out["stale"] is True


def test_api_data_asof_none_when_no_entry_carries_last_seen(tmp_path):
    f = _state_file(tmp_path, FRI_EVE, entries={"X": {"added": "2026-09-11", "in_zone": False}})
    out = build_watchlist_squeeze(state_path=f, now=SUN)
    assert out["data_asof"] is None and out["stale"] is False


def test_api_default_path_is_the_radar_state_file():
    # the endpoint (src/api/server.py) delegates here with no arguments
    from src.api import watchlist_squeeze as m
    assert m.STATE == ROOT / "data" / "watchlist_squeeze.json"
    src = (ROOT / "src" / "api" / "server.py").read_text()
    assert "build_watchlist_squeeze" in src
    assert 'watchlist_squeeze.json' not in src.split("def watchlist_squeeze", 1)[1].split("def ", 1)[0]


# ── staleness rule around a weekend ──────────────────────────────────────────

@pytest.mark.parametrize("now, expected_close", [
    (SAT, datetime(2026, 9, 11, 13, 0)),                 # Saturday -> Friday close
    (SUN, datetime(2026, 9, 11, 13, 0)),                 # Sunday   -> Friday close
    (MON_AM, datetime(2026, 9, 11, 13, 0)),              # Monday before the close -> Friday
    (datetime(2026, 9, 14, 13, 0), datetime(2026, 9, 14, 13, 0)),   # exactly at the close -> today
    (MON_PM, datetime(2026, 9, 14, 13, 0)),              # Monday after the close -> Monday
    (datetime(2026, 9, 15, 12, 59), datetime(2026, 9, 14, 13, 0)),  # Tuesday morning -> Monday
])
def test_last_session_close_is_weekend_aware(now, expected_close):
    assert last_session_close(now) == expected_close


@pytest.mark.parametrize("last_run, now, expected", [
    (FRI_EVE, SAT, False),
    (FRI_EVE, SUN, False),
    (FRI_EVE, MON_AM, False),
    (FRI_EVE, MON_PM, False),                            # Monday closed 1 min ago: inside the 90-min grace
    (FRI_EVE, datetime(2026, 9, 14, 13, 30), False),     # the ~:55 sweep has not run yet — no flicker
    (FRI_EVE, datetime(2026, 9, 14, 14, 29), False),
    (FRI_EVE, datetime(2026, 9, 14, 14, 30), True),      # grace over, no Monday run -> stale
    (FRI_EVE, MON_LATE, True),
    (datetime(2026, 9, 11, 12, 30), SAT, True),          # ran BEFORE Friday's close -> stale all weekend
    (datetime(2026, 9, 11, 12, 30), datetime(2026, 9, 14, 13, 30), True),   # ... and stays stale Monday (monotonic)
    (datetime(2026, 9, 14, 13, 30), datetime(2026, 9, 15, 12, 59), False),
    (datetime(2026, 9, 14, 13, 30), datetime(2026, 9, 15, 13, 0), False),   # grace
    (datetime(2026, 9, 14, 13, 30), datetime(2026, 9, 15, 14, 30), True),
    (None, SUN, True),
])
def test_stale_rule_around_a_weekend(last_run, now, expected):
    assert is_stale(last_run, now) is expected


def test_stale_grace_is_the_sweep_interval_plus_runtime():
    from src.api import watchlist_squeeze as m
    assert m.STALE_GRACE.total_seconds() == 90 * 60


def test_stale_flag_flows_through_the_builder(tmp_path):
    f = _state_file(tmp_path, FRI_EVE)
    assert build_watchlist_squeeze(state_path=f, now=MON_AM)["stale"] is False
    assert build_watchlist_squeeze(state_path=f, now=MON_PM)["stale"] is False    # grace
    assert build_watchlist_squeeze(state_path=f, now=MON_LATE)["stale"] is True


# ── 2. collection expectation accepted by its consumer ───────────────────────

def _entry(name):
    m = yaml.safe_load(MANIFEST.read_text())
    return next(e for e in m["expectations"] if e["name"] == name)


def test_expectation_entry_follows_the_manifest_schema():
    e = _entry("buyzone_radar_daily")
    assert e["type"] == "ledger_row_for_date"
    assert e["path"] == "logs/alerts/summary.jsonl"
    assert e["date_field"] == "date"
    assert re.fullmatch(r"\d\d:\d\d", e["due_after"])
    assert e["due_after"] > "18:40", "must mature AFTER the 18:40 chain's ops_checks step"


def test_every_manifest_entry_has_the_keys_its_type_needs():
    m = yaml.safe_load(MANIFEST.read_text())
    for e in m["expectations"]:
        assert {"name", "path", "due_after"} <= set(e), e
        kind = e.get("type", "jsonl_min_rows")
        need = {"ledger_row_for_date": {"date_field"},
                "exit_mark_series_ratio": {"min_ratio"},
                "rvol_shadow_scan": {"min_names"}}.get(kind, {"min_rows"})
        assert need <= set(e), (e["name"], kind)


def test_consumer_accepts_the_radar_expectation(tmp_path):
    import ops_checks as oc
    e = _entry("buyzone_radar_daily")
    root = tmp_path
    f = root / "logs" / "alerts" / "summary.jsonl"
    f.parent.mkdir(parents=True)
    row = {"schema": "wl_summary_v1", "date": "2026-09-11", "ts": "2026-09-11T18:52:10",
           "data_day": "2026-09-11", "summary": "events: none | instances +0/-0",
           "watching": 8, "entered": 0, "archived": 0}
    f.write_text(json.dumps(row) + "\n")
    # matured Friday evening -> asserts Friday: row present -> ok
    r = oc.evaluate_expectation(e, root, now=datetime(2026, 9, 11, 20, 0))
    assert r["target_day"] == "2026-09-11" and r["ok"] is True, r
    # Saturday and Sunday -> still Friday
    for wk in (SAT, SUN):
        r = oc.evaluate_expectation(e, root, now=wk)
        assert r["target_day"] == "2026-09-11" and r["ok"] is True, r
    # the 18:40 chain's ops_checks step runs BEFORE due_after -> previous weekday (Thu): no row -> alarm
    r = oc.evaluate_expectation(e, root, now=datetime(2026, 9, 11, 18, 40))
    assert r["target_day"] == "2026-09-10" and r["ok"] is False and "0 rows" in r["detail"], r
    # Monday evening after maturity with no Monday row -> alarm (the 09-08..09-12 failure mode)
    r = oc.evaluate_expectation(e, root, now=datetime(2026, 9, 14, 20, 0))
    assert r["target_day"] == "2026-09-14" and r["ok"] is False, r


# ── 3b. the shadow-scan expectation reads the PAYLOAD, not the row count ─────
# Regression for 2026-10-28: `rvol_shadow_daily` was a jsonl_min_rows check over
# a single-line JSON OBJECT, so the scan's 87-byte failure marker counted as one
# row and the check scored three lost sessions (10-23/10-26/10-27) `ok=True`.

_SHADOW_DAY = datetime(2026, 10, 27, 21, 30)      # Tuesday, past due_after 21:00


def _shadow_file(root, day, doc):
    f = root / "logs" / "observation" / f"rvol_shadow-{day}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc))                 # one line, as the scan writes it
    return f


def test_shadow_expectation_declares_the_payload_type():
    e = _entry("rvol_shadow_daily")
    assert e["type"] == "rvol_shadow_scan", "min_rows cannot fail on a marker file"
    assert "min_rows" not in e
    assert e["min_names"] >= 1


def test_shadow_expectation_passes_on_a_real_twenty_name_file(tmp_path):
    import ops_checks as oc
    # shaped from the real logs/observation/rvol_shadow-2026-10-22.json
    _shadow_file(tmp_path, "2026-10-27", {
        "date": "2026-10-27", "generated_at": "2026-10-27T14:10:02",
        "candidates_scanned": 1532, "candidates_total": 1532,
        "coverage_pct": 100.0, "ranked_total": 1532, "scan_errors": 0,
        "top20": [{"ticker": f"T{i}", "rvol": 9.0 - i * 0.1} for i in range(20)],
        "watchlist_in_top20": [], "watchlist_rvols": {}})
    r = oc.evaluate_expectation(_entry("rvol_shadow_daily"), tmp_path, now=_SHADOW_DAY)
    assert r["target_day"] == "2026-10-27"
    assert r["ok"] is True and "20 names" in r["detail"], r


def test_shadow_expectation_FAILS_on_the_87_byte_failure_marker(tmp_path):
    import ops_checks as oc
    # byte-for-byte the shape of the real 10-27 stub that scored ok
    f = _shadow_file(tmp_path, "2026-10-27", {
        "date": "2026-10-27", "scan_failed": True, "attempts": 3,
        "at": "2026-10-27T14:20:41-07:00"})
    assert len(f.read_bytes()) < 120, "the marker is small — that is why it slipped through"
    r = oc.evaluate_expectation(_entry("rvol_shadow_daily"), tmp_path, now=_SHADOW_DAY)
    assert r["ok"] is False, "a lost session must not score ok"
    assert "SCAN FAILED" in r["detail"] and "3 attempts" in r["detail"], r


def test_shadow_expectation_fails_on_an_absent_or_short_top20(tmp_path):
    import ops_checks as oc
    e = _entry("rvol_shadow_daily")
    for doc, frag in (({"date": "2026-10-27"}, "0 names"),
                      ({"date": "2026-10-27", "top20": []}, "0 names"),
                      ({"date": "2026-10-27", "top20": None}, "0 names")):
        _shadow_file(tmp_path, "2026-10-27", doc)
        r = oc.evaluate_expectation(e, tmp_path, now=_SHADOW_DAY)
        assert r["ok"] is False and frag in r["detail"], (doc, r)


def test_shadow_expectation_fails_loudly_rather_than_raising(tmp_path):
    import ops_checks as oc
    e = _entry("rvol_shadow_daily")
    # missing file
    r = oc.evaluate_expectation(e, tmp_path, now=_SHADOW_DAY)
    assert r["ok"] is False and r["detail"] == "FILE MISSING", r
    # unparseable, and a JSON non-object: reported, never an exception
    p = tmp_path / "logs" / "observation" / "rvol_shadow-2026-10-27.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert oc.evaluate_expectation(e, tmp_path, now=_SHADOW_DAY)["ok"] is False
    p.write_text("[]")
    r = oc.evaluate_expectation(e, tmp_path, now=_SHADOW_DAY)
    assert r["ok"] is False and "not a JSON object" in r["detail"], r


def test_shadow_expectation_scan_failed_outranks_a_populated_top20(tmp_path):
    """If both are present the failure flag wins — it is the loud signal."""
    import ops_checks as oc
    _shadow_file(tmp_path, "2026-10-27", {
        "date": "2026-10-27", "scan_failed": True, "attempts": 3,
        "top20": [{"ticker": "T1"}]})
    r = oc.evaluate_expectation(_entry("rvol_shadow_daily"), tmp_path, now=_SHADOW_DAY)
    assert r["ok"] is False and "SCAN FAILED" in r["detail"], r


def test_consumer_reports_a_missing_summary_file(tmp_path):
    import ops_checks as oc
    r = oc.evaluate_expectation(_entry("buyzone_radar_daily"), tmp_path,
                                now=datetime(2026, 9, 11, 20, 0))
    assert r["ok"] is False and r["detail"] == "FILE MISSING"


# ── 4. the radar persists its summary row ────────────────────────────────────

def test_persist_summary_appends_one_json_row_per_run(tmp_path, monkeypatch):
    import watch_buyzone as wb          # import only; main() is never called
    target = tmp_path / "alerts" / "summary.jsonl"
    monkeypatch.setattr(wb, "SUMMARY", target)
    text = "events: zone_entered=1 | instances +1/-0"
    row = wb.persist_summary("2026-09-11", text, 8, 1, 0)
    rows = [json.loads(l) for l in target.read_text().splitlines()]
    assert rows == [row]
    assert row["schema"] == "wl_summary_v1"
    assert row["date"] == date.today().isoformat()      # RUN date = the expectation's marker
    assert row["data_day"] == "2026-09-11"
    assert row["summary"] == text
    assert (row["watching"], row["entered"], row["archived"]) == (8, 1, 0)
    assert row["manual_decision_support"] is True
    wb.persist_summary("2026-09-11", text, 8, 0, 3)
    assert len(target.read_text().splitlines()) == 2


def test_persist_summary_never_raises(tmp_path, monkeypatch, capsys):
    import watch_buyzone as wb
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setattr(wb, "SUMMARY", blocker / "summary.jsonl")   # mkdir fails
    row = wb.persist_summary("2026-09-11", "events: none | instances +0/-0", 0, 0)
    assert row["summary"] == "events: none | instances +0/-0"
    assert "summary row not persisted" in capsys.readouterr().out


def test_radar_stdout_and_rules_untouched():
    src = (ROOT / "scripts" / "watch_buyzone.py").read_text()
    # the printed line is exactly the persisted text; both branches persist
    assert 'print(f"[buyzone-alerts] {summary}"' in src
    assert 'persist_summary(day, summary, len(state), len(entered), rolled)' in src
    assert 'persist_summary(day, "watchlist empty", 0, 0)' in src
    # notification + event-emission surface pinned as literals (2026-09-13
    # baseline): one ntfy send, one event schema, no new emit sites. A radar-
    # logic change is a separate, pre-registered item — update these on purpose.
    assert src.count("send(") == 1
    assert src.count('"schema": "wl_event_v1"') == 3
    assert src.count("emit(") == 4            # 1 def + 3 call sites
    assert 'priority="high", tags="eyes"' in src


# ── 1. scheduling: nightly chain step + sweep no longer discards the summary ─

def _radar_line(script: Path) -> str:
    lines = [l for l in script.read_text().splitlines()
             if "watch_buyzone.py" in l and not l.strip().startswith("#")]
    assert len(lines) == 1, lines
    return lines[0]


def test_nightly_ops_runs_the_radar_as_its_own_step():
    src = NIGHTLY.read_text()
    line = _radar_line(NIGHTLY)
    assert line.startswith("run_step buyzone_radar"), line
    assert "logs/alerts/radar-$DATE.log" in line and "2>&1" in line
    assert "tail -1" not in line
    # ordered: after the existing deterministic steps, before the Claude interpretation
    i_radar = src.index("run_step buyzone_radar")
    assert src.index("run_step cll_nightly") < i_radar < src.index('"$CLAUDE_BIN" -p')
    assert "set -euo pipefail" in src


def test_travel_catchup_no_longer_discards_the_radar_summary():
    src = SWEEP.read_text()
    line = _radar_line(SWEEP)
    assert "tail -1" not in line, line
    assert "logs/alerts/radar-" in line
    # a radar failure must not abort the (set -e) sweep: the command is guarded
    # on its own line or the continuation line
    idx = src.splitlines().index(line)
    guarded = "\n".join(src.splitlines()[idx: idx + 2])
    assert re.search(r"\|\|\s*echo", guarded), guarded
    assert "set -euo pipefail" in src


def _fake_python(tmp_path: Path, rc: int = 3) -> Path:
    """<tmp>/.venv/bin/python that behaves like a radar failing with `rc`."""
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/bash\necho radar-output-stdout\necho radar-output-stderr >&2\n"
                  f"exit {rc}\n")
    py.chmod(0o755)
    return py


def _nightly_helpers() -> str:
    """The REAL step_alert/run_step definitions from nightly_ops.sh, verbatim,
    so a regression in the helper itself (not a re-typed copy) is caught."""
    src = NIGHTLY.read_text()
    start = src.index("step_alert() {")
    end = src.index("\n}\n", src.index("run_step() {")) + len("\n}\n")
    text = src[start:end]
    assert '"$@" || rc=$?' in text, "run_step must not regress to a bare \"$@\" under set -e"
    return text


def test_run_step_pages_and_continues_after_a_failing_radar(tmp_path):
    """Executes nightly_ops.sh's real helpers + its real radar line with ROOT
    at tmp_path, the shared sender (scripts/ops/ntfy_send.sh, 2026-09-13)
    replaced by a recorder under that ROOT (no network) and the venv python
    swapped for a radar that exits 3."""
    _fake_python(tmp_path, rc=3)
    (tmp_path / ".env").write_text("NTFY_TOPIC=test-topic\n")
    sender = tmp_path / "scripts" / "ops" / "ntfy_send.sh"
    sender.parent.mkdir(parents=True)
    sender.write_text('#!/bin/bash\necho "SEND-STUB $*" >> "$(dirname "$0")/../../curl.calls"\n')
    sender.chmod(0o755)
    src = NIGHTLY.read_text()
    mkdir_line = 'mkdir -p "$ROOT/logs/alerts"'
    assert mkdir_line in src
    script = ("set -euo pipefail\n"
              f'ROOT="{tmp_path}"\nDATE=2026-09-11\n'
              + _nightly_helpers() + "\n"
              + mkdir_line + "\n" + _radar_line(NIGHTLY) + "\n"
              + "echo NEXT_STEP_RAN\n")
    sh = tmp_path / "chain.sh"
    sh.write_text(script)
    out = subprocess.run(["bash", str(sh)], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out
    assert "STEP-FAIL buyzone_radar (exit 3)" in out.stdout
    assert "NEXT_STEP_RAN" in out.stdout                      # chain continued
    calls = (tmp_path / "curl.calls").read_text()
    assert "NIGHTLY CHAIN STEP FAILED: buyzone_radar" in calls  # page attempted (stubbed)
    log = (tmp_path / "logs" / "alerts" / "radar-2026-09-11.log").read_text()
    assert "radar-output-stdout" in log and "radar-output-stderr" in log


def test_sweep_reports_a_failing_radar_and_continues(tmp_path):
    """Executes travel_catchup.sh's real radar block (mkdir .. appended echo)
    under set -e in tmp_path with the venv python swapped for a failing radar."""
    _fake_python(tmp_path, rc=3)
    lines = SWEEP.read_text().splitlines()
    i0 = lines.index("mkdir -p logs/alerts")
    i1 = next(i for i, l in enumerate(lines) if "buy-zone radar run appended" in l)
    block = "\n".join(lines[i0:i1 + 1])
    assert "watch_buyzone.py" in block and "|| echo" in block
    sh = tmp_path / "sweep.sh"
    sh.write_text(f'set -euo pipefail\ncd "{tmp_path}"\n{block}\necho SWEEP_CONTINUED\n')
    out = subprocess.run(["bash", str(sh)], capture_output=True, text=True)
    assert out.returncode == 0, out
    assert "buy-zone radar exited 3" in out.stdout
    assert "SWEEP_CONTINUED" in out.stdout
    logs = list((tmp_path / "logs" / "alerts").glob("radar-*.log"))
    assert len(logs) == 1
    text = logs[0].read_text()
    assert "radar-output-stdout" in text and "radar-output-stderr" in text


# ── 5. single-instance run lock (two launchd runners can overlap) ────────────

def _redirect_radar_paths(wb, tmp_path, monkeypatch):
    for attr in ("LOCK", "STATE", "ALERT_STATE", "EVENTS", "SUMMARY"):
        monkeypatch.setattr(wb, attr, tmp_path / f"{attr.lower()}.jsonl")
        assert not str(getattr(wb, attr)).startswith(str(ROOT))
    empty = tmp_path / "grouped_empty"
    empty.mkdir()
    # a guard failure dies on IndexError at the first line of _run — it can
    # never reach Polygon, yfinance or a production file
    monkeypatch.setattr(wb.scr, "GROUPED", empty)


def test_run_lock_skips_when_another_run_holds_it(tmp_path, monkeypatch, capsys):
    import watch_buyzone as wb
    _redirect_radar_paths(wb, tmp_path, monkeypatch)
    assert wb.LOCK.name != "watchlist_squeeze.lock" or not str(wb.LOCK).startswith(str(ROOT))
    # a separate open-file-description conflicts under flock exactly as another
    # process would (verified on this macOS: BlockingIOError errno 35)
    holder = wb.LOCK.open("a")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        wb.main()                  # returns normally: exit 0 -> no STEP-FAIL page, no sweep echo
    finally:
        holder.close()
    out = capsys.readouterr().out
    assert "another radar run in progress" in out and "skipped" in out
    assert not wb.STATE.exists() and not wb.SUMMARY.exists() and not wb.EVENTS.exists()


def test_run_lock_is_released_on_normal_exit_and_on_error(tmp_path, monkeypatch):
    import watch_buyzone as wb
    _redirect_radar_paths(wb, tmp_path, monkeypatch)
    seen = {}

    def fake_run():
        seen["held_during_run"] = wb.acquire_run_lock() is None

    monkeypatch.setattr(wb, "_run", fake_run)
    wb.main()
    assert seen["held_during_run"] is True
    fh = wb.acquire_run_lock()
    assert fh is not None, "lock not released after a normal exit"
    wb.release_run_lock(fh)

    def boom():
        raise RuntimeError("radar blew up")

    monkeypatch.setattr(wb, "_run", boom)
    with pytest.raises(RuntimeError):
        wb.main()
    fh = wb.acquire_run_lock()
    assert fh is not None, "lock not released after an exception"
    wb.release_run_lock(fh)
    assert wb.LOCK.exists() and not wb.STATE.exists()


def test_radar_default_lock_path_and_guard_wiring():
    import watch_buyzone as wb
    assert wb.LOCK == ROOT / "data" / "watchlist_squeeze.lock"
    src = (ROOT / "scripts" / "watch_buyzone.py").read_text()
    body = src.split("def main() -> None:", 1)[1].split("def _run() -> None:", 1)[0]
    assert "acquire_run_lock()" in body and "release_run_lock(lock)" in body and "finally:" in body
    assert "GROUPED" not in body            # nothing is read before the lock is held
