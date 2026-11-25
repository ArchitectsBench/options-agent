"""Entry-snapshot feature enrichment — INSTRUMENTATION ONLY (2026-08-03).

Adds candidate context features to every entry snapshot GOING FORWARD, recorded
under `entry_features` on the snapshot. These are captured for PROSPECTIVE
evaluation post-cutover ONLY.

HARD CONTRACT (matches the task boundary):
  * NEVER consulted by any live-trading decision, risk gate, options selector,
    freshness gate, or the exit shadow-tracker's evaluation. This module is
    imported by the two snapshot WRITE sites and by nothing on any decision path.
  * Purely additive: read-only over data already present at the write site
    (`signals[ticker]`, `regime`, `trade`, entry ts) plus one cached daily-bar
    read for prior-day H/L. It computes NOTHING that feeds a trade.
  * Fail-open: every field is independently guarded; a failure records None for
    that field and never raises. `build_entry_features` itself never raises.
  * Field definitions are frozen and documented in config/entry_features.yaml
    (schema_version must match). Zero comparisons run now.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("trader")

SCHEMA_VERSION = "entry_features_v1"
_OPEN = time(6, 30)  # session open, America/Los_Angeles (config agent.market_hours.start)
_PRIOR_HL_CACHE: dict = {}  # (ticker, date_str) -> (prior_high, prior_low)


def _time_from_open_min(entry_ts: str):
    """Minutes from the 06:30 PT session open to the entry timestamp. The entry
    ts is naive local (PT) by construction (datetime.now().isoformat)."""
    try:
        dt = datetime.fromisoformat(entry_ts)
        open_dt = datetime.combine(dt.date(), _OPEN)
        return round((dt - open_dt).total_seconds() / 60.0, 1)
    except Exception:
        return None


def _opening_range_position(sig: dict):
    """Where the current underlying price sits within TODAY's opening range.
    signals already carries opening_range={high,low,position}; we add a numeric
    pos_pct = (last-low)/(high-low) in [0,1] (can exceed for breakouts)."""
    try:
        orng = sig.get("opening_range") or {}
        last = sig.get("last")
        if not orng and last is None:
            return None                       # nothing usable -> clean missing
        hi, lo = orng.get("high"), orng.get("low")
        pos_pct = None
        if hi is not None and lo is not None and last is not None and hi > lo:
            pos_pct = round((last - lo) / (hi - lo), 3)
        return {"high": hi, "low": lo, "last": last,
                "position": orng.get("position"), "pos_pct": pos_pct}
    except Exception:
        return None


def _spy_breadth_proxy(regime: dict):
    """The already-computed breadth proxy from the regime object (net-advancers
    % over the ~50-name snapshot). Recorded verbatim — it is the labelled proxy,
    not a real TICK feed."""
    try:
        tp = regime.get("tick_proxy") or {}
        if not tp:
            return None                       # no proxy present -> clean missing
        return {"net_adv_pct": tp.get("net_adv_pct"), "source": tp.get("source"),
                "breadth": regime.get("breadth")}
    except Exception:
        return None


def _spread_width_strikes(trade: dict):
    """Structural strike width of the debit/credit spread in $ (distance between
    the two strikes), from the leg option symbols. None for single-leg. The
    bid-ask EXECUTION spread is captured separately in
    logs/observation/spread_telemetry.jsonl (joinable by ticker+time); it is
    intentionally NOT re-derived here so this module touches no selector code."""
    try:
        from src.engine.paper_trader import strike_width
        return strike_width(trade.get("legs") or [])
    except Exception:
        return None


def _prior_day_hl_proximity(ticker: str, sig: dict, entry_ts: str):
    """Proximity of the current underlying price to the PRIOR session's high/low,
    in ATR units (entry-time-available: prior session only). Reads the cached
    daily-bar parquet; None if unavailable."""
    try:
        last, atr = sig.get("last"), sig.get("atr")
        if last is None or not atr:
            return None
        day = entry_ts[:10]
        key = (ticker, day)
        if key not in _PRIOR_HL_CACHE:
            import pandas as pd
            p = ROOT / "data" / "historical" / f"{ticker}.parquet"
            hi = lo = None
            if p.exists():
                df = pd.read_parquet(p)
                idx = df.index
                prior = df[idx < day] if idx.dtype == object else df[idx < pd.Timestamp(day)]
                if len(prior):
                    hi = round(float(prior["High"].iloc[-1]), 4)
                    lo = round(float(prior["Low"].iloc[-1]), 4)
            _PRIOR_HL_CACHE[key] = (hi, lo)
        hi, lo = _PRIOR_HL_CACHE[key]
        if hi is None or lo is None:
            return None
        return {"prior_high": hi, "prior_low": lo,
                "to_prior_high_atr": round((hi - last) / atr, 3),
                "to_prior_low_atr": round((last - lo) / atr, 3)}
    except Exception:
        return None


def build_entry_features(ticker: str, signals: dict, regime: dict,
                         trade: dict, entry_ts: str) -> dict:
    """Assemble the entry_features block. Never raises; returns a dict with a
    schema_version stamp and each field (None where uncomputable). Recorded for
    prospective evaluation only — consulted by nothing live."""
    try:
        # signals may be the full {ticker: snapshot} map (SCALP path) OR an
        # already-per-ticker snapshot (funnel path) — handle both.
        sig = (signals or {}).get(ticker)
        if sig is None and isinstance(signals, dict) and any(
                k in signals for k in ("opening_range", "bar_time", "last")):
            sig = signals
        sig = sig or {}
        regime = regime or {}
        trade = trade or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "note": "instrumentation only; NEVER consulted by any live decision, "
                    "gate, or shadow-tracker evaluation",
            "time_from_open_min": _time_from_open_min(entry_ts),
            "opening_range": _opening_range_position(sig),
            "spy_breadth_proxy": _spy_breadth_proxy(regime),
            "spread_width_strikes": _spread_width_strikes(trade),
            "prior_day_hl_proximity": _prior_day_hl_proximity(ticker, sig, entry_ts),
        }
    except Exception as exc:  # never break the (already-guarded) snapshot path
        logger.warning("build_entry_features failed for %s: %s", ticker, exc)
        return {"schema_version": SCHEMA_VERSION, "error": str(exc)}
