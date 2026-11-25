#!/usr/bin/env python3
"""Shadow-gate report — the option-(b) counterfactual test (pre-registered
2026-07-10, config/shadow_gate_20260710.yaml).

The LIVE gate is unchanged (user chose option c). This script scores, purely as
observation, what a calibrated>=0.50 gate WOULD have selected: it reads the
labels dataset from 2026-07-11 forward (out-of-sample vs the frozen map's fit
window), maps each candidate's raw confidence through the FROZEN breakpoints,
and compares shadow-PASS vs shadow-FAIL cohorts on counterfactual underlying
forward returns (cf_30m/60m/eod — Level-1 proxy, not option P&L).

HARD RULE (from the spec): results inform the OCTOBER confidence-source
decision only; never a mid-stream live-gate change.

Run: .venv/bin/python scripts/shadow_gate_report.py [--json]
"""
from __future__ import annotations

import glob
import json
import statistics as st
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "config" / "shadow_gate_20260710.yaml"


def load_spec() -> dict:
    return yaml.safe_load(SPEC.read_text())["shadow_gate"]


def calibrate(raw: float, bps: list[dict]) -> float:
    """Linear interpolation over the frozen breakpoints, clamped outside."""
    if raw <= bps[0]["raw"]:
        return bps[0]["calibrated"]
    for a, b in zip(bps, bps[1:]):
        if raw <= b["raw"]:
            if b["raw"] == a["raw"]:
                return b["calibrated"]
            f = (raw - a["raw"]) / (b["raw"] - a["raw"])
            return a["calibrated"] + f * (b["calibrated"] - a["calibrated"])
    return bps[-1]["calibrated"]


def cohorts(spec: dict):
    start = spec["window_start_labels"]
    thr = spec["threshold_calibrated"]
    bps = spec["breakpoints"]
    rows = {"pass": [], "fail": [], "posted": []}
    for f in sorted(glob.glob(str(ROOT / "logs/observation/labels-2026-*.jsonl"))):
        day = f.split("labels-")[-1][:10]
        if day < start:
            continue
        for line in Path(f).read_text().splitlines():
            r = json.loads(line)
            if r.get("kind") != "candidate" or r.get("cf_eod") is None:
                continue
            conf = r.get("local_confidence")
            if conf is None or not r.get("direction"):
                continue
            if str(r.get("status", "")).startswith("posted"):
                rows["posted"].append(r)
            rows["pass" if calibrate(conf, bps) >= thr else "fail"].append(r)
    return rows


def stats(rs: list[dict]) -> dict | None:
    vals = [r["cf_eod"] for r in rs]
    if not vals:
        return None
    return {"n": len(vals),
            "hit_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
            "mean_cf_eod_pct": round(st.fmean(vals) * 100, 3),
            "median_cf_eod_pct": round(st.median(vals) * 100, 3)}


def main() -> int:
    spec = load_spec()
    rows = cohorts(spec)
    out = {"spec": {"threshold_calibrated": spec["threshold_calibrated"],
                    "window_start": spec["window_start_labels"],
                    "map_frozen": spec["registered"]},
           "shadow_pass": stats(rows["pass"]),
           "shadow_fail": stats(rows["fail"]),
           "posted_funnel_reference": stats(rows["posted"]),
           "note": "OBSERVATION ONLY — informs the October decision, never a "
                   "mid-stream gate change. cf_eod = underlying proxy, not option P&L."}
    if "--json" in sys.argv:
        print(json.dumps(out, indent=1))
        return 0
    print(f"SHADOW GATE (option-b counterfactual) — calibrated >= {spec['threshold_calibrated']}, "
          f"map frozen {spec['registered']}, labels from {spec['window_start_labels']}")
    for name in ("shadow_pass", "shadow_fail", "posted_funnel_reference"):
        s = out[name]
        if s:
            print(f"  {name:24} n={s['n']:5}  hit {s['hit_rate']*100:4.1f}%  "
                  f"mean cf_eod {s['mean_cf_eod_pct']:+.3f}%  median {s['median_cf_eod_pct']:+.3f}%")
        else:
            print(f"  {name:24} (no data yet)")
    print(f"  {out['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
