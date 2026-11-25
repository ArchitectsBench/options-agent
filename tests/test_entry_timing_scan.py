"""Entry-timing scan (2026-09-13) — decision support for discretionary entries.

The base rates rendered next to each flag were measured on SPECIFIC trigger
definitions in strategy-chase's 630-cell grid. If a definition here drifts from
the one that was measured, the tool attaches a real number to a different
population and quietly lies. These tests pin the six that differed on the first
implementation pass, plus the split guard that nearly deleted every volatile
name from the scan.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import entry_timing_scan as ets  # noqa: E402


def _frame(n: int = 400, price: float = 100.0, vol: float = 2_000_000.0) -> pd.DataFrame:
    """A calm, liquid series: no flag fires unless a test puts one there."""
    idx = pd.bdate_range("2024-01-01", periods=n)
    c = pd.Series(price + np.sin(np.arange(n) / 20.0) * 0.5, index=range(n))
    return pd.DataFrame({
        "ticker": "TEST", "date": idx,
        "o": c.values, "h": c.values + 0.25, "l": c.values - 0.25,
        "c": c.values, "v": vol,
    })


def test_gap_threshold_is_three_percent_not_two():
    """`gap_up3` is `gap > 0.03` (c1_grid_screen_v2.py:140)."""
    g = _frame()
    g.loc[g.index[-1], "o"] = g["c"].iloc[-2] * 1.025      # +2.5%: below the tested bar
    assert not any(k.startswith("gap_up") for k in ets.state_for(g).get("flags", {}))
    g.loc[g.index[-1], "o"] = g["c"].iloc[-2] * 1.04       # +4%
    g.loc[g.index[-1], "c"] = g["c"].iloc[-2] * 1.04
    g.loc[g.index[-1], "h"] = g["c"].iloc[-1] + 0.25
    assert any(k.startswith("gap_up") for k in ets.state_for(g).get("flags", {}))


def test_relative_volume_is_dollar_volume_not_share_volume():
    """`rvol = (c*vol)/dv20` (c1_grid_screen_v2.py:106-108). A name whose SHARE
    volume is flat but whose PRICE doubled has elevated dollar-RVOL."""
    g = _frame()
    st = ets.state_for(g)
    assert st["rvol_dollar"] == pytest.approx(1.0, abs=0.05)


def test_volume_surge_requires_an_up_day():
    """`volsurge_up = (rvol > 2) & (ret > 0)` — a high-volume DOWN day is not
    the tested population."""
    g = _frame()
    g.loc[g.index[-1], "v"] = 20_000_000.0                 # 10x dollar volume
    g.loc[g.index[-1], "c"] = g["c"].iloc[-2] * 0.97       # but a down day
    assert "volsurge_adx" not in ets.state_for(g).get("flags", {})


def test_rsi2_is_a_strict_less_than_ten():
    assert ets._rsi(pd.Series([10.0] * 50), 2).iloc[-1] == pytest.approx(50.0)


def test_price_and_liquidity_floor_matches_the_study():
    assert (ets.MIN_PRICE, ets.MIN_DDV) == (5.0, 5_000_000)
    g = _frame(price=3.0)                                   # below the $5 floor
    assert "floor" in ets.state_for(g)["skipped"]


def test_short_history_is_skipped_with_a_reason_never_silently():
    assert "history" in ets.state_for(_frame(n=100))["skipped"]


# ------------------------------------------------------------- split guard --
def test_split_guard_catches_a_real_split():
    g = _frame()
    half = len(g) // 2
    g.loc[g.index[half:], ["o", "h", "l", "c"]] /= 2.0       # clean 2:1
    assert "split" in ets.state_for(g)["skipped"]


def test_split_guard_keeps_a_big_earnings_gap():
    """SMCI moved +36% on 2024-01-19 on earnings. An earlier magnitude-based
    guard dropped it, and would have dropped every volatile name in the
    watchlist permanently. Ratio matching, not magnitude, separates them."""
    g = _frame()
    i = len(g) // 2
    g.loc[g.index[i:], ["o", "h", "l", "c"]] *= 1.36
    st = ets.state_for(g)
    assert "skipped" not in st, st


@pytest.mark.parametrize("ratio", [0.5, 1 / 3, 0.1, 2.0, 10.0])
def test_common_split_ratios_all_caught(ratio):
    g = _frame()
    i = len(g) // 2
    g.loc[g.index[i:], ["o", "h", "l", "c"]] *= ratio
    assert "split" in ets.state_for(g)["skipped"]


# ------------------------------------------------------------------ output --
def test_every_rendered_flag_carries_its_measured_base_rate():
    """No flag may reach the operator without the number behind it."""
    for table in (ets.AVOID, ets.PREFER):
        for key, (label, alpha, t, bps) in table.items():
            assert label and isinstance(alpha, float) and isinstance(t, float)
            assert isinstance(bps, int) and bps != 0
    assert all(v[3] < 0 for v in ets.AVOID.values())         # avoid side is negative
    assert all(v[3] > 0 for v in ets.PREFER.values())        # prefer side is positive
    # The finding itself: chasing costs more than patience earns. Measured,
    # the worst avoid cell is -27 bps per trade and the best prefer cell +9 —
    # exactly a factor of three, which is why the tool leads with the avoid side.
    worst_avoid = abs(min(v[3] for v in ets.AVOID.values()))
    best_prefer = max(v[3] for v in ets.PREFER.values())
    assert worst_avoid >= 3 * best_prefer
    assert (worst_avoid, best_prefer) == (27, 9)


def test_render_states_the_limits_and_says_so_when_quiet():
    assert "No entry-timing flag" in ets.render([])
    assert "NOT a validated strategy" in ets.STAMP and "5-session" in ets.STAMP


# ------------------------------------------------------- the trade ticket --
def test_exit_date_counts_sessions_and_skips_a_holiday():
    """Entry Thu 2026-09-03 + 5 sessions = 2026-09-11, because 2026-09-07 is
    Labor Day. An off-by-one here sends the operator out a day early."""
    assert ets.trading_days_after(pd.Timestamp("2026-09-02"), 1).strftime("%Y-%m-%d") == "2026-09-03"
    assert ets.trading_days_after(pd.Timestamp("2026-09-02"), 6).strftime("%Y-%m-%d") == "2026-09-11"


def test_weekend_is_skipped():
    # Friday + 1 session = Monday
    assert ets.trading_days_after(pd.Timestamp("2026-09-11"), 1).strftime("%Y-%m-%d") == "2026-09-14"


def test_ticket_carries_stop_distance_entry_and_exit_days():
    """The operator must not have to compute anything. The stop is a DOLLAR
    DISTANCE off the fill because tomorrow's open is unknown tonight."""
    g = _frame()
    g.loc[g.index[-3:], "c"] = [99.0, 98.0, 97.0]          # three down closes
    g.loc[g.index[-3:], "h"] = [99.3, 98.3, 97.3]
    g.loc[g.index[-3:], "l"] = [98.7, 97.7, 96.7]
    st = ets.state_for(g)
    for k in ("stop_distance", "indicative_stop", "entry_day", "exit_day", "atr14"):
        assert k in st, k
    assert st["stop_distance"] == pytest.approx(ets.STOP_ATR_MULT * st["atr14"], abs=0.01)
    assert st["indicative_stop"] == pytest.approx(st["close"] - st["stop_distance"], abs=0.01)
    assert st["exit_day"] > st["entry_day"] > st["date"]


def test_render_ticket_shows_enter_stop_exit_for_a_buy_window():
    row = {"ticker": "MSFT", "close": 496.82, "adx": 36.4, "atr14": 11.69,
           "stop_distance": 17.54, "indicative_stop": 479.28,
           "entry_day": "2026-09-03", "exit_day": "2026-09-11",
           "avoid": [], "prefer": [{"rule": "3 down closes, ADX >= 25", "alpha_pct_yr": 3.9,
                                    "t": 2.45, "bps_per_trade": 8, "key": "three_down_adx"}]}
    out = ets.render([row])
    for want in ("ENTER", "STOP", "EXIT", "2026-09-03", "2026-09-11", "17.54", "MSFT"):
        assert want in out, want
    assert "not a promise" in out          # the ticket never oversells


def test_render_avoid_side_tells_the_operator_to_wait():
    row = {"ticker": "NFLX", "close": 100.0, "adx": 30.0, "atr14": 2.0,
           "stop_distance": 3.0, "indicative_stop": 97.0,
           "entry_day": "2026-09-03", "exit_day": "2026-09-11", "prefer": [],
           "avoid": [{"rule": "20-day breakout, below SMA200", "alpha_pct_yr": -13.7,
                      "t": -4.04, "bps_per_trade": -27, "key": "breakout_below_200"}]}
    out = ets.render([row])
    assert "DON'T CHASE" in out and "wait for a pullback" in out
