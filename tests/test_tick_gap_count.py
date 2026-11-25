"""Hermetic tests for scripts/ops/tick_gap_count.py.

O-20 / destructive-probe discipline: every case here writes its own synthetic
log into tmp_path. No test in this file reads production `logs/agent.log`.
"""

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "tick_gap_count",
    Path(__file__).resolve().parents[1] / "scripts" / "ops" / "tick_gap_count.py",
)
tgc = importlib.util.module_from_spec(_SPEC)
# @dataclass resolves annotations through sys.modules, so register before exec.
sys.modules["tick_gap_count"] = tgc
_SPEC.loader.exec_module(tgc)


def _log(tmp_path, lines):
    p = tmp_path / "agent.log"
    p.write_text("\n".join(lines) + "\n")
    return p


def _hb(ts):
    return f"2026-10-08 {ts},123 INFO market closed — heartbeat only"


def _cycle(ts, n):
    return f"2026-10-08 {ts},456 INFO cycle #{n} bias=bearish trend=chop tickers=30"


def test_single_lost_tick_is_counted_though_invisible_to_wake_gap_min(tmp_path):
    """The blind spot this tool exists for: a 10-min pre-open gap = 1 lost tick,
    below WAKE_GAP_MIN = 15 so the engine records no wake_gap_recovery row."""
    p = _log(tmp_path, [_hb("05:35:32"), _hb("05:45:35"), _hb("05:56:00")])
    gaps = tgc.find_gaps(tgc.parse_tick_times(p.read_text().splitlines()))
    assert [g.lost_ticks for g in gaps] == [1, 1]
    s = tgc.summarize(gaps)
    assert s["lost_ticks"] == 2
    assert s["lost_ticks_by_phase"]["pre_open"] == 2
    assert s["lost_ticks_by_phase"]["rth"] == 0
    # The whole point: the engine's own counter would have reported neither.
    assert s["wake_gap_min_would_report"] == 0


def test_on_time_ticks_produce_no_gaps(tmp_path):
    p = _log(tmp_path, [_cycle("07:00:14", 1), _cycle("07:05:14", 2), _cycle("07:10:12", 3)])
    assert tgc.find_gaps(tgc.parse_tick_times(p.read_text().splitlines())) == []


def test_gap_straddling_the_open_is_split_by_phase(tmp_path):
    """Today's real shape: 06:07 -> 06:52 spans the 06:30 open. Slots at 06:12,
    06:17, 06:22, 06:27 are pre_open; 06:32..06:47 are lost RTH cycles."""
    p = _log(tmp_path, [_hb("06:07:27"), _cycle("06:52:30", 5655)])
    gaps = tgc.find_gaps(tgc.parse_tick_times(p.read_text().splitlines()))
    assert len(gaps) == 1
    assert gaps[0].phases == {"pre_open": 4, "rth": 4}
    assert gaps[0].lost_ticks == 8
    assert tgc.summarize(gaps)["wake_gap_min_would_report"] == 1


def test_non_tick_and_unparseable_lines_are_skipped(tmp_path):
    """Restart banners, sweeps and corrupt lines must not mask or invent a gap."""
    p = _log(
        tmp_path,
        [
            _hb("07:00:00"),
            "garbage with no timestamp at all",
            "2026-10-08 07:02:00,000 INFO risk watchdog started",
            "not-a-date 07:03:00 INFO cycle #9999 noise",
            "2026-10-08 07:04:00,000 WARNING wake guard: 4-min tick gap detected",
            _hb("07:05:00"),
        ],
    )
    ticks = tgc.parse_tick_times(p.read_text().splitlines())
    assert len(ticks) == 2  # only the two heartbeat lines
    assert tgc.find_gaps(ticks) == []


def test_date_filter_isolates_one_session(tmp_path):
    p = _log(
        tmp_path,
        [
            "2026-10-07 12:55:00,000 INFO market closed — heartbeat only",
            _hb("06:00:00"),
            _hb("06:25:00"),
        ],
    )
    lines = p.read_text().splitlines()
    assert len(tgc.parse_tick_times(lines, date_filter="2026-10-08")) == 2
    # Without the filter the overnight boundary would read as one huge gap.
    assert len(tgc.find_gaps(tgc.parse_tick_times(lines))) == 2
    assert len(tgc.find_gaps(tgc.parse_tick_times(lines, date_filter="2026-10-08"))) == 1


def test_main_runs_on_a_synthetic_file(tmp_path, capsys):
    p = _log(tmp_path, [_hb("05:35:32"), _hb("05:45:35")])
    assert tgc.main([str(p)]) == 0
    out = capsys.readouterr().out
    assert "INVISIBLE to wake_gap_recovery" in out
    assert "lost_ticks=1" in out


def test_log_path_is_required(tmp_path):
    """No production default: the tool cannot be run without naming a file."""
    import pytest

    with pytest.raises(SystemExit):
        tgc.main([])


# ---------------------------------------------------------------------------
# Cause attribution (clamshell | idle_sleep | unknown) — added 2026-11-06.
# The fixture is a saved `pmset -g log` excerpt; no test shells out to pmset.
# ---------------------------------------------------------------------------

_PMSET_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "pmset_log_clamshell.txt"
)


def _cyc(day, ts):
    return f"{day} {ts},001 INFO cycle #1 bias=bullish trend=mild vix=20.0"


def test_parse_sleep_events_classifies_clamshell_and_idle():
    events = tgc.parse_sleep_events(_PMSET_FIXTURE.read_text())
    causes = [c for _ts, c, _r in events]
    assert causes == [
        tgc.CAUSE_CLAMSHELL,  # 'Clamshell Sleep'
        tgc.CAUSE_IDLE,  # 'Maintenance Sleep'
        tgc.CAUSE_IDLE,  # 'Idle Sleep'
    ]
    assert events[0][2] == "Clamshell Sleep"
    assert events[0][0].strftime("%H:%M:%S") == "11:05:07"


def test_parse_sleep_events_accepts_lines_and_honours_date_filter():
    lines = _PMSET_FIXTURE.read_text().splitlines()
    assert len(tgc.parse_sleep_events(lines)) == 3
    assert tgc.parse_sleep_events(lines, date_filter="2026-11-04") == []


def test_parse_sleep_events_skips_non_sleep_and_unparseable_lines():
    assert tgc.parse_sleep_events("Entering Sleep state due to 'Idle Sleep'") == []
    assert tgc.parse_sleep_events("2026-11-05 11:05:07 -0800 Wake  lid open") == []


def test_attribute_gaps_names_clamshell_for_the_lid_close_window(tmp_path):
    # A lid close at 11:05 inside an 11:00 -> 12:00 tick gap.
    log = _log(
        tmp_path,
        [_cyc("2026-11-05", "11:00:05"), _cyc("2026-11-05", "12:00:10")],
    )
    gaps = tgc.find_gaps(tgc.parse_tick_times(log.read_text().splitlines()))
    assert len(gaps) == 1
    tgc.attribute_gaps(gaps, tgc.parse_sleep_events(_PMSET_FIXTURE.read_text()))
    assert gaps[0].cause == tgc.CAUSE_CLAMSHELL
    assert "Clamshell Sleep @ 11:05:07" == gaps[0].cause_detail


def test_attribute_gaps_prefers_clamshell_over_idle_in_the_same_window(tmp_path):
    # 11:00 -> 12:40 contains the clamshell (11:05) AND two idle sleeps.
    log = _log(
        tmp_path,
        [_cyc("2026-11-05", "11:00:05"), _cyc("2026-11-05", "12:40:10")],
    )
    gaps = tgc.find_gaps(tgc.parse_tick_times(log.read_text().splitlines()))
    tgc.attribute_gaps(gaps, tgc.parse_sleep_events(_PMSET_FIXTURE.read_text()))
    assert gaps[0].cause == tgc.CAUSE_CLAMSHELL


def test_attribute_gaps_names_idle_sleep_when_that_is_all_there_is(tmp_path):
    # 12:25 -> 12:50 contains only the 'Idle Sleep' at 12:30.
    log = _log(
        tmp_path,
        [_cyc("2026-11-05", "12:25:05"), _cyc("2026-11-05", "12:50:10")],
    )
    gaps = tgc.find_gaps(tgc.parse_tick_times(log.read_text().splitlines()))
    tgc.attribute_gaps(gaps, tgc.parse_sleep_events(_PMSET_FIXTURE.read_text()))
    assert gaps[0].cause == tgc.CAUSE_IDLE
    assert gaps[0].cause_detail.startswith("Idle Sleep @ 12:30:05")


def test_gap_with_no_sleep_event_stays_unknown(tmp_path):
    # 14:00 -> 15:00: the host logged no sleep there (process death, busy host,
    # or a pmset buffer that no longer reaches back) — never guessed at.
    log = _log(
        tmp_path,
        [_cyc("2026-11-05", "14:00:05"), _cyc("2026-11-05", "15:00:10")],
    )
    gaps = tgc.find_gaps(tgc.parse_tick_times(log.read_text().splitlines()))
    tgc.attribute_gaps(gaps, tgc.parse_sleep_events(_PMSET_FIXTURE.read_text()))
    assert gaps[0].cause == tgc.CAUSE_UNKNOWN
    assert gaps[0].cause_detail == ""


def test_summary_and_render_carry_the_cause(tmp_path):
    log = _log(
        tmp_path,
        [
            _cyc("2026-11-05", "11:00:05"),
            _cyc("2026-11-05", "12:00:10"),
            _cyc("2026-11-05", "12:25:05"),
            _cyc("2026-11-05", "12:50:10"),
        ],
    )
    gaps = tgc.find_gaps(tgc.parse_tick_times(log.read_text().splitlines()))
    tgc.attribute_gaps(gaps, tgc.parse_sleep_events(_PMSET_FIXTURE.read_text()))
    summary = tgc.summarize(gaps)
    # Three gaps, not two: the 12:00 -> 12:25 stretch is one too, and it has no
    # sleep event in it, so a mixed report must keep `unknown` as a category
    # rather than folding it into whichever cause is nearby.
    assert summary["gaps_by_cause"] == {
        tgc.CAUSE_CLAMSHELL: 1,
        tgc.CAUSE_IDLE: 1,
        tgc.CAUSE_UNKNOWN: 1,
    }
    assert summary["lost_ticks_by_cause"][tgc.CAUSE_CLAMSHELL] == 11
    out = tgc.render(gaps, summary, tgc.DEFAULT_THRESHOLD_SECONDS)
    assert "cause=clamshell [Clamshell Sleep @ 11:05:07]" in out
    assert "BY CAUSE (pmset):" in out
    # The lid-close finding must come with its own fix, not just a label.
    assert "caffeinate does not prevent" in out


def test_render_omits_cause_section_when_nothing_is_attributed(tmp_path):
    log = _log(
        tmp_path,
        [_cyc("2026-11-05", "14:00:05"), _cyc("2026-11-05", "15:00:10")],
    )
    gaps = tgc.find_gaps(tgc.parse_tick_times(log.read_text().splitlines()))
    summary = tgc.summarize(gaps)
    out = tgc.render(gaps, summary, tgc.DEFAULT_THRESHOLD_SECONDS)
    assert "cause=unknown" in out
    assert "caffeinate does not prevent" not in out


def test_main_attributes_from_a_saved_pmset_log(tmp_path, capsys):
    log = _log(
        tmp_path,
        [_cyc("2026-11-05", "11:00:05"), _cyc("2026-11-05", "12:00:10")],
    )
    rc = tgc.main([str(log), "--pmset-log", str(_PMSET_FIXTURE)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cause=clamshell" in out


def test_main_no_attribute_never_reads_pmset(tmp_path, capsys, monkeypatch):
    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("--no-attribute must not read pmset")

    monkeypatch.setattr(tgc, "read_host_pmset_log", _boom)
    log = _log(
        tmp_path,
        [_cyc("2026-11-05", "11:00:05"), _cyc("2026-11-05", "12:00:10")],
    )
    rc = tgc.main([str(log), "--no-attribute"])
    assert rc == 0
    assert "cause=unknown" in capsys.readouterr().out


def test_unreadable_pmset_degrades_to_counts_not_a_crash(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(tgc, "read_host_pmset_log", lambda *_a, **_k: "")
    log = _log(
        tmp_path,
        [_cyc("2026-11-05", "11:00:05"), _cyc("2026-11-05", "12:00:10")],
    )
    rc = tgc.main([str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "without cause attribution" in out
    assert "lost_ticks=11" in out


def test_read_host_pmset_log_returns_empty_on_failure(monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr(
        tgc.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("no"))
    )
    assert tgc.read_host_pmset_log() == ""

    class _P:
        returncode = 1
        stdout = "ignored"

    monkeypatch.setattr(tgc.subprocess, "run", lambda *_a, **_k: _P())
    assert tgc.read_host_pmset_log() == ""

    monkeypatch.setattr(
        tgc.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(_sp.TimeoutExpired("pmset", 1)),
    )
    assert tgc.read_host_pmset_log() == ""
