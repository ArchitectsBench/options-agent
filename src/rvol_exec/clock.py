"""Injectable clock for the RVOL-EXEC lane (spec §5, §16.3).

`SystemClock` is production; `FakeClock` drives the session simulator. Both
expose `now_et()` (aware, America/New_York), `now_utc()`, `sleep(s)` and
`monotonic()`. The FakeIB moves an injected clock through `set(dt)` so broker
time and lane time never diverge (`tests/rvol_exec/fake_ib.py::advance`).
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone

from src.rvol_exec.constants import ET


class SystemClock:
    @staticmethod
    def now_et() -> datetime:
        return datetime.now(ET)

    @staticmethod
    def now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def sleep(secs: float) -> None:
        if secs > 0:
            _time.sleep(secs)

    @staticmethod
    def monotonic() -> float:
        return _time.monotonic()


class FakeClock:
    """A settable clock. `sleep` advances it; `set` jumps it."""

    def __init__(self, start: datetime):
        self.t = start if start.tzinfo else start.replace(tzinfo=ET)
        self._epoch = self.t

    def now_et(self) -> datetime:
        return self.t.astimezone(ET)

    def now_utc(self) -> datetime:
        return self.t.astimezone(timezone.utc)

    def sleep(self, secs: float) -> None:
        self.t = self.t + timedelta(seconds=float(secs))

    def set(self, dt: datetime) -> None:
        self.t = dt if dt.tzinfo else dt.replace(tzinfo=ET)

    def monotonic(self) -> float:
        return (self.t - self._epoch).total_seconds()


def at_et(day, hh: int, mm: int, ss: int = 0) -> datetime:
    """Aware ET datetime on `day` (date or 'YYYY-MM-DD')."""
    if isinstance(day, str):
        y, m, d = (int(x) for x in day.split("-"))
    else:
        y, m, d = day.year, day.month, day.day
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)
