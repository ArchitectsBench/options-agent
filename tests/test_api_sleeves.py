"""RVOL-RANK cockpit panel (src/api/sleeves._rvolrank) — 2026-09-13.

Every test writes a SYNTHETIC ledger + criteria file to tmp_path and
monkeypatches sleeves.RVOL / sleeves.CRITERIA. The production ledger and the
frozen criteria file are never written (O-20 seal, tests/conftest.py); the
one read-only test against the real criteria file only READS it.

Run: .venv/bin/python -m pytest -q tests/test_api_sleeves.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api import sleeves  # noqa: E402

CRITERIA_YAML = """\
window_start: "{window_start}T00:00:00-07:00"
acceptance:
  min_sim_sessions: 60
  selection_vs_control_gap_min: 0.10
  worst_month_mean_R_min: -0.25
  min_trades_selection: 400
amendment_a1_2026_07_23:
  gate_primary_lane: selection_exec
  deflation:
    selection_t_min_DEFLATED: 2.9
  excluded_from_gates:
{excluded}
  binding_constraint:
    hard_min_exec_selection_trades: 400
  cost_tiers:
    gate_binding_tier: stress_2x
"""


def _trade(r: float, tiers: dict | None = None, pnl: float | None = None) -> dict:
    t = {"r": r, "pnl": pnl if pnl is not None else round(r * 100, 2),
         "fill_t": "10:05", "ticker": "SYN", "reason": "eod"}
    if tiers is not None:
        t["r_tiers"] = tiers
    else:
        t["r_tiers"] = {"base": r, "stress_2x": r - 0.1, "stress_3x": r - 0.2}
    return t


def _lane(trades: list[dict]) -> dict:
    n = len(trades)
    return {"n": n, "pnl": round(sum(t["pnl"] for t in trades), 2),
            "mean_R": round(sum(t["r"] for t in trades) / n, 4) if n else None,
            "skips": [], "trades": trades}


def _row(day: str, generated: str | None = None, *, sel=(), ctl=(),
         sel_x=None, ctl_x=None) -> dict:
    """A ledger row. sel/ctl = idealized 09:35 lanes (always present);
    sel_x/ctl_x = exec 09:45 lanes (None => pre-A1 row without exec lanes)."""
    row = {"date": day, "generated": generated or f"{day}T21:47:00",
           "stamp": "UNVALIDATED — PAPER ONLY",
           "selection": _lane(list(sel)), "control": _lane(list(ctl))}
    if sel_x is not None or ctl_x is not None:
        row["gate_primary_lane"] = "selection_exec (09:45)"
        row["selection_exec"] = _lane(list(sel_x or []))
        row["control_exec"] = _lane(list(ctl_x or []))
    return row


@pytest.fixture
def synth(tmp_path, monkeypatch):
    """Returns a writer: synth(rows, window_start=..., excluded=[...])."""
    def _write(rows, window_start="2026-07-20", excluded=()):
        ledger = tmp_path / "rvolrank_sleeve.jsonl"
        ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
        exc = "".join(f'    - "{d}"\n' for d in excluded) or "    []\n"
        crit = tmp_path / "criteria.yaml"
        crit.write_text(CRITERIA_YAML.format(window_start=window_start, excluded=exc.rstrip("\n")))
        monkeypatch.setattr(sleeves, "RVOL", ledger)
        monkeypatch.setattr(sleeves, "CRITERIA", crit)
        return sleeves.build_sleeves()["rvolrank"]
    return _write


# ------------------------------------------------------------- hygiene -----
def test_excludes_pre_window_and_excluded_dates(synth):
    # 07-21 is IN-WINDOW and excluded_from_gates: it must be dropped by the
    # exclusion clause alone (07-15 is also pre-window, so it never isolates
    # that branch).
    p = synth([
        _row("2026-07-13", sel_x=[_trade(9.0)]),               # pre-window
        _row("2026-07-15", sel_x=[_trade(9.0)]),               # excluded + pre-window
        _row("2026-07-20", sel_x=[_trade(1.0)]),
        _row("2026-07-21", sel_x=[_trade(9.0)]),               # in-window, excluded
        _row("2026-07-22", sel_x=[_trade(2.0)]),
    ], excluded=["2026-07-15", "2026-07-21"])
    h = p["hygiene"]
    assert h["rows_total"] == 5
    assert h["rows_excluded_pre_window"] == 3
    assert h["excluded_dates"] == ["2026-07-13", "2026-07-15", "2026-07-21"]
    assert h["sessions_valid"] == 2
    assert h["window_start"] == "2026-07-20"
    # An in-window excluded date HAS a ledger row: reported as excluded, not
    # double-counted as a missing weekday.
    assert h["missing_weekdays"] == []
    assert p["gate_primary"]["selection"]["n"] == 2          # the 9.0 trades are gone
    assert p["gate_primary"]["selection"]["mean_R"] == 1.5


def test_dedupe_keeps_later_generated_even_when_written_first(synth):
    # The later-generated (supersede repair) row is written FIRST in the file;
    # keeping "last row in file order" would wrongly keep the 0.5 trade.
    p = synth([
        _row("2026-07-20", "2026-07-20T23:28:30", sel_x=[_trade(2.0)]),
        _row("2026-07-20", "2026-07-20T23:05:35", sel_x=[_trade(0.5)]),
        _row("2026-07-21", sel_x=[_trade(1.0)]),
    ])
    assert p["hygiene"]["duplicate_dates"] == ["2026-07-20"]
    assert p["hygiene"]["sessions_valid"] == 2
    assert p["gate_primary"]["selection"]["n"] == 2
    assert p["gate_primary"]["selection"]["mean_R"] == 1.5   # (2.0 + 1.0) / 2


def test_missing_weekdays_skips_labor_day(synth):
    rows = [_row(d, sel_x=[_trade(1.0)]) for d in ("2026-09-03", "2026-09-04", "2026-09-08")]
    p = synth(rows, window_start="2026-09-03")
    assert p["hygiene"]["missing_weekdays"] == []              # 09-05/06 weekend, 09-07 holiday
    p = synth([rows[0], rows[2]], window_start="2026-09-03")
    assert p["hygiene"]["missing_weekdays"] == ["2026-09-04"]


def test_missing_weekdays_uses_max_date_not_last_row(synth):
    # catch-up replay appended out of order: the last ROW is 09-02, the last DATE is 09-04
    p = synth([_row("2026-09-01", sel_x=[_trade(1.0)]),
               _row("2026-09-04", sel_x=[_trade(1.0)]),
               _row("2026-09-02", "2026-09-07T15:37:48", sel_x=[_trade(1.0)])],
              window_start="2026-09-01")
    assert p["hygiene"]["last_ledger_date"] == "2026-09-04"
    assert p["hygiene"]["missing_weekdays"] == ["2026-09-03"]


# ------------------------------------------------------ gate-primary block --
def test_exec_lanes_aggregated_not_idealized(synth):
    p = synth([_row("2026-07-23",
                    sel=[_trade(5.0), _trade(5.0), _trade(5.0)],
                    ctl=[_trade(4.0)],
                    sel_x=[_trade(1.0), _trade(2.0)],
                    ctl_x=[_trade(0.5)])])
    gp = p["gate_primary"]
    assert gp["label"].startswith("GATE-PRIMARY")
    assert gp["selection"]["n"] == 2 and gp["selection"]["mean_R"] == 1.5
    assert gp["control"]["n"] == 1 and gp["control"]["mean_R"] == 0.5
    assert gp["gap"] == 1.0
    ideal = p["idealized"]
    assert ideal["status"] == "measurement_only"
    assert ideal["selection"]["n"] == 3 and ideal["selection"]["mean_R"] == 5.0
    assert ideal["gap"] == 1.0
    assert p["recent_days"][0]["lane"] == "exec"
    assert p["recent_days"][0]["sel_n"] == 2


def test_t_stat_on_known_sample(synth):
    p = synth([_row("2026-07-23", sel_x=[_trade(r) for r in (1, 2, 3, 4, 5)])])
    s = p["gate_primary"]["selection"]
    assert s["n"] == 5 and s["mean_R"] == 3.0
    assert s["sd"] == pytest.approx(1.5811, abs=1e-4)
    assert s["t"] == pytest.approx(3.0 / (1.5811 / 5 ** 0.5), abs=1e-3)   # 4.2426
    assert p["gate_primary"]["flags"]["raw_r_t_vs_bar"] is True            # 4.24 >= 2.9


def test_t_none_when_fewer_than_two_trades(synth):
    p = synth([_row("2026-07-23", sel_x=[_trade(1.0)], ctl_x=[_trade(0.2)])])
    s = p["gate_primary"]["selection"]
    assert s["n"] == 1 and s["mean_R"] == 1.0 and s["sd"] is None and s["t"] is None
    assert p["gate_primary"]["flags"]["raw_r_t_vs_bar"] is None
    assert p["gate_primary"]["flags"]["stress_2x_t_vs_bar"] is None
    assert p["gate_primary"]["gap"] == 0.8


def test_stress_tiers_from_r_tiers(synth):
    p = synth([_row("2026-07-23",
                    sel_x=[_trade(1.0, {"base": 0.9, "stress_2x": 0.5, "stress_3x": 0.1}),
                           _trade(2.0, {"base": 1.9, "stress_2x": 1.0, "stress_3x": 0.5})],
                    ctl_x=[_trade(0.3, {"base": 0.25, "stress_2x": 0.1, "stress_3x": 0.0}),
                           _trade(0.3, {"base": 0.25, "stress_2x": 0.1, "stress_3x": 0.0})])])
    gp = p["gate_primary"]
    tiers = gp["tiers"]
    assert tiers["base"]["selection_mean_R"] == 1.4
    assert tiers["stress_2x"]["selection_mean_R"] == 0.75
    assert tiers["stress_2x"]["control_mean_R"] == 0.1
    assert tiers["stress_2x"]["gap"] == 0.65
    assert tiers["stress_2x"]["selection_t"] == pytest.approx(0.75 / (0.35355 / 2 ** 0.5), abs=1e-3)
    assert tiers["stress_3x"]["selection_mean_R"] == 0.3
    assert gp["gate_binding_tier"] == "stress_2x"
    assert gp["flags"]["stress_2x_gap_vs_bar"] is True
    assert gp["selection"]["mean_R"] == 1.5                    # raw r untouched
    # The binding tier's worst month is computed on r_tiers[stress_2x], not raw r.
    assert gp["worst_month_stress_2x"] == {"month": "2026-07", "n": 2, "mean_R": 0.75}
    assert gp["binding"]["worst_month"] == gp["worst_month_stress_2x"]
    assert gp["binding"]["tier"] == "stress_2x"
    assert gp["binding"]["selection_mean_R"] == 0.75 and gp["binding"]["gap"] == 0.65
    assert gp["flags"]["stress_2x_worst_month_vs_bar"] is True   # 0.75 >= -0.25
    assert gp["worst_month"] == {"month": "2026-07", "n": 2, "mean_R": 1.5}


def test_raw_r_and_binding_tier_flags_can_diverge(synth):
    # The misrepresentation the panel must never make: raw `r` clears every
    # bar while the gate-binding stress_2x tier clears none. Both flag
    # families are keyed by basis so the UI cannot conflate them.
    sel = [_trade(r, {"base": r, "stress_2x": r - 3.0, "stress_3x": r - 4.0})
           for r in (1.0, 1.1, 1.2, 1.3, 1.4)]
    ctl = [_trade(0.0, {"base": 0.0, "stress_2x": 0.0, "stress_3x": 0.0})] * 2
    p = synth([_row("2026-07-23", sel_x=sel, ctl_x=ctl)])
    gp = p["gate_primary"]
    f = gp["flags"]
    assert gp["selection"]["t"] > 2.9 and gp["gap"] == 1.2
    assert f["raw_r_t_vs_bar"] is True
    assert f["raw_r_gap_vs_bar"] is True
    assert f["raw_r_worst_month_vs_bar"] is True
    assert gp["binding"]["selection_mean_R"] == -1.8 and gp["binding"]["gap"] == -1.8
    assert f["stress_2x_t_vs_bar"] is False
    assert f["stress_2x_gap_vs_bar"] is False
    assert f["stress_2x_worst_month_vs_bar"] is False
    assert f["stamp"] == "interim, NOT a gate judgment"
    assert "continuity" in gp["raw_r_basis"] and "not a cost tier" in gp["raw_r_basis"]


def test_worst_calendar_month(synth):
    p = synth([_row("2026-07-23", sel_x=[_trade(-1.0), _trade(-1.0)]),
               _row("2026-08-03", sel_x=[_trade(1.0), _trade(1.0)]),
               _row("2026-08-04", sel_x=[_trade(0.0)])])
    gp = p["gate_primary"]
    assert gp["worst_month"] == {"month": "2026-07", "n": 2, "mean_R": -1.0}
    assert gp["months"]["2026-08"] == {"n": 3, "mean_R": 0.6667}
    assert gp["flags"]["raw_r_worst_month_vs_bar"] is False     # -1.0 < -0.25
    # default _trade tiers: stress_2x = r - 0.1
    assert gp["worst_month_stress_2x"] == {"month": "2026-07", "n": 2, "mean_R": -1.1}
    assert gp["flags"]["stress_2x_worst_month_vs_bar"] is False


def test_projection_arithmetic(synth):
    # 10 exec sessions x 10 trades = 100 -> 300 short at 10/session -> 30
    # sessions after Fri 2026-09-04, skipping Labor Day 09-07 => Mon 2026-10-19.
    days = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
            "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    p = synth([_row(d, sel_x=[_trade(0.1)] * 10) for d in days], window_start="2026-08-24")
    pr = p["gate_primary"]["progress"]
    assert p["hygiene"]["missing_weekdays"] == []
    assert pr["exec_trades"] == 100 and pr["exec_trades_goal"] == 400
    assert pr["sessions_valid"] == 10 and pr["sessions_goal"] == 60
    assert pr["exec_sessions"] == 10
    assert pr["run_rate"] == 10.0
    assert pr["run_rate_basis"] == "selection_exec trades / 10 exec sessions"
    assert pr["projected_sessions_to_400"] == 30
    assert pr["projected_date_400"] == "2026-10-19"
    assert p["gate_primary"]["selection"]["t"] is None          # identical R => sd 0 => t None


def test_projection_rounds_up_on_remainder_and_skips_thanksgiving(synth):
    # 3 exec sessions x 7 trades = 21 -> 379 short at 7/session = 54.14 -> ceil 55
    # trading days after Thu 2026-09-10 => Fri 2026-11-27 (11-26 is Thanksgiving).
    p = synth([_row(d, sel_x=[_trade(0.2)] * 7)
               for d in ("2026-09-08", "2026-09-09", "2026-09-10")],
              window_start="2026-09-08")
    pr = p["gate_primary"]["progress"]
    assert pr["exec_trades"] == 21 and pr["run_rate"] == 7.0
    assert pr["projected_sessions_to_400"] == 55
    assert pr["projected_date_400"] == "2026-11-27"
    assert p["gate_primary"]["selection"]["t"] is None


def test_projection_zero_when_hard_min_met(synth):
    p = synth([_row("2026-07-23", sel_x=[_trade(0.1)] * 400)])
    pr = p["gate_primary"]["progress"]
    assert pr["exec_trades"] == 400 and pr["projected_sessions_to_400"] == 0
    assert pr["projected_date_400"] is None


def test_run_rate_denominator_is_exec_sessions_only(synth):
    # 2 idealized-only sessions + 2 exec sessions with 6 trades: rate = 6/2, not 6/4
    p = synth([_row("2026-07-20", sel=[_trade(1.0)]),
               _row("2026-07-21", sel=[_trade(1.0)]),
               _row("2026-07-23", sel_x=[_trade(1.0)] * 3),
               _row("2026-07-24", sel_x=[_trade(1.0)] * 3)])
    assert p["hygiene"]["sessions_valid"] == 4
    assert p["hygiene"]["exec_sessions"] == 2
    assert p["gate_primary"]["progress"]["run_rate"] == 3.0
    assert p["gate_primary"]["progress"]["run_rate_basis"] == "selection_exec trades / 2 exec sessions"
    assert p["gate_primary"]["selection"]["t"] is None          # identical R
    assert p["recent_days"][-1]["lane"].startswith("idealized (fallback")


def test_nightly_binding_constraint_passthrough_uses_latest_date(synth):
    # The nightly's own A1-required run-rate/projection is surfaced from the
    # latest-DATE row, even when a catch-up replay appended an older date last.
    r1 = _row("2026-09-01", sel_x=[_trade(1.0)])
    r2 = _row("2026-09-03", sel_x=[_trade(1.0)])
    r2["binding_constraint"] = {"exec_selection_trades_cum": 2, "gate_valid_sessions": 2,
                                "trades_per_session_run_rate": 1.0,
                                "projected_sessions_to_400": 398, "hard_min": 400}
    r3 = _row("2026-09-02", "2026-09-05T10:00:00", sel_x=[_trade(1.0)])
    r3["binding_constraint"] = {"trades_per_session_run_rate": 99.0}
    p = synth([r1, r2, r3], window_start="2026-09-01")
    bc = p["gate_primary"]["progress"]["nightly_binding_constraint"]
    assert bc["trades_per_session_run_rate"] == 1.0 and bc["projected_sessions_to_400"] == 398


def test_ledger_with_no_exec_lanes_yields_none_not_exception(synth):
    p = synth([_row("2026-07-20", sel=[_trade(0.7)], ctl=[_trade(0.2)]),
               _row("2026-07-21", sel=[_trade(0.3)], ctl=[_trade(0.1)])])
    gp = p["gate_primary"]
    assert gp["exec_sessions"] == 0
    assert gp["selection"] == {"n": 0, "pnl": 0.0, "mean_R": None, "sd": None, "t": None, "skips": 0}
    assert gp["gap"] is None
    assert gp["tiers"]["stress_2x"]["selection_mean_R"] is None
    assert gp["tiers"]["stress_2x"]["gap"] is None
    assert gp["worst_month"] is None and gp["months"] == {}
    assert gp["worst_month_stress_2x"] is None and gp["binding"]["worst_month"] is None
    pr = gp["progress"]
    assert pr["run_rate"] is None and pr["projected_sessions_to_400"] is None
    assert pr["projected_date_400"] is None and pr["nightly_binding_constraint"] is None
    assert all(v is None for k, v in gp["flags"].items() if k != "stamp")
    assert p["idealized"]["selection"]["n"] == 2 and p["idealized"]["gap"] == 0.35
    assert [d["lane"] for d in p["recent_days"]] == ["idealized (fallback, pre-exec row)"] * 2
    json.dumps(p, allow_nan=False)


def test_empty_ledger(synth):
    p = synth([])
    assert p["hygiene"]["sessions_valid"] == 0
    assert p["hygiene"]["missing_weekdays"] == []
    assert p["gate_primary"]["selection"]["n"] == 0
    assert p["recent_days"] == []


# --------------------------------------------------------- stamps/criteria --
def test_stamps_and_labels(synth):
    p = synth([_row("2026-07-23", sel_x=[_trade(1.0)])])
    assert p["stamp"] == "UNVALIDATED — PAPER ONLY"
    assert p["stamp_interim"] == "interim, NOT a gate judgment"
    gp = p["gate_primary"]
    assert gp["flags"]["stamp"] == "interim, NOT a gate judgment"
    assert set(gp["flags"]) == {
        "stamp", "stress_2x_t_vs_bar", "stress_2x_gap_vs_bar", "stress_2x_worst_month_vs_bar",
        "raw_r_t_vs_bar", "raw_r_gap_vs_bar", "raw_r_worst_month_vs_bar"}
    # trial_count_N added 2026-09-13 with the A2a registry re-freeze (N 27 -> 30);
    # this fixture carries only the A1 block, so it stays at the frozen default.
    assert gp["bars"] == {
        "t_min_deflated": 2.9, "gap_min": 0.10, "worst_month_mean_R_min": -0.25,
        "min_sim_sessions": 60, "hard_min_exec_selection_trades": 400,
        "trial_count_N": 27}
    assert gp["gate_binding_tier"] == "stress_2x" and gp["binding"]["tier"] == "stress_2x"
    assert "worst_month_stress_2x" in gp
    assert gp["raw_r_basis"].startswith("r = net of base slippage, gross of commission")
    assert p["idealized"]["label"] == "measurement-only (09:35, no real-time data)"


def test_missing_criteria_file_degrades_with_flag(tmp_path, monkeypatch):
    ledger = tmp_path / "l.jsonl"
    ledger.write_text(json.dumps(_row("2026-07-23", sel_x=[_trade(1.0)])) + "\n")
    monkeypatch.setattr(sleeves, "RVOL", ledger)
    monkeypatch.setattr(sleeves, "CRITERIA", tmp_path / "nope.yaml")
    p = sleeves.build_sleeves()["rvolrank"]
    assert p["hygiene"]["criteria_loaded"] is False
    assert p["hygiene"]["window_start"] == "2026-07-20"       # frozen default
    assert p["gate_primary"]["bars"]["t_min_deflated"] == 2.9


TWO_AMENDMENT_YAML = """\
window_start: "2026-07-28T00:00:00-07:00"
acceptance:
  min_sim_sessions: 60
  selection_vs_control_gap_min: 0.10
  worst_month_mean_R_min: -0.25
amendment_a1_2026_07_23:
  deflation: {selection_t_min_DEFLATED: 2.9}
  excluded_from_gates: ["2026-07-15", "2026-07-16", "2026-07-17"]
  binding_constraint: {hard_min_exec_selection_trades: 400}
  cost_tiers: {gate_binding_tier: stress_2x}
amendment_a2_2026_09_14:
  excluded_from_gates: ["2026-07-29", "2026-07-30", "2026-07-31"]
  deflation: {selection_t_min_DEFLATED: 3.1}
"""


def test_criteria_unions_every_amendment_block(tmp_path, monkeypatch):
    # Amend-only by dated block: a future A2 must not be silently ignored.
    # Exclusions union across blocks; a later block's scalar wins; scalars the
    # later block does not mention survive from the earlier one.
    crit = tmp_path / "criteria.yaml"
    crit.write_text(TWO_AMENDMENT_YAML)
    monkeypatch.setattr(sleeves, "CRITERIA", crit)
    c = sleeves._criteria()
    assert c["loaded"] is True
    assert c["amendment_blocks"] == ["amendment_a1_2026_07_23", "amendment_a2_2026_09_14"]
    assert c["excluded_from_gates"] == ["2026-07-15", "2026-07-16", "2026-07-17",
                                        "2026-07-29", "2026-07-30", "2026-07-31"]
    assert c["selection_t_min_deflated"] == 3.1
    assert c["hard_min_exec_selection_trades"] == 400 and c["gate_binding_tier"] == "stress_2x"
    # ...and the panel applies the unioned exclusions to the ledger.
    ledger = tmp_path / "l.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in (
        _row("2026-07-28", sel_x=[_trade(1.0)]),
        _row("2026-07-29", sel_x=[_trade(9.0)]),
        _row("2026-07-30", sel_x=[_trade(9.0)]),
        _row("2026-07-31", sel_x=[_trade(9.0)]),
        _row("2026-08-03", sel_x=[_trade(2.0)]))))
    monkeypatch.setattr(sleeves, "RVOL", ledger)
    p = sleeves.build_sleeves()["rvolrank"]
    assert p["hygiene"]["excluded_dates"] == ["2026-07-29", "2026-07-30", "2026-07-31"]
    assert p["hygiene"]["criteria_amendment_blocks"] == c["amendment_blocks"]
    assert p["hygiene"]["missing_weekdays"] == []            # excluded rows exist; not "missing"
    assert p["gate_primary"]["selection"]["n"] == 2 and p["gate_primary"]["selection"]["mean_R"] == 1.5
    assert p["gate_primary"]["bars"]["t_min_deflated"] == 3.1


def test_real_criteria_file_parses_read_only():
    # READ-ONLY on the frozen file: window_start + A1 exclusions + deflated bar.
    # Membership (not equality) on the block list so an appended A2 does not
    # require a test edit; the A1 exclusions are frozen and asserted as a subset.
    c = sleeves._criteria()
    assert c["loaded"] is True
    assert c["window_start"] == "2026-07-20"
    assert "amendment_a1_2026_07_23" in c["amendment_blocks"]
    assert {"2026-07-15", "2026-07-16", "2026-07-17"} <= set(c["excluded_from_gates"])
    assert c["selection_t_min_deflated"] >= 2.9               # A1 floor; amendments may only raise
    assert c["gap_min"] == 0.10
    assert c["hard_min_exec_selection_trades"] == 400         # "never relaxed"
    assert c["gate_binding_tier"] == "stress_2x"
