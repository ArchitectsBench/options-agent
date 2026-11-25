"""G2 extension (mark 2): post-exit truth continuation for target_50 exits.

For the 25 clean + 3 phantom target_50 winners, fetch the structure's
truth net-mid path from exit_ts -> EOD (16:00 ET, same session) and report
max favorable / max adverse move vs the EXIT level, and the EOD level.
This is the evidence a looser-target hypothesis actually requires (the
in-hold clamped paths cannot show post-target continuation). Descriptive.
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import importlib
gbe = importlib.import_module("give_back_engine")
enr = gbe.enr


def main():
    legsmap = enr.fallback_legs_map()
    pm = {json.load(open(f))["meta"]["rec_id"]: json.load(open(f))
          for f in glob.glob(str(ROOT / "logs/post_mortems/controls/*.json"))}
    targets = [json.load(open(f)) for f in glob.glob(str(ROOT / "logs/give_back/*.json"))
               if json.load(open(f))["exit"]["trigger"] == "target_50"]
    rows = []
    for g in targets:
        rid = g["meta"]["rec_id"]
        legs = legsmap.get(rid)
        p = pm.get(rid)
        if not legs or not p:
            continue
        lc = p["lifecycle"]
        exit_ts = lc.get("exit_ts", "")
        q = (p["entry_snapshot"]["recorded"].get("risk") or {}).get("quantity") or 1
        risk = g["risk"]["max_dollar_risk"]
        day = exit_ts[:10]
        res = gbe.leg_series(legs, [day])
        if res is None:
            continue
        srows, _ = res
        xk = f"{day}T{enr.pt_to_et_minute(exit_ts)}"
        post = [(k, v["net_mid"]) for k, v in sorted(srows.items())
                if k != "__legmed__" and k > xk]
        if len(post) < 2:
            rows.append({"rid": rid, "phantom": g["exit"]["truth_reached_trigger"] is False,
                         "note": "no_post_exit_path"})
            continue
        mids = np.array([m for _, m in post])
        exit_mid = None
        # exit-level structure mid: use the give_back exit P&L back-solved, or last in-hold
        exit_pnl = g["path"]["truth_pnl_at_exit"]
        e_mid = lc.get("truth_mid_entry")
        exit_mid = e_mid + exit_pnl / (100 * q) if e_mid is not None else mids[0]
        favor = (mids.max() - exit_mid) * 100 * q
        adverse = (mids.min() - exit_mid) * 100 * q
        eod = (mids[-1] - exit_mid) * 100 * q
        rows.append({"rid": rid, "phantom": g["exit"]["truth_reached_trigger"] is False,
                     "risk": risk, "post_n": len(post),
                     "max_favorable_pct": round(favor / risk * 100, 1),
                     "max_adverse_pct": round(adverse / risk * 100, 1),
                     "eod_vs_exit_pct": round(eod / risk * 100, 1)})
    Path(ROOT / "logs/give_back/postexit_target50.json").write_text(json.dumps(rows, indent=1))
    ok = [r for r in rows if "max_favorable_pct" in r]
    def med(key):
        a = np.array([r[key] for r in ok], dtype=float)
        return round(float(np.median(a)), 1), round(float(np.percentile(a, 25)), 1), round(float(np.percentile(a, 75)), 1)
    print(f"post-exit continuation for target_50 (n={len(ok)}; phantoms={sum(1 for r in ok if r['phantom'])}):")
    for key, lbl in (("max_favorable_pct", "max FAVORABLE (more upside a looser target could catch)"),
                     ("max_adverse_pct", "max ADVERSE (give-back a looser target risks)"),
                     ("eod_vs_exit_pct", "EOD vs exit level")):
        m = med(key)
        print(f"  {lbl}: median {m[0]}% (p25-75 {m[1]}-{m[2]})")
    up = sum(1 for r in ok if r["eod_vs_exit_pct"] > 0)
    print(f"  ended ABOVE exit at EOD: {up}/{len(ok)} | BELOW: {len(ok)-up}/{len(ok)}")
    print(f"  no-post-path: {sum(1 for r in rows if 'note' in r)} (likely 0DTE expiring at/after exit)")


if __name__ == "__main__":
    main()
