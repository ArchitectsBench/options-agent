"""Live market regime engine (Track B step 6). Deterministic — no LLM.

Builds a regime object each cycle from live feeds:
  - SPY/QQQ vs session VWAP (position + VWAP slope) on 5m bars
  - EMA 9/21/50 stack alignment on 5m and 15m
  - VIX level + 1-hour change
  - ADX(14) on SPY 15m (trend vs chop)
  - /ES /NQ daily gap context
Sources that fail land in `missing` (degraded mode, never a crash).
GEX / TICK / put-call have no affordable real feed; each is wired as a
clearly-labeled estimate (2026-07-01):
  - GEX: chain-derived estimate (options_chain.gex_estimate)
  - tick_proxy: breadth snapshot over a ~50-name basket (src/data/breadth.py)
  - put_call_proxy: put/call volume over the SPY+QQQ chains already fetched
    for GEX (options_chain.put_call_proxy)
Each counts as missing only when its computation fails.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd

from src.data.feeds import fetch_futures_quote, fetch_intraday
from src.indicators.core import ema, session_vwap
from src.indicators.regime import adx

# 2026-07-01: empty — TICK and put_call now have labeled proxies (see module
# docstring), so nothing is permanently missing-by-design anymore. Kept (as a
# name) because pipeline.py imports it to filter known-missing feeds out of
# degradation alerts; with proxies wired, a missing TICK/put_call IS a real
# degradation and should alert.
UNAVAILABLE_FEEDS: list[str] = []

# R8: an OPEN-session feed whose newest bar is older than this (minutes) is
# marked stale in the regime object (analogous to the tick_proxy/put_call_proxy
# provisional labels) so the LLM / Claude context can't consume a ~6h-stale
# frame — VIX via yfinance going stale is the realistic daily case — as if live.
REGIME_STALE_FLOOR_MIN = 20


def _today_bars(df_5m: pd.DataFrame) -> pd.DataFrame:
    last_session = pd.DatetimeIndex(df_5m.index).normalize()[-1]
    return df_5m[pd.DatetimeIndex(df_5m.index).normalize() == last_session]


def _resample_15m(df_5m: pd.DataFrame) -> pd.DataFrame:
    out = df_5m.resample("15min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return out.dropna(subset=["Close"])


def _ema_stack(close: pd.Series) -> str:
    e9, e21, e50 = (ema(close, p).iloc[-1] for p in (9, 21, 50))
    if e9 > e21 > e50:
        return "bullish"
    if e9 < e21 < e50:
        return "bearish"
    return "mixed"


def _index_snapshot(ticker: str, missing: list) -> dict | None:
    df = fetch_intraday(ticker, "5m", "5d")
    if df is None or len(df) < 60:
        missing.append(f"{ticker}_5m")
        return None
    today = _today_bars(df)
    if len(today) < 3:
        missing.append(f"{ticker}_today")
        return None
    vwap = session_vwap(today)
    above = float(today["Close"].iloc[-1]) > float(vwap.iloc[-1])
    slope = float(vwap.iloc[-1] - vwap.iloc[-6]) if len(vwap) >= 6 else 0.0
    df15 = _resample_15m(df)
    return {
        "last": round(float(today["Close"].iloc[-1]), 2),
        "vwap": round(float(vwap.iloc[-1]), 2),
        "vs_vwap": "above" if above else "below",
        "vwap_slope": "rising" if slope > 0 else "falling",
        "ema_stack_5m": _ema_stack(df["Close"]),
        "ema_stack_15m": _ema_stack(df15["Close"]),
        "age_min": df.attrs.get("age_min"),   # R8: newest-bar age + source
        "source": df.attrs.get("source"),     #      (from feeds._annotate)
        "_df15": df15,  # internal: reused for ADX
    }


def _feed_stale(age_min, sess) -> bool | None:
    """R8: a feed is 'stale' only when its newest bar is beyond the floor DURING
    an open session (mirrors freshness.freshness_verdict's stale_bar semantics —
    an old bar outside the session is expected, not a defect). None = age
    unknown (feed absent)."""
    if age_min is None:
        return None
    return bool(age_min > REGIME_STALE_FLOOR_MIN and sess == "open")


def tick_proxy_from_breadth(breadth: dict | None) -> dict | None:
    """NYSE TICK proxy mapped from the breadth snapshot (labeled — there is
    no real $TICK feed). `extreme` inherits breadth's existing provisional
    +/-60 net-advancers threshold (src/data/breadth.py EXTREME_THRESHOLD).
    None when breadth is unavailable — caller marks the feed missing."""
    if breadth is None or breadth.get("net_advancers_pct") is None:
        return None
    side = breadth.get("extreme_side")
    return {
        "net_adv_pct": breadth["net_advancers_pct"],
        "extreme": {"bullish": "positive", "bearish": "negative"}.get(side),
        "source": "breadth_proxy",
    }


def build_regime() -> dict:
    missing: list[str] = list(UNAVAILABLE_FEEDS)
    # R8: per-feed staleness marks. Session state once per cycle (half-day aware
    # via freshness); a feed is only flagged stale when its bar is old DURING an
    # open session.
    from src.data import freshness as _fresh
    _sess = _fresh.current_session_state()
    feed_staleness: dict[str, dict] = {}

    def _mark(name, age_min, source):
        feed_staleness[name] = {"age_min": age_min, "source": source,
                                "stale": _feed_stale(age_min, _sess)}

    spy = _index_snapshot("SPY", missing)
    qqq = _index_snapshot("QQQ", missing)
    if spy is not None:
        _mark("SPY_5m", spy.get("age_min"), spy.get("source"))
    if qqq is not None:
        _mark("QQQ_5m", qqq.get("age_min"), qqq.get("source"))

    vix_level = vix_1h_change = None
    vix_note = None
    vix_df = fetch_intraday("^VIX", "5m", "2d")
    if vix_df is not None and len(vix_df) >= 13:
        vix_level = round(float(vix_df["Close"].iloc[-1]), 2)
        vix_1h_change = round(float(vix_df["Close"].iloc[-1] - vix_df["Close"].iloc[-13]), 2)
        _mark("VIX_5m", vix_df.attrs.get("age_min"), vix_df.attrs.get("source"))
        from src.data.tradier import get_client
        if get_client().available():
            vix_note = "15-min delayed (Tradier index data)"
    else:
        missing.append("VIX_5m")

    adx_15m = None
    if spy is not None and len(spy["_df15"]) >= 30:
        adx_15m = round(float(adx(spy["_df15"]).iloc[-1]), 1)

    gex = None
    if spy is not None:
        from src.data.options_chain import chain_snapshot
        try:
            snap = chain_snapshot("SPY", spy["last"], bucket="2-7dte")
            gex = snap["gex"] if snap else None
        except Exception:
            gex = None
    if gex is None:
        missing.append("GEX")
    gex_regime = _gex_regime(gex)

    # IV term-structure outlook (Track A 2026-06-16 validated: term slope predicts
    # next-day |move| at corr ~-0.42 cross-ETF — the strongest regime signal found;
    # backwardation -> ~2x bigger moves). Composite with 5d IV-momentum.
    term = None
    if spy is not None:
        from src.data.options_chain import term_structure
        try:
            term = term_structure("SPY", spy["last"])
        except Exception:
            term = None
    if term is None:
        missing.append("term_structure")
    iv_mom = _iv_momentum("SPY")
    vol_outlook = _vol_outlook(term, iv_mom)
    richness = _richness(term)   # K2 feature (2026-07-16): logged context only,
                                 # consumed by NOTHING — observation accrual for
                                 # a future pre-registered decision

    # breadth_daily — cross-sectional daily context (item 2, 2026-07-19 batch;
    # see docs/external_edge_map.md + analysis/discovery/IDEAS.md F3). CONTEXT
    # ONLY: consumed by nothing downstream except regime_tags (paper_trader.py
    # item 1) for descriptive weekly-review slicing — not a gate/rule input.
    breadth_daily = _breadth_daily_snapshot()

    # breadth proxy — stand-in for NYSE TICK (no live $TICK feed); estimated
    breadth = None
    try:
        from src.data.breadth import compute_breadth, log_breadth
        breadth = compute_breadth()
        if breadth:
            log_breadth(breadth)
    except Exception:
        breadth = None
    if breadth is None:
        missing.append("breadth")

    # tick_proxy — labeled NYSE TICK stand-in derived from the SAME breadth
    # reading (no extra fetch). Missing only when breadth is unavailable.
    tick = tick_proxy_from_breadth(breadth)
    if tick is None:
        missing.append("TICK")

    # put_call_proxy — labeled CBOE equity P/C stand-in from SPY+QQQ chain
    # volume; reuses the SPY chain fetched for GEX above (per-cycle stash in
    # options_chain), plus one QQQ chain per cycle. Missing (null value, no
    # crash) when chains are unavailable.
    put_call = None
    try:
        from src.data.options_chain import put_call_proxy
        put_call = put_call_proxy()
    except Exception:
        put_call = None
    if put_call is None or put_call.get("value") is None:
        missing.append("put_call")

    es = fetch_futures_quote("ES=F")
    nq = fetch_futures_quote("NQ=F")
    if es is None:
        missing.append("ES=F")
    if nq is None:
        missing.append("NQ=F")

    # ── deterministic composite ──
    votes = 0
    for snap in (spy, qqq):
        if snap:
            votes += 1 if snap["vs_vwap"] == "above" else -1
            votes += 1 if snap["ema_stack_5m"] == "bullish" else (-1 if snap["ema_stack_5m"] == "bearish" else 0)
    if vix_1h_change is not None:
        votes += -1 if vix_1h_change > 0.5 else (1 if vix_1h_change < -0.5 else 0)
    bias = "bullish" if votes >= 2 else ("bearish" if votes <= -2 else "neutral")

    if adx_15m is None:
        trend = "unknown"
    elif adx_15m >= 30:
        trend = "strong"
    elif adx_15m >= 20:
        trend = "mild"
    else:
        trend = "chop"

    vol_regime = "unknown"
    if vix_level is not None:
        vol_regime = "calm" if vix_level < 15 else ("elevated" if vix_level < 22 else "stressed")

    for snap in (spy, qqq):
        if snap:
            snap.pop("_df15", None)

    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "bias": bias,
        "trend": trend,
        "vol_regime": vol_regime,
        "vix": vix_level,
        "vix_note": vix_note,
        "vix_1h_change": vix_1h_change,
        "gex": gex,
        "gex_regime": gex_regime,
        "term_structure": term,
        "iv_mom_5d": iv_mom,
        "vol_outlook": vol_outlook,
        "richness": richness,
        "breadth_daily": breadth_daily,
        "breadth": breadth,
        "tick_proxy": tick,
        "put_call_proxy": put_call,
        # R8: per-feed newest-bar age/source + a stale flag (open-session only),
        # so downstream never treats a ~6h-stale SPY/QQQ/VIX frame as live.
        "feed_staleness": feed_staleness,
        "stale_feeds": [k for k, v in feed_staleness.items() if v.get("stale")],
        "adx_15m_spy": adx_15m,
        "spy": spy,
        "qqq": qqq,
        "es_gap_pct": None if es is None else round(es["gap_pct"], 2),
        "nq_gap_pct": None if nq is None else round(nq["gap_pct"], 2),
        "missing": missing,
        "degraded": len(missing) > len(UNAVAILABLE_FEEDS),
    }


def _gex_regime(gex: dict | None) -> str | None:
    """Validated (Track A 2026-06-16): positive dealer gamma -> next-day move
    ~25-30% smaller (suppressive/mean-revert); negative -> amplified (trend/vol).
    Live classifier = spot vs the zero-gamma flip, falling back to net sign.
    None when GEX is unavailable (degraded mode)."""
    if not gex:
        return None
    zg, spot = gex.get("zero_gamma_level"), gex.get("spot")
    if zg is not None and spot is not None:
        if gex["sign"] == "positive" and spot > zg:
            return "suppressive"
        if gex["sign"] == "negative" or spot < zg:
            return "amplifying"
        return "neutral"
    return "suppressive" if gex["sign"] == "positive" else "amplifying"


_GEX_REGIME_NOTE = {
    "suppressive": "positive-gamma → expect range/mean-reversion (favor premium/fade)",
    "amplifying": "negative-gamma → expect trend/volatility (favor directional/breakout)",
    "neutral": "near zero-gamma flip",
}


def _richness(term: dict | None) -> dict | None:
    """K2 front-richness feature (2026-07-16): SPY iv30 minus 10d realized
    vol, with a trailing percentile from the daily IV-snapshot history and
    daily bars. Logged context ONLY — no rule consumes it (the DC factor
    study showed the vol edge lives in this quantity's top quintile; the
    live labels will show whether the same is true for funnel outcomes)."""
    try:
        iv30 = (term or {}).get("iv30")
        if iv30 is None:
            return None
        import pandas as pd
        import numpy as np
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        closes = pd.read_parquet(root / "data" / "historical" / "SPY.parquet")["Close"]
        rv = (np.log(closes).diff().rolling(10).std() * np.sqrt(252)).dropna()
        rv10 = float(rv.iloc[-1])
        spread = iv30 - rv10
        # trailing spread history from the daily IV snapshots + daily closes
        ivh = pd.read_csv(root / "data" / "iv_history" / "SPY.csv")
        ivh["date"] = pd.to_datetime(ivh["date"], errors="coerce")
        ivh = ivh.dropna().set_index("date")["atm_iv"].astype(float).rename("iv")
        rv_hist = rv.copy()
        rv_hist.index = pd.to_datetime(rv_hist.index).tz_localize(None).normalize()
        joined = pd.concat([ivh, rv_hist.rename("rv")], axis=1, join="inner").tail(252)
        if len(joined) < 40:
            pct = None
        else:
            hist_spread = (joined["iv"] - joined["rv"]).values
            pct = round(float((hist_spread < spread).mean()), 2)
        return {"iv30": round(iv30, 4), "rv10": round(rv10, 4),
                "spread": round(spread, 4), "pctile": pct,
                "note": "K2 feature — context only, consumed by nothing"}
    except Exception:
        return None


def _breadth_staleness(last_date, today) -> tuple:
    """Business-day age of the latest breadth row, and whether that counts as
    stale. Pure function so it is testable without touching the production
    parquet (CLAUDE.md destructive-probe rule).

    Why business days and why >= 2 (verified against the real incident dates,
    autopilot mid_session 2026-10-27): the previous calendar-day `> 4` test is
    what HID the 10-23/10-26 stall — the data was 1 and 2 CALENDAR days old
    then, so the snapshot read friendly while `breadth_daily_update.py` had
    stopped appending. Business days separate the two cases that calendar days
    cannot:
      Thu 10-22 -> Mon 10-26 = 2 busdays  -> STALE (the incident)
      Fri 10-23 -> Mon 10-26 = 1 busday   -> FRESH (an ordinary Monday)
    Calendar `>4` misses the first; calendar `>1` false-alarms on the second.
    Known and accepted false positive: a Monday market holiday reads stale.
    That is acceptable for a context-only field that gates nothing.

    Returns (stale: bool, busdays: int). Never raises — an unparseable date
    degrades to (False, -1) so a bad row cannot take the regime object down.
    """
    try:
        import numpy as np
        last = pd.to_datetime(last_date).date()
        busdays = int(np.busday_count(last, today))
    except Exception:
        return (False, -1)
    return (busdays >= 2, busdays)


def _breadth_daily_snapshot() -> dict:
    """Item 2 of the 2026-07-19 observation-instrumentation batch (see
    docs/external_edge_map.md, analysis/discovery/IDEAS.md F3). Latest row of
    data/breadth/daily.parquet — a REGIME CONTEXT candidate per F3 ("healthy"
    pct_above_20dma>50% -> next-day SPY Sharpe 1.08 vs 0.33 in that screen),
    NOT a validated edge and NOT a gate. CONTEXT ONLY, logged for weekly
    review / regime_tags. The file is kept fresh by
    scripts/breadth_daily_update.py (launchd 20:00 PT weekdays, appends only
    — full rebuild is scripts/build_breadth.py, run on demand).
    Guard: never crashes. Missing/stale/empty file degrades to an
    'available': False dict carrying an explanatory note rather than a bare
    None, since JSON can't attach a note to null."""
    try:
        import pandas as pd
        p = Path(__file__).resolve().parents[2] / "data" / "breadth" / "daily.parquet"
        if not p.exists():
            return {"available": False, "stale": True, "stale_busdays": None,
                    "note": "data/breadth/daily.parquet absent — breadth_daily_update.py has not run yet"}
        df = pd.read_parquet(p)
        if df.empty:
            return {"available": False, "stale": True, "stale_busdays": None,
                    "note": "daily.parquet has zero rows"}
        last = df.sort_values("date").iloc[-1]
        note = "F3 regime-context candidate — context only, consumed by nothing but regime_tags"
        stale, busdays = _breadth_staleness(last["date"], datetime.now().date())
        if stale:
            note = (f"STALE: latest row is {busdays} business day(s) old ({last['date']}) — "
                    f"check breadth_daily_update.py (a Monday market holiday reads stale; "
                    f"context-only field, gates nothing)")
        pct_above_20dma = last.get("pct_above_20dma")
        healthy = bool(pct_above_20dma > 0.5) if pd.notna(pct_above_20dma) else None
        return {
            "available": True,
            "date": str(last["date"]),
            "pct_adv": None if pd.isna(last.get("pct_adv")) else round(float(last["pct_adv"]), 4),
            "pct_above_20dma": None if pd.isna(pct_above_20dma) else round(float(pct_above_20dma), 4),
            "breadth_thrust_10d": None if pd.isna(last.get("breadth_thrust_10d"))
                                   else round(float(last["breadth_thrust_10d"]), 4),
            "healthy": healthy,
            "stale": stale,
            "stale_busdays": busdays,
            "note": note,
        }
    except Exception as exc:
        return {"available": False, "stale": True, "stale_busdays": None,
                "note": f"breadth_daily read error: {exc}"}


def _iv_momentum(ticker: str, window: int = 5) -> float | None:
    """5-session ATM-IV change from the daily IV-snapshot history (None if short)."""
    import pandas as pd
    p = Path(__file__).resolve().parents[2] / "data" / "iv_history" / f"{ticker}.csv"
    if not p.exists():
        return None
    s = pd.read_csv(p)["atm_iv"].dropna()
    if len(s) < window + 1:
        return None
    return round(float(s.iloc[-1] - s.iloc[-1 - window]), 4)


def _vol_outlook(term: dict | None, iv_mom: float | None) -> str | None:
    """Forward move-magnitude regime from term slope (primary) + IV momentum.
    active = expect bigger moves (favor directional); calm = smaller (favor
    premium/range). None when term structure is unavailable."""
    if term is None:
        return None
    slope = term["slope"]
    votes = 0
    if slope < 0:           # backwardation -> active
        votes -= 1
    elif slope > 0.01:      # contango -> calm
        votes += 1
    if iv_mom is not None:
        if iv_mom > 0.005:  # rising IV -> active
            votes -= 1
        elif iv_mom < -0.005:
            votes += 1
    return "active" if votes <= -1 else ("calm" if votes >= 1 else "neutral")


_VOL_OUTLOOK_NOTE = {
    "calm": "contango/falling IV → expect smaller moves (favor premium/range)",
    "active": "backwardation/rising IV → expect bigger moves (favor directional)",
    "neutral": "flat term structure",
}


def _gex_line(r: dict) -> str:
    g = r.get("gex")
    if not g:
        line = "  GEX: unavailable"
    else:
        line = f"  GEX: {g['sign']} net {g['net_gex']:,} | zero-gamma {g['zero_gamma_level']} [{g['label']}]"
        gr = r.get("gex_regime")
        if gr:
            line += f"\n  GEX regime: {gr.upper()} — {_GEX_REGIME_NOTE.get(gr, '')}"
    ts = r.get("term_structure")
    vo = r.get("vol_outlook")
    if ts is not None and vo:
        line += (f"\n  Term structure: iv30={ts['iv30']} iv60={ts['iv60']} slope={ts['slope']:+.3f}"
                 f" | IV mom5d={r.get('iv_mom_5d')} → vol_outlook {vo.upper()}"
                 f" ({_VOL_OUTLOOK_NOTE.get(vo, '')})")
    if r.get("vix_note"):
        line += f" | VIX: {r['vix_note']}"
    return line


def format_regime(r: dict) -> str:
    def idx_line(name, s):
        if s is None:
            return f"  {name}: UNAVAILABLE"
        return (f"  {name}: {s['last']} ({s['vs_vwap']} VWAP {s['vwap']}, {s['vwap_slope']}) "
                f"| EMA stack 5m={s['ema_stack_5m']} 15m={s['ema_stack_15m']}")

    lines = [
        f"── REGIME @ {r['ts']} ──",
        f"  Bias: {r['bias'].upper()} | Trend: {r['trend']} (ADX15m {r['adx_15m_spy']}) | "
        f"Vol: {r['vol_regime']} (VIX {r['vix']}, 1h {r['vix_1h_change']:+.2f})"
        if r["vix_1h_change"] is not None else
        f"  Bias: {r['bias'].upper()} | Trend: {r['trend']} | Vol: {r['vol_regime']}",
        idx_line("SPY", r["spy"]),
        idx_line("QQQ", r["qqq"]),
        _gex_line(r),
        (f"  Breadth (daily, {r['breadth_daily']['date']}): pct_above_20dma="
         f"{r['breadth_daily']['pct_above_20dma']} healthy={r['breadth_daily']['healthy']}"
         "  (context only)") if r.get("breadth_daily", {}).get("available")
        else f"  Breadth (daily): unavailable — {r.get('breadth_daily', {}).get('note', 'n/a')}",
        (f"  Breadth: {r['breadth']['net_advancers_pct']:+.0f}% net advancers"
         + (f"  [{r['breadth']['extreme_side'].upper()} EXTREME]" if r['breadth'].get('extreme') else "")
         + "  (proxy/estimated)") if r.get("breadth") else "  Breadth: unavailable",
        (f"  Put/Call: {r['put_call_proxy']['value']} (chain proxy SPY+QQQ,"
         f" n={r['put_call_proxy']['n_contracts']})"
         + (f"  [{r['put_call_proxy']['extreme'].upper()}]" if r['put_call_proxy'].get('extreme') else ""))
        if r.get("put_call_proxy") and r["put_call_proxy"].get("value") is not None
        else "  Put/Call: unavailable",
        f"  Futures gap: ES {r['es_gap_pct']}% | NQ {r['nq_gap_pct']}%",
        f"  Stale feeds: {', '.join(r.get('stale_feeds') or []) or 'none'}"
        + ("  [STALE — open-session bars beyond floor]" if r.get("stale_feeds") else ""),
        f"  Missing feeds: {', '.join(r['missing']) or 'none'}" + ("  [DEGRADED]" if r["degraded"] else ""),
    ]
    return "\n".join(lines)
