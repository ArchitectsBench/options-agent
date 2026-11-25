"""Nightly prep for the RVOL-EXEC lane (spec §14). UNVALIDATED — PAPER ONLY.

For D_today the lane needs, per ticker: `atr14`, `avgvol14` (the shadow
scan's `rolling(14).mean().shift(1)` value for D_today, i.e. the mean over the
14 sessions ending D_prev), `baseline14` (mean 09:30 5m-bar volume over the 14
most recent sessions <= D_prev that HAVE a 09:30 bar; `n_baseline` must be
14) and `prev_close`.

Inputs are read-only stores: `data/research_orb/grouped/*.parquet` (daily
OHLCV per ticker, one file per session), `bars5m_fresh/<date>/<T>.parquet`,
`bars5m/<T>.parquet` (ends 2026-07-17), the lane's own cache
`data/lane_rvol/v930_history.parquet`, and — PRODUCTION ONLY, `--allow-api` —
the Polygon 5m aggregates via an injected client. Tests never touch the API:
without `allow_api` the client is never called (asserted).
"""

from __future__ import annotations

import json
import logging
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import src.rvol_exec.paths as paths
from src.rvol_exec.constants import (BASELINE_N, BASELINE_SPAN_DAYS, FILTER_ATR14_MIN, FILTER_AVGVOL14_MIN,
                                     HOLIDAYS_2026, PREP_MIN_BASELINE_FRAC,
                                     PREP_MIN_CANDIDATES, STAMP)
from src.util.atomic import atomic_save

logger = logging.getLogger("rvol_exec")

PREP_COLUMNS = ["ticker", "atr14", "avgvol14", "baseline14", "n_baseline", "prev_close"]


# ── calendar ─────────────────────────────────────────────────────────────────
def is_trading_day(d: date, holidays=HOLIDAYS_2026) -> bool:
    return d.weekday() < 5 and d.isoformat() not in set(holidays)


def previous_trading_day(d: date, holidays=HOLIDAYS_2026) -> date:
    p = d - timedelta(days=1)
    while not is_trading_day(p, holidays):
        p -= timedelta(days=1)
    return p


# ── the shadow scan's formula (verbatim shape) ───────────────────────────────
def shadow_features(hist: pd.DataFrame, today: str) -> pd.DataFrame:
    """EXACT copy of rvol_shadow_scan.py step 2 on a `hist` frame with
    columns [ticker, date, open, high, low, close, volume] that INCLUDES the
    `today` rows: atr14/avgvol14 = rolling(14).mean().shift(1)."""
    h = hist.sort_values(["ticker", "date"]).reset_index(drop=True)
    g = h.groupby("ticker", sort=False)
    prev_close = g["close"].shift(1)
    tr = pd.concat([h["high"] - h["low"], (h["high"] - prev_close).abs(),
                    (h["low"] - prev_close).abs()], axis=1).max(axis=1)
    h["atr14"] = tr.groupby(h["ticker"]).transform(lambda s: s.rolling(14).mean().shift(1))
    h["avgvol14"] = g["volume"].transform(lambda s: s.rolling(14).mean().shift(1))
    return h[h["date"] == today][["ticker", "atr14", "avgvol14"]].reset_index(drop=True)


def load_grouped(grouped_dir: Path, through: str, n_files: int = 16) -> pd.DataFrame:
    """Last `n_files` grouped files dated <= `through` -> one frame."""
    files = [f for f in sorted(Path(grouped_dir).glob("2*.parquet")) if f.stem <= through][-n_files:]
    frames = []
    for f in files:
        d = pd.read_parquet(f)
        d["date"] = f.stem
        frames.append(d)
    if not frames:
        return pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close", "volume"])
    return pd.concat(frames, ignore_index=True)


def daily_features(grouped_dir: Path, d_prev: str, d_today: str) -> pd.DataFrame:
    """atr14/avgvol14 for D_today from the store through D_prev, computed by
    appending a placeholder D_today row per ticker so the shadow formula
    (with its shift(1)) yields the identical value. Also prev_close."""
    hist = load_grouped(grouped_dir, d_prev)
    if hist.empty:
        return pd.DataFrame(columns=["ticker", "atr14", "avgvol14", "prev_close"])
    last = hist[hist["date"] == hist["date"].max()][["ticker", "close"]].rename(columns={"close": "prev_close"})
    ph = last.copy()
    ph["date"] = d_today
    for col in ("open", "high", "low", "close"):
        ph[col] = ph["prev_close"]
    ph["volume"] = 0.0
    ph = ph[["ticker", "date", "open", "high", "low", "close", "volume"]]
    feats = shadow_features(pd.concat([hist, ph], ignore_index=True), d_today)
    return feats.merge(last, on="ticker", how="left")


# ── 09:30 volumes ────────────────────────────────────────────────────────────
def baseline14(v930_by_date: dict[str, float], through: str, n: int = BASELINE_N,
               span_days: int = BASELINE_SPAN_DAYS) -> tuple[float | None, int]:
    """Mean over the `n` most recent sessions <= `through` with a 09:30 bar,
    all within `span_days` calendar days of `through` (the shadow scan pulls a
    30-day window, so a sparsely-printing name is rankable in neither);
    (None, n_found) when fewer than `n` or the mean is not > 0."""
    from datetime import date as _date, timedelta as _td
    floor = (_date.fromisoformat(through) - _td(days=span_days)).isoformat()
    dates = sorted(d for d in v930_by_date if floor <= d <= through)[-n:]
    if len(dates) < n:
        return None, len(dates)
    m = float(sum(float(v930_by_date[d]) for d in dates) / n)
    if m <= 0:
        return None, len(dates)
    return m, len(dates)


def v930_from_fresh(fresh_dir: Path, ticker: str, through: str) -> dict[str, float]:
    out: dict[str, float] = {}
    root = Path(fresh_dir)
    if not root.exists():
        return out
    for dd in sorted(p for p in root.iterdir() if p.is_dir() and p.name <= through):
        f = dd / f"{ticker}.parquet"
        if not f.exists():
            continue
        try:
            df = pd.read_parquet(f, columns=["hm", "v"])
        except Exception:  # noqa: BLE001
            continue
        row = df[df["hm"] == "09:30"]
        if len(row):
            out[dd.name] = float(row["v"].iloc[0])
    return out


def v930_from_store(bars5m_dir: Path, ticker: str, through: str) -> dict[str, float]:
    f = Path(bars5m_dir) / f"{ticker}.parquet"
    if not f.exists():
        return {}
    try:
        df = pd.read_parquet(f, columns=["date", "hm", "v"])
    except Exception:  # noqa: BLE001
        return {}
    df = df[(df["hm"] == "09:30") & (df["date"] <= through)]
    return {str(d): float(v) for d, v in zip(df["date"], df["v"])}


def v930_from_api_rows(rows) -> dict[str, float]:
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    ts = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    df["d"] = ts.dt.strftime("%Y-%m-%d")
    df["hm"] = ts.dt.strftime("%H:%M")
    b = df[df["hm"] == "09:30"]
    return {str(d): float(v) for d, v in zip(b["d"], b["v"])}


def load_cache(path: Path) -> dict[str, dict[str, float]]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        df = pd.read_parquet(p)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, float]] = {}
    for t, d, v in zip(df["ticker"], df["date"], df["v930"]):
        out.setdefault(str(t), {})[str(d)] = float(v)
    return out


def save_cache(path: Path, cache: dict[str, dict[str, float]]) -> None:
    rows = [{"ticker": t, "date": d, "v930": v} for t, m in cache.items() for d, v in m.items()]
    df = pd.DataFrame(rows, columns=["ticker", "date", "v930"])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix(".tmp.parquet")
    df.to_parquet(tmp)
    tmp.replace(Path(path))


# ── the build ────────────────────────────────────────────────────────────────
def build_prep(d_today: str, grouped_dir: Path | None = None, bars5m_dir: Path | None = None,
               fresh_dir: Path | None = None, cache_path: Path | None = None,
               out_dir: Path | None = None, api=None, allow_api: bool = False,
               holidays=HOLIDAYS_2026, now: datetime | None = None,
               api_sleep_s: float = 0.0) -> tuple[pd.DataFrame, dict]:
    """Build `<D_today>.parquet` + `<D_today>.meta.json`. Returns (df, meta).
    `api` must expose `bars_5m_range(ticker, start, end)`; it is called ONLY
    when `allow_api` is True."""
    grouped_dir = Path(grouped_dir or paths.GROUPED_DIR)
    bars5m_dir = Path(bars5m_dir or paths.BARS5M_DIR)
    fresh_dir = Path(fresh_dir or paths.BARS5M_FRESH_DIR)
    cache_path = Path(cache_path or paths.V930_CACHE)
    out_dir = Path(out_dir or paths.PREP_DIR)
    today = date.fromisoformat(d_today)
    d_prev = previous_trading_day(today, holidays).isoformat()

    feats = daily_features(grouped_dir, d_prev, d_today)
    cand = feats[(feats["avgvol14"] >= FILTER_AVGVOL14_MIN) & (feats["atr14"] > FILTER_ATR14_MIN)]
    cand = cand.dropna(subset=["atr14", "avgvol14"]).reset_index(drop=True)

    cache = load_cache(cache_path)
    sources = {"cache": 0, "fresh": 0, "store": 0, "api": 0}
    api_calls = 0
    api_errors = 0
    rows = []
    start = (date.fromisoformat(d_prev) - timedelta(days=30)).isoformat()
    for rec in cand.to_dict("records"):
        t = str(rec["ticker"])
        have = dict(cache.get(t, {}))
        base, n = baseline14(have, d_prev)
        used = "cache" if base is not None else None
        if base is None:
            more = v930_from_fresh(fresh_dir, t, d_prev)
            if more:
                have.update(more)
                base, n = baseline14(have, d_prev)
                used = "fresh" if base is not None else None
        if base is None:
            more = v930_from_store(bars5m_dir, t, d_prev)
            if more:
                have.update(more)
                base, n = baseline14(have, d_prev)
                used = "store" if base is not None else None
        if base is None and allow_api and api is not None:
            api_calls += 1
            try:
                out = api.bars_5m_range(t, start, d_prev)
            except Exception as exc:  # noqa: BLE001
                out = {"error": str(exc)}
            if isinstance(out, dict) and "error" in out:
                api_errors += 1
                if "429" in str(out["error"]):
                    _time.sleep(65)
                    try:
                        out = api.bars_5m_range(t, start, d_prev)
                    except Exception as exc:  # noqa: BLE001
                        out = {"error": str(exc)}
            if isinstance(out, list):
                have.update(v930_from_api_rows(out))
                base, n = baseline14(have, d_prev)
                used = "api" if base is not None else None
            if api_sleep_s:
                _time.sleep(api_sleep_s)
        if have:
            cache[t] = {**cache.get(t, {}), **have}
        if used:
            sources[used] += 1
        rows.append({"ticker": t, "atr14": float(rec["atr14"]), "avgvol14": float(rec["avgvol14"]),
                     "baseline14": float(base) if base is not None else None,
                     "n_baseline": int(n),
                     "prev_close": float(rec["prev_close"]) if pd.notna(rec.get("prev_close")) else None})
    df = pd.DataFrame(rows, columns=PREP_COLUMNS)
    with_base = int(df["baseline14"].notna().sum()) if len(df) else 0
    meta = {"prep_for": d_today, "built_from_through": d_prev,
            "generated_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "n_candidates": int(len(df)), "n_rankable": with_base,
            "api_calls": api_calls, "api_errors": api_errors, "allow_api": bool(allow_api),
            "sources": sources,
            "coverage": {"candidates": int(len(df)), "with_baseline14": with_base,
                         "without_baseline": int(len(df)) - with_base,
                         "store_through_date": d_prev},
            "stamp": STAMP}
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"{d_today}.tmp.parquet"
    df.to_parquet(tmp)
    tmp.replace(out_dir / f"{d_today}.parquet")
    atomic_save(out_dir / f"{d_today}.meta.json", json.dumps(meta, indent=1))
    try:
        save_cache(cache_path, cache)
    except Exception as exc:  # noqa: BLE001
        logger.warning("v930 cache save failed: %s", exc)
    return df, meta


# ── §14.5 freshness gate (the daemon reads only the sidecar) ─────────────────
def freshness(meta: dict | None, today: str, holidays=HOLIDAYS_2026,
              min_candidates: int = PREP_MIN_CANDIDATES,
              min_baseline_frac: float = PREP_MIN_BASELINE_FRAC) -> tuple[bool, str]:
    if not meta:
        return False, "prep_missing"
    if meta.get("prep_for") != today:
        return False, f"prep_for={meta.get('prep_for')}!={today}"
    d_prev = previous_trading_day(date.fromisoformat(today), holidays).isoformat()
    if meta.get("built_from_through") != d_prev:
        return False, f"built_from_through={meta.get('built_from_through')}!={d_prev}"
    cov = meta.get("coverage") or {}
    n = int(cov.get("candidates") or meta.get("n_candidates") or 0)
    wb = int(cov.get("with_baseline14") or meta.get("n_rankable") or 0)
    if n < min_candidates:
        return False, f"n_candidates={n}<{min_candidates}"
    if n and wb / n < min_baseline_frac:
        return False, f"baseline_frac={wb / n:.2f}<{min_baseline_frac}"
    return True, "fresh"


def read_meta(prep_dir: Path, today: str) -> dict | None:
    p = Path(prep_dir) / f"{today}.meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def read_prep(prep_dir: Path, today: str) -> pd.DataFrame | None:
    p = Path(prep_dir) / f"{today}.parquet"
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p)
    except Exception:  # noqa: BLE001
        return None
