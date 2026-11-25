"""Polygon.io / Massive historical-data adapter (Track A research).

Polygon rebranded to Massive (Oct 2025); the same API key works on both
api.polygon.io and api.massive.com. The Starter tier is 15-MINUTE DELAYED —
that's fine here: this adapter is for HISTORICAL bars feeding the offline
backtester (Track A), NOT the live 5-minute loop (Tradier serves that
real-time). Cleaner/deeper than yfinance (5yr minute history, all US
tickers, no 730d/60d caps).

Key from .env only (POLYGON_API_KEY); missing key => available() is False
and callers degrade gracefully. Raw REST with pagination, matching the
existing tradier.py pattern (no SDK dependency).

CLI:
    python src/data/polygon_data.py --check                 # connectivity
    python src/data/polygon_data.py --download SPY AAPL --interval 1d --years 2
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
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

BASE = "https://api.polygon.io"          # api.massive.com also works
OUTPUT_DIR = ROOT / "data" / "historical"

# ── Rate-limit / transient-failure handling (2026-09-13) ──────────────────
# Until now `_get` had NO 429 handling: a throttled request came back as
# {"error": "HTTP 429 ..."} and every caller treated that like "no data"
# (rvol_shadow_scan silently dropped 591 of 1,780 candidates on 2026-07-29;
# the sleeve's night-1 429s were masked as "no_bars"). Now: 429 and 5xx (and
# transient network errors) are retried with exponential backoff + jitter,
# Retry-After is honoured when present, and an exhausted request returns a
# DISTINGUISHABLE error shape {"error", "http_status", "retries"} — never a
# silent empty result. Other 4xx (403/404) return immediately: they are not
# transient and every other caller of this adapter inherits this path.
BACKOFF_TRIES = 5                # total attempts (1 initial + 4 retries)
BACKOFF_BASE_S = 1.0             # 1s, 2s, 4s, 8s (+ jitter) between attempts
BACKOFF_CAP_S = 16.0             # ceiling for the computed delay
BACKOFF_JITTER_FRAC = 0.25       # uniform(0, frac*delay) added to each wait
RETRY_AFTER_CAP_S = 120.0        # never trust a Retry-After longer than this
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_sleep = time.sleep              # injectable for tests (no real waits)
_urlopen = urllib.request.urlopen  # injectable for tests (no network)


def _error_body(exc) -> str:
    """First 200 chars of an HTTPError body; never raises (HTTPError.read()
    returns str when its fp is a StringIO, bytes otherwise)."""
    try:
        raw = exc.read()
    except Exception:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    return str(raw)[:200]


def _retry_after_seconds(headers) -> float | None:
    """Parse an integer-seconds Retry-After header; HTTP-date forms (or a
    missing/garbled header) return None so the computed backoff applies."""
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except Exception:
        raw = None
    if raw is None:
        return None
    try:
        secs = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if secs < 0:
        return None
    return min(secs, RETRY_AFTER_CAP_S)


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Delay before retry number `attempt` (0-based): Retry-After if the
    server sent one, else base*2^attempt capped, plus jitter."""
    if retry_after is not None:
        base = retry_after
    else:
        base = min(BACKOFF_BASE_S * (2 ** attempt), BACKOFF_CAP_S)
    return base + random.uniform(0, BACKOFF_JITTER_FRAC * base)

# our interval string -> (multiplier, timespan, is_intraday, filename suffix)
INTERVALS = {
    "1d": (1, "day", False, ""),
    "1h": (1, "hour", True, "_1h"),
    "30m": (30, "minute", True, "_30m"),
    "5m": (5, "minute", True, "_5m"),
}


class PolygonData:
    def __init__(self):
        self.key = os.environ.get("POLYGON_API_KEY") or None

    def available(self) -> bool:
        return bool(self.key)

    def _get(self, url: str) -> dict | None:
        """GET with backoff (see the module note above). Returns the parsed
        JSON body, or an error shape {"error", "http_status", "retries",
        "attempts"}: http_status is the HTTP code (None for network/decode
        failures), retries the number of re-attempts made. 429/5xx/network
        errors are retried up to BACKOFF_TRIES; any other 4xx (403 not
        entitled, 404) returns immediately. Signature unchanged."""
        sep = "&" if "?" in url else "?"
        full = f"{url}{sep}apiKey={self.key}" if "apiKey=" not in url else url
        last: dict = {"error": "no attempt made", "http_status": None,
                      "retries": 0, "attempts": 0}
        for attempt in range(BACKOFF_TRIES):
            req = urllib.request.Request(full, headers={"Accept": "application/json"})
            retry_after = None
            try:
                with _urlopen(req, timeout=30) as resp:
                    return jsonlib.loads(resp.read())
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                last = {"error": f"HTTP {status}: {_error_body(exc)}",
                        "http_status": status, "retries": attempt,
                        "attempts": attempt + 1}
                if status not in RETRYABLE_STATUS:
                    return last
                retry_after = _retry_after_seconds(getattr(exc, "headers", None))
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    socket.timeout) as exc:
                last = {"error": str(exc), "http_status": None,
                        "retries": attempt, "attempts": attempt + 1}
            except Exception as exc:
                # not transient (e.g. undecodable body) — report, don't loop
                return {"error": str(exc), "http_status": None,
                        "retries": attempt, "attempts": attempt + 1}
            if attempt < BACKOFF_TRIES - 1:
                _sleep(_backoff_delay(attempt, retry_after))
        return last

    def aggregates(self, ticker: str, multiplier: int, timespan: str,
                   start: str, end: str) -> list[dict]:
        """Paginated aggregate bars. start/end are YYYY-MM-DD."""
        url = (f"{BASE}/v2/aggs/ticker/{ticker.upper()}/range/{multiplier}/{timespan}"
               f"/{start}/{end}?adjusted=true&sort=asc&limit=50000")
        results = []
        for _ in range(50):  # hard page cap
            out = self._get(url)
            if out is None or "error" in out:
                break
            results.extend(out.get("results", []) or [])
            nxt = out.get("next_url")
            if not nxt:
                break
            url = nxt
            time.sleep(0.2)  # politeness between pages
        return results

    def fetch_bars(self, ticker: str, interval: str,
                   start: str, end: str) -> pd.DataFrame | None:
        if not self.available() or interval not in INTERVALS:
            return None
        mult, span, intraday, _ = INTERVALS[interval]
        rows = self.aggregates(ticker, mult, span, start, end)
        if not rows:
            return None
        df = pd.DataFrame(rows)
        idx = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
        if not intraday:
            idx = idx.dt.tz_localize(None).dt.normalize()
        df.index = pd.DatetimeIndex(idx)
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low",
                                "c": "Close", "v": "Volume"})
        return df[["Open", "High", "Low", "Close", "Volume"]].sort_index()


_client: PolygonData | None = None


def get_client() -> PolygonData:
    global _client
    if _client is None:
        _client = PolygonData()
    return _client


def download_history(tickers: list[str], interval: str = "1d", years: float = 2.0,
                     out_dir: Path = OUTPUT_DIR, merge: bool = True) -> list[str]:
    """Download history for tickers and write/merge parquet in our store format.
    Merges with any existing file (dedup on timestamp, new wins)."""
    client = get_client()
    if not client.available():
        return ["POLYGON_API_KEY not set — fill it in .env"]
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = INTERVALS[interval][3]
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=int(years * 365))).isoformat()
    log = []
    for t in tickers:
        df = client.fetch_bars(t, interval, start, end)
        if df is None or df.empty:
            log.append(f"{t} {interval}: no data")
            continue
        path = out_dir / f"{t}{suffix}.parquet"
        if merge and path.exists():
            old = pd.read_parquet(path)
            df = pd.concat([old, df])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        df.to_parquet(path)
        log.append(f"{t} {interval}: {len(df)} bars, {df.index.min()} .. {df.index.max()}")
        time.sleep(0.1)
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="connectivity test")
    ap.add_argument("--download", nargs="+", metavar="TICKER")
    ap.add_argument("--interval", default="1d", choices=list(INTERVALS))
    ap.add_argument("--years", type=float, default=2.0)
    args = ap.parse_args()

    client = get_client()
    if not client.available():
        print("POLYGON_API_KEY not set. Add it to .env, then re-run.")
        return
    if args.check:
        # one cheap daily bar for SPY to confirm the key works
        df = client.fetch_bars("SPY", "1d",
                               (date.today() - timedelta(days=7)).isoformat(),
                               date.today().isoformat())
        if df is not None and len(df):
            print(f"OK — key valid. SPY last {len(df)} daily bars, latest close "
                  f"{df['Close'].iloc[-1]} @ {df.index[-1].date()}")
            print("(reminder: Starter is 15-min delayed — historical/backtest use only)")
        else:
            raw = client.aggregates("SPY", 1, "day",
                                    (date.today() - timedelta(days=7)).isoformat(),
                                    date.today().isoformat())
            print(f"key set but no data returned — check plan/entitlements. raw: {str(raw)[:200]}")
    elif args.download:
        for line in download_history(args.download, args.interval, args.years):
            print(" ", line)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
