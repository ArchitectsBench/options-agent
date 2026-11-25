"""CLL one-time seed: pin the sandbox baseline (corrected metric) and backfill
classified_ledger from existing post-mortems. Idempotent-ish: rewrites the
baseline snapshot + ACTIVE + repin_state, and (re)writes classified_ledger from
scratch. Run once before nightly goes live. Spec §8, §12.
"""
from __future__ import annotations

import glob
import importlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "scripts" / "learning"))
clf = importlib.import_module("post_mortem_classify")
pregate = importlib.import_module("pregate")
guard = importlib.import_module("guard")
integrity = importlib.import_module("integrity")

PIN = "sandbox-2026-07-24"


def _date(rid: str) -> str:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", rid)
    if m:
        return m.group(1)
    return f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}"


def _strategy(pm):
    rid = pm["meta"]["rec_id"]
    s = pm["meta"].get("strategy")
    # LANE MARKER (2026-08-10): guarded on the literal "-TEST-", so after the
    # 2026-08-08 forward-only rename this fallback stopped firing for SCALP ids
    # and strategy fell through as None. meta.strategy is absent on ~83% of
    # post-mortems, so the fallback is the normal path, not an edge case, and
    # nightly APPENDS to classified_ledger — mislabelled rows would accumulate.
    # Field positions are identical in both eras; only the marker changed.
    from src.engine.paper_trader import is_scalp
    if not s and is_scalp(rid):
        parts = rid.split("-")
        s = "-".join(parts[4:]) if len(parts) > 4 else None
    return s


def _signal_age_min(pm):
    """decision wall-clock (entry_ts, PT) minus the signal bar timestamp (ET)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    snap = (pm["entry_snapshot"]["recorded"].get("indicator_snapshot") or {})
    bt, ets = snap.get("bar_time"), pm["lifecycle"].get("entry_ts")
    if not bt or not ets:
        return None
    try:
        bar = datetime.fromisoformat(bt); ent = datetime.fromisoformat(ets)
        if ent.tzinfo is None:
            ent = ent.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        return round((ent - bar).total_seconds() / 60.0, 2)
    except Exception:
        return None


def _row(pm, cl, cohort):
    lc = pm["lifecycle"]; rr = pm.get("reconstructed_risk") or {}
    return {"date": _date(pm["meta"]["rec_id"]), "rec_id": pm["meta"]["rec_id"],
            "lane": pm["meta"]["lane"], "stream": "scalp" if pm["meta"]["lane"] == "SCALP" else "funnel",
            "strategy": _strategy(pm), "signal_data_age_min": _signal_age_min(pm),
            "underlying_drift_over_lag": pregate.drift_over_lag_atr(pm),
            "missing_bars_over_lag": pregate.bar_gap(pm)[0], "cross_session": pregate.bar_gap(pm)[1],
            "cohort": cohort, "primary": (cl["primary"] if cl else None),
            "primary_rule": (cl.get("primary_rule") if cl else None),
            "truth_pnl": pm["failure_definition"]["truth_pnl"],
            "truth_pnl_pct_of_risk": pm["failure_definition"]["truth_pnl_pct_of_risk"],
            "reconstructed_qty": rr.get("quantity"), "max_dollar_risk": rr.get("max_dollar_risk"),
            "ruleset": "p2_v1", "baseline_pin": PIN}


def main(reseed: bool = False):
    # R20 write-guard, checked FIRST (before any read or write): the nightly loop
    # APPENDS forward-window rows to classified_ledger.jsonl after this one-time
    # seed, so re-running seed's "w" rewrite would silently destroy them. Refuse
    # to clobber a non-empty existing ledger unless an explicit --reseed is passed
    # (a from-scratch rebuild, intended only for a ruleset change). Nothing is
    # read or written on refusal.
    lp = guard.guarded_path("seed", "classified_ledger.jsonl")
    if lp.exists() and lp.stat().st_size > 0 and not reseed:
        existing = sum(1 for _ in lp.open())
        sys.exit(
            f"REFUSING TO RESEED: {lp} already exists and is non-empty "
            f"({existing} rows). Re-running seed.py would destroy the "
            "forward-window rows the nightly loop appended. Pass --reseed ONLY "
            "if you intend to rebuild the baseline from scratch (ruleset "
            "change). No files were written. UNVALIDATED — PAPER ONLY.")

    legs = importlib.import_module("post_mortem_engine").enr.fallback_legs_map()
    fails = [json.load(open(f)) for f in glob.glob(str(ROOT / "logs/post_mortems/*.json")) if "phase3_results" not in f]
    ctrls = [json.load(open(f)) for f in glob.glob(str(ROOT / "logs/post_mortems/controls/*.json"))]

    # per-lane frozen p95 (corrected loss mags == size-consistent)
    p95 = {}
    for lane in ("SCALP", "funnel"):
        ls = [abs(p["failure_definition"]["truth_pnl_pct_of_risk"]) for p in fails if p["meta"]["lane"] == lane]
        p95[lane] = round(float(np.percentile(ls, 95)), 1) if ls else 100.0

    # classify failures on the corrected metric; build ledger rows, applying the
    # data_suspect PRE-GATE symmetrically (failures AND controls, identical rule).
    fl = pregate.floors()
    ledger, suspect = [], []
    dist = {"SCALP": Counter(), "funnel": Counter()}
    tagged = {("SCALP", "failure"): 0, ("SCALP", "control"): 0,
              ("funnel", "failure"): 0, ("funnel", "control"): 0}

    def _route(pm, cl, cohort):
        lane = pm["meta"]["lane"]
        row = _row(pm, cl, cohort)
        ds, reason = pregate.verdict(lane, row["underlying_drift_over_lag"], row["signal_data_age_min"],
                                     row["missing_bars_over_lag"], row["cross_session"], fl)
        if ds:
            tagged[(lane, cohort)] += 1
            suspect.append({"date": row["date"], "rec_id": row["rec_id"], "cohort": cohort,
                            "lane": lane, "resolution": "data_suspect", "pregate_reason": reason,
                            "signal_data_age_min": row["signal_data_age_min"],
                            "underlying_drift_over_lag": row["underlying_drift_over_lag"],
                            "missing_bars_over_lag": row["missing_bars_over_lag"],
                            "cross_session": row["cross_session"]})
        else:
            ledger.append(row)
            if cohort == "failure":
                dist[lane][cl["primary"]] += 1

    for pm in fails:
        _route(pm, clf.classify(pm, p95[pm["meta"]["lane"]], legs), "failure")
    for pm in ctrls:
        if pm["failure_definition"]["truth_pnl"] > 0:
            _route(pm, None, "control")

    funnel_fail = [p for p in fails if p["meta"]["lane"] == "funnel"]
    baseline = {
        "pin_id": PIN, "created_at": "2026-07-24", "ruleset": "p2_v1",
        "ruleset_hash": integrity.ruleset_hash(), "corrected_metric": True,
        "pregate": "applied",   # item 1: baseline EXCLUDES data_suspect, same basis as
                                # forward output -> comparable. Same artifact class as the
                                # D-share size-mix drop; the stamp makes it un-missable.
        "data_suspect_excluded": len(suspect),
        "supersedes": "24adf87 (pre-fix, quantity-buggy SCALP D-share; archived in git)",
        "frozen_p95": p95,
        "dist": {k: dict(v) for k, v in dist.items()},
        "funnel_control": {"rec_ids": [r["rec_id"] for r in ledger
                                       if r["lane"] == "funnel" and r["cohort"] == "failure"],
                           "dist": dict(dist["funnel"]),
                           "note": "INV4 reference (clean, post-pregate) — must not move without ruleset change / new funnel data"},
        "n": {"SCALP_failures": sum(dist["SCALP"].values()),
              "funnel_failures": sum(dist["funnel"].values())},
    }

    # write baseline + ACTIVE + repin_state (guarded)
    guard.guarded_path("seed", f"baselines/baseline-{PIN}.json").write_text(json.dumps(baseline, indent=1))
    guard.guarded_path("seed", "ACTIVE").write_text(PIN + "\n")
    guard.guarded_path("seed", "repin_state.json").write_text(json.dumps(
        {"status": "active", "active_pin": PIN, "suspend_rounds": False, "suspend_drift": False}, indent=1))

    # (re)write classified_ledger (clean) + quarantine_resolved (data_suspect)
    with lp.open("w") as f:
        for r in ledger:
            f.write(json.dumps(r) + "\n")
    qp = guard.guarded_path("seed", "quarantine_resolved.jsonl")
    with qp.open("w") as f:
        for r in suspect:
            f.write(json.dumps(r) + "\n")

    print(f"seeded baseline {PIN}: p95={p95} dist={baseline['dist']}")
    print(f"classified_ledger rows: {len(ledger)} "
          f"({sum(1 for r in ledger if r['cohort']=='failure')} failures + "
          f"{sum(1 for r in ledger if r['cohort']=='control')} controls)")
    print("PRE-GATE data_suspect tagged (symmetric, both cohorts per lane):")
    for (lane, coh), n in sorted(tagged.items()):
        print(f"  {lane} {coh}: {n} data_suspect")
    print(f"  total quarantined: {len(suspect)} (INPUT PURITY, not 'bad trades' — see pregate.py)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="CLL one-time baseline seed (Spec §8, §12)")
    ap.add_argument("--reseed", action="store_true",
                    help="DESTROY and rebuild classified_ledger.jsonl from post-mortems "
                         "— drops forward-window rows; ruleset-change use ONLY")
    main(reseed=ap.parse_args().reseed)
