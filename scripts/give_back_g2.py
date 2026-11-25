"""Winner Give-Back Study — G2 aggregation (marks 1-4, 2026-07-23).

Reads gb_v1 winner records + control post-mortems (for truth_mid_entry
needed by the structural/decay split). Trigger-agnostic aggregation was
NOT influenced by criteria — those were frozen first (commit fb8415c).

mark 1: give-back decomposed structural (price-dep: peak - trigger_level)
        vs decay (signal/time: peak - exit; price-dep: sub-target bleed).
        All comparisons in give-back % of max risk, within lane.
mark 2: 3 phantom target_50 exits (truth never reached target) excluded
        from trigger slices; standalone table with truth P&L at that ts.
mark 3: rescue table = identical gb_v1 on MFE>=10% losers (context only).
mark 4: minutes_peak_to_exit distribution for flatten_1545.
Descriptive only. No variant declared. Holds after output.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def truth_entry_map():
    m = {}
    for f in glob.glob(str(ROOT / "logs/post_mortems/controls/*.json")):
        pm = json.load(open(f))
        lc = pm["lifecycle"]
        q = (pm["entry_snapshot"]["recorded"].get("risk") or {}).get("quantity") or 1
        m[pm["meta"]["rec_id"]] = (lc.get("truth_mid_entry"), q)
    return m


def decompose(g, tem):
    risk = g["risk"]["max_dollar_risk"]
    smfe = g["path"]["sustained_mfe_usd"]
    if smfe is None:                       # below-floor: use raw for dollars context
        smfe = g["path"]["raw_mfe_usd"]
    exit_pnl = g["path"]["truth_pnl_at_exit"]
    dep = g["exit"]["dependency"]
    struct = decay = None
    if dep == "price_dependent" and g["exit"]["trigger_level_usd"]:
        e_mid, q = tem.get(g["meta"]["rec_id"], (None, 1))
        if e_mid is not None:
            trig_pnl = (g["exit"]["trigger_level_usd"] - e_mid) * 100 * q
            struct = smfe - trig_pnl        # peaked above the target cap
            decay = trig_pnl - exit_pnl      # sub-target bleed (fill worse than level)
    if struct is None:                       # signal/time: all give-back is decay
        struct, decay = 0.0, smfe - exit_pnl
    pct = lambda x: round(x / risk * 100, 1)
    return {"gross_pct": pct(smfe - exit_pnl),
            "structural_pct": pct(struct), "decay_pct": pct(decay),
            "net_cap_pct": g["give_back"]["net_capturable_pct_of_risk"],
            "eff": g["efficiency"]["value"]}


def dist(vals):
    a = np.array([v for v in vals if v is not None], dtype=float)
    if len(a) == 0:
        return {}
    return {"n": len(a), "median": round(float(np.median(a)), 1),
            "p25": round(float(np.percentile(a, 25)), 1),
            "p75": round(float(np.percentile(a, 75)), 1),
            "mean": round(float(a.mean()), 1)}


def main():
    tem = truth_entry_map()
    wins = [json.load(open(f)) for f in glob.glob(str(ROOT / "logs/give_back/*.json"))]
    phantom = [g for g in wins if g["exit"]["truth_reached_trigger"] is False]
    clean = [g for g in wins if g not in phantom]

    print("=== G2 WINNER GIVE-BACK (138 controls; phantoms excluded from slices) ===\n")
    print(f"(0) frozen criteria commit: fb8415c (pre-aggregation)\n")

    # mark 2: phantom standalone
    print(f"(2) PHANTOM target_50 exits (truth never reached target) — n={len(phantom)}:")
    for g in phantom:
        print(f"    {g['meta']['rec_id'][:40]} | truth P&L @ phantom exit = ${g['path']['truth_pnl_at_exit']} "
              f"| trigger_level ${g['exit']['trigger_level_usd']} | sustained_mfe% {g['path']['sustained_mfe_pct_of_risk']}")

    # mark 1: decomposition by trigger, within lane (SCALP dominant)
    print("\n(1) GIVE-BACK by exit trigger (SCALP, % of max risk, clean set):")
    by_trig = {}
    for g in clean:
        if g["meta"]["lane"] != "SCALP":
            continue
        by_trig.setdefault(g["exit"]["trigger"], []).append(decompose(g, tem))
    print(f"    {'trigger':18} {'n':>3} {'gross%':>18} {'structural%':>13} {'decay%':>13} {'net_cap%':>10}")
    for trig, rows in sorted(by_trig.items(), key=lambda kv: -len(kv[1])):
        g_ = dist([r["gross_pct"] for r in rows])
        s_ = dist([r["structural_pct"] for r in rows])
        d_ = dist([r["decay_pct"] for r in rows])
        nc = dist([r["net_cap_pct"] for r in rows])
        print(f"    {trig:18} {g_['n']:>3}  med {g_['median']:>6} (p25-75 {g_['p25']}-{g_['p75']}) "
              f"struct {s_['median']:>6}  decay {d_['median']:>6}  netcap {nc['median']:>6}")

    # efficiency overall
    effs = [g["efficiency"]["value"] for g in clean if g["efficiency"]["computable"]]
    print(f"\n    exit efficiency (computable n={len(effs)}): "
          f"median {round(float(np.median(effs)),3)} "
          f"(captured fraction of sustained peak)")

    # mark 4: flatten_1545 peak-to-exit timing
    fl = [g for g in clean if g["exit"]["trigger"] == "flatten_1545"]
    p2x = dist([g["path"]["minutes_peak_to_exit"] for g in fl])
    print(f"\n(4) flatten_1545 minutes_peak_to_exit (n={len(fl)}): {p2x}")

    # mark 3: rescue table on losers (context only)
    lf = glob.glob(str(ROOT / "logs/give_back/losers/*.json"))
    if lf:
        losers = [json.load(open(f)) for f in lf]
        rg = dist([decompose(g, tem)["gross_pct"] for g in losers
                   if g["meta"]["lane"] == "SCALP"])
        print(f"\n(3) RESCUE-POTENTIAL table (MFE>=10% SCALP losers, CONTEXT ONLY, n={rg.get('n')}):")
        print(f"    median gross give-back {rg.get('median')}% of risk (p25-75 {rg.get('p25')}-{rg.get('p75')})")
        print("    NOTE: descriptive only; rule economics remain prospective in the shadow tracker.")
    else:
        print("\n(3) RESCUE table: losers run not yet complete.")


if __name__ == "__main__":
    main()
