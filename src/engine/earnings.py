"""Earnings-calendar hard veto — Phase L exit-policy completion (2026-07-02).

docs/market-internals-report.md: an earnings event inside a position's
holding window is a hard SAFETY GATE, not a signal ("selling a condor through
an unnoticed earnings date is the classic retail blow-up"). Debit structures
suffer the same class of harm (gap through the spread, IV crush on entry
pricing), so the gate applies to every strategy family.

Semantics (pre-specified):
  earnings_veto(ticker, expiration) -> reason | None
  - VETO if the ticker has an earnings date in [today, expiration] — the
    maximum window the position could exist, robust to mandate drift.
  - FAIL-CLOSED: no calendar entry, or an entry staler than
    STALE_AFTER_DAYS, vetoes single names ("unverifiable" beats "unnoticed").
  - ETFs/indices (config sectors macro_etf + index) are exempt — they have
    no earnings; a calendar outage never silences index trading.

Calendar store: data/earnings_calendar.json
  {ticker: {"dates": ["YYYY-MM-DD", ...], "fetched_at": iso}}
Refreshed once daily (scheduler 06:00 PT job + startup-if-stale) via
yfinance get_earnings_dates — per-ticker failures keep the previous entry
(per-ticker staleness then governs).
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CALENDAR_FILE = ROOT / "data" / "earnings_calendar.json"

logger = logging.getLogger("trader")

STALE_AFTER_DAYS = 3          # calendar entry older than this = unverifiable
FETCH_HORIZON_LIMIT = 8       # yfinance rows per ticker (future + recent past)


def _etf_symbols() -> set[str]:
    try:
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
        sectors = (cfg.get("watchlist") or {}).get("sectors") or {}
        return set(sectors.get("macro_etf", [])) | set(sectors.get("index", []))
    except Exception:
        # conservative fallback: the known no-earnings instruments
        return {"SPY", "QQQ", "IWM", "TLT", "SLV", "XLE", "XLF"}


def load_calendar() -> dict:
    if CALENDAR_FILE.exists():
        try:
            return json.loads(CALENDAR_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_calendar(cal: dict):
    CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CALENDAR_FILE.write_text(json.dumps(cal, indent=1, default=str))


def refresh_calendar(watchlist: list[str]) -> dict:
    """Fetch upcoming earnings dates for single names; per-ticker failures
    keep the prior entry. Never raises. Once-daily cadence."""
    cal = load_calendar()
    etfs = _etf_symbols()
    now = datetime.now().isoformat(timespec="seconds")
    ok = fail = 0
    for ticker in watchlist:
        if ticker in etfs:
            continue
        try:
            import yfinance as yf
            # .calendar (JSON API, no lxml) returns the NEXT earnings window as
            # 'Earnings Date': [date] or [start, end] estimate — enough for the
            # [today, expiration] veto horizon. Unconfirmed ranges still veto.
            c = yf.Ticker(ticker).calendar or {}
            dates = sorted({d.isoformat() for d in (c.get("Earnings Date") or [])
                            if d >= date.today()})
            cal[ticker] = {"dates": dates, "fetched_at": now}
            ok += 1
        except Exception as exc:
            fail += 1
            logger.warning("earnings refresh failed for %s (%s) — keeping prior entry",
                           ticker, exc)
    _save_calendar(cal)
    logger.info("earnings calendar refreshed: %d ok, %d failed, %d entries",
                ok, fail, len(cal))
    return cal


def calendar_is_fresh_today() -> bool:
    cal = load_calendar()
    today = f"{date.today()}"
    return any(str(e.get("fetched_at", "")).startswith(today) for e in cal.values())


def earnings_veto(ticker: str, expiration: str | None,
                  cal: dict | None = None) -> str | None:
    """Hard gate: reason string to VETO the entry, or None to allow."""
    if ticker in _etf_symbols():
        return None
    cal = cal if cal is not None else load_calendar()
    entry = cal.get(ticker)
    if not entry:
        return f"no earnings-calendar data for {ticker} — unverifiable (fail-closed)"
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, ValueError):
        return f"earnings-calendar entry for {ticker} unreadable (fail-closed)"
    if datetime.now() - fetched > timedelta(days=STALE_AFTER_DAYS):
        return (f"earnings calendar for {ticker} stale "
                f"(fetched {entry['fetched_at'][:10]}) — unverifiable (fail-closed)")
    if not expiration:
        return f"no expiration on trade for {ticker} — window unverifiable (fail-closed)"
    try:
        end = date.fromisoformat(str(expiration)[:10])
    except ValueError:
        return f"unparseable expiration {expiration!r} (fail-closed)"
    for d in entry.get("dates", []):
        try:
            ed = date.fromisoformat(d)
        except ValueError:
            continue
        if date.today() <= ed <= end:
            return f"earnings {d} inside holding window (today..{end})"
    return None
