"""CLL Tier 2 — Weekly (automatic report, DESCRIPTIVE ONLY).

Drift monitors vs the ACTIVE baseline: class distribution, D-share trend, win
rate vs trailing band, loss percentiles vs frozen p95, baseline-comparability
flag. MAY NOT test, tune, or propose (guard-enforced). Blocked while an
integrity_hold is set. Suspended during a re-pin window.

Mode (config): `dev` -> writes weekly/dev/drift-<date>.json and does NOT register
observations to the live queue (exercise/debug on real data). `live` -> writes
weekly/drift-<date>.json and registers interesting drifts to the hypothesis
queue as UNTESTED, dated. Flips to live when the re-pin completes.

Spec §7. Reads classified_ledger (no re-derivation). Run weekly.
"""
from __future__ import annotations

import importlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "learning"))
guard = importlib.import_module("guard")
LEARN = ROOT / "logs" / "learning"
CFG = yaml.safe_load((ROOT / "config" / "learning" / "loop.yaml").read_text())
TRAILING = 10


def _ledger():
    p = LEARN / "classified_ledger.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def main():
    mode = CFG["tiers"]["weekly"].get("mode", "dev")
    day = datetime.now().isoformat(timespec="seconds")[:10]

    # governance gates (descriptive tier still respects holds / re-pin)
    if (LEARN / "integrity_hold.flag").exists():
        print("[cll-weekly] BLOCKED — integrity_hold active; drift not described on a broken ruler.")
        return
    repin = json.loads((LEARN / "repin_state.json").read_text()) if (LEARN / "repin_state.json").exists() else {}
    if repin.get("suspend_drift"):
        print(f"[cll-weekly] SUSPENDED — re-pin window ({repin.get('status')}); drift monitoring paused.")
        return

    active_pin = (LEARN / "ACTIVE").read_text().strip()
    baseline = json.loads((LEARN / "baselines" / f"baseline-{active_pin}.json").read_text())
    ledger = _ledger()
    scalp_fail = [r for r in ledger if r["lane"] == "SCALP" and r["cohort"] == "failure"]

    # --- class distribution vs baseline ---
    cur_dist = Counter(r["primary"] for r in scalp_fail)
    base_dist = baseline["dist"]["SCALP"]
    dist_delta = {k: cur_dist.get(k, 0) - base_dist.get(k, 0) for k in set(cur_dist) | set(base_dist)}

    # --- per-session win rate + D-share trend ---
    by_sess = defaultdict(lambda: {"fail": 0, "ctrl": 0, "D": 0})
    for r in ledger:
        if r["lane"] != "SCALP":
            continue
        s = by_sess[r["date"]]
        if r["cohort"] == "control":
            s["ctrl"] += 1
        else:
            s["fail"] += 1
            if r["primary"] == "D":
                s["D"] += 1
    sessions = sorted(by_sess)[-TRAILING:]
    win = [by_sess[s]["ctrl"] / (by_sess[s]["ctrl"] + by_sess[s]["fail"])
           for s in sessions if (by_sess[s]["ctrl"] + by_sess[s]["fail"])]
    dshare = [by_sess[s]["D"] / by_sess[s]["fail"] for s in sessions if by_sess[s]["fail"]]

    # --- loss percentiles vs frozen p95 ---
    losses = [abs(r["truth_pnl_pct_of_risk"]) for r in scalp_fail]
    p95 = baseline["frozen_p95"]["SCALP"]

    report = {
        "date": day, "mode": mode, "baseline_pin": active_pin, "tier": "weekly",
        "class_distribution": {"current": dict(cur_dist), "baseline": base_dist, "delta": dist_delta},
        "d_share_trend": {"per_session": [round(x, 3) for x in dshare],
                          "latest": round(dshare[-1], 3) if dshare else None,
                          "note": "expected ~1.0 until honest fills (corrected metric)"},
        "win_rate": {"per_session": [round(x, 3) for x in win],
                     "mean": round(float(np.mean(win)), 3) if win else None,
                     "band": [round(min(win), 3), round(max(win), 3)] if win else None},
        "loss_percentiles": {"p50": round(float(np.percentile(losses, 50)), 1) if losses else None,
                             "p95": round(float(np.percentile(losses, 95)), 1) if losses else None,
                             "frozen_p95": p95},
        "baseline_comparability": {"note": "quantity-mix sensitive metrics on corrected basis; "
                                   "re-pin at migration will refresh (spec §8)"},
        "governance": "DESCRIPTIVE ONLY — weekly may not test/propose (guard-enforced)",
    }

    if mode == "dev":
        out = guard.guarded_path("weekly", f"weekly/dev/drift-{day}.json")
        out.write_text(json.dumps(report, indent=1))
        print(f"[cll-weekly] DEV drift written -> {out.relative_to(ROOT)} "
              f"(dist delta {dist_delta}, win mean {report['win_rate']['mean']}, "
              f"D-share latest {report['d_share_trend']['latest']}). No queue registration in dev.")
    else:
        out = guard.guarded_path("weekly", f"weekly/drift-{day}.json")
        out.write_text(json.dumps(report, indent=1))
        print(f"[cll-weekly] LIVE drift written -> {out.relative_to(ROOT)}. "
              f"(observation registration to queue: TODO when round machinery lands)")


if __name__ == "__main__":
    main()
