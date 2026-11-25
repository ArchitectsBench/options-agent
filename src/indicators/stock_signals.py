"""Per-ticker live signal engine (Track B step 7). Deterministic, causal —
same discipline as Track A: every value uses only bars up to now.

OBSERVATION-ONLY: no strategy here is Stage 6-validated. Each signal carries
a `lineage` tag naming the Track A family it derives from and that family's
status, so observation-mode logs stay connected to the research record.

Note on the last bar: intraday the most recent bar may still be forming;
signals are computed on data as fetched and stamped with the bar time — fine
for observation logging, NOT a fill-quality claim.
"""

import numpy as np
import pandas as pd

from src.data import feeds
from src.indicators.core import atr, bar_of_session, ema, rolling_percentile, session_id, session_vwap, sma

# gen-1 squeeze parameterization — our best-studied signal (unvalidated)
SQUEEZE = {"bb_period": 36, "width_pct": 0.31, "mom_period": 23, "rank_window": 126}

LINEAGE = {
    "squeeze": "squeeze_proxy (Track A gen-1/gen-3, UNVALIDATED — regime-dependent)",
    "ema_stack": "momentum_reentry (Track A gen-1, RETIRED — no standalone edge)",
    "vwap": "vwap_trend/vwap_reversion (Track A gen-1, RETIRED)",
    "opening_range": "orb (Track A gen-2, UNVALIDATED — edge below cost floor)",
    "macd": "not separately backtested (CLAUDE.md confirmatory indicator)",
    "rs_vs_spy": "not separately backtested (CLAUDE.md confirmatory indicator)",
    "volume": "not separately backtested (CLAUDE.md confirmatory indicator)",
    "iv_rank": "chain-derived ATM IV daily snapshots (UNEVALUABLE below 60 "
               "sessions; PROVISIONAL 60-150; full above 150 — tier carried "
               "per ticker in iv_rank.tier)",
}


def batch_intraday(tickers: list[str], interval: str, period: str) -> dict[str, pd.DataFrame]:
    """Resilient batch intraday — delegates to the consolidated feeds layer
    (bulk-yfinance fast path -> per-ticker Tradier/last-good-cache backfill) so a
    single throttled Yahoo bulk call no longer drops the whole watchlist."""
    return feeds.batch_intraday(tickers, interval, period)


def _resample(df_5m: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df_5m.resample(rule).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return out.dropna(subset=["Close"])


def _ema_stack(close: pd.Series) -> str:
    if len(close) < 25:
        return "insufficient_data"
    e9, e21 = ema(close, 9).iloc[-1], ema(close, 21).iloc[-1]
    return "bullish" if e9 > e21 else "bearish"


def _macd_state(close: pd.Series) -> dict | None:
    if len(close) < 40:
        return None
    macd = ema(close, 12) - ema(close, 26)
    hist = macd - ema(macd, 9)
    return {
        "sign": "positive" if hist.iloc[-1] > 0 else "negative",
        "direction": "rising" if hist.iloc[-1] > hist.iloc[-2] else "falling",
    }


def _squeeze_state(close: pd.Series) -> str:
    p = SQUEEZE
    need = p["bb_period"] + p["rank_window"]
    if len(close) < need:
        return "insufficient_data"
    mid = sma(close, p["bb_period"])
    width = 4 * close.rolling(p["bb_period"]).std() / mid
    rank = rolling_percentile(width, p["rank_window"])
    squeezed = rank <= p["width_pct"]
    if squeezed.iloc[-1]:
        return "on"
    recently = squeezed.iloc[-4:-1].any()
    expanding = width.iloc[-1] > width.iloc[-2]
    if recently and expanding:
        mom = close.iloc[-1] > ema(close, p["mom_period"]).iloc[-1]
        return "fired_long" if mom else "fired_short"
    return "off"


def _drop_forming_bar(df: pd.DataFrame, interval_min: int) -> pd.DataFrame:
    """Volume of the in-progress bar is partial (often ~0 seconds after the
    bar opens) — root cause of the volume_ratio=0.0 artifact. Use the last
    COMPLETED bar for volume comparisons."""
    if len(df) < 2:
        return df
    last_start = df.index[-1]
    now = pd.Timestamp.now(tz=getattr(last_start, "tz", None))
    if now < last_start + pd.Timedelta(minutes=interval_min):
        return df.iloc[:-1]
    return df


def compute_ticker_signals(df_5m: pd.DataFrame, df_30m: pd.DataFrame | None,
                           spy_5m: pd.DataFrame | None, ticker: str | None = None) -> dict:
    close5 = df_5m["Close"]
    df_15m = _resample(df_5m, "15min")

    sid = session_id(df_5m.index)
    today = df_5m[sid == sid[-1]]
    vwap = session_vwap(today)
    a = atr(df_5m, 14)
    vwap_dist_atr = float((today["Close"].iloc[-1] - vwap.iloc[-1]) / a.iloc[-1]) if len(today) >= 2 and a.iloc[-1] > 0 else None

    vol_ratio = None
    df_vol = _drop_forming_bar(df_5m, 5)
    vol_avg = df_vol["Volume"].rolling(20).mean().iloc[-1]
    if vol_avg and vol_avg > 0:
        vol_ratio = float(df_vol["Volume"].iloc[-1] / vol_avg)

    rs = None
    if spy_5m is not None and len(close5) >= 7 and len(spy_5m) >= 7:
        rs = float((close5.iloc[-1] / close5.iloc[-7] - 1) - (spy_5m["Close"].iloc[-1] / spy_5m["Close"].iloc[-7] - 1)) * 100

    or_state = None
    bos = bar_of_session(df_5m.index)
    today_bos = bos[sid == sid[-1]]
    if len(today) >= 7:  # opening range = first 30 min = 6 x 5m bars
        or_bars = today[today_bos < 6]
        or_high, or_low = float(or_bars["High"].max()), float(or_bars["Low"].min())
        last = float(today["Close"].iloc[-1])
        or_state = {
            "high": round(or_high, 2), "low": round(or_low, 2),
            "position": "above" if last > or_high else ("below" if last < or_low else "inside"),
        }

    from src.data.options_chain import iv_rank_status
    iv = iv_rank_status(ticker) if ticker else {"rank": None, "sessions": 0, "tier": "unevaluable"}
    # iv_rank_asof (completeness-audit gap #1, docs/post_mortem_program_state.md
    # §8 2026-07-25): iv_rank_status() builds its rank from the ticker's daily
    # atm_iv CSV but returns no snapshot date/asof — the persisted history has
    # no timestamp column today, so there is nothing real to surface here. Kept
    # explicitly None (not fabricated) so the gap stays visible in the snapshot
    # rather than silently absent; wire a real asof if/when the IV snapshot
    # store gains a date field.
    iv_rank_asof = None

    return {
        "last": round(float(close5.iloc[-1]), 2),
        "atr": round(float(a.iloc[-1]), 3) if a.iloc[-1] > 0 else None,  # dollars; follow-on drift checks
        "iv_rank": iv,  # tiered; from daily IV snapshots (data-gap fix #2)
        "iv_rank_asof": iv_rank_asof,  # completeness-audit gap #1 — see comment above
        "bar_time": str(df_5m.index[-1]),
        # R8: the 30m frame's own newest-bar time — the freshness gate now takes
        # the OLDER of (5m, 30m) so a ~6h-stale 30m squeeze/EMA frame can't feed
        # confidence as if live. None when no 30m data this cycle.
        "bar_time_30m": str(df_30m.index[-1]) if df_30m is not None and len(df_30m) else None,
        "spy_bar_time": str(spy_5m.index[-1]) if spy_5m is not None and len(spy_5m) else None,  # gap #2: SPY leg's own freshness
        "ema_stack": {
            "5m": _ema_stack(close5),
            "15m": _ema_stack(df_15m["Close"]),
            "30m": _ema_stack(df_30m["Close"]) if df_30m is not None else "unavailable",
        },
        "macd_5m": _macd_state(close5),
        "squeeze": {
            "5m": _squeeze_state(close5),
            "15m": _squeeze_state(df_15m["Close"]),
            "30m": _squeeze_state(df_30m["Close"]) if df_30m is not None else "insufficient_data",
        },
        "vwap_dist_atr": None if vwap_dist_atr is None else round(vwap_dist_atr, 2),
        "volume_ratio": None if vol_ratio is None else round(vol_ratio, 2),
        "rs_vs_spy_30min_pct": None if rs is None else round(rs, 2),
        "opening_range": or_state,
    }


# The 5-minute frames of the most recent scan (2026-09-14). Read by the
# pipeline's bar log (src/engine/bars_log.py) so the cockpit TRADE page charts
# the exact bars these indicators were computed from. Nothing reads it for a
# decision.
LAST_BARS: dict[str, pd.DataFrame] = {}


def scan_watchlist(tickers: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Signals for every ticker. Returns (signals, failed_tickers)."""
    fetch_list = sorted(set(tickers) | {"SPY"})
    d5 = batch_intraday(fetch_list, "5m", "15d")
    LAST_BARS.clear()
    LAST_BARS.update({t: d5[t] for t in tickers if t in d5})
    # 30d (was 60d) keeps the 30m request inside Tradier's ~35-day history window
    # so Tradier can serve it during a Yahoo throttle; ~390 30m bars still gives
    # EMA-50 ample warmup. The forming-bar guard handles partial last bars.
    d30 = batch_intraday(fetch_list, "30m", "30d")
    spy_5m = d5.get("SPY")

    signals, failed = {}, []
    for t in tickers:
        if t not in d5:
            failed.append(t)
            continue
        try:
            signals[t] = compute_ticker_signals(d5[t], d30.get(t), spy_5m, ticker=t)
        except Exception as exc:
            failed.append(t)
            signals[t] = {"error": str(exc)}
    return signals, failed
