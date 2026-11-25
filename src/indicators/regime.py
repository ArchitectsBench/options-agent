"""Daily regime features for gating intraday strategies (gen 3).

Causality contract: the regime label attached to every bar of session N is
computed from data through session N-1's CLOSE only (one-session shift).

Realized vol and ADX are derived from the 1h bars themselves (session
aggregates), so the empirical truncation test in regime_lookahead_test()
genuinely exercises them. VIX is an exogenous daily series; its causality
is enforced by the same one-session shift (and ffill across holidays),
which the truncation test cannot probe — flagged in reports.

Warmup note: rv_pctile needs 20 + 252 prior sessions, so it is NaN (gate
OFF) for roughly the first year of 1h history. ADX(14) needs ~30 sessions.
NaN features always gate OFF — never a default-ON regime.
"""

import numpy as np
import pandas as pd

RV_WINDOW = 20
RV_RANK_WINDOW = 252
ADX_PERIOD = 14


def _session_dates(index: pd.Index) -> pd.DatetimeIndex:
    """tz-naive normalized session date per bar (keys shared with daily data)."""
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def session_daily_ohlc(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Aggregate intraday bars into one OHLC row per session."""
    dates = _session_dates(df_1h.index)
    g = df_1h.groupby(dates)
    return pd.DataFrame(
        {
            "Open": g["Open"].first(),
            "High": g["High"].max(),
            "Low": g["Low"].min(),
            "Close": g["Close"].last(),
        }
    )


def realized_vol_pctile(daily_close: pd.Series) -> pd.Series:
    """20-day realized vol, percentile-ranked over the trailing 252 days."""
    rv = daily_close.pct_change().rolling(RV_WINDOW).std() * np.sqrt(252)
    return rv.rolling(RV_RANK_WINDOW).rank(pct=True)


def adx(daily: pd.DataFrame, period: int = ADX_PERIOD) -> pd.Series:
    """Standard Wilder ADX on daily bars."""
    h, l, c = daily["High"], daily["Low"], daily["Close"]
    up, dn = h.diff(), -l.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def compute_regime_features(df_1h: pd.DataFrame, vix_close: pd.Series) -> pd.DataFrame:
    """Per-session features, each row for session N computed through N-1's
    close (already shifted — safe to read on any bar of session N)."""
    daily = session_daily_ohlc(df_1h)
    feats = pd.DataFrame(
        {
            "rv_pctile": realized_vol_pctile(daily["Close"]),
            "adx": adx(daily),
        },
        index=daily.index,
    ).shift(1)  # session N sees features through N-1

    vix = vix_close.copy()
    vix.index = _session_dates(vix.index)
    # prior session's VIX close; ffill covers calendar mismatches (still prior data)
    feats["vix"] = vix.shift(1).reindex(feats.index).ffill()
    return feats


def attach_regime(df_1h: pd.DataFrame, vix_close: pd.Series) -> pd.DataFrame:
    """Return a copy of df_1h with regime feature columns broadcast per bar."""
    feats = compute_regime_features(df_1h, vix_close)
    dates = _session_dates(df_1h.index)
    out = df_1h.copy()
    for col in feats.columns:
        out[col] = feats[col].reindex(dates).to_numpy()
    return out


def regime_lookahead_test(df_1h: pd.DataFrame, vix_close: pd.Series, cuts=(0.4, 0.55, 0.7, 0.85, 0.95)):
    """Stage 2 truncation test on the regime labels themselves: recompute
    features on truncated 1h history; every previously emitted session row
    must be identical. Returns (passed, detail)."""
    full = compute_regime_features(df_1h, vix_close)
    leaks = []
    n = len(df_1h)
    for frac in cuts:
        trunc = compute_regime_features(df_1h.iloc[: int(n * frac)], vix_close)
        for col in full.columns:
            if not trunc[col].equals(full[col].reindex(trunc.index)):
                leaks.append(f"{col}@{frac}")
    if leaks:
        return False, f"labels changed when future removed: {leaks}"
    return True, f"labels stable across {len(cuts)} truncation points (VIX exogenous: causality by 1-session shift)"
