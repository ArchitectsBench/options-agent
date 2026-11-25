"""Live data feeds — Tradier primary, yfinance automatic fallback.

Every fetcher degrades gracefully (CLAUDE.md principle 4): Tradier missing
token / API failure -> yfinance; yfinance failure -> None, and the gap lands
in the regime object's `missing` list. The agent warns and continues.

Source routing:
  - quotes: Tradier batched (all symbols, one call) -> yfinance per symbol
  - intraday bars <= ~35 days: Tradier timesales (5min native; 30m built
    from 15min pairs) -> yfinance
  - intraday bars beyond Tradier's history window, daily bars, futures
    (ES=F/NQ=F — not on Tradier market data): yfinance directly
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

from src.data.tradier import get_client

ROOT = Path(__file__).resolve().parents[2]
TRADIER_MAX_DAYS = 35  # stay inside Tradier's ~40-day intraday history window
_FRESH_CFG: dict = {}


def _fresh_cfg() -> dict:
    """Feed-layer freshness thresholds — single source of truth (mark 3)."""
    if not _FRESH_CFG:
        try:
            _FRESH_CFG.update(yaml.safe_load((ROOT / "config" / "signal_freshness.yaml").read_text()))
        except Exception:
            _FRESH_CFG.update({"cache_max_serve_min": 360, "backfill_stale_trigger_min": 3})
    return _FRESH_CFG

# Resilience (fix for 2026-06-17/18 intraday mass ticker failures): when BOTH
# Tradier and yfinance miss for a ticker, reuse the last-good bars (slightly
# stale) instead of dropping the ticker; and after a yfinance failure, skip
# yfinance for a cooldown so we don't hammer a throttled Yahoo (which escalates
# the rate-limit ban that caused the cascade).
# 6a (2026-07-24): the cache is keyed on the NEWEST BAR TIMESTAMP, not fetch
# time. The old (fetch_time, df) scheme re-stamped a stale frame as "fresh"
# whenever it was re-fetched — the INV8 root cause (a Friday bar served Monday).
# Now a staler frame can neither overwrite a fresher cached one nor be served
# once its bar is beyond _CACHE_MAX_SERVE_MIN. The per-lane decision guard
# (freshness.py / pipeline) does the tight, session-aware gating on top.
_BAR_CACHE: dict[tuple, tuple[pd.Timestamp, pd.DataFrame]] = {}   # (newest_bar_ts, df)
# cache-serve cap now lives in config/signal_freshness.yaml (mark 3)
_yf_skip_until = 0.0
_YF_COOLDOWN = 90        # seconds to skip yfinance after a failure


def _newest_bar_ts(df: pd.DataFrame):
    try:
        return pd.Timestamp(df.index[-1])
    except Exception:
        return None


def _bar_age_min_of(ts) -> float | None:
    if ts is None:
        return None
    try:
        now = pd.Timestamp.now(tz=ts.tz) if getattr(ts, "tz", None) else pd.Timestamp.utcnow()
        return (now - ts).total_seconds() / 60.0
    except Exception:
        return None


def _cache_put(key: tuple, df: pd.DataFrame) -> None:
    """Store keyed on the newest bar timestamp. A frame whose newest bar is
    older than the cached one does NOT overwrite it (no going backward, no
    re-stamping stale as fresh)."""
    if df is None or not len(df):
        return
    ts = _newest_bar_ts(df)
    if ts is None:
        return
    prev = _BAR_CACHE.get(key)
    if prev is not None and prev[0] is not None and ts < prev[0]:
        return
    _BAR_CACHE[key] = (ts, df)


def _annotate(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """R8: stamp a frame with its newest-bar age (minutes) and source via
    pandas .attrs, so callers (the regime engine) can MARK a stale/degraded
    feed instead of consuming a ~6h-old frame as if it were live. Non-invasive:
    the return type stays a DataFrame; only .attrs gains age_min/source."""
    try:
        df.attrs["source"] = source
        df.attrs["age_min"] = _bar_age_min_of(_newest_bar_ts(df))
    except Exception:
        pass
    return df


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    return df


def _period_days(period: str) -> int | None:
    try:
        if period.endswith("d"):
            return int(period[:-1])
    except ValueError:
        pass
    return None


def _aggregate_30m(df_15m: pd.DataFrame) -> pd.DataFrame:
    out = df_15m.resample("30min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    return out.dropna(subset=["Close"])


def _yf_intraday(ticker: str, interval: str, period: str) -> pd.DataFrame | None:
    global _yf_skip_until
    if time.time() < _yf_skip_until:      # circuit breaker: Yahoo is throttling
        return None
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         auto_adjust=False, progress=False, prepost=False)
        if df.empty:
            _yf_skip_until = time.time() + _YF_COOLDOWN
            return None
        out = _flatten(df)
        out.attrs["source"] = "yfinance"
        return out
    except Exception:
        _yf_skip_until = time.time() + _YF_COOLDOWN
        return None


def _fetch_intraday_live(ticker: str, interval: str, period: str) -> pd.DataFrame | None:
    """Tradier when the request fits its history window, else/fallback yfinance."""
    days = _period_days(period)
    client = get_client()
    if client.available() and days is not None and days <= TRADIER_MAX_DAYS:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 09:30")
        if interval in ("1m", "5m", "15m"):
            df = client.timesales(ticker, interval.replace("m", "min"), start=start)
            if df is not None and len(df):
                df.attrs["source"] = "tradier"
                return df
        elif interval == "30m":
            df15 = client.timesales(ticker, "15min", start=start)
            if df15 is not None and len(df15):
                agg = _aggregate_30m(df15)
                agg.attrs["source"] = "tradier"
                return agg
    return _yf_intraday(ticker, interval, period)


def fetch_intraday(ticker: str, interval: str = "5m", period: str = "2d") -> pd.DataFrame | None:
    """Recent intraday bars including today's session. Tradier -> yfinance ->
    last-good cache (so a transient dual-source miss doesn't drop the ticker)."""
    df = _fetch_intraday_live(ticker, interval, period)
    key = (ticker, interval)
    if df is not None and len(df):
        _cache_put(key, df)
        return _annotate(df, df.attrs.get("source", "live"))  # R8: age + source
    cached = _BAR_CACHE.get(key)
    if cached is not None:
        age = _bar_age_min_of(cached[0])
        if age is not None and age <= _fresh_cfg().get("cache_max_serve_min", 360):
            return _annotate(cached[1], "cache")   # last-good; still stamped stale-age
    return None


def _yf_bulk(tickers: list[str], interval: str, period: str) -> dict[str, pd.DataFrame]:
    """One bulk yfinance request, gated by the shared circuit breaker. Returns
    {} (and trips the breaker) when Yahoo throttles, so the caller falls back to
    the per-ticker Tradier path instead of dropping every ticker at once."""
    global _yf_skip_until
    if time.time() < _yf_skip_until:
        return {}
    try:
        raw = yf.download(" ".join(tickers), period=period, interval=interval,
                          auto_adjust=False, progress=False, group_by="ticker", prepost=False)
    except Exception:
        _yf_skip_until = time.time() + _YF_COOLDOWN
        return {}
    out: dict[str, pd.DataFrame] = {}
    multi = isinstance(raw.columns, pd.MultiIndex)
    for t in tickers:
        try:
            df = raw[t] if multi else raw
            df = df.dropna(subset=["Close"])
            if not df.empty:
                out[t] = _flatten(df)
        except KeyError:
            continue
    if not out:                      # bulk returned nothing -> Yahoo is throttling
        _yf_skip_until = time.time() + _YF_COOLDOWN
    return out


def _log_freshness_recovery(ticker, stale_age_min, recovered_age_min):
    """Countable record that a stale bulk frame was recovered via Tradier backfill
    (reason stale_bar_recovered) — distinct from a decision-guard block
    (stale_bar). Same log so both are countable in one place."""
    try:
        p = ROOT / "logs" / "observation" / "blocked_stale_bars.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                "ticker": ticker, "reason": "stale_bar_recovered",
                                "stale_age_min": stale_age_min,
                                "recovered_age_min": recovered_age_min}) + "\n")
    except Exception:
        pass


def batch_intraday(tickers: list[str], interval: str = "5m",
                   period: str = "15d") -> dict[str, pd.DataFrame]:
    """Resilient batch intraday: bulk-yfinance fast path, then per-ticker Tradier
    (-> yfinance -> last-good cache) backfill for any miss. Replaces the old rogue
    bulk yf.download in stock_signals that dropped the WHOLE watchlist whenever
    Yahoo throttled a single bulk call (the 2026-06-17/18/24 burst failures)."""
    out = _yf_bulk(tickers, interval, period)
    for t, df in out.items():                 # refresh last-good (bar-time keyed)
        _cache_put((t, interval), df)
    for t in tickers:                         # backfill true misses resiliently
        if t not in out:
            df = fetch_intraday(t, interval, period)
            if df is not None and len(df):
                out[t] = df
    # mark 1: a STALE bulk 5m frame is ALSO a miss — route it to the per-ticker
    # Tradier backfill BEFORE the decision guard blocks. Recovery (a fresher bar
    # came back) and still-stale are logged separately, both countable.
    if interval == "5m":
        trig = _fresh_cfg().get("backfill_stale_trigger_min", 3)
        for t in list(out):
            age = _bar_age_min_of(_newest_bar_ts(out[t]))
            if age is None or age <= trig:
                continue
            bf = fetch_intraday(t, interval, period)          # Tradier -> yf -> cache
            bf_age = _bar_age_min_of(_newest_bar_ts(bf)) if (bf is not None and len(bf)) else None
            if bf_age is not None and bf_age < age:
                _log_freshness_recovery(t, round(age, 2), round(bf_age, 2))
                out[t] = bf                                   # recovered a fresher bar
    return out


def fetch_quotes(symbols: list[str]) -> dict[str, dict] | None:
    """Batched real-time quotes (one Tradier call). Falls back to yfinance
    last closes. Each quote dict includes `delayed` (True for indices —
    15-min delayed on Tradier even in production)."""
    client = get_client()
    if client.available():
        quotes = client.quotes(symbols)
        if quotes:
            return quotes
    out = {}
    for s in symbols:
        df = _yf_intraday(s, "5m", "1d")
        if df is not None:
            out[s] = {"last": float(df["Close"].iloc[-1]), "delayed": True,
                      "source": "yfinance"}
    return out or None


def fetch_daily(ticker: str, period: str = "60d") -> pd.DataFrame | None:
    try:
        df = yf.download(ticker, period=period, interval="1d",
                         auto_adjust=False, progress=False)
        return _flatten(df) if not df.empty else None
    except Exception:
        return None


def fetch_futures_quote(symbol: str) -> dict | None:
    """Futures context (ES=F, NQ=F) — yfinance only; Tradier market data
    does not carry futures."""
    daily = fetch_daily(symbol, "5d")
    if daily is None or len(daily) < 2:
        return None
    return {
        "last": float(daily["Close"].iloc[-1]),
        "prior_close": float(daily["Close"].iloc[-2]),
        "gap_pct": float(daily["Close"].iloc[-1] / daily["Close"].iloc[-2] - 1) * 100,
    }
