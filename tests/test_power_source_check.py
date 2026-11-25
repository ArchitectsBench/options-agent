"""Hermetic tests for scripts/ops/power_source_check.py.

Synthetic `pmset -g batt` text only — nothing here unplugs, sleeps or changes a
power setting, and no production file is read. Fixtures are anchored on real
readings: the 2026-10-29 mid_session breach (42%, discharging, 4:18) and the
same day's post_close recovery (96%, charging, 0:18).
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "power_source_check",
    Path(__file__).resolve().parents[1] / "scripts" / "ops" / "power_source_check.py",
)
psc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(psc)


# The real 2026-10-29 10:00 PT reading that cost 67 ticks.
BATTERY = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=21954659)\t42%; discharging; 4:18 remaining present: true\n"
)
# The real 2026-10-29 13:20 PT reading, after the operator plugged in.
AC = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=21954659)\t96%; charging; 0:18 remaining present: true\n"
)
BATTERY_NO_ESTIMATE = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=21954659)\t100%; discharging; (no estimate) present: true\n"
)
DESKTOP = "Now drawing from 'AC Power'\n"
UPS = "Now drawing from 'UPS Power'\n"

# A Thursday 10:00 PT — inside the window; and the same clock on a Saturday.
IN_WINDOW = datetime(2026, 10, 29, 10, 0, tzinfo=psc.PT)
PRE_OPEN = datetime(2026, 10, 29, 5, 30, tzinfo=psc.PT)
AFTER_WINDOW = datetime(2026, 10, 29, 14, 0, tzinfo=psc.PT)
BEFORE_WINDOW = datetime(2026, 10, 29, 4, 59, tzinfo=psc.PT)
SATURDAY = datetime(2026, 10, 31, 10, 0, tzinfo=psc.PT)


def _measure(text, now):
    return psc.measure(text, now, psc.DEFAULT_START, psc.DEFAULT_END)


# ── parsing ───────────────────────────────────────────────────────────────

def test_parses_the_real_battery_reading():
    p = psc.parse_pmset(BATTERY)
    assert p["on_battery"] is True
    assert p["source"] == "Battery Power"
    assert p["pct"] == 42
    assert p["state"] == "discharging"
    assert p["remaining"] == "4:18"


def test_parses_the_real_ac_reading():
    p = psc.parse_pmset(AC)
    assert p["on_battery"] is False
    assert p["pct"] == 96
    assert p["state"] == "charging"


def test_no_estimate_variant_parses_with_remaining_none():
    p = psc.parse_pmset(BATTERY_NO_ESTIMATE)
    assert p["on_battery"] is True and p["pct"] == 100
    assert p["remaining"] is None


def test_desktop_shape_has_no_battery_row():
    p = psc.parse_pmset(DESKTOP)
    assert p["on_battery"] is False and p["pct"] is None


@pytest.mark.parametrize("text", [UPS, "", "garbage\nlines\n", "Now drawing from nothing"])
def test_unrecognised_source_is_unmeasurable_not_a_guess(text):
    # A UPS is deliberately NOT guessed to be mains — see the module docstring.
    assert psc.parse_pmset(text) is None


# ── window ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "now,expected",
    [(PRE_OPEN, True), (IN_WINDOW, True), (BEFORE_WINDOW, False),
     (AFTER_WINDOW, False), (SATURDAY, False)],
)
def test_window_covers_weekday_pre_open_through_post_close(now, expected):
    assert psc.in_window(now) is expected


def test_window_bounds_are_half_open():
    assert psc.in_window(datetime(2026, 10, 29, 5, 0, tzinfo=psc.PT)) is True
    assert psc.in_window(datetime(2026, 10, 29, 13, 30, tzinfo=psc.PT)) is False


# ── breach logic ──────────────────────────────────────────────────────────

def test_battery_in_window_breaches():
    r = _measure(BATTERY, IN_WINDOW)
    assert r["breach"] is True


def test_battery_before_the_open_breaches_too():
    # 60 of the 67 ticks lost on 2026-10-29 were pre-open; this is the case a
    # session-only (06:30-13:00) window would have missed.
    assert _measure(BATTERY, PRE_OPEN)["breach"] is True


def test_full_charge_on_battery_still_breaches():
    # The 2026-10-22 sleep initiated at Charge:84%; % is not a condition.
    assert _measure(BATTERY_NO_ESTIMATE, IN_WINDOW)["breach"] is True


def test_ac_in_window_is_clean():
    assert _measure(AC, IN_WINDOW)["breach"] is False


def test_battery_outside_the_window_is_clean():
    assert _measure(BATTERY, AFTER_WINDOW)["breach"] is False


def test_battery_on_saturday_is_clean():
    assert _measure(BATTERY, SATURDAY)["breach"] is False


def test_unmeasurable_returns_none_not_a_clean_reading():
    assert _measure(UPS, IN_WINDOW) is None


# ── summary text ──────────────────────────────────────────────────────────

def test_breach_summary_quotes_charge_and_estimate():
    s = psc.summarize(_measure(BATTERY, IN_WINDOW))
    assert s.startswith("BREACH")
    assert "42%" in s and "4:18" in s and "discharging" in s


def test_clean_summary_says_ok_and_names_the_source():
    s = psc.summarize(_measure(AC, IN_WINDOW))
    assert s.startswith("OK") and "AC Power" in s


# ── exit codes (the deadman contract) ─────────────────────────────────────

def _main(tmp_path, text, now):
    f = tmp_path / "pmset.txt"
    f.write_text(text)
    return psc.main(["--pmset-file", str(f), "--now", now])


def test_exit_3_on_battery_in_window(tmp_path):
    assert _main(tmp_path, BATTERY, "2026-10-29T10:00") == 3


def test_exit_0_on_ac(tmp_path):
    assert _main(tmp_path, AC, "2026-10-29T10:00") == 0


def test_exit_0_on_battery_outside_window(tmp_path):
    assert _main(tmp_path, BATTERY, "2026-10-29T14:00") == 0


def test_exit_1_when_unmeasurable(tmp_path):
    # Must be 1, never 0: "could not measure" is not "all clear", and never 3:
    # deadman layer 3g alerts on 3 only, so this stays silent.
    assert _main(tmp_path, UPS, "2026-10-29T10:00") == 1


def test_exit_1_when_pmset_binary_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(psc, "read_pmset", lambda *a, **k: "")
    assert psc.main(["--now", "2026-10-29T10:00"]) == 1


def test_json_output_is_machine_readable(tmp_path, capsys):
    f = tmp_path / "pmset.txt"
    f.write_text(BATTERY)
    rc = psc.main(["--pmset-file", str(f), "--now", "2026-10-29T10:00", "--json"])
    import json as _json
    payload = _json.loads(capsys.readouterr().out)
    assert rc == 3 and payload["breach"] is True and payload["pct"] == 42


def test_window_flags_are_honoured(tmp_path):
    f = tmp_path / "pmset.txt"
    f.write_text(BATTERY)
    rc = psc.main(
        ["--pmset-file", str(f), "--now", "2026-10-29T14:00",
         "--window-start", "05:00", "--window-end", "16:00"]
    )
    assert rc == 3


# ── transition bracket (half (i), added 2026-11-12) ───────────────────────
#
# Fixtures are real `pmset -g log` observation lines, both spacings and both
# charge formats: 2026-11-12's `Assertions Summary-` rows carry `Using Batt(Charge:
# 100)`, while the 2026-10-22 clamshell sleep carried `Using Batt (Charge:84%)`.
# The point of this block is that an UNBRACKETABLE log reads `unknown` rather
# than a plausible-looking number — that is the error class the item fixes.

LOG_BOTH_ENDS = (
    "2026-11-12 01:43:52 -0800 Assertions \tSummary- [System: PrevIdle kCPU] Using AC(Charge: 100)\n"
    "2026-11-12 06:10:14 -0800 Assertions \tSummary- [System: DeclUser kDisp] Using AC(Charge: 100)\n"
    "2026-11-12 09:50:15 -0800 Assertions \tSummary- [System: IntPrevDisp kDisp] Using Batt(Charge: 100)\n"
)
LOG_OLD_SPACING = (
    "2026-10-22 06:00:00 -0700 Assertions \tSummary- [System: kCPU] Using AC (Charge:100%)\n"
    "2026-10-22 07:53:31 -0700 Sleep      \tEntering Sleep state due to 'Clamshell Sleep': Using Batt (Charge:84%)\n"
)
# The sparse case, 2026-11-11 in miniature: AC observations only, no `Using Batt`
# line at all, even though `pmset -g batt` says the host is unplugged right now.
LOG_NO_BATT_LINE = (
    "2026-11-11 01:38:48 -0800 Assertions \tSummary- [System: kCPU] Using AC(Charge: 100)\n"
    "2026-11-11 12:14:14 -0800 Assertions \tSummary- [System: kDisp] Using AC(Charge: 100)\n"
)
# A ring buffer that no longer reaches back to the previous AC stint.
LOG_BATT_ONLY = (
    "2026-11-12 08:00:00 -0800 Assertions \tSummary- [System: kDisp] Using Batt(Charge: 100)\n"
)
NOV12_IN_WINDOW = datetime(2026, 11, 12, 10, 0, tzinfo=psc.PT)


def _bracket(log_text, now, on_battery=True):
    return psc.power_bracket(psc.parse_power_observations(log_text), now, on_battery)


def test_observations_parse_both_spacings_and_charge_formats():
    obs = psc.parse_power_observations(LOG_BOTH_ENDS + LOG_OLD_SPACING)
    assert len(obs) == 5
    # sorted ascending, and the clamshell `Using Batt (Charge:84%)` line parsed
    assert obs[0][0] == datetime(2026, 10, 22, 6, 0, 0)
    assert obs[1] == (datetime(2026, 10, 22, 7, 53, 31), True)


def test_both_ends_present_is_a_measured_bracket():
    b = _bracket(LOG_BOTH_ENDS, NOV12_IN_WINDOW)
    assert b["basis"] == psc.BASIS_MEASURED
    assert b["last_seen_ac"] == "2026-11-12T06:10:14"
    assert b["first_seen_on_battery"] == "2026-11-12T09:50:15"


def test_sparse_log_with_no_battery_line_is_unknown_not_inferred():
    """The 2026-11-11 case: on battery per `pmset -g batt`, nothing in the log."""
    b = _bracket(LOG_NO_BATT_LINE, datetime(2026, 11, 11, 19, 0, tzinfo=psc.PT))
    assert b == {
        "last_seen_ac": None,
        "first_seen_on_battery": None,
        "basis": psc.BASIS_UNKNOWN,
    }


def test_ring_buffer_not_reaching_the_previous_ac_stint_is_unknown():
    b = _bracket(LOG_BATT_ONLY, NOV12_IN_WINDOW)
    assert b["basis"] == psc.BASIS_UNKNOWN
    assert b["last_seen_ac"] is None


def test_empty_and_garbage_logs_are_unknown_never_a_crash():
    for text in ("", "   ", "not a pmset log at all\n", "9999-99-99 99:99:99 Using AC\n"):
        assert _bracket(text, NOV12_IN_WINDOW)["basis"] == psc.BASIS_UNKNOWN


def test_on_ac_reports_no_bracket():
    """There is no battery stint to bracket, and the last AC observation must
    not be offered as one — it would read as 'plugged in at', which it is not."""
    b = _bracket(LOG_BOTH_ENDS, NOV12_IN_WINDOW, on_battery=False)
    assert b["basis"] == psc.BASIS_UNKNOWN
    assert b["last_seen_ac"] is None


def test_multiple_stints_brackets_the_current_one():
    log = LOG_BOTH_ENDS + (
        "2026-11-12 10:30:00 -0800 Assertions \tSummary- Using AC(Charge: 100)\n"
        "2026-11-12 11:15:00 -0800 Assertions \tSummary- Using Batt(Charge: 99)\n"
    )
    b = _bracket(log, datetime(2026, 11, 12, 12, 0, tzinfo=psc.PT))
    assert b["last_seen_ac"] == "2026-11-12T10:30:00"
    assert b["first_seen_on_battery"] == "2026-11-12T11:15:00"


def test_observations_after_now_are_ignored():
    """Evaluating a past instant must not borrow a later observation."""
    b = _bracket(LOG_BOTH_ENDS, datetime(2026, 11, 12, 9, 0, tzinfo=psc.PT))
    assert b["basis"] == psc.BASIS_UNKNOWN  # the 09:50 Batt line is in the future


def test_bracket_phrase_states_bounds_and_never_a_duration():
    r = psc.measure(BATTERY, NOV12_IN_WINDOW, psc.DEFAULT_START, psc.DEFAULT_END,
                    LOG_BOTH_ENDS)
    text = psc.summarize(r)
    assert "measured-bracket" in text
    assert "2026-11-12T06:10:14" in text and "2026-11-12T09:50:15" in text
    # no synthesized elapsed figure anywhere in the output
    assert "3:40" not in text and "3 h" not in text and "hours" not in text


def test_unknown_basis_says_so_out_loud():
    r = psc.measure(BATTERY, NOV12_IN_WINDOW, psc.DEFAULT_START, psc.DEFAULT_END, "")
    assert "basis unknown" in psc.summarize(r)


def test_bracket_never_changes_the_breach_verdict_or_exit_code(tmp_path):
    """Reporting only: every (batt, log) combination keeps its original verdict."""
    for batt, now, expected in (
        (BATTERY, NOV12_IN_WINDOW, True),
        (AC, NOV12_IN_WINDOW, False),
        (BATTERY, AFTER_WINDOW, False),
    ):
        for log in ("", LOG_BOTH_ENDS, LOG_BATT_ONLY, "garbage\n"):
            r = psc.measure(batt, now, psc.DEFAULT_START, psc.DEFAULT_END, log)
            assert r["breach"] is expected


def test_unmeasurable_batt_still_exits_1_regardless_of_the_log(tmp_path):
    p = tmp_path / "batt.txt"
    p.write_text(UPS)
    lp = tmp_path / "log.txt"
    lp.write_text(LOG_BOTH_ENDS)
    assert psc.main(["--pmset-file", str(p), "--pmset-log", str(lp),
                     "--now", "2026-11-12T10:00"]) == 1


def test_pmset_log_flag_feeds_the_json_output(tmp_path, capsys):
    p = tmp_path / "batt.txt"
    p.write_text(BATTERY)
    lp = tmp_path / "log.txt"
    lp.write_text(LOG_BOTH_ENDS)
    rc = psc.main(["--pmset-file", str(p), "--pmset-log", str(lp),
                   "--now", "2026-11-12T10:00", "--json"])
    assert rc == 3
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["basis"] == "measured-bracket"
    assert out["last_seen_ac"] == "2026-11-12T06:10:14"
    assert out["breach"] is True


def test_missing_pmset_log_file_degrades_instead_of_crashing(tmp_path):
    p = tmp_path / "batt.txt"
    p.write_text(BATTERY)
    assert psc.main(["--pmset-file", str(p), "--pmset-log", str(tmp_path / "nope.txt"),
                     "--now", "2026-11-12T10:00"]) == 3


def test_no_bracket_flag_skips_the_host_read(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(psc, "read_host_pmset_log", lambda *a, **k: called.append(1) or "")
    p = tmp_path / "batt.txt"
    p.write_text(BATTERY)
    assert psc.main(["--pmset-file", str(p), "--now", "2026-11-12T10:00",
                     "--no-bracket"]) == 3
    assert called == []


def test_host_log_reader_returns_empty_string_when_pmset_is_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("no pmset here")
    monkeypatch.setattr(psc.subprocess, "run", boom)
    assert psc.read_host_pmset_log() == ""
