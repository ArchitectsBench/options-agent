"""5-minute bar log (src/engine/bars_log.py, 2026-09-14).

The cockpit TRADE page charts the bars the engine computed its indicators
from. Pins: only completed bars of TODAY's session are written, the forming bar
never is; a process's first call backfills the session (mid-day restart), later
calls write only new bars; one bad frame never costs the others; and nothing
ever raises into the cycle.
"""
import json

import pandas as pd

import src.engine.bars_log as bl

NY = "America/New_York"


def _frame(start, n, tz=NY):
    idx = pd.date_range(start, periods=n, freq="5min", tz=tz)
    return pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 100}, index=idx)


def _rows(tmp_path):
    return [json.loads(l) for l in (tmp_path / "bars5m-2026-09-14.jsonl").read_text().splitlines()]


def setup_function():
    bl._written.clear()


def test_writes_completed_session_bars_only(tmp_path):
    # yesterday's bars + today's 09:30..09:45 ET; at 06:47 PT the 09:45 bar is forming
    df = pd.concat([_frame("2026-09-11 15:50", 2), _frame("2026-09-14 09:30", 4)])
    now = pd.Timestamp("2026-09-14 06:47", tz=bl.PT)
    assert bl.append_cycle_bars({"MSFT": df}, 1, tmp_path, now=now) == 3
    assert [r["t"] for r in _rows(tmp_path)] == ["06:30", "06:35", "06:40"]
    assert _rows(tmp_path)[0]["date"] == "2026-09-14"


def test_second_call_writes_only_new_bars_and_first_call_backfills(tmp_path):
    df = _frame("2026-09-14 09:30", 6)
    bl.append_cycle_bars({"AMD": df}, 1, tmp_path, now=pd.Timestamp("2026-09-14 06:41", tz=bl.PT))
    bl.append_cycle_bars({"AMD": df}, 2, tmp_path, now=pd.Timestamp("2026-09-14 06:56", tz=bl.PT))
    assert [r["t"] for r in _rows(tmp_path)] == ["06:30", "06:35", "06:40", "06:45", "06:50"]
    assert [r["cycle"] for r in _rows(tmp_path)] == [1, 1, 2, 2, 2]


def test_naive_frames_are_read_as_new_york(tmp_path):
    df = _frame("2026-09-14 09:30", 2, tz=None)
    bl.append_cycle_bars({"SPY": df}, 1, tmp_path, now=pd.Timestamp("2026-09-14 07:00", tz=bl.PT))
    assert [r["t"] for r in _rows(tmp_path)] == ["06:30", "06:35"]


def test_a_bad_frame_never_raises_or_costs_the_others(tmp_path):
    good = _frame("2026-09-14 09:30", 2)
    bad = pd.DataFrame({"Close": [1.0]}, index=pd.date_range("2026-09-14 09:30", periods=1, freq="5min", tz=NY))
    n = bl.append_cycle_bars({"BAD": bad, "GOOD": good, "NONE": None}, 1, tmp_path,
                             now=pd.Timestamp("2026-09-14 07:00", tz=bl.PT))
    assert n == 2 and {r["ticker"] for r in _rows(tmp_path)} == {"GOOD"}
    assert bl.append_cycle_bars(None, 1, tmp_path / "x") == 0
