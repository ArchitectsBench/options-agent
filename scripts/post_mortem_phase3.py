"""Phase 3 — pattern aggregation with winner controls (approved w/ 5 marks).

Reads ONLY the pinned post-mortem state (failures + controls). Computes
the regime-opposed fingerprint FRESH on both cohorts via the identical
direction-vs-regime.bias rule (NOT by A3 classification membership — mark 1).
Slippage cap applied symmetrically to both cohorts (mark 3). Association
test (Fisher, OR + Woolf 95% CI) AND blocked-set economics (mark 4).
A3 is the pre-registered PRIMARY; A1/A4 secondary/exploratory (mark 5).
Writes docs/lessons.md. Does NOT write /proposed_rules (Phase 4, held).
"""

from __future__ import annotations

import glob
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.stats import fisher_exact

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import importlib

enr = importlib.import_module("enrich_trades_thetadata")
LEGS = enr.fallback_legs_map()
PIN = "24adf87"
SLIP_CAP = 0.40  # symmetric: exclude if slippage_usd > 40% of |truth_pnl|


def direction_of(rid: str, meta: dict) -> str | None:
    d = (meta.get("direction") or "").lower()
    if d in ("long", "short"):
        return d
    for leg in (LEGS.get(rid) or []):
        side = str(leg.get("side", "")).lower()
        sym = str(leg.get("symbol", ""))
        right = "C" if "C" in sym[-9:] else ("P" if "P" in sym[-9:] else None)
        if side.startswith("buy") and right:
            return "long" if right == "C" else "short"
    return None


def features(pm: dict) -> dict:
    rec = pm["entry_snapshot"]["recorded"]
    snap = rec.get("indicator_snapshot") or {}
    bias = (rec.get("regime") or {}).get("bias")
    rid = pm["meta"]["rec_id"]
    direction = direction_of(rid, pm["meta"])
    lc = pm["lifecycle"]
    be, te = lc.get("booked_entry_fill"), lc.get("truth_mid_entry")
    bx, tx = lc.get("booked_exit_fill"), lc.get("truth_mid_exit")
    # batch-0 fix (2026-07-24): authoritative real fill quantity (see engine
    # reconstructed_risk), not rec.risk.quantity which is None->1 for SCALP.
    qty = (pm.get("reconstructed_risk") or {}).get("quantity") \
        or (rec.get("risk") or {}).get("quantity") or 1
    slip = None
    if None not in (be, te, bx, tx):
        slip = (abs(be - te) + abs(bx - tx)) * 100 * qty
    truth = pm["failure_definition"]["truth_pnl"]
    # fresh fingerprints
    opposed = (bias in ("bullish", "bearish") and direction in ("long", "short")
               and ((bias == "bullish" and direction == "short")
                    or (bias == "bearish" and direction == "long")))
    opp_evaluable = bias in ("bullish", "bearish") and direction in ("long", "short")
    vd = snap.get("vwap_dist_atr")
    a1 = (vd is not None and direction in ("long", "short") and abs(vd) >= 2.0
          and ((direction == "long" and vd > 0) or (direction == "short" and vd < 0)))
    ivr = (snap.get("iv_rank") or {}).get("rank") if isinstance(snap.get("iv_rank"), dict) else None
    strat = pm["meta"].get("strategy")
    is_debit = strat in ("debit_spread", "0dte_directional", "long_call", "long_put")
    a4 = ivr is not None and is_debit and ivr >= 0.90
    return {"rid": rid, "bias": bias, "direction": direction, "truth": truth,
            "slip": slip, "opposed": opposed, "opp_evaluable": opp_evaluable,
            "a1_chase": bool(a1), "a4_ivhead": bool(a4),
            "slip_share": (slip / abs(truth) if slip is not None and truth else None)}


def woolf_ci(a, b, c, d):
    a2, b2, c2, d2 = a + 0.5, b + 0.5, c + 0.5, d + 0.5
    logor = math.log((a2 * d2) / (b2 * c2))
    se = math.sqrt(1 / a2 + 1 / b2 + 1 / c2 + 1 / d2)
    return math.exp(logor - 1.96 * se), math.exp(logor + 1.96 * se)


def association(fails, ctrls, key, cap):
    def keep(t):
        return t["opp_evaluable" if key == "opposed" else "opp_evaluable"] if key == "opposed" \
            else (t["direction"] is not None)
    def capped(t):
        return (not cap) or t["slip"] is None or t["truth"] == 0 \
            or t["slip"] <= SLIP_CAP * abs(t["truth"])
    fev = [t for t in fails if (t["opp_evaluable"] if key == "opposed" else True) and capped(t)]
    cev = [t for t in ctrls if (t["opp_evaluable"] if key == "opposed" else True)
           and t["truth"] > 0 and capped(t)]
    a = sum(1 for t in fev if t[key]); b = len(fev) - a
    c = sum(1 for t in cev if t[key]); d = len(cev) - c
    orr, p = fisher_exact([[a, b], [c, d]], alternative="greater")
    lo, hi = woolf_ci(a, b, c, d)
    return {"a_opp_loser": a, "b_aln_loser": b, "c_opp_winner": c, "d_aln_winner": d,
            "n_fail_eval": len(fev), "n_ctrl_eval": len(cev),
            "odds_ratio": round(orr, 2), "fisher_p_greater": round(p, 4),
            "or_95ci": [round(lo, 2), round(hi, 2)]}


def economics(fails, ctrls, key, cap):
    def capped(t):
        return (not cap) or t["slip"] is None or t["truth"] == 0 \
            or t["slip"] <= SLIP_CAP * abs(t["truth"])
    blocked = [t for t in (fails + [c for c in ctrls if c["truth"] > 0])
               if t[key] and (t["opp_evaluable"] if key == "opposed" else True) and capped(t)]
    losers = [t for t in blocked if t["truth"] < 0]
    winners = [t for t in blocked if t["truth"] > 0]
    total = sum(t["truth"] for t in blocked)
    return {"n_blocked": len(blocked), "n_avoided_losers": len(losers),
            "n_forgone_winners": len(winners),
            "sum_avoided_loss": round(sum(t["truth"] for t in losers), 0),
            "sum_forgone_win": round(sum(t["truth"] for t in winners), 0),
            "net_truth_pnl_of_blocked_set": round(total, 0),
            "expectancy_per_blocked_trade": round(total / len(blocked), 1) if blocked else None,
            "trades": [{"rid": t["rid"], "truth": t["truth"],
                        "dir": t["direction"], "bias": t["bias"]} for t in blocked]}


def main():
    fails = [features(json.load(open(f)))
             for f in glob.glob(str(ROOT / "logs/post_mortems/*.json"))
             if json.load(open(f))["meta"]["lane"] == "SCALP"]
    ctrls = [features(json.load(open(f)))
             for f in glob.glob(str(ROOT / "logs/post_mortems/controls/*.json"))
             if json.load(open(f))["meta"]["lane"] == "SCALP"]
    # slippage-cap exclusion counts (symmetric report — mark 3)
    def excl(cohort, winners_only=False):
        pool = [t for t in cohort if (not winners_only) or t["truth"] > 0]
        return sum(1 for t in pool if t["slip"] is not None and t["truth"]
                   and t["slip"] > SLIP_CAP * abs(t["truth"]))
    report = {
        "pinned_commit": PIN,
        "cohorts": {"scalp_failures": len(fails),
                    "scalp_controls_all": len(ctrls),
                    "scalp_controls_truth_pos": sum(1 for t in ctrls if t["truth"] > 0),
                    "controls_truth_zero_dropped": sum(1 for t in ctrls if t["truth"] == 0)},
        "slippage_cap": {"threshold_share": SLIP_CAP,
                         "failures_excluded": excl(fails),
                         "controls_excluded": excl(ctrls, winners_only=True)},
        "opp_unevaluable": {
            "failures_neutral_or_nodir": sum(1 for t in fails if not t["opp_evaluable"]),
            "controls_neutral_or_nodir": sum(1 for t in ctrls if t["truth"] > 0 and not t["opp_evaluable"])},
        "PRIMARY_A3_regime_opposed": {
            "capped": association(fails, ctrls, "opposed", True),
            "uncapped_secondary": association(fails, ctrls, "opposed", False),
            "economics_capped": economics(fails, ctrls, "opposed", True)},
        "EXPLORATORY_A1_extension_chase": {
            "capped": association(fails, ctrls, "a1_chase", True),
            "economics_capped": economics(fails, ctrls, "a1_chase", True)},
        "EXPLORATORY_A4_iv_headwind": {
            "capped": association(fails, ctrls, "a4_ivhead", True),
            "economics_capped": economics(fails, ctrls, "a4_ivhead", True)},
    }
    (ROOT / "logs" / "post_mortems" / "phase3_results.json").write_text(
        json.dumps(report, indent=1))
    a3 = report["PRIMARY_A3_regime_opposed"]
    print(json.dumps({k: v for k, v in report.items() if k != "PRIMARY_A3_regime_opposed"}, indent=1))
    print("\nPRIMARY A3:")
    print(" capped assoc:", a3["capped"])
    print(" economics:", {k: v for k, v in a3["economics_capped"].items() if k != "trades"})
    return report


if __name__ == "__main__":
    main()
