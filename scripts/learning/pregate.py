"""CLL data_suspect PRE-GATE — entry-freshness contamination filter.

An entry whose signal was computed on a stale bar is a CONTAMINATED INPUT, not a
failure mode. It is tagged `data_suspect` and quarantined from the A-E taxonomy,
SYMMETRICALLY across failures AND winner controls (no asymmetric quarantining).

────────────────────────────────────────────────────────────────────────────────
JUSTIFICATION — READ BEFORE INTERPRETING data_suspect (directive 2026-07-24):
  Drift does NOT separate failures from controls in either lane:
    - SCALP: 101/110 failures vs 96/104 controls under 0.05 ATR (both ~0)
    - funnel: controls drift MORE than failures (median 0.595 vs 0.498)
  The pre-gate is justified on INPUT PURITY, not on drift being a failure cause.
  Anyone reading data_suspect as "the bad trades" is MISREADING it — it means
  "we can't trust what the signal saw," nothing about the outcome.

  Historical SCALP value is near-zero: of 7 SCALP failures tagged at drift>0.10,
  4 are already D-class; the pre-gate reclassifies only 3 of 125. It is
  PRIMARILY A FORWARD-LOOKING instrument for SCALP.
────────────────────────────────────────────────────────────────────────────────

Rule (floors in config/signal_freshness.yaml):
  SCALP  : drift-primary — tag if underlying_drift_over_lag > drift_floor_atr.SCALP
           (0.10). When drift is UNCOMPUTABLE (no underlying path — a store gap,
           backfillable), fall back to age > pre_gate_floor_min.SCALP (3).
  funnel : NO drift gate (drift persisted as a covariate only). Tag on
           age > pre_gate_floor_min.funnel (configurable 10-15).
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts" / "learning"))
PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
_F = importlib.import_module("guard")  # only to anchor logs/learning if needed later


def floors() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "config" / "signal_freshness.yaml").read_text())


def signal_age_min(pm) -> float | None:
    snap = (pm["entry_snapshot"]["recorded"].get("indicator_snapshot") or {})
    bt, ets = snap.get("bar_time"), pm["lifecycle"].get("entry_ts")
    if not bt or not ets:
        return None
    try:
        bar = datetime.fromisoformat(bt); ent = datetime.fromisoformat(ets)
        if ent.tzinfo is None:
            ent = ent.replace(tzinfo=PT)
        return round((ent - bar).total_seconds() / 60.0, 2)
    except Exception:
        return None


def drift_over_lag_atr(pm) -> float | None:
    """|underlying(entry) - underlying(signal-bar)| / ATR, both looked up in the
    SAME retrospective path (aligned — the fix for the source-mismatch bug)."""
    snap = (pm["entry_snapshot"]["recorded"].get("indicator_snapshot") or {})
    atr = snap.get("atr")
    ov = pm.get("market_truth_overlay") or {}
    path = ov.get("underlying_path_5m")
    bt, ets = snap.get("bar_time"), pm["lifecycle"].get("entry_ts")
    if not path:  # pm was built before the store had this (ticker, day) — re-derive
        try:
            rid = pm["meta"]["rec_id"]
            day = rid[:8] if "-TEST-" in rid else rid[:10].replace("-", "")
            day = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
            up = importlib.import_module("post_mortem_engine").underlying_path(pm["meta"].get("ticker"), day)
            if isinstance(up, list) and up:
                path = up
        except Exception:
            pass
    if not (atr and path and bt and ets):
        return None
    try:
        bar_et = datetime.fromisoformat(bt).astimezone(ET).strftime("%Y-%m-%dT%H:%M")
        ent = datetime.fromisoformat(ets)
        ent = ent.replace(tzinfo=PT) if ent.tzinfo is None else ent
        ent_et = ent.astimezone(ET).strftime("%Y-%m-%dT%H:%M")

        def _at(ts):
            v = None
            for p in path:
                if p["ts"] <= ts:
                    v = p["value"]
                else:
                    break
            return v
        pb, pe = _at(bar_et), _at(ent_et)
        if pb is None or pe is None:
            return None
        return round(abs(pe - pb) / atr, 3)
    except Exception:
        return None


def bar_gap(pm):
    """(missing_bars, spans_session): the general MISSING-BAR test (replaces the
    same-date cross_session mechanism). Weekend, halt, and feed dropout all fall
    out of one rule — the count of 5-min bars that SHOULD sit between the signal
    bar and entry but are absent (the signal saw nothing after its last bar).
    - spans_session (different ET session date): drift-blind overnight/weekend
      staleness; the derived `cross_session` label.
    - missing_bars (same session): gap_minutes // 5 — a halt or feed dropout
      leaves several bars missing WHILE the market is open."""
    snap = (pm["entry_snapshot"]["recorded"].get("indicator_snapshot") or {})
    bt, ets = snap.get("bar_time"), pm["lifecycle"].get("entry_ts")
    if not bt or not ets:
        return None, False
    try:
        bar = datetime.fromisoformat(bt); ent = datetime.fromisoformat(ets)
        if ent.tzinfo is None:
            ent = ent.replace(tzinfo=PT)
        if bar.astimezone(ET).date() != ent.astimezone(ET).date():
            return None, True                        # cross-session (derived)
        gap_min = (ent.astimezone(ET) - bar.astimezone(ET)).total_seconds() / 60.0
        return int(gap_min // 5), False
    except Exception:
        return None, False


def cross_session(pm) -> bool:
    """Derived label (not a separate mechanism): the spans_session flag from the
    one missing-bar test."""
    return bar_gap(pm)[1]


def verdict(lane, drift, age, missing_bars=None, spans_session=False, fl=None):
    """(data_suspect: bool, reason). One missing-bar test first (spans_session =
    cross-session, drift-blind; missing_bars > threshold = halt/dropout), then
    lane-specific: SCALP drift-primary (age fallback when drift uncomputable),
    funnel age-only. Identical for failures and controls."""
    fl = fl or floors()
    if spans_session:
        return True, "cross_session"                 # derived from missing-bar test
    if missing_bars is not None and missing_bars > fl.get("missing_bars_max", 2):
        return True, f"missing_bars={missing_bars}"   # halt / feed dropout
    if drift is None and age is None and missing_bars is None and not spans_session:
        # bar_time entirely absent: none of the three mechanisms could compute
        # anything (completeness-audit gap #3, docs/post_mortem_program_state.md
        # §8 2026-07-25 — pre-gate previously FAILED OPEN here, falling through
        # to "clean"/untagged). Forward-only: fail CLOSED on an unmeasurable
        # record instead.
        return True, "no_bar_time"
    dfloor = (fl.get("drift_floor_atr") or {}).get(lane)
    afloor = (fl.get("pre_gate_floor_min") or {}).get(lane)
    if lane == "SCALP" and dfloor is not None:
        if drift is not None:
            return (drift > dfloor, f"drift>{dfloor}ATR" if drift > dfloor else "clean")
        return (age is not None and afloor is not None and age > afloor, "age_fallback_no_path")
    return (age is not None and afloor is not None and age > afloor, "age")
