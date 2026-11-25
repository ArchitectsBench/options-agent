#!/usr/bin/env python3
"""L-e MIGRATION — split the shared book into per-lane files. ENGINE-DOWN ONLY.

Before: one paper_positions.json / paper_orders.json for both lanes; isolation
was a rec_id CONVENTION ('-TEST-' => scalp), not storage.
After : data/lane/{funnel,scalp}/positions.json + orders.json — isolation is
        STRUCTURAL. One lane physically cannot read or write the other's book.

Safety: refuses to run with the engine up; backs up both files with a timestamp;
verifies the partition is loss-less (every row lands in exactly one lane, counts
reconcile) before declaring success. Rollback is documented in the report.

Run: .venv/bin/python scripts/ops/migrate_lane_books.py [--apply]
"""
import json, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OLD_POS = ROOT / "data" / "paper_positions.json"
OLD_PEND = ROOT / "data" / "paper_orders.json"
LANE_DIR = ROOT / "data" / "lane"


def lane_of(rec_id): return "scalp" if "-TEST-" in str(rec_id) else "funnel"


def main():
    apply = "--apply" in sys.argv
    # match a PYTHON process actually running the scheduler, not any command
    # line that merely mentions it (an editor/shell writing this file would
    # otherwise false-positive — it did on first run).
    _ps = subprocess.run(["/bin/ps", "-eo", "pid,command"],
                         capture_output=True, text=True).stdout
    _running = [ln for ln in _ps.splitlines()
                if ln.rstrip().endswith("src/scheduler.py")
                and "python" in ln.lower()]
    if _running:
        print("REFUSING: engine is RUNNING — migration is engine-down only."); return 2
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    rpt = {"stamp": stamp, "applied": apply, "lanes": {}}
    for name, src in (("positions", OLD_POS), ("orders", OLD_PEND)):
        rows = json.loads(src.read_text() or "[]") if src.exists() else []
        parts = {"funnel": [], "scalp": []}
        for r in rows:
            parts[lane_of(r.get("rec_id"))].append(r)
        assert len(parts["funnel"]) + len(parts["scalp"]) == len(rows), "row lost"
        rpt["lanes"][name] = {"total": len(rows),
                              "funnel": len(parts["funnel"]),
                              "scalp": len(parts["scalp"])}
        print(f"  {name}: {len(rows)} rows -> funnel {len(parts['funnel'])}, "
              f"scalp {len(parts['scalp'])}")
        if apply:
            if src.exists():
                bk = src.with_suffix(f".json.bak-{stamp}")
                shutil.copy(src, bk); print(f"    backup -> {bk.name}")
            for lane, rws in parts.items():
                d = LANE_DIR / lane; d.mkdir(parents=True, exist_ok=True)
                (d / f"{name}.json").write_text(json.dumps(rws, indent=2, default=str))
                print(f"    wrote data/lane/{lane}/{name}.json ({len(rws)})")
    # verification: per-lane books reconcile to the original totals
    if apply:
        ok = True
        for name in ("positions", "orders"):
            got = sum(len(json.loads((LANE_DIR/l/f"{name}.json").read_text() or "[]"))
                      for l in ("funnel", "scalp"))
            exp = rpt["lanes"][name]["total"]
            print(f"  VERIFY {name}: per-lane sum {got} vs original {exp} "
                  f"{'OK' if got == exp else 'MISMATCH'}")
            ok &= (got == exp)
        rpt["verified"] = ok
        print("\n  ROLLBACK: restore data/paper_*.json from the .bak-%s files and "
              "delete data/lane/ — the engine reads the shared files until "
              "paper_trader is repointed." % stamp)
    (ROOT/"logs"/"ops"/f"lane_migration-{stamp}.json").write_text(
        json.dumps(rpt, indent=1, default=str))
    print(f"\n  report -> logs/ops/lane_migration-{stamp}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
