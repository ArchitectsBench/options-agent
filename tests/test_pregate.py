"""Tests for the pre-gate completeness-audit fixes (TASK 4, 2026-07-25):

  GAP 3 (scripts/learning/pregate.py): verdict() previously FAILED OPEN when
  bar_time was entirely absent -- drift/age/missing_bars all None and
  spans_session False fell through to a lane-specific "clean"/"age" return.
  This adds ONE leaf: that exact all-uncomputable case now returns
  (True, "no_bar_time") instead. Signature and (bool, reason) return shape are
  unchanged; every other branch is unchanged (asserted below).

  GAP 1 + GAP 2 (src/indicators/stock_signals.py): compute_ticker_signals()
  now stamps `iv_rank_asof` (None -- the daily IV snapshot store carries no
  timestamp yet, see inline comment at the call site) and `spy_bar_time` (the
  SPY reference leg's own last-bar timestamp) alongside the existing
  `bar_time`, so both previously-unstamped clocks are independently
  observable.

Run: .venv/bin/python -m pytest tests/test_pregate.py -x -q
"""
import sys
import importlib
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "learning"))

pregate = importlib.import_module("pregate")

from src.indicators.stock_signals import compute_ticker_signals  # noqa: E402

FL = pregate.floors()


# ── GAP 3: pregate.verdict() no_bar_time leaf ──────────────────────────────

class TestNoBarTimeLeaf:
    """When bar_time is entirely absent, every upstream mechanism
    (signal_age_min, drift_over_lag_atr, bar_gap) returns None/False, so the
    call site passes drift=None, age=None, missing_bars=None,
    spans_session=False. That combination must now fail CLOSED."""

    def test_scalp_lane_no_bar_time(self):
        assert pregate.verdict("SCALP", None, None, None, False, FL) == (True, "no_bar_time")

    def test_funnel_lane_no_bar_time(self):
        assert pregate.verdict("funnel", None, None, None, False, FL) == (True, "no_bar_time")

    def test_return_shape_unchanged(self):
        result = pregate.verdict("SCALP", None, None, None, False, FL)
        assert isinstance(result, tuple) and len(result) == 2
        ds, reason = result
        assert isinstance(ds, bool)
        assert isinstance(reason, str)


# ── Regression: normal computable-clean records are unaffected ─────────────

class TestExistingBehaviorUnchanged:
    def test_scalp_clean_drift_below_floor(self):
        # drift 0.02 ATR, well under the 0.10 SCALP floor -> still clean
        assert pregate.verdict("SCALP", 0.02, 0.5, 0, False, FL) == (False, "clean")

    def test_funnel_clean_age_below_floor(self):
        # age 5 min, under the 12-min funnel floor -> still "age" reason, False
        ds, reason = pregate.verdict("funnel", None, 5.0, 0, False, FL)
        assert ds is False
        assert reason == "age"

    def test_scalp_age_fallback_when_drift_uncomputable_but_age_is(self):
        # drift None (store-gap, backfillable) but age IS computable and clean
        # -- must NOT hit the new no_bar_time leaf (age is not None here).
        afloor = FL["pre_gate_floor_min"]["SCALP"]
        ds, reason = pregate.verdict("SCALP", None, afloor - 1, 0, False, FL)
        assert ds is False
        assert reason == "age_fallback_no_path"

    def test_stale_record_still_tags_scalp_drift(self):
        dfloor = FL["drift_floor_atr"]["SCALP"]
        ds, reason = pregate.verdict("SCALP", dfloor + 0.5, 1.0, 0, False, FL)
        assert ds is True
        assert reason == f"drift>{dfloor}ATR"

    def test_stale_record_still_tags_funnel_age(self):
        afloor = FL["pre_gate_floor_min"]["funnel"]
        ds, reason = pregate.verdict("funnel", None, afloor + 1, 0, False, FL)
        assert ds is True
        assert reason == "age"

    def test_cross_session_unaffected(self):
        # spans_session True short-circuits before the new leaf regardless of
        # drift/age/missing_bars being None
        assert pregate.verdict("SCALP", None, None, None, True, FL) == (True, "cross_session")

    def test_missing_bars_over_threshold_unaffected(self):
        cap = FL.get("missing_bars_max", 2)
        ds, reason = pregate.verdict("funnel", None, None, cap + 1, False, FL)
        assert ds is True
        assert reason == f"missing_bars={cap + 1}"


# ── GAP 1 / GAP 2: compute_ticker_signals() snapshot regression ────────────

def _bars(start: str, periods: int, freq: str = "5min", price0: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=periods, freq=freq)
    close = pd.Series([price0 + i * 0.05 for i in range(periods)], index=idx)
    return pd.DataFrame({
        "Open": close - 0.02,
        "High": close + 0.05,
        "Low": close - 0.05,
        "Close": close,
        "Volume": [1000 + 10 * i for i in range(periods)],
    }, index=idx)


class TestSnapshotStamps:
    def test_snapshot_has_spy_bar_time_and_iv_rank_asof(self):
        df_5m = _bars("2026-07-20 09:30", 40)
        spy_5m = _bars("2026-07-20 09:30", 40, price0=500.0)
        snap = compute_ticker_signals(df_5m, None, spy_5m, ticker=None)

        assert "spy_bar_time" in snap
        assert "iv_rank_asof" in snap
        # ticker's own bar_time is untouched and distinct from the new key's purpose
        assert "bar_time" in snap
        assert snap["spy_bar_time"] == str(spy_5m.index[-1])

    def test_iv_rank_asof_is_none_no_fabricated_timestamp(self):
        # no live asof exists anywhere in the daily IV snapshot store today --
        # the field must be None, never a fabricated value.
        df_5m = _bars("2026-07-20 09:30", 40)
        snap = compute_ticker_signals(df_5m, None, None, ticker=None)
        assert snap["iv_rank_asof"] is None

    def test_spy_bar_time_none_when_spy_unavailable(self):
        df_5m = _bars("2026-07-20 09:30", 40)
        snap = compute_ticker_signals(df_5m, None, None, ticker=None)
        assert snap["spy_bar_time"] is None

    def test_spy_bar_time_independent_of_ticker_bar_time(self):
        # SPY's last bar is one bar BEHIND the ticker's -- spy_bar_time must
        # reflect SPY's own clock, not silently mirror bar_time.
        df_5m = _bars("2026-07-20 09:30", 40)
        spy_5m = _bars("2026-07-20 09:30", 39)  # one bar short -> earlier last index
        snap = compute_ticker_signals(df_5m, None, spy_5m, ticker=None)
        assert snap["bar_time"] == str(df_5m.index[-1])
        assert snap["spy_bar_time"] == str(spy_5m.index[-1])
        assert snap["spy_bar_time"] != snap["bar_time"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
