#!/usr/bin/env python3
"""Give-back analysis — how much peak unrealized profit each exit rule surrenders.

The scenario this measures (logged as lesson L13, 2026-07-08): the SLV put
spread reached +$135 unrealized and closed -$110 on a VWAP-invalidation exit,
a $245 peak-to-exit round-trip. The invalidation trigger watches the
UNDERLYING's VWAP relationship, not the POSITION's P&L, and in evaluate_exit it
is checked BEFORE the profit target; the target itself only arms at 50% of MAX
profit, which intraday spreads rarely approach. Result: a position can be up
big and give it all back before anything protects the gain.

Every paper exit records mfe_value (best per-contract mark reached),
entry_net_debit, and exit_net_credit. Give-back is therefore exactly
computable. This script quantifies, per exit reason:
  * capture ratio = realized_profit / peak_profit  (1.0 = kept it all)
  * give-back $    = (mfe_value - exit_net_credit) * 100 * qty
  * winners that turned flat/red (the SLV pattern)
and runs a counterfactual: what a hard profit-lock at various levels WOULD have
captured, exactly where the recorded peak permits.

This is a MEASUREMENT instrument, not a trading change. Its job is to size the
fix before any exit-rule parameter is touched.

Run:  .venv/bin/python scripts/giveback_analysis.py [--json]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILLS = ROOT / "logs" / "observation" / "fills.jsonl"


# the trailing-stop fix (lesson L13) may be tuned once we have this many funnel
# positions carrying an ordered path (mark_series), including >=1 choppy week —
# give-back is worst in chop, so a trending-only sample would under-fit it.
PATH_SAMPLE_TARGET = 25
MIN_PATH_POINTS = 3   # a path shorter than this carries no reversal to learn from


def _rows():
    out = []
    for line in FILLS.read_text().splitlines():
        e = json.loads(line)
        if e.get("event") != "paper_exit":
            continue
        rt = e.get("round_trip") or {}
        mfe = rt.get("mfe_value")
        exit_cr = rt.get("exit_net_credit")
        entry_db = rt.get("entry_net_debit")
        if mfe is None or exit_cr is None or entry_db is None:
            continue
        qty = int(rt.get("quantity") or e.get("quantity") or 1)
        series = rt.get("mark_series") or []
        out.append({
            "ts": e["ts"], "rec_id": e.get("rec_id", ""), "ticker": e.get("ticker"),
            "reason": e.get("exit_reason") or rt.get("exit_reason"),
            "is_test": "-TEST-" in e.get("rec_id", ""),
            "qty": qty,
            "entry_debit": entry_db,
            "peak_profit": mfe - entry_db,      # best per-contract unrealized profit
            "realized": exit_cr - entry_db,     # actual per-contract result
            "giveback_pc": mfe - exit_cr,       # per-contract peak-to-exit surrender
            "has_path": len(series) >= MIN_PATH_POINTS,
        })
    return out


def fix_readiness(rows):
    """How close we are to being able to TUNE the trailing-stop fix.

    The exit mechanics are identical for test and funnel positions, so ordered
    paths from BOTH are pooled for tuning (2026-07-08 decision) — test paths are
    ~5x more frequent and make the estimate far more stable. The fix stays
    STAGED until the pooled path sample reaches PATH_SAMPLE_TARGET *and* includes
    a choppy week (give-back is worst in chop). The funnel path count is tracked
    separately because the chosen parameter's P&L effect is VALIDATED on funnel
    paths — the evidence set — even though it is TUNED on the pool.
    """
    pooled = [r for r in rows if r["has_path"]]
    funnel = [r for r in pooled if not r["is_test"]]
    return {
        "ordered_path_positions_pooled": len(pooled),
        "ordered_path_positions_funnel": len(funnel),
        "target_pooled": PATH_SAMPLE_TARGET,
        "ready_to_tune": len(pooled) >= PATH_SAMPLE_TARGET,
        "note": "TUNE on pooled (test+funnel) paths; VALIDATE the chosen "
                "parameter on funnel paths only. Ordered paths accrue from "
                "2026-07-08 (mark_series added that day); older exits have "
                "peak/trough only and can only be APPROXIMATELY reconstructed.",
    }


def analyze(rows):
    by_reason = defaultdict(list)
    for r in rows:
        by_reason[r["reason"]].append(r)

    report = {}
    for reason, rs in by_reason.items():
        in_profit = [r for r in rs if r["peak_profit"] > 0]
        winners_turned = [r for r in in_profit if r["realized"] <= 0]
        tot_peak = sum(r["peak_profit"] * 100 * r["qty"] for r in in_profit)
        tot_real = sum(r["realized"] * 100 * r["qty"] for r in in_profit)
        report[reason] = {
            "n": len(rs),
            "n_reached_profit": len(in_profit),
            "n_winners_turned_flat_or_red": len(winners_turned),
            "total_peak_profit_usd": round(tot_peak),
            "total_realized_usd": round(tot_real),
            "aggregate_capture_ratio": round(tot_real / tot_peak, 3) if tot_peak > 0 else None,
            "total_giveback_usd": round(sum(r["giveback_pc"] * 100 * r["qty"] for r in in_profit)),
            "worst_roundtrips": sorted(
                [{"ts": r["ts"][:10], "ticker": r["ticker"],
                  "peak_usd": round(r["peak_profit"] * 100 * r["qty"]),
                  "realized_usd": round(r["realized"] * 100 * r["qty"]),
                  "giveback_usd": round(r["giveback_pc"] * 100 * r["qty"])}
                 for r in winners_turned],
                key=lambda x: x["giveback_usd"], reverse=True)[:5],
        }
    return report


def counterfactual_target_sweep(rows):
    """What a hard profit-lock at level f (as a fraction of entry debit) would
    have captured. A lock that exits when per-contract profit reaches
    f * entry_debit fires for every position whose recorded peak (peak_profit)
    reached that level — exact, since peak_profit is the recorded max. Positions
    that never reached it keep their ACTUAL outcome. A position that ended above
    the lock is capped at the lock (the cost of protection, included in the sum).
    """
    levels = [0.20, 0.30, 0.40, 0.50, 0.75, 1.00]
    baseline = round(sum(r["realized"] * 100 * r["qty"] for r in rows))
    out = {}
    for f in levels:
        total = 0.0
        fired = 0
        for r in rows:
            trigger = f * r["entry_debit"]
            if r["peak_profit"] >= trigger:
                total += trigger * 100 * r["qty"]   # locked at the trigger
                fired += 1
            else:
                total += r["realized"] * 100 * r["qty"]
        out[f"{int(f*100)}pct_of_debit"] = {"policy_pnl_usd": round(total), "n_locked": fired}
    return {"baseline_actual_usd": baseline, "n_positions": len(rows), "levels": out}


def main() -> int:
    rows = _rows()
    funnel = [r for r in rows if not r["is_test"]]
    report = {
        "all": analyze(rows),
        "funnel_only": analyze(funnel),
        "counterfactual_funnel": counterfactual_target_sweep(funnel),
        "fix_readiness": fix_readiness(rows),
        "note": "capture_ratio = realized/peak profit for positions that reached "
                "profit; 1.0 = kept everything. Negative capture on "
                "vwap_invalidation is the give-back scenario (lesson L13).",
    }
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
        return 0

    print("GIVE-BACK BY EXIT REASON (funnel trades only — the evidence set)\n")
    for reason, s in sorted(report["funnel_only"].items()):
        cr = s["aggregate_capture_ratio"]
        cr_s = f"{cr:.0%}" if cr is not None else "n/a"
        print(f"  {reason}: {s['n']} exits, {s['n_reached_profit']} reached profit")
        print(f"     capture ratio {cr_s}  (kept ${s['total_realized_usd']:+} of "
              f"${s['total_peak_profit_usd']:+} peak; gave back ${s['total_giveback_usd']})")
        print(f"     winners that turned flat/red: {s['n_winners_turned_flat_or_red']}")
        for w in s["worst_roundtrips"]:
            print(f"       - {w['ts']} {w['ticker']}: peak ${w['peak_usd']:+} -> "
                  f"exit ${w['realized_usd']:+}  (gave back ${w['giveback_usd']})")

    cf = report["counterfactual_funnel"]
    print(f"\nCOUNTERFACTUAL — hard profit-lock vs actual "
          f"(funnel, n={cf['n_positions']}, actual ${cf['baseline_actual_usd']:+}):")
    for lvl, d in cf["levels"].items():
        print(f"  lock at {lvl:>16}: ${d['policy_pnl_usd']:+}  ({d['n_locked']} positions would lock)")
    fr = report["fix_readiness"]
    print(f"\nFIX READINESS (trailing-stop tuning): "
          f"{fr['ordered_path_positions_pooled']}/{fr['target_pooled']} pooled ordered-path "
          f"positions ({fr['ordered_path_positions_funnel']} funnel) — "
          f"{'READY TO TUNE' if fr['ready_to_tune'] else 'still collecting'}")
    print("\n(all trades incl. execution-test in --json under key 'all')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
