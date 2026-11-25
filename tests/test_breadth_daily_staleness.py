"""Hermetic tests for the breadth_daily staleness flag (autopilot queue item,
spec pinned mid_session 2026-10-27, built post_close 2026-10-27).

What is under test: `_breadth_staleness` turns the latest breadth row's date
into a BUSINESS-day age and a machine-readable `stale` boolean, and
`_breadth_daily_snapshot` carries that flag on every return path. Before this,
staleness was PROSE ONLY on a calendar `> 4` test — which is exactly why the
2026-10-23/10-26 stall read friendly while the updater had stopped appending.

No production file is read or written: the parquet path is stubbed out
(CLAUDE.md destructive-probe rule). Dates are derived relative to a fixed
"today" or via business-day offsets, so the suite does not drift with the
calendar.
"""

from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.indicators import regime_live


# ---------------------------------------------------------------- pure helper

# The four rows of the table verified against the real incident dates.
@pytest.mark.parametrize("last_row, today, want_stale, want_busdays", [
    (date(2026, 10, 22), date(2026, 10, 26), True, 2),   # the incident shape
    (date(2026, 10, 23), date(2026, 10, 26), False, 1),  # an ordinary Monday
    (date(2026, 10, 22), date(2026, 10, 27), True, 3),   # the day it was found
    (date(2026, 10, 27), date(2026, 10, 27), False, 0),  # same-day = fresh
])
def test_incident_table(last_row, today, want_stale, want_busdays):
    assert regime_live._breadth_staleness(last_row, today) == (want_stale, want_busdays)


def test_calendar_threshold_would_have_missed_the_incident():
    """The regression this flag exists for: on 2026-10-26 the stalled data was
    only 4 CALENDAR days old, so the old `age_days > 4` test stayed quiet."""
    last, today = date(2026, 10, 22), date(2026, 10, 26)
    assert (today - last).days == 4          # old test: 4 > 4 is False -> silent
    assert regime_live._breadth_staleness(last, today)[0] is True


def test_friday_to_monday_is_not_stale():
    """And the obvious over-correction (calendar > 1) would false-alarm every
    Monday. One business day is the normal weekend gap."""
    assert regime_live._breadth_staleness(date(2026, 10, 23), date(2026, 10, 26)) == (False, 1)


def test_accepts_string_and_timestamp_dates():
    """The parquet column arrives as a string or a pandas Timestamp depending
    on how it was written; both must parse."""
    assert regime_live._breadth_staleness("2026-10-22", date(2026, 10, 27)) == (True, 3)
    assert regime_live._breadth_staleness(
        pd.Timestamp("2026-10-22"), date(2026, 10, 27)) == (True, 3)


def test_unparseable_date_degrades_without_raising():
    """A bad row must never take the regime object down — degrade, don't raise."""
    assert regime_live._breadth_staleness("not-a-date", date(2026, 10, 27)) == (False, -1)
    assert regime_live._breadth_staleness(None, date(2026, 10, 27)) == (False, -1)


def test_future_dated_row_is_not_stale():
    """A row dated ahead of today is wrong, but it is not STALE — negative
    business days must not wrap into the stale branch."""
    stale, busdays = regime_live._breadth_staleness(date(2026, 10, 28), date(2026, 10, 27))
    assert stale is False and busdays < 0


# ------------------------------------------------------------------- snapshot

PINNED_TODAY = date(2026, 10, 27)   # a Tuesday


class _FixedDT:
    """Pins the snapshot's clock. Deriving test dates from the real clock is the
    defect this guards: `date.today()` on a SATURDAY is one business day ahead of
    a Friday-rolled offset, so a weekend run (the Sat 09:00 checkpoint) would see
    these assertions go red for calendar reasons, not code reasons."""

    @staticmethod
    def now():
        return datetime(PINNED_TODAY.year, PINNED_TODAY.month, PINNED_TODAY.day, 12, 0)


def _stub_parquet(monkeypatch, df, exists=True):
    """Point the snapshot at a synthetic frame and a fixed clock; the real
    parquet is never read."""
    monkeypatch.setattr(Path, "exists", lambda self: exists)
    monkeypatch.setattr(regime_live.pd, "read_parquet", lambda p: df)
    monkeypatch.setattr(regime_live, "datetime", _FixedDT)


def _frame(last_date, pct_above_20dma=0.62):
    return pd.DataFrame([{"date": str(last_date), "pct_adv": 0.55,
                          "pct_above_20dma": pct_above_20dma,
                          "breadth_thrust_10d": 0.51}])


def _busday_offset(n):
    """n business days before the PINNED today — never the real clock."""
    return np.busday_offset(PINNED_TODAY, -n, roll="backward").astype("O")


def test_snapshot_fresh_row_carries_stale_false(monkeypatch):
    _stub_parquet(monkeypatch, _frame(_busday_offset(0)))
    snap = regime_live._breadth_daily_snapshot()
    assert snap["available"] is True
    assert snap["stale"] is False
    assert snap["stale_busdays"] == 0
    assert "STALE" not in snap["note"]
    assert snap["healthy"] is True          # unchanged semantics: pct>0.5


def test_snapshot_stale_row_flags_and_explains(monkeypatch):
    _stub_parquet(monkeypatch, _frame(_busday_offset(3)))
    snap = regime_live._breadth_daily_snapshot()
    assert snap["available"] is True        # the data is still returned...
    assert snap["stale"] is True            # ...but it is marked, machine-readably
    assert snap["stale_busdays"] == 3
    assert snap["note"].startswith("STALE:")
    assert "breadth_daily_update.py" in snap["note"]
    assert "holiday" in snap["note"]        # the accepted false positive is stated


def test_snapshot_missing_file_is_stale(monkeypatch):
    _stub_parquet(monkeypatch, _frame(_busday_offset(0)), exists=False)
    snap = regime_live._breadth_daily_snapshot()
    assert snap["available"] is False
    assert snap["stale"] is True            # no data is maximally stale
    assert snap["stale_busdays"] is None


def test_snapshot_empty_file_is_stale(monkeypatch):
    _stub_parquet(monkeypatch, pd.DataFrame())
    snap = regime_live._breadth_daily_snapshot()
    assert snap["available"] is False and snap["stale"] is True


def test_snapshot_read_error_is_stale_not_a_crash(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)

    def boom(_p):
        raise OSError("parquet is corrupt")

    monkeypatch.setattr(regime_live.pd, "read_parquet", boom)
    snap = regime_live._breadth_daily_snapshot()
    assert snap["available"] is False and snap["stale"] is True
    assert "read error" in snap["note"]


def test_every_return_path_carries_the_flag(monkeypatch):
    """A consumer must be able to read `stale` without a .get() default — that
    is the whole point of making it machine-readable."""
    for df, exists in [(_frame(_busday_offset(0)), True),
                       (_frame(_busday_offset(5)), True),
                       (pd.DataFrame(), True),
                       (_frame(_busday_offset(0)), False)]:
        _stub_parquet(monkeypatch, df, exists=exists)
        snap = regime_live._breadth_daily_snapshot()
        assert "stale" in snap and isinstance(snap["stale"], bool)
        assert "stale_busdays" in snap


def test_snapshot_does_not_gate_anything(monkeypatch):
    """Deliberate scope limit, pinned so a later change is a decision rather
    than a drift: `healthy` is computed from pct_above_20dma ALONE and is not
    suppressed by staleness. Gating it would be a live-path behaviour change
    (paper_trader consumes `healthy`) and is on the operator's list."""
    _stub_parquet(monkeypatch, _frame(_busday_offset(9), pct_above_20dma=0.62))
    snap = regime_live._breadth_daily_snapshot()
    assert snap["stale"] is True
    assert snap["healthy"] is True
