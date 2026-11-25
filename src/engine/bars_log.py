"""Per-cycle 5-minute bar log (2026-09-14) — the bars the engine computed its
indicators from, so the cockpit TRADE page charts exactly what the engine saw
rather than a third-party refetch.

Each cycle, for every watchlist ticker, appends the session's newly COMPLETED
5-minute bars to logs/observation/bars5m-<date>.jsonl. The first call of a
process for a ticker writes the whole session so far (a mid-day restart
backfills itself); readers dedupe on (ticker, t), keeping the last row.

Logging only: nothing here feeds a decision, and no failure ever reaches the
cycle — every path is caught and logged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger("trader")

PT = "America/Los_Angeles"
BAR_MIN = 5

# (date, ticker) -> start of the newest bar already written by this process
_written: dict[tuple[str, str], pd.Timestamp] = {}


def completed_session_bars(df: pd.DataFrame | None, now: pd.Timestamp | None = None) -> pd.DataFrame | None:
    """Today's (PT) completed 5-minute bars, index in PT. A bar is complete once
    its full 5 minutes have elapsed; the forming bar is dropped. Tz-naive
    frames are market-data convention (America/New_York)."""
    if df is None or not len(df):
        return None
    idx = df.index if df.index.tz is not None else df.index.tz_localize("America/New_York")
    out = df.copy()
    out.index = idx.tz_convert(PT)
    now = (now if now is not None else pd.Timestamp.now(tz=PT)).tz_convert(PT)
    out = out[out.index.date == now.date()]
    return out[out.index + pd.Timedelta(minutes=BAR_MIN) <= now]


def append_cycle_bars(frames: dict[str, pd.DataFrame], cycle: int, obs_dir: Path,
                      now: pd.Timestamp | None = None) -> int:
    """Append new completed bars for every ticker. Returns rows written (0 on
    any failure — the cycle must never see an exception from here)."""
    try:
        now = now if now is not None else pd.Timestamp.now(tz=PT)
        day = str(now.tz_convert(PT).date())
        rows = []
        for ticker, df in (frames or {}).items():
            try:
                bars = completed_session_bars(df, now)
                if bars is None or not len(bars):
                    continue
                last = _written.get((day, ticker))
                new = bars[bars.index > last] if last is not None else bars
                for ts, b in new.iterrows():
                    rows.append({"date": day, "t": ts.strftime("%H:%M"), "ticker": ticker,
                                 "o": round(float(b["Open"]), 4), "h": round(float(b["High"]), 4),
                                 "l": round(float(b["Low"]), 4), "c": round(float(b["Close"]), 4),
                                 "v": int(b["Volume"]) if pd.notna(b["Volume"]) else 0,
                                 "cycle": cycle})
                if len(new):
                    _written[(day, ticker)] = new.index[-1]
            except Exception as exc:  # noqa: BLE001 — one ticker never costs the rest
                logger.warning("bars log: %s skipped: %s", ticker, exc)
        if rows:
            obs_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().isoformat(timespec="seconds")
            with open(obs_dir / f"bars5m-{day}.jsonl", "a") as fh:
                for r in rows:
                    fh.write(json.dumps({**r, "logged": stamp}) + "\n")
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bars log failed (cycle unaffected): %s", exc)
        return 0
