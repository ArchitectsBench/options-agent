"""Nasdaq Data Link (Quandl) datatables adapter — HISTORICAL / OFFLINE ONLY.

This is an EOD equity-research feed (Sharadar SEP/TICKERS/EVENTS/SF1/SF2 plus
NDAQ/RTAT10). It is for Track A research and the nightly mirror — it must NEVER
be called from the live 5-minute cycle (see docs/nasdaq_data_link_assessment_
20260920.md: "never feeds intraday SCALP"). Tradier serves the live loop.

Key from .env only (NASDAQ_DATA_LINK_KEY); a missing key => available() is
False and callers degrade gracefully. Raw REST, no SDK, matching the
polygon_data.py pattern.

DATASET NAMES — verified live 2026-10-19 / 2026-10-20, read this before adding one:
    SHARADAR/TICKERS, SHARADAR/SEP, SHARADAR/EVENTS, SHARADAR/SF1, SHARADAR/SF2
    NDAQ/RTAT10      <- RTAT10 lives in the NDAQ namespace, NOT SHARADAR.
`SHARADAR/RTAT10` and `SHARADAR/RTAT` both return HTTP 404 ("datatable does not
exist"). That 404 is a NAMESPACE error, not an entitlement failure — do not
read it as a missing subscription and do not point a mirror at it.

PAGINATION is cursor-based, not page-numbered (probed 2026-10-20): a truncated
response carries `meta.next_cursor_id`, which is passed back as the
`qopts.cursor_id` parameter. `qopts.per_page` only sizes a page; it does not
advance one.

KEY HYGIENE: the key travels as the `api_key` query parameter, so the request
URL is a secret. No error shape, log line or exception message produced here
ever embeds the URL, and _scrub() strips the key from any body we do surface.

CLI:
    .venv/bin/python src/data/nasdaq_data_link.py --check
    .venv/bin/python src/data/nasdaq_data_link.py --probe SHARADAR/SEP --ticker AAPL
"""

import argparse
import json as jsonlib
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

BASE = "https://data.nasdaq.com/api/v3/datatables"

# Transient-failure handling mirrors polygon_data.py deliberately: 429/5xx and
# network errors are retried with exponential backoff + jitter; any other 4xx
# (403 not entitled, 404 wrong namespace) returns IMMEDIATELY and
# distinguishably, because neither is transient and retrying only burns quota.
BACKOFF_TRIES = 5
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 16.0
BACKOFF_JITTER_FRAC = 0.25
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
PAGE_CAP = 100                   # hard cap on cursor hops per fetch
PER_PAGE = 10000                 # NDL's datatable page maximum

_sleep = time.sleep               # injectable for tests (no real waits)
_urlopen = urllib.request.urlopen  # injectable for tests (no network)

KNOWN_DATATABLES = (
    "SHARADAR/TICKERS", "SHARADAR/SEP", "SHARADAR/EVENTS",
    "SHARADAR/SF1", "SHARADAR/SF2", "NDAQ/RTAT10",
)


def _backoff_delay(attempt: int) -> float:
    base = min(BACKOFF_BASE_S * (2 ** attempt), BACKOFF_CAP_S)
    return base + random.uniform(0, BACKOFF_JITTER_FRAC * base)


class NasdaqDataLink:
    """Read-only datatables client. No method here writes or deletes anything."""

    def __init__(self, key: str | None = None):
        # Only key=None consults the environment. An EXPLICIT key="" means
        # "no key" and must never fall through to the operator's real .env
        # value — otherwise a test constructed to be keyless would make a
        # live, quota-consuming call with the production key.
        if key is None:
            key = os.environ.get("NASDAQ_DATA_LINK_KEY")
        self.key = key or None

    def available(self) -> bool:
        return bool(self.key)

    def _scrub(self, text: str) -> str:
        """Never let the api_key reach a caller, a log or a traceback."""
        out = str(text)
        if self.key:
            out = out.replace(self.key, "<KEY>")
        return out

    def _error_body(self, exc) -> str:
        try:
            raw = exc.read()
        except Exception:
            return ""
        if isinstance(raw, bytes):
            raw = raw.decode(errors="replace")
        return self._scrub(raw)[:200]

    def _get(self, datatable: str, params: dict) -> dict:
        """GET one page. Returns the parsed JSON body, or an error shape
        {"error", "http_status", "retries", "attempts"} — http_status is the
        HTTP code (None for network/decode failures). Retries 429/5xx/network
        up to BACKOFF_TRIES; other 4xx returns at once."""
        query = dict(params)
        query["api_key"] = self.key
        url = f"{BASE}/{datatable}.json?" + urllib.parse.urlencode(query)
        last: dict = {"error": "no attempt made", "http_status": None,
                      "retries": 0, "attempts": 0}
        for attempt in range(BACKOFF_TRIES):
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            try:
                with _urlopen(req, timeout=60) as resp:
                    return jsonlib.loads(resp.read())
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                last = {"error": f"HTTP {status}: {self._error_body(exc)}",
                        "http_status": status, "retries": attempt,
                        "attempts": attempt + 1}
                if status not in RETRYABLE_STATUS:
                    return last
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    socket.timeout) as exc:
                last = {"error": self._scrub(exc), "http_status": None,
                        "retries": attempt, "attempts": attempt + 1}
            except Exception as exc:
                # not transient (e.g. undecodable body) — report, don't loop
                return {"error": self._scrub(exc), "http_status": None,
                        "retries": attempt, "attempts": attempt + 1}
            if attempt < BACKOFF_TRIES - 1:
                _sleep(_backoff_delay(attempt))
        return last

    def fetch(self, datatable: str, **filters) -> dict:
        """Fetch a whole datatable selection, following cursors.

        Returns {"columns": [name, ...], "rows": [[...], ...], "pages": n} on
        success, or the _get error shape (with "error") on failure. Filters are
        passed through as NDL query params (e.g. ticker="AAPL",
        date={"gte": "2026-01-01"} -> "date.gte"). Nothing is cached or written.
        """
        if not self.available():
            return {"error": "NASDAQ_DATA_LINK_KEY not set", "http_status": None,
                    "retries": 0, "attempts": 0}
        params: dict = {"qopts.per_page": PER_PAGE}
        for name, value in filters.items():
            if isinstance(value, dict):      # range filter: date={"gte": ...}
                for op, operand in value.items():
                    params[f"{name}.{op}"] = operand
            elif isinstance(value, (list, tuple)):
                params[name] = ",".join(str(v) for v in value)
            else:
                params[name] = value
        columns: list[str] = []
        rows: list[list] = []
        pages = 0
        while pages < PAGE_CAP:
            out = self._get(datatable, params)
            if not isinstance(out, dict) or "error" in out:
                return out if isinstance(out, dict) else {
                    "error": "malformed response", "http_status": None,
                    "retries": 0, "attempts": 1}
            pages += 1
            table = out.get("datatable") or {}
            if not columns:
                columns = [c.get("name") for c in (table.get("columns") or [])]
            rows.extend(table.get("data") or [])
            cursor = (out.get("meta") or {}).get("next_cursor_id")
            if not cursor:
                break
            params["qopts.cursor_id"] = cursor
        return {"columns": columns, "rows": rows, "pages": pages}


def main() -> int:
    ap = argparse.ArgumentParser(description="Nasdaq Data Link read-only probe")
    ap.add_argument("--check", action="store_true", help="key presence only (no network)")
    ap.add_argument("--probe", metavar="DATATABLE", help="e.g. SHARADAR/SEP or NDAQ/RTAT10")
    ap.add_argument("--ticker")
    args = ap.parse_args()
    client = NasdaqDataLink()
    if args.check or not args.probe:
        print(f"available={client.available()} known={', '.join(KNOWN_DATATABLES)}")
        return 0 if client.available() else 1
    filters = {"ticker": args.ticker} if args.ticker else {}
    out = client.fetch(args.probe, **filters)
    if "error" in out:
        print(f"FAIL {args.probe}: {out['error']} (http={out.get('http_status')})")
        return 1
    print(f"OK {args.probe}: {len(out['rows'])} rows, {len(out['columns'])} cols, "
          f"{out['pages']} page(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
