"""Tradier API client — primary market data source (Track B).

- Tokens from .env only (TRADIER_PROD_TOKEN / TRADIER_SANDBOX_TOKEN); never
  hardcoded. Missing token => available() is False and callers fall back to
  yfinance (graceful degradation).
- Token-bucket rate limiter capped WELL under Tradier's limits:
  production 120/min -> bucket 60/min; sandbox 60/min -> bucket 30/min.
- THIS MODULE NEVER PLACES ORDERS. The production token is data-only by
  construction: only GET /markets/* endpoints exist here. Order placement
  lives in tradier_orders.py, which is sandbox-only by hard assertion.
- Index quotes (VIX et al.) are 15-minute delayed even on production —
  surfaced via the `delayed` flag so the regime output can disclose it.
"""

import os
import threading
import time
import urllib.parse
import urllib.request
import json as jsonlib
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

PROD_BASE = "https://api.tradier.com/v1"
SANDBOX_BASE = "https://sandbox.tradier.com/v1"

# yfinance-style -> Tradier symbology
SYMBOL_MAP = {"^VIX": "VIX"}
INDEX_SYMBOLS = {"VIX"}  # 15-min delayed even in production


class TokenBucket:
    def __init__(self, per_minute: int):
        self.capacity = per_minute
        self.tokens = float(per_minute)
        self.rate = per_minute / 60.0
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                time.sleep(wait)
                self.tokens = 0.0
                self.last = time.monotonic()
            else:
                self.tokens -= 1


class TradierData:
    """Market data only (production token). No order endpoints exist here."""

    # R9 failure-breaker (hung-cycle blindness fix): under a black-holed network
    # every _get burns 3 attempts x 15s timeout, and a full cycle iterates that
    # over many symbols -> a 25-30 min cycle. A consecutive-failure breaker
    # opens after N straight failures and fast-fails (return None WITHOUT hitting
    # the network) for a cooldown, so the cycle degrades to yfinance/last-good
    # promptly instead of retry-multiplying into the void. Reset on any success.
    BREAKER_THRESHOLD = 5       # consecutive _get failures before the breaker opens
    BREAKER_COOLDOWN_SEC = 60   # fast-fail window once open

    def __init__(self):
        self.token = os.environ.get("TRADIER_PROD_TOKEN") or None
        self.bucket = TokenBucket(per_minute=60)  # half of the 120/min limit
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._breaker_lock = threading.Lock()

    def available(self) -> bool:
        return bool(self.token)

    def breaker_open(self) -> bool:
        """True while the failure-breaker is tripped (fast-fail window)."""
        return time.monotonic() < self._breaker_open_until

    def _record_success(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures = 0
            self._breaker_open_until = 0.0

    def _record_failure(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + self.BREAKER_COOLDOWN_SEC

    def _get(self, path: str, params: dict) -> dict | None:
        if not self.available():
            return None
        # R9 breaker: while open, fast-fail without touching the network so a
        # dead endpoint can't stretch the cycle by retry-multiplying per symbol.
        if self.breaker_open():
            return None
        url = f"{PROD_BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        # Retry transient failures (timeout / 429) before giving up to the
        # yfinance fallback — yfinance is heavily rate-limited intraday, so the
        # silent Tradier->yfinance cascade is what caused the 2026-06-17 mass
        # per-cycle ticker failures. 1 try + 2 backoff retries.
        for attempt in range(3):
            self.bucket.acquire()
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = jsonlib.loads(resp.read())
                self._record_success()
                return data
            except Exception as exc:
                if attempt == 2:
                    self._record_failure()  # only a fully-exhausted _get counts
                    return None
                time.sleep((2.0 if getattr(exc, "code", None) == 429 else 0.5) * (attempt + 1))
        return None

    # ── quotes (batched: all symbols in ONE call) ──
    def quotes(self, symbols: list[str]) -> dict[str, dict] | None:
        mapped = [SYMBOL_MAP.get(s, s) for s in symbols]
        out = self._get("/markets/quotes", {"symbols": ",".join(mapped), "greeks": "false"})
        if out is None or "quotes" not in out or not out["quotes"]:
            return None
        raw = out["quotes"].get("quote", [])
        raw = [raw] if isinstance(raw, dict) else raw
        reverse = {v: k for k, v in SYMBOL_MAP.items()}
        quotes = {}
        for q in raw:
            sym = reverse.get(q["symbol"], q["symbol"])
            quotes[sym] = {**q, "delayed": q["symbol"] in INDEX_SYMBOLS or q.get("type") == "index"}
        return quotes

    # ── intraday timesales bars ──
    def timesales(self, symbol: str, interval: str = "5min",
                  start: str | None = None, end: str | None = None) -> pd.DataFrame | None:
        """interval: 1min|5min|15min. History limits ~20-40 days per Tradier."""
        params = {"symbol": SYMBOL_MAP.get(symbol, symbol), "interval": interval,
                  "session_filter": "open"}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        out = self._get("/markets/timesales", params)
        if out is None or not out.get("series"):
            return None
        rows = out["series"].get("data", [])
        rows = [rows] if isinstance(rows, dict) else rows
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df.index = pd.DatetimeIndex(pd.to_datetime(df["time"])).tz_localize("America/New_York")
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
        return df[["Open", "High", "Low", "Close", "Volume"]].sort_index()

    # ── market calendar / clock ──
    def clock(self) -> dict | None:
        out = self._get("/markets/clock", {})
        return out.get("clock") if out else None

    def calendar(self, month: int, year: int) -> dict[str, dict] | None:
        """{date: {status, open, close}} for the month — the intraday open/close
        times are under the keys `open`/`close` (ET "HH:MM"), matching the code
        below. Consumers must read those keys, not open_time/close_time (R12:
        the freshness half-day path silently died reading the wrong keys)."""
        out = self._get("/markets/calendar", {"month": month, "year": year})
        if out is None:
            return None
        days = out.get("calendar", {}).get("days", {}).get("day", [])
        days = [days] if isinstance(days, dict) else days
        result = {}
        for d in days:
            hours = d.get("open") or {}
            result[d["date"]] = {"status": d["status"],
                                 "open": hours.get("start"), "close": hours.get("end")}
        return result

    # ── options ──
    def expirations(self, symbol: str) -> list[str] | None:
        out = self._get("/markets/options/expirations",
                        {"symbol": symbol, "includeAllRoots": "true"})
        if out is None or not out.get("expirations"):
            return None
        dates = out["expirations"].get("date", [])
        return [dates] if isinstance(dates, str) else dates

    def chain(self, symbol: str, expiration: str) -> list[dict] | None:
        out = self._get("/markets/options/chains",
                        {"symbol": symbol, "expiration": expiration, "greeks": "true"})
        if out is None or not out.get("options"):
            return None
        opts = out["options"].get("option", [])
        return [opts] if isinstance(opts, dict) else opts


_client: TradierData | None = None


def get_client() -> TradierData:
    global _client
    if _client is None:
        _client = TradierData()
    return _client
