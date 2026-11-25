"""Parameterized signal factories for the research pipeline."""

import pandas as pd

from src.indicators.core import cross_above, cross_below, ema


def ema_crossover(fast_period: int, slow_period: int):
    """Return a signal_func for an EMA fast/slow crossover (long-only)."""
    if fast_period >= slow_period:
        raise ValueError(f"fast_period ({fast_period}) must be < slow_period ({slow_period})")

    def signals(df: pd.DataFrame):
        fast = ema(df["Close"], fast_period)
        slow = ema(df["Close"], slow_period)
        return cross_above(fast, slow), cross_below(fast, slow)

    return signals
