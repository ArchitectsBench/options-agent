"""Cockpit research fields (2026-09-13 reorganization).

Two stale-data defects the operator hit ("too much data and I couldn't even
read it" / stale values):

1. The deflated t-bar. A1 froze it at 2.90 under `deflation.selection_t_min_
   DEFLATED`; amendment A2a re-froze it UP to 2.94 under a different key shape
   (`registry.deflated_t_bar`, N 27 -> 30). The reader only knew A1's key, so
   the cockpit kept showing a SUPERSEDED bar next to live numbers.
2. The squeeze backtest verdict. A verdict written into a component's source
   keeps being displayed after the result file changes, so it is served from
   the frozen result file and simply absent when that file is.
"""
from __future__ import annotations

import json

import pytest
import yaml

from src.api import sleeves as sl
from src.api import watchlist_squeeze as ws


# ------------------------------------------------------------- deflated bar --
def _criteria_file(tmp_path, blocks: dict) -> object:
    base = {
        "window_start": "2026-07-20T00:00:00-07:00",
        "acceptance": {"min_sim_sessions": 60, "selection_vs_control_gap_min": 0.10,
                       "worst_month_mean_R_min": -0.25},
        "amendment_a1_2026_07_23": {
            "deflation": {"selection_t_min_DEFLATED": 2.9},
            "binding_constraint": {"hard_min_exec_selection_trades": 400},
            "cost_tiers": {"gate_binding_tier": "stress_2x"},
        },
    }
    base.update(blocks)
    f = tmp_path / "criteria.yaml"
    f.write_text(yaml.safe_dump(base, sort_keys=False))
    return f


def test_later_amendment_registry_bar_supersedes_a1(tmp_path, monkeypatch):
    f = _criteria_file(tmp_path, {
        "amendment_a2a_2026_09_13": {"registry": {"trial_count_N": 30, "deflated_t_bar": 2.94}}})
    monkeypatch.setattr(sl, "CRITERIA", f)
    c = sl._criteria()
    assert c["loaded"] is True
    assert c["selection_t_min_deflated"] == 2.94      # not A1's 2.90
    assert c["trial_count_N"] == 30


def test_a1_bar_still_used_when_no_later_registry_block(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "CRITERIA", _criteria_file(tmp_path, {}))
    c = sl._criteria()
    assert c["selection_t_min_deflated"] == 2.9
    assert c["trial_count_N"] == 27                    # frozen default


def test_highest_amendment_block_wins_for_the_bar(tmp_path, monkeypatch):
    f = _criteria_file(tmp_path, {
        "amendment_a2a_2026_09_13": {"registry": {"deflated_t_bar": 2.94}},
        "amendment_a2b_2026_09_20": {"registry": {"deflated_t_bar": 3.01}},
    })
    monkeypatch.setattr(sl, "CRITERIA", f)
    assert sl._criteria()["selection_t_min_deflated"] == 3.01


def test_real_criteria_file_serves_the_current_frozen_bar():
    """The shipped file must not serve a superseded bar (read-only)."""
    c = sl._criteria()
    assert c["loaded"] is True
    assert c["selection_t_min_deflated"] == 2.94
    assert c["trial_count_N"] == 30


# --------------------------------------------------------- backtest verdict --
SUMMARY = {"prereg_id": "SQZBZ-2026-09-13",
           "summary": {"verdict": "FAIL",
                       "decision": {"verdict": "FAIL", "consequence": "scored log",
                                    "gate_t": -0.47, "bar_t": 3.37,
                                    "n_treated_long_valid": 1828,
                                    "sessions_with_treated_long": 863}}}


def test_backtest_verdict_from_results_file(tmp_path):
    p = tmp_path / "results.json"
    p.write_text(json.dumps(SUMMARY))
    b = ws.backtest_verdict(p)["backtest"]
    assert b["verdict"] == "FAIL" and b["bar_t"] == 3.37
    assert b["prereg_id"] == "SQZBZ-2026-09-13" and b["n_treated"] == 1828
    assert b["as_of"]                                  # file mtime, ISO seconds


@pytest.mark.parametrize("body", ["", "{", json.dumps({"summary": {}}), json.dumps([1, 2])])
def test_absent_or_malformed_results_yield_no_verdict(tmp_path, body):
    """No file, no verdict — the panel must say NOTHING rather than something
    stale. Never an exception."""
    p = tmp_path / "results.json"
    if body:
        p.write_text(body)
    assert ws.backtest_verdict(p) == {}


def test_payload_carries_the_verdict_without_breaking_core_fields(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"AAA": {"added": "2026-09-11", "in_zone": True,
                                         "dist_atr": 0.2, "last_seen": "2026-09-11"}}))
    res = tmp_path / "results.json"
    res.write_text(json.dumps(SUMMARY))
    d = ws.build_watchlist_squeeze(state_path=state, outcomes_path=tmp_path / "none.jsonl",
                                   events_hot=tmp_path / "none2.jsonl",
                                   archive_dir=tmp_path / "nodir", results_path=res)
    assert d["stamp"] and len(d["rows"]) == 1          # core fields intact
    assert d["backtest"]["verdict"] == "FAIL"
    assert d["no_claim_stamp"] == ws.NO_CLAIM_STAMP


def test_payload_without_a_results_file_has_no_backtest_key(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({}))
    d = ws.build_watchlist_squeeze(state_path=state, outcomes_path=tmp_path / "n.jsonl",
                                   events_hot=tmp_path / "n2.jsonl",
                                   archive_dir=tmp_path / "nodir",
                                   results_path=tmp_path / "missing.json")
    assert "backtest" not in d
