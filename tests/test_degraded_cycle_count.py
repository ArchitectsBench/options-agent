"""Hermetic tests for scripts/ops/degraded_cycle_count.py.

Every fixture is synthetic and written to tmp_path — no production log is read
(CLAUDE.md destructive-probe rule: a tool that reads evidence files is tested
against copies, never the originals).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "degraded_cycle_count.py"
_spec = importlib.util.spec_from_file_location("degraded_cycle_count", _MOD_PATH)
dcc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dcc)


def _row(cycle: int, ts: str, **regime) -> str:
    base = {"degraded": False, "missing": [], "stale_feeds": [], "vix": 20.0, "bias": "neutral"}
    base.update(regime)
    return json.dumps({"cycle": cycle, "ts": ts, "regime": base})


def _clean(cycle: int, minute: int) -> str:
    return _row(cycle, f"2026-10-08T07:{minute:02d}:00")


# --- the motivating incident: 6 consecutive degraded cycles in ~23 minutes ---


def _incident_lines() -> list[str]:
    """The 10-08 shape: degraded flag set, missing lists ONE field, three are null."""
    lines = [_clean(5654, 47)]
    for i, minute in enumerate([52, 56, 60, 65, 70, 75]):
        lines.append(
            _row(
                5655 + i,
                f"2026-10-08T07:{minute - 60:02d}:00" if minute >= 60 else f"2026-10-08T06:{minute:02d}:00",
                degraded=True,
                missing=["term_structure"],
                term_structure=None,
                vol_outlook=None,
                richness=None,
            )
        )
    lines.append(_clean(5661, 20))
    return lines


def test_reproduces_the_six_cycle_incident_window():
    r = dcc.collect(_incident_lines())
    assert r["degraded_cycles"] == 6
    assert len(r["windows"]) == 1
    w = r["windows"][0]
    assert (w["start_cycle"], w["end_cycle"], w["cycles"]) == (5655, 5660, 6)
    assert w["span_min"] == pytest.approx(23.0)


def test_counts_null_fields_missing_under_reports():
    """`missing` named only term_structure; all three nulls must be counted."""
    r = dcc.collect(_incident_lines())
    assert r["field_counts"] == {"richness": 6, "term_structure": 6, "vol_outlook": 6}
    assert r["detail"][0]["missing"] == ["term_structure"]


def test_single_open_of_session_degraded_cycle_is_one_not_a_window_run():
    """The routine 10-06/10-07/10-23 shape: exactly one degraded cycle."""
    lines = [
        _row(6502, "2026-10-23T06:35:00", degraded=True,
             missing=["SPY_today", "GEX"], spy=None, qqq=None, gex=None, term_structure=None),
        _clean(6503, 40),
        _clean(6504, 45),
    ]
    r = dcc.collect(lines)
    assert r["degraded_cycles"] == 1
    assert [w["cycles"] for w in r["windows"]] == [1]
    assert max((w["cycles"] for w in r["windows"]), default=0) == 1


def test_clean_day_reports_nothing():
    r = dcc.collect([_clean(1, 10), _clean(2, 15)])
    assert r["degraded_cycles"] == 0
    assert r["windows"] == []
    assert "no degraded cycles" in dcc.render(r)


# --- window grouping ---


def test_non_consecutive_cycles_are_separate_windows():
    lines = [
        _row(10, "2026-10-08T06:30:00", degraded=True, gex=None),
        _row(11, "2026-10-08T06:35:00", degraded=True, gex=None),
        _clean(12, 40),
        _row(13, "2026-10-08T06:45:00", degraded=True, gex=None),
    ]
    r = dcc.collect(lines)
    assert [(w["start_cycle"], w["end_cycle"]) for w in r["windows"]] == [(10, 11), (13, 13)]


def test_missing_cycle_number_is_not_merged_into_a_window():
    lines = [
        _row(10, "2026-10-08T06:30:00", degraded=True, gex=None),
        json.dumps({"ts": "2026-10-08T06:35:00", "regime": {"degraded": True, "gex": None}}),
        _row(11, "2026-10-08T06:40:00", degraded=True, gex=None),
    ]
    r = dcc.collect(lines)
    assert len(r["windows"]) == 3


# --- flag vs nulls, which are counted independently ---


def test_null_field_without_the_flag_is_still_counted():
    r = dcc.collect([_row(5, "2026-10-08T06:30:00", degraded=False, gex=None)])
    assert r["degraded_cycles"] == 1
    assert r["null_without_flag"] == 1
    assert r["flag_only"] == 0


def test_flag_without_any_null_field_is_still_counted():
    r = dcc.collect([_row(5, "2026-10-08T06:30:00", degraded=True, missing=["GEX"])])
    assert r["degraded_cycles"] == 1
    assert r["flag_only"] == 1
    assert r["windows"][0]["fields"] == []


def test_bookkeeping_keys_are_not_feed_fields():
    """A null `stale_feeds`/`vix_note`/`missing` must not read as a degraded feed."""
    r = dcc.collect(
        [json.dumps({"cycle": 1, "ts": "2026-10-08T06:30:00",
                     "regime": {"degraded": False, "missing": None,
                                "stale_feeds": None, "vix_note": None, "vix": 20.0}})]
    )
    assert r["degraded_cycles"] == 0


# --- robustness ---


def test_date_filter_excludes_other_days():
    lines = [
        _row(1, "2026-10-07T06:30:00", degraded=True, gex=None),
        _row(2, "2026-10-08T06:30:00", degraded=True, gex=None),
    ]
    r = dcc.collect(lines, date="2026-10-08")
    assert r["cycles_parsed"] == 1
    assert r["degraded_cycles"] == 1
    assert r["detail"][0]["cycle"] == 2


def test_malformed_and_regimeless_lines_are_skipped_not_fatal():
    lines = ["{not json", "", json.dumps({"cycle": 1}), "[1,2,3]", _clean(2, 10)]
    r = dcc.collect(lines)
    assert r["cycles_parsed"] == 1
    assert r["lines_skipped"] == 3
    assert r["degraded_cycles"] == 0


def test_unparseable_timestamps_give_span_none_not_a_crash():
    lines = [
        _row(1, "not-a-time", degraded=True, gex=None),
        _row(2, "also-not", degraded=True, gex=None),
    ]
    r = dcc.collect(lines)
    assert r["windows"][0]["span_min"] is None
    assert "n/a" in dcc.render(r)


# --- CLI ---


def test_cli_requires_a_path_no_production_default():
    with pytest.raises(SystemExit):
        dcc.main([])


def test_cli_reads_a_tmp_file_and_prints_a_total(tmp_path, capsys):
    p = tmp_path / "obs.jsonl"
    p.write_text("\n".join(_incident_lines()), encoding="utf-8")
    assert dcc.main([str(p)]) == 0
    out = capsys.readouterr().out
    assert "TOTAL degraded_cycles=6 windows=1 longest_window=6" in out
    assert "cycles 5655-5660" in out


def test_cli_json_mode_is_machine_readable(tmp_path, capsys):
    p = tmp_path / "obs.jsonl"
    p.write_text("\n".join(_incident_lines()), encoding="utf-8")
    assert dcc.main([str(p), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["degraded_cycles"] == 6
    assert payload["windows"][0]["end_cycle"] == 5660


def test_cli_unreadable_path_exits_2(tmp_path, capsys):
    assert dcc.main([str(tmp_path / "nope.jsonl")]) == 2
    assert "cannot read" in capsys.readouterr().err
