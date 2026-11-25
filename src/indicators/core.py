"""Core indicator math. Deterministic, causal (no future data), no LLM."""

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average. Causal: value at t uses data up to t only."""
    return series.ewm(span=period, adjust=False).mean()


def cross_above(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """True on the bar where fast closes above slow after being at-or-below."""
    prev_below = fast.shift(1) <= slow.shift(1)
    return (fast > slow) & prev_below


def cross_below(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """True on the bar where fast closes below slow after being at-or-above."""
    prev_above = fast.shift(1) >= slow.shift(1)
    return (fast < slow) & prev_above


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR. Causal: uses prior close, smoothed with past data only."""
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ── session helpers (intraday bars) ──

def session_id(index: pd.Index) -> pd.DatetimeIndex:
    """One id per trading session (the bar's calendar date)."""
    return pd.DatetimeIndex(index).normalize()


def bar_of_session(index: pd.Index) -> pd.Series:
    """0-based position of each bar within its session."""
    s = pd.Series(1, index=index)
    return s.groupby(session_id(index)).cumsum() - 1


def last_bar_of_session(index: pd.Index, final_bar_hour: int = 15) -> pd.Series:
    """True on each session's final bar, determined by the CLOCK (schedule),
    not by looking at the next bar — detecting the boundary from the next
    bar's date is lookahead and fails the truncation test. On 1h bars the
    15:30 ET bar is the regular session's last. Half days (1pm close) have
    no flagged bar, which is causal and merely skips that day's signal."""
    return pd.Series(pd.DatetimeIndex(index).hour == final_bar_hour, index=index)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP from typical price. Causal: cumulative within session."""
    sid = session_id(df.index)
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = (typical * df["Volume"]).groupby(sid).cumsum()
    vol = df["Volume"].groupby(sid).cumsum()
    return pv / vol


def rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """Percentile rank (0-1) of the current value within the trailing window."""
    return series.rolling(window).rank(pct=True)
