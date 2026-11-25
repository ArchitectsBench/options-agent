"""CLL integrity-hold clear — HUMAN ONLY. Requires a logged reason string.

Removes integrity_hold.flag, records the reason (+ optional commit ref) to
integrity_alarms.jsonl, and re-runs nightly to re-adjudicate the quarantined
batch (which commits if the invariant now passes). No reason -> refuses.
Spec §6 / decision #4. Run: clear_hold.py --reason "<why>" [--commit <ref>]
"""
from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "learning"))
guard = importlib.import_module("guard")
LEARN = ROOT / "logs" / "learning"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reason", required=True, help="why the hold is cleared / rows discarded (logged)")
    ap.add_argument("--commit", default=None, help="optional commit ref for the fix")
    ap.add_argument("--discard-outstanding", action="store_true",
                    help="drain-DISCARD: mark all outstanding quarantined rows as discarded "
                         "(with reason) so nightly stops rebuilding them. Use for irreparably "
                         "bad data. Default (no flag) is the PROMOTE path: clear + re-run.")
    ap.add_argument("--no-rerun", action="store_true")
    args = ap.parse_args()
    if not args.reason.strip():
        raise SystemExit("refused: --reason must be a non-empty string")
    now = datetime.now().isoformat(timespec="seconds")
    night = importlib.import_module("nightly")

    # drain-DISCARD path
    if args.discard_outstanding:
        outstanding = night._quarantine_depth()
        for rid, cohort in outstanding:
            guard.append_jsonl("nightly", "quarantine_resolved.jsonl",
                               {"date": now[:10], "rec_id": rid, "cohort": cohort,
                                "resolution": "discarded", "reason": args.reason, "commit_ref": args.commit})
        print(f"discarded {len(outstanding)} outstanding quarantined rows (reason logged).")

    # clear the hold flag if present
    flag = LEARN / "integrity_hold.flag"
    if flag.exists():
        flag.unlink()
        guard.append_jsonl("nightly", "integrity_alarms.jsonl",
                           {"date": now[:10], "run": "clear_hold", "status": "CLEARED",
                            "reason": args.reason, "commit_ref": args.commit,
                            "discarded_outstanding": bool(args.discard_outstanding)})
        print(f"integrity_hold cleared. reason logged: {args.reason!r}"
              + (f" (commit {args.commit})" if args.commit else ""))
    elif not args.discard_outstanding:
        print("no active integrity_hold — nothing to clear.")
        return

    if not args.no_rerun:
        print("re-running nightly to re-adjudicate (promote what passes; discarded stay out)...")
        night.main()


if __name__ == "__main__":
    main()
