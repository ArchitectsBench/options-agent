"""Signal container passed from strategies to the backtester/validator.

Boolean series are read at bar close and fill at the next bar's open.
Level series (stop/target) are absolute prices captured at the SIGNAL bar —
they must be computable at signal time (the rule validator's truncation
test covers them too). NaN level = no stop/target.
"""

from dataclasses import dataclass, fields

import pandas as pd


@dataclass
class Signals:
    entry_long: pd.Series | None = None
    exit_long: pd.Series | None = None
    entry_short: pd.Series | None = None
    exit_short: pd.Series | None = None
    stop_long: pd.Series | None = None
    target_long: pd.Series | None = None
    stop_short: pd.Series | None = None
    target_short: pd.Series | None = None

    BOOL_FIELDS = ("entry_long", "exit_long", "entry_short", "exit_short")
    LEVEL_FIELDS = ("stop_long", "target_long", "stop_short", "target_short")

    def named_series(self) -> dict[str, pd.Series]:
        return {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) is not None}


def as_signals(x) -> Signals:
    """Accept a Signals object or the legacy (entry, exit) long-only tuple."""
    if isinstance(x, Signals):
        return x
    entry, exit_ = x
    return Signals(entry_long=entry, exit_long=exit_)
