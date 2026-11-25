#!/usr/bin/env python3
"""Daily SCALP cohort report — corrected-metric, trimmed (2026-07-24).

Runs today's SCALP session through the committed post-mortem pipeline (engine +
p2_v1 classifier) on the batch-0-corrected slip metric (real fill quantity via
`reconstructed_risk`). TRUTH basis; report only, no writes to logs/post_mortems.

Trim rationale: post batch-0, classified D-share reads ~100% every session until
honest IBKR fills arrive (execution fiction dominates sandbox losses). So
D-share is reduced to ONE confirming line with a LOUD flag if it ever drops
materially — that drop is the signal that the fill regime is changing. Full
detail is kept on the axes that still carry information:
  - win rate vs the trailing-session band
  - loss magnitudes vs the frozen lane p95
  - TAIL members (loss > p95 AND no A-D rule fired)
  - slice anomalies (entry hour / regime / ticker / exit trigger)

Run: .venv/bin/python scripts/daily_cohort_report.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import glob
import importlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
eng = importlib.import_module("post_mortem_engine")
clf = importlib.import_module("post_mortem_classify")

D_SHARE_FLOOR = 0.85       # below this = LOUD flag (fill regime may be changing)
TRAILING_SESSIONS = 10


def _pt_today() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def build_pms(day_iso: str):
    """Build failure + winner-control pms for one day's SCALP cohort."""
    recs, events = eng.load_recs(), eng.load_exits()
    legs = eng.enr.fallback_legs_map()
    compact = day_iso.replace("-", "")
    rows = [json.loads(l) for l in
            (ROOT / "logs/observation/enriched_trades_v1.jsonl").read_text().splitlines() if l.strip()]
    today = [r for r in rows if str(r.get("rec_id", "")).startswith(compact)
             and "-TEST-" in r.get("rec_id", "") and r.get("status") == "ok"
             and r.get("true_pnl_mid") is not None]

    def build(tr, control):
        rid = tr["rec_id"]; ex = events.get(rid)
        if not ex:
            return None
        ent = events.get(rid + "::entry", {}); rec = recs.get(rid)
        if not rec:
            m = re.match(r"^(\d{4})(\d{2})(\d{2})", rid)
            d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
            asm = eng.assemble_from_cycles(rid, tr.get("ticker", ""), d,
                                           str(ent.get("ts") or ex.get("ts") or ""))
            if asm is None:
                return None
            rec = {"_assembled": asm, "trade": {}, "risk": None}
        return eng.build_one(rid, tr, rec, ex, ent, legs, control=control)

    fails = [pm for tr in today if (pm := build(tr, False))]
    ctrls = [pm for tr in today if (pm := build(tr, True))]
    return today, fails, ctrls, legs


def baseline_and_trailing(day_iso: str):
    """Frozen SCALP p95 + trailing per-session win-rate band + baseline slices."""
    base = []
    for f in glob.glob(str(ROOT / "logs/post_mortems/*.json")):
        if "phase3_results" in f:
            continue
        try:
            p = json.load(open(f))
        except Exception:
            continue
        if isinstance(p, dict) and p.get("meta", {}).get("lane") == "SCALP":
            base.append(p)
    p95 = float(np.percentile([abs(p["failure_definition"]["truth_pnl_pct_of_risk"]) for p in base], 95))
    # trailing win-rate per session (truth-pos / scored), sessions strictly before today
    rows = [json.loads(l) for l in
            (ROOT / "logs/observation/enriched_trades_v1.jsonl").read_text().splitlines() if l.strip()]
    by_sess = {}
    for r in rows:
        rid = r.get("rec_id", "")
        if "-TEST-" not in rid or r.get("status") != "ok" or r.get("true_pnl_mid") is None:
            continue
        m = re.match(r"^(\d{8})", rid)
        if not m:
            continue
        by_sess.setdefault(m.group(1), []).append(r["true_pnl_mid"])
    compact = day_iso.replace("-", "")
    prior = sorted(s for s in by_sess if s < compact)[-TRAILING_SESSIONS:]
    wr = [sum(1 for x in by_sess[s] if x >= 0) / len(by_sess[s]) for s in prior]
    return base, p95, wr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=_pt_today())
    args = ap.parse_args()
    day = args.date

    today, fails, ctrls, legs = build_pms(day)
    base, p95, wr = baseline_and_trailing(day)
    n = len(today)
    print(f"=== SCALP daily cohort — {day} (TRUTH basis, p2_v1 corrected metric) ===")
    if n == 0:
        print("no scored SCALP trades this session.")
        return
    n_fail, n_win = len(fails), len(ctrls)

    # classify on corrected metric
    dist = Counter(clf.classify(pm, p95, legs)["primary"] for pm in fails)
    for pm in fails:
        pm["classification"] = clf.classify(pm, p95, legs)
    d_share = dist.get("D", 0) / max(n_fail, 1)

    # --- D-share: single confirming line + loud flag ---
    if d_share >= D_SHARE_FLOOR:
        print(f"D-share: {d_share:.0%} of {n_fail} failures — CONFIRMS execution-fiction "
              f"regime (expected ~100% until honest fills).")
    else:
        print(f"🚨 D-SHARE DROP: {d_share:.0%} (< {D_SHARE_FLOOR:.0%}) — MATERIAL. The fill "
              f"regime may be changing (honest fills arriving / less slippage). "
              f"Non-D dist: { {k: v for k, v in dist.items() if k != 'D'} }. ESCALATE.")

    # --- win rate vs trailing band ---
    win_rate = n_win / n
    if wr:
        mean, sd = float(np.mean(wr)), float(np.std(wr))
        z = (win_rate - mean) / sd if sd else 0.0
        band = "INSIDE" if min(wr) - 1e-9 <= win_rate <= max(wr) + 1e-9 else "OUTSIDE"
        print(f"win rate: {win_rate:.0%} ({n_win}/{n}) | trailing-{len(wr)} mean {mean:.0%} "
              f"sd {sd:.0%} range {min(wr):.0%}-{max(wr):.0%} | z={z:+.1f} → {band} band")
    else:
        print(f"win rate: {win_rate:.0%} ({n_win}/{n}) | no trailing band")

    # --- loss magnitudes vs frozen p95 ---
    losses = [abs(p["failure_definition"]["truth_pnl_pct_of_risk"]) for p in fails]
    over = [(p["meta"].get("ticker") or "?", abs(p["failure_definition"]["truth_pnl_pct_of_risk"]))
            for p in fails if abs(p["failure_definition"]["truth_pnl_pct_of_risk"]) > p95]
    over_str = ", ".join(f"{t} {v:.0f}%" for t, v in over) or "none"
    print(f"loss %risk: med {np.median(losses):.1f} max {max(losses):.1f} | frozen p95 {p95:.1f} "
          f"| over p95: {over_str}")

    # --- TAIL members ---
    tails = [p["meta"]["rec_id"] for p in fails if p["classification"]["primary"] == "UNCLASSIFIED_TAIL"]
    print(f"TAIL members: {tails or 'none'}")

    # --- slice anomalies vs baseline ---
    def sl(pms, fn):
        c = Counter(fn(p) for p in pms)
        return c
    def hour(p):
        ts = p["lifecycle"].get("entry_ts", "") or ""
        return ts[11:13] + "h" if len(ts) >= 13 else "??"
    def regime(p):
        return (p["entry_snapshot"]["recorded"].get("regime") or {}).get("bias") or "unknown"
    def ticker(p):
        return p["meta"].get("ticker") or "?"
    def trig(p):
        return p["lifecycle"].get("exit_trigger") or "?"
    anomalies = []
    for name, fn in [("hour", hour), ("regime", regime), ("ticker", ticker), ("exit", trig)]:
        tc, bc = sl(fails, fn), sl(base, fn)
        for k, v in tc.items():
            t_sh, b_sh = v / n_fail, bc.get(k, 0) / max(len(base), 1)
            if (t_sh - b_sh) > 0.15 and v >= 3:
                anomalies.append(f"{name}={k} {t_sh:.0%} vs base {b_sh:.0%} (n={v})")
    print(f"slice anomalies: {anomalies or 'none'}")
    print(f"cohort: {n} scored = {n_fail} failures + {n_win} winner controls")


if __name__ == "__main__":
    main()
