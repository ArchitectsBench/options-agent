#!/usr/bin/env python3
"""EOD lifecycle: expire UNACTIONED recommendations (2026-08-07).

A recommendation that was never confirmed or declined during its session is no
longer actionable — the market it was computed against is gone. It is marked
`expired_eod` and ARCHIVED. NOTHING IS DELETED: the row moves to
data/archive/expired_confirmations-<date>.jsonl so the decision history stays
complete for evidence purposes.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PENDING = ROOT / "data" / "pending_confirmations.json"
ARCHIVE = ROOT / "data" / "archive"


def main() -> int:
    apply = "--apply" in sys.argv
    today = f"{datetime.now():%Y-%m-%d}"
    rows = json.loads(PENDING.read_text() or "[]") if PENDING.exists() else []
    stale = [r for r in rows if str(r.get("status")) == "pending"]
    keep = [r for r in rows if str(r.get("status")) != "pending"]
    print(f"  pending rows: {len(rows)} | unactioned to expire: {len(stale)}")
    if not stale or not apply:
        if not apply:
            print("  (dry run — pass --apply)")
        return 0
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    out = ARCHIVE / f"expired_confirmations-{today}.jsonl"
    with out.open("a") as f:
        for r in stale:
            r["status"] = "expired_eod"
            r["expired_at"] = datetime.now().isoformat(timespec="seconds")
            r["NOT_GATE_EVIDENCE"] = True
            f.write(json.dumps(r, default=str) + "\n")
    PENDING.write_text(json.dumps(keep, indent=2, default=str))
    print(f"  archived {len(stale)} -> {out.name}; pending now {len(keep)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
