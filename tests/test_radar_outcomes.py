"""Buy-zone radar outcome ledger + push ledger + M5 query (2026-09-13).

Pins scripts/radar_outcomes.py (forward close-to-close math with known
answers on a synthetic store, frozen-at-entry invalidation breach, MAE/MFE,
idempotency under the ledger writer lock, fixture exclusion, direction
fallback), the pushes ledger row shape in scripts/watch_buyzone.py, the M5
guard in scripts/radar_m5_query.py (>= 30 COMPLETED instances per side — the
pre-registered population, from the event log, never the matured outcome rows;
no decision-rule language below it), the None-safe additive
/api/watchlist-squeeze fields, the cockpit stamp literal, and the nightly
chain step (runs AFTER buyzone_radar, never blocks). Everything is tmp_path
(O-20 seal); no network, no production file is read or written.

Run: .venv/bin/python -m pytest -q tests/test_radar_outcomes.py
"""
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import radar_outcomes as ro                                            # noqa: E402
import radar_m5_query as m5                                            # noqa: E402
import src.api.watchlist_squeeze as ws                                 # noqa: E402
from src.api.watchlist_squeeze import (                                # noqa: E402
    M5_MIN_PER_SIDE, NO_CLAIM_STAMP, build_watchlist_squeeze, m5_population_counts,
    outcome_counts)

NIGHTLY = ROOT / "scripts" / "nightly_ops.sh"
NOW = datetime(2026, 9, 13, 19, 0)
SESSIONS = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-02", periods=115)]
ANCHOR_I = 100                     # >= 55 bars of flat history before the anchor
ANCHOR = SESSIONS[ANCHOR_I]
# LONG ticker: flat 100 (h 101 / l 99 -> TR 2 -> ATR14 2, EMA21 100), then this path.
LNG_FWD = [101, 103, 99, 96, 104, 105, 106, 107, 108, 110]
# SHORT ticker: flat 50 (h 51 / l 49 -> ATR 2, EMA 50), then this path.
SHT_FWD = [49, 48.5, 51, 53.5, 47, 46, 45, 44, 43, 42]


# ── synthetic store + events ─────────────────────────────────────────────────

def _bar(t, c, o=None, h=None, l=None):
    return {"ticker": t, "open": o if o is not None else c, "high": h if h is not None else c + 1,
            "low": l if l is not None else c - 1, "close": c, "volume": 1000.0}


def make_store(tmp_path, n_sessions=len(SESSIONS), extra=None):
    """grouped/<date>.parquet for LNG/SHT (flat, then the known paths) plus
    `extra` = {ticker: {date: close}} rows. Returns the grouped dir."""
    g = tmp_path / "grouped"
    g.mkdir(exist_ok=True)
    for i, d in enumerate(SESSIONS[:n_sessions]):
        rows = []
        k = i - ANCHOR_I - 1                      # forward index (0-based) after the anchor
        for t, base, fwd in (("LNG", 100.0, LNG_FWD), ("SHT", 50.0, SHT_FWD)):
            c = float(fwd[k]) if 0 <= k < len(fwd) else (base if k < 0 else float(fwd[-1]))
            rows.append(_bar(t, c))
        for t, series in (extra or {}).items():
            if d in series:
                rows.append(_bar(t, float(series[d])))
        pd.DataFrame(rows).to_parquet(g / f"{d}.parquet")
    (g / "2025-12-31.empty").write_text("")     # not a session
    return g


def ev(ticker, tf, iid, transition, asof, **kw):
    base = {"schema": "wl_event_v1", "ticker": ticker, "timeframe": tf,
            "transition": transition, "state_instance_id": iid, "state": "LONG-ZONE",
            "dist_atr": 0.0, "zone_dir": "long", "setup_dir": "long", "bias_d": "bull",
            "mean_level": 100.0, "invalidation_level_atr": 1.5, "data_asof": asof,
            "detected_at": asof[:10] + "T15:00:00", "manual_decision_support": True,
            "re_entered": False}
    base.update(kw)
    return base


FIXTURE_ROW = {"ticker": "OLD", "timeframe": "1d", "transition": "zone_entered",
               "detected_at": "2026-05-01T10:00:00", "state_instance_id": "OLD-x"}


def write_events(tmp_path, hot_rows, archive_rows=None):
    hot = tmp_path / "events.jsonl"
    hot.write_text("".join(json.dumps(r) + "\n" for r in hot_rows))
    adir = tmp_path / "archive"
    adir.mkdir(exist_ok=True)
    (adir / "events-2026-05.jsonl").write_text(json.dumps(FIXTURE_ROW) + "\n")
    if archive_rows:
        (adir / "events-2026-07.jsonl").write_text("".join(json.dumps(r) + "\n" for r in archive_rows))
    return hot, adir


def base_events():
    asof = ANCHOR + " 00:00:00"
    return [
        ev("LNG", "1d", "LNG-1d-x-000001", "zone_entered", asof),
        ev("SHT", "1d", "SHT-1d-x-000002", "zone_entered", asof, state="SHORT-ZONE",
           zone_dir="short", setup_dir="short", bias_d="bear", mean_level=50.0,
           invalidation_level_atr=-1.5),
        ev("SHT", "1d", "SHT-1d-x-000002", "fired", asof, state="FIRED-SHORT",
           zone_dir="short", setup_dir="short", fire_direction="short"),
        ev("SHT", "1d", "SHT-1d-x-000002", "invalidated", SESSIONS[ANCHOR_I + 2] + " 00:00:00",
           state="FIRED-SHORT", zone_dir="short", setup_dir="short"),
    ]


def run(tmp_path, grouped, hot, adir, **kw):
    out = kw.pop("outcomes", tmp_path / "outcomes.jsonl")
    s = ro.write_outcomes(events_hot=hot, archive_dir=adir, outcomes=out, grouped=grouped,
                          bars5m=kw.pop("bars5m", tmp_path / "bars5m_absent"),
                          cfg_path=tmp_path / "no_cfg.yaml", now=NOW,
                          lock_path=kw.pop("lock_path", tmp_path / "radar.lock"), **kw)
    rows = [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
    return s, rows, out


def by_key(rows):
    return {(r["state_instance_id"], r["horizon"]): r for r in rows}


# ── 1. forward-close math with known answers ─────────────────────────────────

def test_long_instance_known_answers(tmp_path):
    grouped = make_store(tmp_path)
    hot, adir = write_events(tmp_path, base_events())
    s, rows, _ = run(tmp_path, grouped, hot, adir)
    k = by_key(rows)
    r3, r5, r10 = (k[("LNG-1d-x-000001", h)] for h in (3, 5, 10))
    for r in (r3, r5, r10):
        assert r["schema"] == "wl_outcome_v1"
        assert r["stamp"] == "exploratory, close-to-close, no costs — NO EDGE CLAIM"
        assert r["manual_decision_support"] is True
        assert r["direction"] == "long" and r["direction_source"] == "zone_dir"
        assert r["entry_close"] == 100.0 and r["entry_close_source"] == "1d_store"
        assert r["entry_session"] == ANCHOR and r["entry_session_close"] == 100.0
        assert r["ema21_entry"] == 100.0 and r["atr14_entry"] == 2.0
        assert r["stop_atr"] == 1.5 and r["inval_level_frozen"] == 97.0
        assert r["ema21_abs_diff"] == 0.0 and r["dist_atr_recomputed"] == 0.0
        assert r["inval_dist_atr_recomputed"] == 1.5 == r["inval_dist_atr_recorded"]
        assert r["fired"] is False and r["invalidated_by_machine"] is False
        assert r["direction_conflict"] is False
        assert r["computed_at"] == "2026-09-13T19:00:00"
    # +3: closes 101,103,99 -> -1% / -0.5 ATR, no breach (min 99 > 97)
    assert r3["horizon_session"] == SESSIONS[ANCHOR_I + 3]
    assert (r3["close_at_horizon"], r3["signed_move_pct"], r3["signed_move_atr"]) == (99.0, -1.0, -0.5)
    assert r3["win"] is False and r3["breached_by_close"] is False
    assert r3["breach_session_index"] is None and r3["breach_session_date"] is None
    assert (r3["mfe_atr"], r3["mae_atr"], r3["mfe_pct"], r3["mae_pct"]) == (1.5, 0.5, 3.0, 1.0)
    # +5: 96 (session 4) < 97 -> breached at index 4; close 104 -> +4% / +2 ATR
    assert (r5["close_at_horizon"], r5["signed_move_pct"], r5["signed_move_atr"]) == (104.0, 4.0, 2.0)
    assert r5["win"] is True and r5["breached_by_close"] is True
    assert r5["breach_session_index"] == 4 and r5["breach_session_date"] == SESSIONS[ANCHOR_I + 4]
    assert (r5["mfe_atr"], r5["mae_atr"]) == (2.0, 2.0)
    # +10: close 110 -> +10% / +5 ATR; breach persists from session 4
    assert (r10["close_at_horizon"], r10["signed_move_pct"], r10["signed_move_atr"]) == (110.0, 10.0, 5.0)
    assert r10["breached_by_close"] is True and r10["breach_session_index"] == 4
    assert (r10["mfe_atr"], r10["mae_atr"]) == (5.0, 2.0)
    assert s["written"] == 6 and s["events"]["fixture_excluded"] == 1


def test_short_instance_known_answers(tmp_path):
    grouped = make_store(tmp_path)
    hot, adir = write_events(tmp_path, base_events())
    _, rows, _ = run(tmp_path, grouped, hot, adir)
    k = by_key(rows)
    r3, r5, r10 = (k[("SHT-1d-x-000002", h)] for h in (3, 5, 10))
    for r in (r3, r5, r10):
        assert r["direction"] == "short" and r["entry_close"] == 50.0
        assert r["ema21_entry"] == 50.0 and r["atr14_entry"] == 2.0
        assert r["inval_level_frozen"] == 53.0                    # EMA21 + 1.5*ATR for a short
        assert r["inval_dist_atr_recomputed"] == -1.5 == r["inval_dist_atr_recorded"]
        assert r["fired"] is True and r["invalidated_by_machine"] is True
        assert r["fire_direction"] == "short"
        assert r["anchor_transition"] == "zone_entered"           # the fired row never moves the anchor
    # +3: closes 49,48.5,51 -> signed = -(51-50) -> -2% / -0.5 ATR; no close > 53
    assert (r3["signed_move_pct"], r3["signed_move_atr"], r3["win"]) == (-2.0, -0.5, False)
    assert r3["breached_by_close"] is False
    assert (r3["mfe_atr"], r3["mae_atr"]) == (0.75, 0.5)
    # +5: 53.5 (session 4) > 53 -> breach idx 4; close 47 -> +6% / +1.5 ATR
    assert (r5["signed_move_pct"], r5["signed_move_atr"], r5["win"]) == (6.0, 1.5, True)
    assert r5["breached_by_close"] is True and r5["breach_session_index"] == 4
    assert (r5["mfe_atr"], r5["mae_atr"]) == (1.5, 1.75)
    # +10: close 42 -> +16% / +4 ATR
    assert (r10["signed_move_pct"], r10["signed_move_atr"]) == (16.0, 4.0)
    assert (r10["mfe_atr"], r10["mae_atr"], r10["breach_session_index"]) == (4.0, 1.75, 4)


def test_frozen_level_is_not_the_trailing_level(tmp_path):
    """The store keeps moving after the anchor; the level must not. A long
    whose closes drift down slowly never breaches the FROZEN level (97) even
    though a trailing EMA21 - 1.5 ATR would have followed price down."""
    series = {SESSIONS[i]: 100.0 for i in range(ANCHOR_I + 1)}
    for j in range(1, 11):
        series[SESSIONS[ANCHOR_I + j]] = 100.0 - 0.25 * j        # ends at 97.5, never < 97
    grouped = make_store(tmp_path, extra={"DRF": series})
    hot, adir = write_events(tmp_path, [ev("DRF", "1d", "DRF-1d-x-000009", "zone_entered",
                                           ANCHOR + " 00:00:00")])
    _, rows, _ = run(tmp_path, grouped, hot, adir)
    k = by_key(rows)
    assert all(k[("DRF-1d-x-000009", h)]["breached_by_close"] is False for h in (3, 5, 10))
    assert k[("DRF-1d-x-000009", 10)]["inval_level_frozen"] == 97.0
    assert k[("DRF-1d-x-000009", 10)]["signed_move_pct"] == -2.5


# ── 2. idempotency, maturity, fixture exclusion, direction fallback ──────────

def test_idempotent_and_incremental_never_rewrites(tmp_path):
    grouped = make_store(tmp_path, n_sessions=ANCHOR_I + 6)     # only 5 forward sessions
    hot, adir = write_events(tmp_path, base_events())
    s1, rows1, out = run(tmp_path, grouped, hot, adir)
    assert s1["written"] == 4 and sorted(r["horizon"] for r in rows1) == [3, 3, 5, 5]
    assert s1["unmatured"] == 2                                  # the two +10 rows
    text1 = out.read_text()
    s2, rows2, _ = run(tmp_path, grouped, hot, adir)
    assert s2["written"] == 0 and s2["already_written"] == 4 and out.read_text() == text1
    make_store(tmp_path)                                          # the store grows -> +10 matures
    s3, rows3, _ = run(tmp_path, grouped, hot, adir)
    assert s3["written"] == 2 and sorted(r["horizon"] for r in rows3[4:]) == [10, 10]
    assert out.read_text().startswith(text1)                      # earlier rows byte-identical
    assert len({(r["state_instance_id"], r["horizon"]) for r in rows3}) == 6 == len(rows3)


def test_fixture_rows_are_never_counted(tmp_path):
    grouped = make_store(tmp_path)
    hot, adir = write_events(tmp_path, base_events())
    s, rows, _ = run(tmp_path, grouped, hot, adir)
    assert s["events"]["fixture_excluded"] == 1
    assert not any(r["ticker"] == "OLD" or r["state_instance_id"].startswith("OLD-x") for r in rows)
    assert ro.is_fixture(FIXTURE_ROW) is True
    assert ro.is_fixture({**FIXTURE_ROW, "schema": "wl_event_v1"}) is True      # ticker OLD
    assert ro.is_fixture({"schema": "wl_event_v1", "ticker": "KR", "state_instance_id": "OLD-x1"}) is True
    assert ro.is_fixture(base_events()[0]) is False
    events, counts = ro.load_events(hot, adir)
    assert counts["read"] == 5 and len(events) == 4              # archive + hot, fixture dropped


def test_direction_falls_back_to_setup_dir_then_fire_direction(tmp_path):
    asof = ANCHOR + " 00:00:00"
    grouped = make_store(tmp_path)
    hot, adir = write_events(tmp_path, [
        ev("LNG", "1d", "LNG-1d-x-000011", "zone_entered", asof, state="EXTENDED-SHORT",
           zone_dir=None, setup_dir="short"),
        ev("SHT", "1d", "SHT-1d-x-000012", "fired", asof, state="FIRED-LONG",
           zone_dir=None, setup_dir=None, fire_direction="long"),
        ev("SHT", "1d", "SHT-1d-x-000013", "zone_entered", asof, zone_dir=None, setup_dir=None),
    ])
    s, rows, _ = run(tmp_path, grouped, hot, adir)
    k = by_key(rows)
    r = k[("LNG-1d-x-000011", 3)]
    assert (r["direction"], r["direction_source"], r["zone_dir"]) == ("short", "setup_dir", None)
    assert r["inval_level_frozen"] == 103.0 and r["signed_move_pct"] == 1.0   # short of 100 -> 99
    r = k[("SHT-1d-x-000012", 3)]
    assert (r["direction"], r["direction_source"], r["anchor_transition"]) == ("long", "fire_direction", "fired")
    assert ("SHT-1d-x-000013", 3) not in k and s["no_direction"] == 1


def test_direction_conflict_is_flagged_and_level_follows_zone_dir(tmp_path):
    grouped = make_store(tmp_path)
    hot, adir = write_events(tmp_path, [ev("LNG", "1d", "LNG-1d-x-000014", "zone_entered",
                                           ANCHOR + " 00:00:00", state="PULLBACK-SHORT",
                                           zone_dir="long", setup_dir="short",
                                           invalidation_level_atr=-1.5)])
    _, rows, _ = run(tmp_path, grouped, hot, adir)
    r = by_key(rows)[("LNG-1d-x-000014", 3)]
    assert r["direction"] == "long" and r["direction_conflict"] is True
    assert r["inval_level_frozen"] == 97.0
    assert r["inval_dist_atr_recorded"] == -1.5 and r["inval_dist_atr_recomputed"] == 1.5


def test_a_running_radar_makes_the_writer_skip_without_reading_or_writing(tmp_path):
    """The radar holds an EXCLUSIVE flock on its run lock for its whole run
    (watch_buyzone.acquire_run_lock); the writer's SHARED attempt must fail
    then and the run must skip — no rows, no anchor read mid-rotation."""
    import fcntl
    grouped = make_store(tmp_path)
    hot, adir = write_events(tmp_path, base_events())
    lock = tmp_path / "radar.lock"
    holder = lock.open("a")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)      # a separate open-file-description
    try:
        s, rows, out = run(tmp_path, grouped, hot, adir, lock_path=lock)
    finally:
        holder.close()
    assert s["skipped_run"] == "radar_lock_held" and s["written"] == 0 and rows == []
    assert not out.exists()
    s2, rows2, _ = run(tmp_path, grouped, hot, adir, lock_path=lock)   # released -> normal run
    assert s2["written"] == 6 and len(rows2) == 6
    fh = ro.acquire_read_lock(lock)                                     # and the writer released its own
    assert fh is not None
    ro.release_read_lock(fh)
    assert ro.LOCK != ROOT / "data" / "watchlist_squeeze.lock"          # conftest redirect in force
    assert 'LOCK = ROOT / "data" / "watchlist_squeeze.lock"' in (ROOT / "scripts" / "radar_outcomes.py").read_text()
    assert 'LOCK = ROOT / "data" / "watchlist_squeeze.lock"' in (ROOT / "scripts" / "watch_buyzone.py").read_text()


def test_writer_lock_skips_an_overlapping_writer(tmp_path):
    """Two writers on the same ledger (a manual run inside the nightly step)
    must not both append the same keys: the ledger's own exclusive lock, held
    from before existing_keys() through the append, makes the second skip."""
    import fcntl
    grouped = make_store(tmp_path)
    hot, adir = write_events(tmp_path, base_events())
    out = tmp_path / "outcomes.jsonl"
    lock = ro.writer_lock_path(out)
    assert lock == tmp_path / "outcomes.jsonl.lock"                     # beside the ledger, tmp here

    def hold():
        h = lock.open("a")
        fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return h

    holder = hold()
    try:
        s, rows, _ = run(tmp_path, grouped, hot, adir)
    finally:
        holder.close()
    assert s["skipped_run"] == "outcomes_lock_held" and s["written"] == 0 and rows == []
    assert not out.exists()
    s2, rows2, _ = run(tmp_path, grouped, hot, adir)                   # released -> normal run
    assert s2["written"] == 6 and len(rows2) == 6
    fh = ro.acquire_writer_lock(out)                                    # and the writer released its own
    assert fh is not None
    ro.release_writer_lock(fh)
    holder = hold()                                                     # CLI path: exit 0, skip message
    try:
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "radar_outcomes.py"),
                            "--events-hot", str(hot), "--archive-dir", str(adir), "--outcomes", str(out),
                            "--grouped", str(grouped), "--bars5m", str(tmp_path / "nope"),
                            "--cfg", str(tmp_path / "nope.yaml"), "--lock", str(tmp_path / "radar.lock")],
                           capture_output=True, text=True)
    finally:
        holder.close()
    assert p.returncode == 0, p.stderr
    assert "another outcome writer holds the ledger lock" in p.stdout and "NO EDGE CLAIM" in p.stdout
    assert len(out.read_text().splitlines()) == 6                      # nothing appended twice
    # production's lock lives beside production's ledger (derived, no extra constant)
    assert ro.writer_lock_path(Path("/x/logs/alerts/outcomes.jsonl")) == Path("/x/logs/alerts/outcomes.jsonl.lock")


def test_missing_forward_bar_writes_nothing_and_reports(tmp_path):
    series = {SESSIONS[i]: 100.0 for i in range(ANCHOR_I + 11)}
    del series[SESSIONS[ANCHOR_I + 2]]                            # gap inside every horizon
    grouped = make_store(tmp_path, extra={"GAP": series})
    hot, adir = write_events(tmp_path, [ev("GAP", "1d", "GAP-1d-x-000015", "zone_entered",
                                           ANCHOR + " 00:00:00")])
    s, rows, _ = run(tmp_path, grouped, hot, adir)
    assert rows == [] and s["skipped"] == {"missing_forward_bar": 3}


# ── 3. the 1h lane: entry from the lane's own bars ───────────────────────────

def _bars5m(tmp_path, ticker="HRL", n_days=12, c=100.0):
    d = tmp_path / "bars5m"
    d.mkdir(exist_ok=True)
    rows = []
    for day in SESSIONS[ANCHOR_I - n_days + 1: ANCHOR_I + 1]:
        for hm in pd.date_range("09:30", "15:55", freq="5min").strftime("%H:%M"):
            rows.append({"date": day, "hm": hm, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 10.0})
    pd.DataFrame(rows).to_parquet(d / f"{ticker}.parquet")
    return d


def test_1h_instance_uses_the_hourly_close_and_hourly_atr(tmp_path):
    daily = {SESSIONS[i]: 100.5 for i in range(ANCHOR_I + 1)}      # daily close differs from 1h (100)
    for j, c in enumerate([102, 104, 97, 95, 99, 100, 101, 102, 103, 104], start=1):
        daily[SESSIONS[ANCHOR_I + j]] = c
    grouped = make_store(tmp_path, extra={"HRL": daily})
    bars = _bars5m(tmp_path)
    hot, adir = write_events(tmp_path, [ev("HRL", "1h", "HRL-1h-x-000021", "zone_entered",
                                           ANCHOR + " 15:00:00")])
    s, rows, _ = run(tmp_path, grouped, hot, adir, bars5m=bars)
    r = by_key(rows)[("HRL-1h-x-000021", 3)]
    assert (r["entry_close"], r["entry_close_source"], r["entry_session_close"]) == (100.0, "1h_bars5m", 100.5)
    assert r["ema21_entry"] == 100.0 and r["atr14_entry"] == 2.0 and r["inval_level_frozen"] == 97.0
    assert r["signed_move_pct"] == -3.0 and r["breached_by_close"] is False     # 97 is not < 97
    r5 = by_key(rows)[("HRL-1h-x-000021", 5)]
    assert r5["breached_by_close"] is True and r5["breach_session_index"] == 4   # 95 < 97
    assert s["written"] == 3


def test_1h_instance_without_bars5m_is_skipped_not_faked(tmp_path):
    grouped = make_store(tmp_path, extra={"HRL": {d: 100.0 for d in SESSIONS}})
    hot, adir = write_events(tmp_path, [ev("HRL", "1h", "HRL-1h-x-000022", "zone_entered",
                                           ANCHOR + " 15:00:00")])
    s, rows, _ = run(tmp_path, grouped, hot, adir)
    assert rows == [] and s["skipped"] == {"1h_no_bars5m": 1}


# ── 4. CLI + defaults ────────────────────────────────────────────────────────

def test_cli_dry_run_writes_nothing_and_prints_the_stamp(tmp_path):
    grouped = make_store(tmp_path)
    hot, adir = write_events(tmp_path, base_events())
    out = tmp_path / "o.jsonl"
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "radar_outcomes.py"), "--dry-run",
                        "--events-hot", str(hot), "--archive-dir", str(adir), "--outcomes", str(out),
                        "--grouped", str(grouped), "--bars5m", str(tmp_path / "nope"),
                        "--cfg", str(tmp_path / "nope.yaml"), "--lock", str(tmp_path / "radar.lock")],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "written=6" in p.stdout and "NO EDGE CLAIM" in p.stdout and not out.exists()
    assert p.stdout.count('"schema": "wl_outcome_v1"') == 6
    assert (tmp_path / "radar.lock").exists()                        # the tmp lock, not production's


def test_default_paths_are_the_production_ledgers():
    src = (ROOT / "scripts" / "radar_outcomes.py").read_text()
    assert 'OUTCOMES = ROOT / "logs" / "alerts" / "outcomes.jsonl"' in src
    assert 'EVENTS_HOT = ROOT / "logs" / "alerts" / "events.jsonl"' in src
    assert 'ARCHIVE_DIR = ROOT / "logs" / "alerts" / "archive"' in src
    api = (ROOT / "src" / "api" / "watchlist_squeeze.py").read_text()
    assert 'OUTCOMES = ROOT / "logs" / "alerts" / "outcomes.jsonl"' in api
    assert 'EVENTS_HOT = ROOT / "logs" / "alerts" / "events.jsonl"' in api
    assert 'ARCHIVE_DIR = ROOT / "logs" / "alerts" / "archive"' in api
    assert ws.M5_TERMINAL == ro.TERMINAL_TRANSITIONS == ("invalidated", "removed")
    assert ro.stop_atr(ROOT / "config" / "squeeze_watchlist.yaml") == 1.5    # read-only
    assert ro.stop_atr(Path("/nonexistent.yaml")) == 1.5                      # lazy default


# ── 5. M5 query guard ────────────────────────────────────────────────────────

VERDICT_WORDS = ("loosen", "earned", "leave as-is", "RULE EVALUATION", "verdict", "rule says")
LATER = SESSIONS[ANCHOR_I + 2] + " 00:00:00"


def _outcome_row(iid, tf, zone_dir, h, win, move, breach):
    return {"schema": "wl_outcome_v1", "state_instance_id": iid, "timeframe": tf,
            "zone_dir": zone_dir, "direction": zone_dir, "horizon": h, "win": win,
            "signed_move_pct": move, "signed_move_atr": move / 2, "breached_by_close": breach,
            "mae_atr": 0.5, "mfe_atr": 1.0}


def _m5_fixture(tmp_path, completed, matured, fire=None, inval=None):
    """Per side (long, short): the first `completed[k]` instances get a terminal
    event (`invalidated` when inval(side, i) else `removed`), the first
    `matured[k]` get +3/+5/+10 outcome rows; the two counts may DISAGREE — the
    guard must read completed, never matured. fire(side, i) -> the instance
    fired. Plus one completed + matured instance with no zone (EXTENDED),
    which is outside M5 and reported as excluded."""
    fire = fire or (lambda side, i: i % 2 == 0)
    inval = inval or (lambda side, i: i % 4 == 0)
    events, outcomes = [], []
    asof = ANCHOR + " 00:00:00"
    for k, side in enumerate(("long", "short")):
        n_c, n_m = completed[k], matured[k]
        for i in range(max(n_c, n_m)):
            iid = f"T{i}-1d-x-{side}"
            events.append(ev("T", "1d", iid, "zone_entered", asof, zone_dir=side, setup_dir=side,
                             bias_d="mixed" if i % 3 == 0 else "bull"))
            if fire(side, i):
                events.append(ev("T", "1d", iid, "fired", asof, zone_dir=side, setup_dir=side,
                                 fire_direction=side))
            if i < n_c:
                events.append(ev("T", "1d", iid, "invalidated" if inval(side, i) else "removed",
                                 LATER, zone_dir=side, setup_dir=side))
            if i < n_m:
                for h in (3, 5, 10):
                    outcomes.append(_outcome_row(iid, "1d", side, h, i % 2 == 0,
                                                 1.0 if i % 2 == 0 else -1.0, i % 4 == 0))
    events.append(ev("T", "1d", "T-1d-x-nozone", "zone_entered", asof,
                     zone_dir=None, setup_dir="short", state="EXTENDED-SHORT"))
    events.append(ev("T", "1d", "T-1d-x-nozone", "removed", LATER, zone_dir=None, setup_dir="short"))
    outcomes += [_outcome_row("T-1d-x-nozone", "1d", None, h, False, -1.0, False) for h in (3, 5, 10)]
    hot, adir = write_events(tmp_path, events)
    out = tmp_path / "outcomes.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in outcomes))
    return hot, adir, out


def _m5_text(hot, adir, out):
    events, _ = ro.load_events(hot, adir)
    counts = {**m5_population_counts(hot, adir), **outcome_counts(out)}   # exactly what main() does
    stats = m5.m5_stats(events, m5.load_outcomes(out), counts)
    return stats, m5.render(stats)


def test_m5_guard_reads_completed_instances_not_matured_rows(tmp_path):
    # 40 matured per side would clear 30/side — but only 29 long instances are COMPLETED
    hot, adir, out = _m5_fixture(tmp_path, completed=(29, 40), matured=(40, 40))
    stats, text = _m5_text(hot, adir, out)
    assert stats["guard"] == {"min_per_side": 30, "population": m5.POPULATION,
                              "n_long": 29, "n_short": 40, "met": False,
                              "consistent_with_cockpit": True}
    assert "zone_entered -> invalidated|removed" in m5.POPULATION
    assert (stats["prereg"]["long"]["n"], stats["prereg"]["short"]["n"]) == (29, 40)
    assert (stats["matured"]["long"]["n"], stats["matured"]["short"]["n"]) == (40, 40)
    assert stats["events_only"]["long"]["n"] == 40                      # incl. the 11 still open
    assert stats["still_open"] == 11 and stats["excluded_null_zone_dir"] == 1
    assert stats["outcomes_recorded"] == 81                             # 80 sided + the no-zone one
    assert "needs >= 30 COMPLETED instances per side" in text and "GUARD NOT MET" in text
    assert "SUPPLEMENTARY — NOT the pre-registered population" in text
    assert "NO EDGE CLAIM" in text and NO_CLAIM_STAMP in text
    assert not any(w in text for w in VERDICT_WORDS), text
    assert stats["matured"]["short"]["h10"]["win_rate"] == 0.5
    assert stats["prereg"]["short"]["terminal"] == {"invalidated": 10, "removed": 30}


def test_m5_empty_ledger_is_guarded_too(tmp_path):
    hot, adir = write_events(tmp_path, base_events())                  # SHT completed, LNG still open
    stats, text = _m5_text(hot, adir, tmp_path / "absent.jsonl")
    assert (stats["guard"]["n_long"], stats["guard"]["n_short"], stats["guard"]["met"]) == (0, 1, False)
    assert "needs >= 30 COMPLETED instances per side" in text
    assert not any(w in text for w in VERDICT_WORDS), text


def test_m5_at_30_completed_per_side_evaluates_the_rule_without_any_ledger(tmp_path):
    hot, adir, _ = _m5_fixture(tmp_path, completed=(30, 30), matured=(0, 0))
    stats, text = _m5_text(hot, adir, tmp_path / "absent.jsonl")
    assert stats["guard"]["met"] is True and stats["guard"]["consistent_with_cockpit"] is True
    assert "GUARD NOT MET" not in text
    assert "PRE-REGISTERED RULE EVALUATION (guard met: long n=30, short n=30 completed instances)" in text
    assert "rule says:" in text and "operator CONFIRM" in text and "NO EDGE CLAIM" in text
    assert stats["matured"]["long"]["n"] == 0 and stats["outcomes_recorded"] == 0   # supplementary is empty
    assert stats["prereg"]["long"]["terminal"] == {"invalidated": 8, "removed": 22}
    assert M5_MIN_PER_SIDE == 30


def test_m5_rule_arithmetic_loosen_vs_leave_as_is(tmp_path):
    # equal fire / invalidation rates per side -> both pre-registered conditions hold -> loosen
    hot, adir, out = _m5_fixture(tmp_path, completed=(30, 30), matured=(0, 0))
    stats, text = _m5_text(hot, adir, out)
    assert stats["prereg"]["long"]["fire_rate"] == stats["prereg"]["short"]["fire_rate"] == 0.5
    assert stats["prereg"]["long"]["invalidation_rate"] == stats["prereg"]["short"]["invalidation_rate"] == 0.267
    assert "short fire_rate >= long fire_rate:                 True  (0.5 vs 0.5)" in text
    assert "rule says: loosen the short gate to accept mixed bias" in text
    # the short side never fires -> condition (a) fails -> leave as-is (the real-data reading of 2026-09-13)
    d2 = tmp_path / "b"
    d2.mkdir()
    hot, adir, out = _m5_fixture(d2, completed=(30, 30), matured=(0, 0),
                                 fire=lambda side, i: side == "long" and i % 2 == 0,
                                 inval=lambda side, i: side == "long" and i % 4 == 0)
    stats, text = _m5_text(hot, adir, out)
    assert (stats["prereg"]["short"]["fire_rate"], stats["prereg"]["long"]["fire_rate"]) == (0.0, 0.5)
    assert "short fire_rate >= long fire_rate:                 False  (0.0 vs 0.5)" in text
    assert "short invalidation_rate <= long invalidation_rate: True  (0.0 vs 0.267)" in text
    assert "rule says: leave as-is (stricter short gate stands)" in text
    assert "loosen the short gate" not in text


def test_m5_cockpit_and_query_agree_on_n(tmp_path):
    hot, adir, out = _m5_fixture(tmp_path, completed=(5, 31), matured=(2, 2))
    assert m5_population_counts(hot, adir) == {"m5_n_long": 5, "m5_n_short": 31, "m5_guard_met": False}
    stats, text = _m5_text(hot, adir, out)
    assert stats["guard"]["consistent_with_cockpit"] is True and "cockpit count mismatch" not in text
    assert (stats["prereg"]["long"]["n"], stats["prereg"]["short"]["n"]) == (5, 31)
    # a drifted cockpit n is flagged loudly, never silently accepted
    events, _ = ro.load_events(hot, adir)
    bad = m5.m5_stats(events, [], {"m5_n_long": 30, "m5_n_short": 31})
    assert bad["guard"]["consistent_with_cockpit"] is False and "cockpit count mismatch" in m5.render(bad)


def test_m5_population_reader_matches_the_writer_loader(tmp_path):
    """src.api.watchlist_squeeze.m5_population_counts (stdlib, feeds the
    cockpit) and radar_outcomes.instances (feeds the query) apply the same
    anchor, terminal, fixture and archive+hot rules."""
    asof = ANCHOR + " 00:00:00"
    archive = [ev("A", "1d", "A-1d-x-1", "zone_entered", asof, zone_dir="long"),       # completed in hot
               ev("B", "1d", "B-1d-x-2", "zone_entered", asof, zone_dir="short"),      # completed in archive
               ev("B", "1d", "B-1d-x-2", "removed", LATER, zone_dir="short")]
    hot = [ev("A", "1d", "A-1d-x-1", "invalidated", LATER, zone_dir="long"),
           ev("A", "1d", "A-1d-x-1", "zone_entered", LATER, zone_dir="short", re_entered=True),  # never moves the anchor
           ev("C", "1d", "C-1d-x-3", "zone_entered", asof, zone_dir="long"),           # still open
           ev("D", "1d", "D-1d-x-4", "fired", asof, zone_dir="long", fire_direction="long"),   # no zone_entered
           ev("D", "1d", "D-1d-x-4", "removed", LATER, zone_dir="long"),
           ev("E", "1d", "E-1d-x-5", "zone_entered", asof, zone_dir="long"),
           ev("E", "1d", "E-1d-x-5", "earnings_passed", LATER, zone_dir="long"),       # informational, not terminal
           ev("OLD", "1d", "OLD-x-9", "zone_entered", asof), ev("OLD", "1d", "OLD-x-9", "removed", LATER),
           ev("F", "1d", "OLD-x-10", "zone_entered", asof), ev("F", "1d", "OLD-x-10", "removed", LATER),
           {"ticker": "G", "transition": "zone_entered", "state_instance_id": "G-1", "zone_dir": "long"},
           ev("H", "1d", None, "zone_entered", asof)]
    hot_p, adir = write_events(tmp_path, hot, archive_rows=archive)
    assert m5_population_counts(hot_p, adir) == {"m5_n_long": 1, "m5_n_short": 1, "m5_guard_met": False}
    insts = ro.instances(ro.load_events(hot_p, adir)[0])
    a = insts["A-1d-x-1"]
    assert a["completed"] is True and a["terminal"] == "invalidated" and a["anchor"]["zone_dir"] == "long"
    assert insts["B-1d-x-2"]["terminal"] == "removed"
    assert insts["C-1d-x-3"]["completed"] is False and insts["C-1d-x-3"]["terminal"] is None
    assert insts["D-1d-x-4"]["anchor"]["transition"] == "fired" and insts["D-1d-x-4"]["completed"] is True
    assert insts["E-1d-x-5"]["completed"] is False
    assert not {"G-1", "OLD-x-9", "OLD-x-10"} & set(insts)
    stats, _ = _m5_text(hot_p, adir, tmp_path / "absent.jsonl")
    assert (stats["prereg"]["long"]["n"], stats["prereg"]["short"]["n"]) == (1, 1)   # D: fired anchor, excluded
    assert stats["guard"]["consistent_with_cockpit"] is True


def test_m5_cli_runs_against_tmp_paths_only(tmp_path):
    hot, adir, out = _m5_fixture(tmp_path, completed=(3, 2), matured=(3, 2))
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "radar_m5_query.py"),
                        "--events-hot", str(hot), "--archive-dir", str(adir), "--outcomes", str(out)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "long n=3, short n=2" in p.stdout and "needs >= 30 COMPLETED instances per side" in p.stdout
    assert not any(w in p.stdout for w in VERDICT_WORDS)
    pj = subprocess.run([sys.executable, str(ROOT / "scripts" / "radar_m5_query.py"), "--json",
                         "--events-hot", str(hot), "--archive-dir", str(adir), "--outcomes", str(out)],
                        capture_output=True, text=True)
    j = json.loads(pj.stdout)
    assert j["guard"]["met"] is False and j["guard"]["consistent_with_cockpit"] is True
    assert (j["prereg"]["long"]["n"], j["matured"]["long"]["n"], j["outcomes_recorded"]) == (3, 3, 6)


# ── 6. /api/watchlist-squeeze additive fields + cockpit stamp ────────────────

def test_api_outcome_fields(tmp_path):
    out = tmp_path / "outcomes.jsonl"
    rows = [_outcome_row("A-1d", "1d", "long", h, True, 1.0, False) for h in (3, 5, 10)]
    rows += [_outcome_row("B-1h", "1h", "short", h, False, -1.0, True) for h in (3, 5, 10)]
    rows += [_outcome_row("C-1d", "1d", "long", h, True, 1.0, False) for h in (3, 5)]      # not matured
    rows += [_outcome_row("D-1d", "1d", None, h, True, 1.0, False) for h in (3, 5, 10)]    # no zone
    out.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    evd = tmp_path / "ev"
    evd.mkdir()
    hot, adir, _ = _m5_fixture(evd, completed=(2, 1), matured=(0, 0))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"KR": {"added": "2026-09-11", "in_zone": True, "dist_atr": 0.5}}))
    o = build_watchlist_squeeze(state_path=state, now=NOW, outcomes_path=out, events_hot=hot, archive_dir=adir)
    assert (o["outcomes_recorded"], o["m5_n_long"], o["m5_n_short"], o["m5_guard_met"]) == (3, 2, 1, False)
    assert o["no_claim_stamp"] == NO_CLAIM_STAMP == \
        "exploratory, close-to-close, no costs — NO EDGE CLAIM (M5 needs >= 30/side)"
    assert o["stamp"] == "UNVALIDATED — MANUAL DECISION SUPPORT ONLY" and o["rows"][0]["ticker"] == "KR"
    o2 = build_watchlist_squeeze(state_path=state, now=NOW, outcomes_path=tmp_path / "absent.jsonl",
                                 events_hot=tmp_path / "absent.jsonl", archive_dir=tmp_path / "absent_dir")
    assert (o2["outcomes_recorded"], o2["m5_n_long"], o2["m5_n_short"], o2["m5_guard_met"]) == (0, 0, 0, False)
    assert ws.ledger_fields(tmp_path / "absent.jsonl")["no_claim_stamp"] == NO_CLAIM_STAMP
    # the default paths are the conftest O-20 redirects (tmp), never production
    assert ws.OUTCOMES == out and ws.EVENTS_HOT == tmp_path / "events.jsonl"
    assert build_watchlist_squeeze(state_path=state, now=NOW)["outcomes_recorded"] == 3


def test_ledger_readers_are_none_safe(tmp_path, monkeypatch):
    """One malformed line must never 500 the endpoint: null horizon, non-dict
    JSON, bad JSON and unreadable paths all degrade to zeros + the stamp."""
    out = tmp_path / "o.jsonl"
    out.write_text("\n".join([
        json.dumps({"schema": "wl_outcome_v1", "horizon": None, "state_instance_id": "X", "zone_dir": "long"}),
        "[1, 2]", '"str"', "null", "not json", "",
        json.dumps({"schema": "wl_outcome_v1", "horizon": "10", "state_instance_id": "OK", "zone_dir": "long"}),
    ]) + "\n")
    assert outcome_counts(out) == {"outcomes_recorded": 1}
    assert m5.load_outcomes(out) == [{"schema": "wl_outcome_v1", "horizon": 10,
                                      "state_instance_id": "OK", "zone_dir": "long"}]
    hot = tmp_path / "e.jsonl"
    hot.write_text("\n".join(["[1]", "null", '"s"', "{broken",
                              json.dumps({"schema": "wl_event_v1", "ticker": "K"}),
                              json.dumps(ev("K", "1d", "K-1", "zone_entered", ANCHOR + " 00:00:00")),
                              json.dumps(ev("K", "1d", "K-1", "removed", LATER))]) + "\n")
    assert m5_population_counts(hot, tmp_path / "no_archive") == \
        {"m5_n_long": 1, "m5_n_short": 0, "m5_guard_met": False}
    events, counts = ro.load_events(hot, tmp_path / "no_archive")
    assert counts["bad_json"] == 4 and len(ro.instances(events)) == 1  # 3 non-dict + 1 broken
    d = tmp_path / "dir"                                                # unreadable as a file
    d.mkdir()
    assert outcome_counts(d) == {"outcomes_recorded": 0}
    assert m5_population_counts(d, d) == {"m5_n_long": 0, "m5_n_short": 0, "m5_guard_met": False}
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"KR": {"added": "2026-09-11", "in_zone": True, "dist_atr": 0.5}}))
    o = build_watchlist_squeeze(state_path=state, now=NOW, outcomes_path=d, events_hot=d, archive_dir=d)
    assert o["stamp"] == "UNVALIDATED — MANUAL DECISION SUPPORT ONLY" and o["rows"][0]["ticker"] == "KR"
    assert (o["outcomes_recorded"], o["m5_n_long"], o["m5_n_short"], o["m5_guard_met"]) == (0, 0, 0, False)
    assert o["no_claim_stamp"] == NO_CLAIM_STAMP

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ws, "outcome_counts", boom)                    # top-level fallback
    assert ws.ledger_fields(out, hot, tmp_path / "no_archive") == {
        "outcomes_recorded": 0, "m5_n_long": 0, "m5_n_short": 0, "m5_guard_met": False,
        "no_claim_stamp": NO_CLAIM_STAMP}
    o = build_watchlist_squeeze(state_path=state, now=NOW)
    assert o["rows"][0]["ticker"] == "KR" and o["outcomes_recorded"] == 0


def test_cockpit_panel_renders_the_stamp_line():
    tsx = (ROOT / "ui" / "src" / "components" / "WatchlistPanel.tsx").read_text()
    assert f'const NO_CLAIM_FALLBACK = "{NO_CLAIM_STAMP}";' in tsx           # fallback == the API literal
    assert "outcomes recorded: ${meta?.outcomes_recorded ?? 0} — ${meta?.no_claim_stamp ?? NO_CLAIM_FALLBACK}" in tsx
    assert "M5 long n=${meta?.m5_n_long ?? 0} short n=${meta?.m5_n_short ?? 0}" in tsx
    assert "loosen" not in tsx and "rule says" not in tsx                  # the reading is print-only


# ── 7. pushes ledger ─────────────────────────────────────────────────────────

@pytest.fixture
def wb(tmp_path, monkeypatch):
    import watch_buyzone as mod                 # import only; _run() is never called
    monkeypatch.setattr(mod, "PUSHES", tmp_path / "pushes.jsonl")
    calls = []
    monkeypatch.setattr(mod, "send", lambda *a, **k: calls.append((a, k)))
    mod._test_calls = calls
    return mod


ITEMS = [{"ticker": "KR", "state": "COILED-LONG", "state_instance_id": "KR-1d-2026-09-11-a54cd2"},
         {"ticker": "SMU", "state": "FIRED-SHORT", "state_instance_id": None}]


def test_push_row_shape_and_delivered_when_topic_set(wb, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "t-test")
    row = wb._send_logged("Squeeze watchlist: ZONE ENTRY", "UNVALIDATED - decision support only\nKR ...",
                          items=ITEMS, priority="high", tags="eyes")
    rows = [json.loads(l) for l in wb.PUSHES.read_text().splitlines()]
    assert rows == [row]
    assert set(row) == {"schema", "ts", "title", "body", "tickers", "state_instance_ids", "items",
                        "delivered", "ntfy_configured", "error", "manual_decision_support"}
    assert row["schema"] == "wl_push_v1" and row["manual_decision_support"] is True
    assert row["title"] == "Squeeze watchlist: ZONE ENTRY" and row["body"].startswith("UNVALIDATED")
    assert row["tickers"] == ["KR", "SMU"]
    assert row["state_instance_ids"] == ["KR-1d-2026-09-11-a54cd2", None]
    assert row["items"] == ITEMS
    assert row["delivered"] is True and row["ntfy_configured"] is True and row["error"] is None
    datetime.fromisoformat(row["ts"])
    # the push itself went through the notify layer unchanged, exactly once
    assert wb._test_calls == [(("Squeeze watchlist: ZONE ENTRY", "UNVALIDATED - decision support only\nKR ..."),
                               {"priority": "high", "tags": "eyes"})]


def test_push_not_delivered_without_a_topic(wb, monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    row = wb._send_logged("t", "b", items=ITEMS[:1], priority="high", tags="eyes")
    assert row["delivered"] is False and row["ntfy_configured"] is False and row["error"] is None
    assert len(wb._test_calls) == 1                                  # the push still happened


def test_push_not_delivered_when_ntfy_channel_fails(wb, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "t-test")

    def failing(*a, **k):                                            # what notify.send does on failure
        logging.getLogger("trader").warning("notification channel %s failed: %s", "ntfy", "boom")
    monkeypatch.setattr(wb, "send", failing)
    row = wb._send_logged("t", "b", items=[], priority="high", tags="eyes")
    assert row["delivered"] is False and row["ntfy_configured"] is True
    assert "boom" in row["error"] and row["tickers"] == [] and row["state_instance_ids"] == []
    # a macOS-channel failure alone is not an ntfy failure
    monkeypatch.setattr(wb, "send", lambda *a, **k: logging.getLogger("trader")
                        .warning("notification channel macos failed: no osascript"))
    assert wb._send_logged("t", "b", items=[])["delivered"] is True


def test_push_ledger_failure_never_blocks_the_push(wb, monkeypatch, capsys):
    monkeypatch.setenv("NTFY_TOPIC", "t-test")
    blocker = wb.PUSHES.parent / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(wb, "PUSHES", blocker / "pushes.jsonl")   # mkdir fails
    row = wb._send_logged("t", "b", items=[], priority="high", tags="eyes")
    assert row["delivered"] is True and len(wb._test_calls) == 1
    assert "push row not persisted" in capsys.readouterr().out


def test_push_call_site_wiring_unchanged_except_the_ledger():
    src = (ROOT / "scripts" / "watch_buyzone.py").read_text()
    assert 'PUSHES = ROOT / "logs" / "alerts" / "pushes.jsonl"' in src
    site = src.split("# 3) notify only on new entries", 1)[1]
    assert site.strip().startswith("if entered:")                   # the same condition, nothing else
    assert '_send_logged("Squeeze watchlist: ZONE ENTRY"' in site
    assert "items=entered_meta" in site and 'priority="high", tags="eyes"' in site
    assert src.count("send(") == 1                                   # the single pass-through
    assert src.count("_send_logged(") == 2                           # 1 def + 1 call site


# ── 8. nightly chain: the step runs AFTER buyzone_radar and never blocks ─────

def _step_line(name: str) -> str:
    lines = [l for l in NIGHTLY.read_text().splitlines()
             if l.startswith(f"run_step {name} ")]
    assert len(lines) == 1, lines
    return lines[0]


def test_nightly_ops_has_the_outcomes_step_after_the_radar():
    src = NIGHTLY.read_text()
    line = _step_line("radar_outcomes")
    assert "radar_outcomes.py" in line and "logs/alerts/outcomes-$DATE.log" in line and "2>&1" in line
    assert "watch_buyzone.py" not in line
    assert src.index("run_step buyzone_radar ") < src.index("run_step radar_outcomes ") < src.index('"$CLAUDE_BIN" -p')


def test_nightly_outcomes_step_pages_and_continues_on_failure(tmp_path):
    """Runs nightly_ops.sh's REAL step_alert/run_step helpers with the real
    radar_outcomes line, ROOT at tmp_path, the shared sender
    (scripts/ops/ntfy_send.sh, 2026-09-13) replaced by a recorder under that
    ROOT (no network), venv python swapped for a script that exits 3."""
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/bash\necho outcomes-stdout\necho outcomes-stderr >&2\nexit 3\n")
    py.chmod(0o755)
    (tmp_path / ".env").write_text("NTFY_TOPIC=test-topic\n")
    sender = tmp_path / "scripts" / "ops" / "ntfy_send.sh"
    sender.parent.mkdir(parents=True)
    sender.write_text('#!/bin/bash\necho "SEND-STUB $*" >> "$(dirname "$0")/../../curl.calls"\n')
    sender.chmod(0o755)
    (tmp_path / "logs" / "alerts").mkdir(parents=True)
    src = NIGHTLY.read_text()
    start = src.index("step_alert() {")
    end = src.index("\n}\n", src.index("run_step() {")) + len("\n}\n")
    helpers = src[start:end]
    assert '"$@" || rc=$?' in helpers
    sh = tmp_path / "chain.sh"
    sh.write_text("set -euo pipefail\n" f'ROOT="{tmp_path}"\nDATE=2026-09-11\n'
                  + helpers + "\n" + _step_line("radar_outcomes") + "\necho NEXT_STEP_RAN\n")
    out = subprocess.run(["bash", str(sh)], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out
    assert "STEP-FAIL radar_outcomes (exit 3)" in out.stdout and "NEXT_STEP_RAN" in out.stdout
    assert "NIGHTLY CHAIN STEP FAILED: radar_outcomes" in (tmp_path / "curl.calls").read_text()
    log = (tmp_path / "logs" / "alerts" / "outcomes-2026-09-11.log").read_text()
    assert "outcomes-stdout" in log and "outcomes-stderr" in log
