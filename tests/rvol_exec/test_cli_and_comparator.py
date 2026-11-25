"""Daemon helpers, the prep CLI and the nightly causal_first10 comparator
helper — offline, paths under tmp, no API. UNVALIDATED — PAPER ONLY.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pandas as pd
import pytest

import src.rvol_exec.paths as paths
from src.rvol_exec.constants import SLOTS, STAMP
from tests.rvol_exec.session_sim import make_bars

ROOT = Path(__file__).resolve().parents[2]


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── daemon ───────────────────────────────────────────────────────────────────
def test_daemon_pid_file_refuses_a_live_holder_and_releases_its_own():
    d = _load("scripts/rvol_exec_daemon.py", "rvol_exec_daemon")
    paths.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.PID_FILE.write_text(str(os.getpid()))            # "another" live process = us
    assert d._claim_pid_file() is True                     # our own pid is not a foreign holder
    paths.PID_FILE.write_text("999999")                    # dead pid -> claimable
    assert d._claim_pid_file() is True and paths.PID_FILE.read_text() == str(os.getpid())
    d._release_pid_file()
    assert not paths.PID_FILE.exists()
    # the in-process isolation assert is exercised by the go/no-go subprocess check
    # (test_isolation.py) — the pytest process itself has paper_trader loaded by the O-20 seal


def test_daemon_flatten_now_refuses_when_a_daemon_is_live():
    d = _load("scripts/rvol_exec_daemon.py", "rvol_exec_daemon")
    paths.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.PID_FILE.write_text(str(os.getpid()))
    assert d.flatten_now("2026-09-14", 4002) == 2
    assert not paths.FLATTEN_NOW_FILE.exists()


def test_plists_are_files_only_and_point_at_the_main_tree():
    for name in ("com.optionsagent.rvolexec.plist", "com.optionsagent.rvolexecprep.plist"):
        p = ROOT / "launchd" / name
        txt = p.read_text()
        assert "NOT loaded" in txt and "/Users/you/projects/OptionsAgent/" in txt
        assert "OptionsAgent-rvolexec" not in txt
    txt = (ROOT / "launchd" / "com.optionsagent.rvolexec.plist").read_text()
    assert "<integer>6</integer><key>Minute</key><integer>33</integer>" in txt
    assert "caffeinate" in txt and "SuccessfulExit" in txt


# ── prep CLI ─────────────────────────────────────────────────────────────────
class _Api:
    def __init__(self):
        self.calls = []

    def bars_5m_range(self, t, s, e):
        self.calls.append(t)
        return []


def test_prep_cli_idempotent_on_fresh_sidecar_and_never_calls_api_without_flag(capsys):
    p = _load("scripts/rvol_exec_prep.py", "rvol_exec_prep")
    day = "2026-09-14"
    paths.PREP_DIR.mkdir(parents=True, exist_ok=True)
    (paths.PREP_DIR / f"{day}.meta.json").write_text(json.dumps({
        "prep_for": day, "built_from_through": "2026-09-11",
        "coverage": {"candidates": 900, "with_baseline14": 880}}))
    api = _Api()
    assert p.main(["--for", day], api=api) == 0 and api.calls == []
    assert "already fresh" in capsys.readouterr().out
    # empty stores: rebuild with --force, no API without the flag, not fresh -> exit 1
    assert p.main(["--for", day, "--force"], api=api) == 1 and api.calls == []
    meta = json.loads((paths.PREP_DIR / f"{day}.meta.json").read_text())
    assert meta["n_candidates"] == 0 and meta["allow_api"] is False and meta["stamp"] == STAMP


# ── comparator helper ────────────────────────────────────────────────────────
def _cross_bars(hm):
    return make_bars((10.0, 10.5, 9.9, 10.4, 100_000),
                     {hm: (10.45, 10.53, 10.40, 10.52), "15:55": (10.9, 10.95, 10.85, 10.92)})


def test_comparator_full_mode_uses_bars_and_ledger_mode_is_labelled_not_comparable(tmp_path):
    c = _load("analysis/rvol_exec/causal_first10_sim.py", "causal_first10_sim")
    # hermetic (O-20): rank_list_for prefers logs/observation/rvol_shadow-<date>.json;
    # on main that REAL file exists for 2026-09-11, so without this the test ranks
    # production names and silently drops to ledger mode (found at the 09-14 merge)
    c.SHADOW = tmp_path / "no_shadow"
    day = "2026-09-11"
    hms = ["09:40", "09:45", "09:50", "09:55", "10:00", "10:05", "10:10", "10:15", "10:20", "09:35", "10:00", "10:05"]
    bars = {f"N{i:02d}": _cross_bars(hm) for i, hm in enumerate(hms)}
    fresh = tmp_path / "fresh" / day
    fresh.mkdir(parents=True)
    for s, df in bars.items():
        df.to_parquet(fresh / f"{s}.parquet")
    trades = [{"ticker": s, "rank": i + 1, "stop_dist": 0.4, "fill_t": hms[i],
               "r_tiers": {"stress_2x": 0.5}} for i, s in enumerate(bars)]
    # the nightly ledger carries the rank-priority top-10 under `selection` and the
    # rest of the triggered names (with their stop_dist) under `selection_exec`
    rec = {"date": day, "selection": {"n": SLOTS, "mean_R": 0.5, "trades": trades[:SLOTS], "skips": [["N11", "doji"]]},
           "selection_exec": {"trades": trades[SLOTS:]}}
    row = c.replay_day(rec, "selection", "09:35", tmp_path / "fresh")
    assert row["mode"] == "full" and row["stamp"] == STAMP
    names = row["causal_first10"]["names"]
    assert len(names) == SLOTS and names[0] == "N09" and "N11" not in names
    # 11 candidates (N11 skipped): the 10th crossing is N07's 10:15 bar; N08 (10:20) is cut
    assert row["cut_bar"] == "10:15" and row["cancelled_slots"] == ["N08"]
    assert names[-1] == "N07" and "N10" in names
    # ledger mode: no bars on disk -> subset re-order, labelled
    row2 = c.replay_day(rec, "selection", "09:35", tmp_path / "nowhere")
    assert row2["mode"] == "ledger" and "NOT the comparator" in row2["causal_first10"]["note"]
    assert row2["causal_first10"]["mean_R_open_exit"] is None


def test_comparator_reads_the_last_record_per_date(tmp_path):
    c = _load("analysis/rvol_exec/causal_first10_sim.py", "causal_first10_sim")
    # hermetic (O-20): rank_list_for prefers logs/observation/rvol_shadow-<date>.json;
    # on main that REAL file exists for 2026-09-11, so without this the test ranks
    # production names and silently drops to ledger mode (found at the 09-14 merge)
    c.SHADOW = tmp_path / "no_shadow"
    led = tmp_path / "sleeve.jsonl"
    led.write_text('{"date": "2026-09-10", "selection": {"n": 1}}\n'
                   '{"date": "2026-09-10", "selection": {"n": 2}}\n'
                   '{"date": "2026-09-01", "selection": {"n": 9}}\n')
    recs = c.load_records(led, "2026-09-05")
    assert [r["date"] for r in recs] == ["2026-09-10"] and recs[0]["selection"]["n"] == 2


def test_shadow_file_forces_dry_run_in_the_daemon():
    d = _load("scripts/rvol_exec_daemon.py", "rvol_exec_daemon")
    from tests.rvol_exec.fake_ib import FakeIB
    assert d.shadow_forced() is False
    lane, client = d.build_lane("2026-09-15", 4002, dry_run=False, ib=FakeIB(), polygon=object())
    assert lane.dry_run is False and client.available() is False       # connect=False: nothing opened
    paths.SHADOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.SHADOW_FILE.write_text("tuesday shadow day")
    assert d.shadow_forced() is True
    lane2, _ = d.build_lane("2026-09-15", 4002, dry_run=False, ib=FakeIB(), polygon=object())
    assert lane2.dry_run is True and lane2.ledger.dry_run is True
    # --flatten-now bypasses SHADOW: a flatten is never stamped dry_run (dry_run gates entries only)
    lane3, _ = d.build_lane("2026-09-15", 4002, dry_run=False, ib=FakeIB(), polygon=object(), force_live=True)
    assert lane3.dry_run is False
    assert str(paths.SHADOW_FILE).startswith(str(paths.LANE_DIR))


def test_comparator_labels_a_day_partial_when_rank_names_lack_an_atr(tmp_path):
    c = _load("analysis/rvol_exec/causal_first10_sim.py", "causal_first10_sim")
    # hermetic (O-20): rank_list_for prefers logs/observation/rvol_shadow-<date>.json;
    # on main that REAL file exists for 2026-09-11, so without this the test ranks
    # production names and silently drops to ledger mode (found at the 09-14 merge)
    c.SHADOW = tmp_path / "no_shadow"
    day = "2026-09-11"
    bars = {"N00": _cross_bars("09:40"), "N01": _cross_bars("09:45")}
    fresh = tmp_path / "fresh" / day
    fresh.mkdir(parents=True)
    for s, df in bars.items():
        df.to_parquet(fresh / f"{s}.parquet")
    trades = [{"ticker": "N00", "rank": 1, "stop_dist": 0.4, "fill_t": "09:40", "r_tiers": {"stress_2x": 0.5}}]
    rec = {"date": day, "selection": {"n": 1, "mean_R": 0.5, "trades": trades, "skips": []},
           "selection_exec": {"trades": [{"ticker": "N01", "rank": 2, "fill_t": "09:45", "r_tiers": {"stress_2x": 0.1}}]}}
    row = c.replay_day(rec, "selection", "09:35", tmp_path / "fresh")
    assert row["mode"] == "partial" and row["causal_first10"]["missing_atr"] == ["N01"]
    assert "partial" in row["causal_first10"]["note"]
    assert row["causal_first10"]["trades"][0]["ticker"] == "N00"           # per-trade rows for the scorer


# ── §20 scorer (analysis/rvol_exec/exec_gap_scorer.py) — known answers ──────
def _paper_ledger_day(day="2026-09-16"):
    """A counted session: A (long, exact, stopped), B (short, exact, flattened),
    C (long, orb_source=snapshot -> non-comparable), E (paper-only fill on a
    comparable name), D never filled in paper (sim fills it)."""
    from src.rvol_exec.constants import STAMP as ST
    base = {"date": day, "lane": "rvol_exec", "stamp": ST, "ts_et": f"{day}T10:00:00-04:00"}
    top = [{"rank": 1, "ticker": "A", "side": "long", "stop_dist": 0.4, "orb_source": "exact"},
           {"rank": 2, "ticker": "B", "side": "short", "stop_dist": 0.5, "orb_source": "exact"},
           {"rank": 3, "ticker": "C", "side": "long", "stop_dist": 0.4, "orb_source": "snapshot"},
           {"rank": 4, "ticker": "D", "side": "long", "stop_dist": 0.4, "orb_source": "exact"},
           {"rank": 5, "ticker": "E", "side": "long", "stop_dist": 0.4, "orb_source": "exact"},
           {"rank": 6, "ticker": "F", "side": "long", "stop_dist": 0.4, "orb_source": "exact"}]
    ev = [dict(base, event="session_start"),
          dict(base, event="t_ready", mode="realtime", delay_s=3.0, contamination_s=0.0),
          dict(base, event="rank", top20=top, foreign_excluded=[]),
          dict(base, event="name_skipped", symbol="F", reason="cap"),
          *[dict(base, event="order_placed", symbol=s) for s in "ABCDE"],
          dict(base, event="entry_filled", symbol="A", avg_fill=10.52, slip_bps_vs_level=4.0),
          dict(base, event="entry_filled", symbol="B", avg_fill=9.88, slip_bps_vs_level=6.0),
          dict(base, event="entry_filled", symbol="C", avg_fill=10.52, slip_bps_vs_level=2.0),
          dict(base, event="entry_filled", symbol="E", avg_fill=10.52, slip_bps_vs_level=8.0),
          dict(base, event="stopped", symbol="A", exit_vwap=10.10, slip_bps_vs_stop=3.0, r_booked=-1.05),
          dict(base, event="flattened", symbol="B", exit_vwap=9.50, r_booked=0.76),
          dict(base, event="flattened", symbol="C", exit_vwap=10.60, r_booked=0.2),
          dict(base, event="flattened", symbol="E", exit_vwap=10.60, r_booked=0.2),
          dict(base, event="session_closed", how="flat", unflattened=False, slot_order=["A", "B", "C", "E"],
               commissions=4.0)]
    return ev


def _causal_rows(day="2026-09-16", mode="full"):
    tr = [{"ticker": "A", "rank": 1, "side": "L", "fill_t": "09:40", "reason": "stop", "exit_open": 10.1,
           "r_tiers": {"stress_2x": -1.0}},
          {"ticker": "B", "rank": 2, "side": "S", "fill_t": "09:45", "reason": "eod", "exit_open": 9.52,
           "r_tiers": {"stress_2x": 0.8}},
          {"ticker": "C", "rank": 3, "side": "L", "fill_t": "09:50", "reason": "eod", "exit_open": 10.6,
           "r_tiers": {"stress_2x": 0.1}},
          {"ticker": "D", "rank": 4, "side": "L", "fill_t": "09:55", "reason": "eod", "exit_open": 10.6,
           "r_tiers": {"stress_2x": 0.1}}]
    return [{"date": day, "mode": mode, "causal_first10": {"trades": tr, "names": [t["ticker"] for t in tr]}}]


def test_exec_gap_scorer_known_answers(tmp_path):
    sc = _load("analysis/rvol_exec/exec_gap_scorer.py", "exec_gap_scorer")
    # R_paper formula: signed gross / sd minus 2 * per_share_side(stress_2x) / sd; slippage is real
    assert sc.r_paper("long", 10.52, 10.10, 0.4) == pytest.approx(-1.05 - 0.05)
    assert sc.r_paper("short", 9.88, 9.50, 0.5) == pytest.approx(0.76 - 0.04)
    res = sc.score(_paper_ledger_day(), {r["date"]: r for r in _causal_rows()})
    assert res["counted_sessions"] == 1 and res["excluded"] == {} and res["verdict"] == "INSUFFICIENT"
    m = res["metrics"]
    # aligned = A, B (C is orb_source=snapshot -> non-comparable; F cap-skipped; D sim-only; E paper-only)
    assert m["mean_R_gap_at_stress_2x"]["n_aligned"] == 2
    assert m["mean_R_gap_at_stress_2x"]["value"] == pytest.approx(((-1.10) - (-1.0) + (0.72 - 0.8)) / 2, abs=1e-4)
    assert m["mean_R_gap_at_stress_2x"]["pass"] is True
    assert m["fill_rate"] == {"value": pytest.approx(2 / 3, abs=1e-3), "both": 2, "sim": 3, "pass": False}
    assert m["overfill_rate"]["paper_only"] == 1 and m["overfill_rate"]["value"] == pytest.approx(1 / 3, abs=1e-3)
    assert m["realized_slippage_bps"]["entry_median"] == 5.0 and m["realized_slippage_bps"]["n_entries"] == 4
    assert m["realized_slippage_bps"]["stop_median"] == 3.0
    # B flattened at 9.50 vs the sim's 15:55 open 9.52: short, buying back below the open is favourable -> -21.0 bps
    assert m["realized_slippage_bps"]["flatten_vs_1555_open_median"] == pytest.approx(-21.01, abs=0.1)
    assert m["stop_integrity"]["pass"] is True and m["integrity_hard"]["pass"] is True
    assert res["diagnostics"]["non_comparable_names"] == {"2026-09-16": {"C": "orb_source=snapshot", "F": "cap"}}
    assert res["diagnostics"]["realtime_share"] == 1.0 and res["diagnostics"]["slot_order_mismatch_sessions"] == 0
    # exclusions: dry_run, kill switch, unflattened, comparator not 'full'
    ev = _paper_ledger_day()
    res2 = sc.score(ev, {r["date"]: r for r in _causal_rows(mode="partial")})
    assert res2["counted_sessions"] == 0 and res2["excluded"] == {"2026-09-16": "comparator_mode=partial"}
    ev3 = ev + [dict(ev[0], event="kill_switch_observed")]
    assert sc.score(ev3, {r["date"]: r for r in _causal_rows()})["excluded"] == {"2026-09-16": "kill_switch"}
    ev4 = [dict(e, dry_run=True) for e in ev]
    assert sc.score(ev4, {r["date"]: r for r in _causal_rows()})["excluded"] == {"2026-09-16": "dry_run"}
    # CLI end to end on files under tmp
    led = tmp_path / "paper.jsonl"
    led.write_text("\n".join(json.dumps(e) for e in ev) + "\n")
    cz = tmp_path / "causal.json"
    cz.write_text(json.dumps(_causal_rows()))
    out = tmp_path / "score.json"
    assert sc.main(["--ledger", str(led), "--causal", str(cz), "--json", str(out)]) == 0
    assert json.loads(out.read_text())["verdict"] == "INSUFFICIENT"
