"""Day-aware staleness check for the nightly ops sweep (deadman check 2).

Added 2026-10-05 after DEADMAN paged four FALSE `ops-sweep-stale` alerts every
Monday: the nightly chain fires 18:40 Weekday-1-5-only, so Friday's run is the
newest legitimate one all weekend, and a flat 26h threshold read it as ~71h
stale on Monday.

DESTRUCTIVE-PROBE RULE (CLAUDE.md): every probe here backdates the mtime of a
log file created inside tmp_path. Production `logs/launchd_nightlyops.log` is
never read, never touched, and never passed to the function under test.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "lib_ops_sweep_stale.sh"

MON, TUE, FRI, SAT, SUN = 1, 2, 5, 6, 7

# Ages in minutes for the cases the Monday bug turned on.
FRIDAY_EVENING_AT_MONDAY_1205 = 62 * 60   # Fri 18:40 read on Mon 12:05
FRIDAY_EVENING_AT_MONDAY_1805 = 71 * 60   # the worst legitimate Monday age
THURSDAY_DEATH_AT_MONDAY = 96 * 60        # chain died after Thursday's run
LAST_NIGHT = 10 * 60                      # normal weekday freshness
TWO_NIGHTS = 50 * 60                      # a genuinely missed weekday run


def _stale(log: Path, dow: int) -> bool:
    """True when the shell helper says the sweep log is stale (exit 0)."""
    script = f'source "{LIB}"; ops_sweep_stale "{log}" {dow}'
    return subprocess.run(["bash", "-c", script], capture_output=True).returncode == 0


def _max_age_min(dow: int) -> int:
    script = f'source "{LIB}"; ops_sweep_max_age_min {dow}'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def _log_aged(tmp_path: Path, age_min: int) -> Path:
    """A disposable sweep log in tmp whose mtime is `age_min` minutes old."""
    log = tmp_path / "launchd_nightlyops.log"
    log.write_text("nightly ops complete: report.md, journal.md\n")
    subprocess.run(["touch", "-A", f"-{age_min // 60:02d}{age_min % 60:02d}00", str(log)],
                   check=True)
    return log


class TestMondayFalsePage:
    """The regression itself: Friday-aged log + Monday must be SILENT."""

    def test_friday_run_is_not_stale_at_monday_midday(self, tmp_path):
        log = _log_aged(tmp_path, FRIDAY_EVENING_AT_MONDAY_1205)
        assert not _stale(log, MON)

    def test_friday_run_is_not_stale_at_monday_evening(self, tmp_path):
        log = _log_aged(tmp_path, FRIDAY_EVENING_AT_MONDAY_1805)
        assert not _stale(log, MON)

    def test_same_age_would_have_paged_under_the_old_flat_threshold(self, tmp_path):
        """Guards the premise: 62h really is past the Tue-Fri bar, so the fix is
        the day-awareness and not a blanket relaxation."""
        assert FRIDAY_EVENING_AT_MONDAY_1205 > _max_age_min(TUE)


class TestMondayStillPages:
    """Mondays are NOT skipped — a chain dead since Thursday must still page,
    otherwise a Friday death hides until Tuesday."""

    def test_thursday_death_still_stale_on_monday(self, tmp_path):
        log = _log_aged(tmp_path, THURSDAY_DEATH_AT_MONDAY)
        assert _stale(log, MON)

    def test_missing_log_is_stale_on_monday(self, tmp_path):
        assert _stale(tmp_path / "never-written.log", MON)


class TestWeekdaysUnchanged:
    def test_last_nights_run_is_fresh(self, tmp_path):
        log = _log_aged(tmp_path, LAST_NIGHT)
        assert not _stale(log, TUE)
        assert not _stale(log, FRI)

    def test_two_nights_stale_on_a_weekday(self, tmp_path):
        log = _log_aged(tmp_path, TWO_NIGHTS)
        assert _stale(log, TUE)
        assert _stale(log, FRI)

    def test_weekday_threshold_is_still_26h(self):
        assert _max_age_min(TUE) == 1560
        assert _max_age_min(FRI) == 1560

    def test_monday_threshold_is_75h(self):
        assert _max_age_min(MON) == 4500


class TestWeekendNotApplicable:
    """Sat/Sun were already silent via the DOW gate; keep it that way."""

    def test_weekend_never_stale_even_when_ancient(self, tmp_path):
        log = _log_aged(tmp_path, THURSDAY_DEATH_AT_MONDAY)
        assert not _stale(log, SAT)
        assert not _stale(log, SUN)
