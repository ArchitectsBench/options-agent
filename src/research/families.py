"""Step 4c — intraday-native strategy families for the research loop.

All families are session-flat by design (backtested with
flat_at_session_end=True) and their signal timescales are intraday — the
lesson from the EMA runs: signal timescale must match holding timescale.

Each factory returns signal_func(df) -> Signals. Every series emitted is
causal; the Stage 2 truncation test verifies that empirically.
"""

import numpy as np
import pandas as pd

from src.indicators.core import (
    atr,
    bar_of_session,
    cross_above,
    cross_below,
    ema,
    last_bar_of_session,
    rolling_percentile,
    session_id,
    session_vwap,
    sma,
)
from src.research.signals import Signals

ATR_PERIOD = 14
VOLUME_LOOKBACK = 20
WIDTH_RANK_WINDOW = 126  # ~18 sessions of 1h bars


def _first_true_in_session(mask: pd.Series, sid) -> pd.Series:
    """Keep only the first True of each session."""
    fired_before = mask.groupby(sid).cummax().groupby(sid).shift(1, fill_value=False)
    return mask & ~fired_before


# ── Family 1: Opening Range Breakout ──

def orb(
    n_or: int,
    atr_mult: float,
    use_target: bool,
    volume_confirm: bool,
    side: str = "both",          # "long" | "short" | "both"
    cutoff_hour: int | None = None,  # no NEW entries at/after this bar-start hour (ET)
):
    def signals(df: pd.DataFrame) -> Signals:
        sid = session_id(df.index)
        bos = bar_of_session(df.index)
        a = atr(df, ATR_PERIOD)

        # OR high/low from the first n_or bars; visible only once the OR is complete
        in_range = bos < n_or
        or_high = df["High"].where(in_range).groupby(sid).transform("max").where(bos >= n_or)
        or_low = df["Low"].where(in_range).groupby(sid).transform("min").where(bos >= n_or)

        brk_up = (df["Close"] > or_high).fillna(False)
        brk_dn = (df["Close"] < or_low).fillna(False)
        if volume_confirm:
            vol_ok = df["Volume"] > df["Volume"].rolling(VOLUME_LOOKBACK).mean()
            brk_up, brk_dn = brk_up & vol_ok, brk_dn & vol_ok
        if cutoff_hour is not None:
            # clock-based and causal; entry signal at bar i fills at bar i+1
            in_window = pd.Series(pd.DatetimeIndex(df.index).hour < cutoff_hour, index=df.index)
            brk_up, brk_dn = brk_up & in_window, brk_dn & in_window

        entry_long = _first_true_in_session(brk_up, sid)
        entry_short = _first_true_in_session(brk_dn, sid)
        # both sides breaking the same bar = chop, stand aside
        clash = entry_long & entry_short
        entry_long, entry_short = entry_long & ~clash, entry_short & ~clash
        if side == "long":
            entry_short = pd.Series(False, index=df.index)
        elif side == "short":
            entry_long = pd.Series(False, index=df.index)

        nan = pd.Series(np.nan, index=df.index)
        target_long = (df["Close"] + atr_mult * a).where(entry_long) if use_target else nan
        target_short = (df["Close"] - atr_mult * a).where(entry_short) if use_target else nan

        return Signals(
            entry_long=entry_long,
            exit_long=pd.Series(False, index=df.index),  # exits via stop/target/session end
            entry_short=entry_short,
            exit_short=pd.Series(False, index=df.index),
            stop_long=or_low.where(entry_long),
            target_long=target_long,
            stop_short=or_high.where(entry_short),
            target_short=target_short,
        )

    return signals


ORB_SPACE = {
    "n_or": ("cat", [1, 2]),
    "atr_mult": ("float", 0.5, 3.0),
    "use_target": ("cat", [True, False]),
    "volume_confirm": ("cat", [True, False]),
}


# ── Family 2a: VWAP trend (pullback that holds) ──

def vwap_trend(ext_atr: float, touch_atr: float):
    def signals(df: pd.DataFrame) -> Signals:
        sid = session_id(df.index)
        a = atr(df, ATR_PERIOD)
        vwap = session_vwap(df)
        dev = df["Close"] - vwap

        # trend established strictly BEFORE this bar (shifted cumulative extreme)
        prior_run_up = dev.groupby(sid).cummax().groupby(sid).shift(1)
        prior_run_dn = dev.groupby(sid).cummin().groupby(sid).shift(1)

        touch_up = df["Low"] <= vwap + touch_atr * a
        touch_dn = df["High"] >= vwap - touch_atr * a
        holds_up = (df["Close"] > vwap) & (df["Close"] > df["Open"])
        holds_dn = (df["Close"] < vwap) & (df["Close"] < df["Open"])

        entry_long = ((prior_run_up >= ext_atr * a) & touch_up & holds_up).fillna(False)
        entry_short = ((prior_run_dn <= -ext_atr * a) & touch_dn & holds_dn).fillna(False)
        clash = entry_long & entry_short
        return Signals(
            entry_long=entry_long & ~clash,
            exit_long=(df["Close"] < vwap).fillna(False),
            entry_short=entry_short & ~clash,
            exit_short=(df["Close"] > vwap).fillna(False),
        )

    return signals


VWAP_TREND_SPACE = {
    "ext_atr": ("float", 0.5, 2.5),
    "touch_atr": ("float", 0.05, 0.75),
}


# ── Family 2b: VWAP reversion (fade extensions) ──

def vwap_reversion(k: float, confirm_bars: int):
    def signals(df: pd.DataFrame) -> Signals:
        a = atr(df, ATR_PERIOD)
        vwap = session_vwap(df)
        dev = df["Close"] - vwap

        ext_up = (dev > k * a).fillna(False)
        ext_dn = (dev < -k * a).fillna(False)
        # extension must persist confirm_bars consecutive bars before fading it
        entry_short = ext_up.rolling(confirm_bars).sum().eq(confirm_bars)
        entry_long = ext_dn.rolling(confirm_bars).sum().eq(confirm_bars)
        clash = entry_long & entry_short
        return Signals(
            entry_long=entry_long & ~clash,
            exit_long=(df["Close"] >= vwap).fillna(False),  # reverted to VWAP
            entry_short=entry_short & ~clash,
            exit_short=(df["Close"] <= vwap).fillna(False),
        )

    return signals


VWAP_REVERSION_SPACE = {
    "k": ("float", 0.75, 3.0),
    "confirm_bars": ("int", 1, 3),
}


# ── Family 3: Intraday momentum continuation (re-entry allowed) ──

def momentum_reentry(fast_period: int, slow_period: int):
    def signals(df: pd.DataFrame) -> Signals:
        fast = ema(df["Close"], fast_period)
        slow = ema(df["Close"], slow_period)
        last_bar = last_bar_of_session(df.index)
        # Re-entry fix: signal on each session's LAST bar when the stack is
        # bullish fills at the NEXT session's open — so an established trend
        # is re-entered every day instead of only on the cross day.
        entry_long = cross_above(fast, slow) | (last_bar & (fast > slow))
        return Signals(
            entry_long=entry_long.fillna(False),
            exit_long=cross_below(fast, slow).fillna(False),
        )

    return signals


MOMENTUM_SPACE = {
    "fast_period": ("int", 3, 12),
    "slow_period": ("int", 10, 30),
}
MOMENTUM_CONSTRAINT = lambda p: p["fast_period"] < p["slow_period"]  # noqa: E731


# ── Family 4: Squeeze proxy (Bollinger width percentile) ──

def squeeze_proxy(bb_period: int, width_pct: float, mom_period: int):
    def signals(df: pd.DataFrame) -> Signals:
        mid = sma(df["Close"], bb_period)
        sd = df["Close"].rolling(bb_period).std()
        width = (4 * sd) / mid  # (upper - lower) / middle with 2-sigma bands
        width_rank = rolling_percentile(width, WIDTH_RANK_WINDOW)

        squeezed = (width_rank <= width_pct).fillna(False)
        expanding = (width > width.shift(1)).fillna(False)
        fired = squeezed.shift(1, fill_value=False) & ~squeezed & expanding

        mom_ema = ema(df["Close"], mom_period)
        mom_up = df["Close"] > mom_ema
        return Signals(
            entry_long=(fired & mom_up).fillna(False),
            exit_long=(df["Close"] < mom_ema).fillna(False),
            entry_short=(fired & ~mom_up).fillna(False),
            exit_short=(df["Close"] > mom_ema).fillna(False),
        )

    return signals


SQUEEZE_SPACE = {
    "bb_period": ("int", 10, 40),
    "width_pct": ("float", 0.10, 0.40),
    "mom_period": ("int", 5, 30),
}


# ── Registry consumed by the research loop ──

FAMILIES = {
    "orb": {
        "factory": orb,
        "space": ORB_SPACE,
        "constraint": None,
        "validation_params": {"n_or": 1, "atr_mult": 1.5, "use_target": True, "volume_confirm": False},
    },
    "vwap_trend": {
        "factory": vwap_trend,
        "space": VWAP_TREND_SPACE,
        "constraint": None,
        "validation_params": {"ext_atr": 1.0, "touch_atr": 0.3},
    },
    "vwap_reversion": {
        "factory": vwap_reversion,
        "space": VWAP_REVERSION_SPACE,
        "constraint": None,
        "validation_params": {"k": 1.5, "confirm_bars": 2},
    },
    "momentum_reentry": {
        "factory": momentum_reentry,
        "space": MOMENTUM_SPACE,
        "constraint": MOMENTUM_CONSTRAINT,
        "validation_params": {"fast_period": 5, "slow_period": 20},
    },
    "squeeze_proxy": {
        "factory": squeeze_proxy,
        "space": SQUEEZE_SPACE,
        "constraint": None,
        "validation_params": {"bb_period": 20, "width_pct": 0.25, "mom_period": 10},
    },
}
