#!/usr/bin/env python3
"""Lessons audit — the enforcement half of the learn-from-mistakes loop.

Reads docs/lessons.yaml and verifies each lesson's enforcement still exists:
  * test/code/config refs ("path" or "path :: marker") — the file must exist
    and, if a marker is given, contain it. A ref that fails = the lesson's
    protection silently rotted (test renamed, guard deleted, plist rebuilt
    without the flag) — that's a BROKEN finding, exit code 1.
  * process refs are not mechanically checkable; a lesson with ONLY process
    refs is RECALL-ONLY and listed so the weekly review keeps pressure on
    converting it to a test/guard.

Run:  .venv/bin/python scripts/lessons_audit.py [--json]
Wired into the Friday weekly review (weekly_checkin.sh) so rot is caught
within a week, not on the next incident.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "lessons.yaml"

CHECKABLE = {"test", "code", "config"}


def _check_ref(ref: str) -> tuple[bool, str]:
    """Verify 'path' or 'path :: marker' (also 'path::marker'). Returns (ok, detail)."""
    if "::" in ref:
        path_part, marker = (p.strip() for p in ref.split("::", 1))
    else:
        path_part, marker = ref.strip(), None
    fp = ROOT / path_part
    if not fp.is_file():
        return False, f"missing file: {path_part}"
    if marker:
        try:
            text = fp.read_text(errors="replace")
        except OSError as e:
            return False, f"unreadable {path_part}: {e}"
        if marker not in text and marker.lower() not in text.lower():
            return False, f"marker not found in {path_part}: {marker!r}"
    return True, "ok"


def _enforcements(lesson: dict) -> list:
    """Normalise a lesson's `enforcement` field to a list of dicts.

    2026-10-03 fix: four registry entries write `enforcement:` as a bare
    string. Iterating a str yielded characters and `enf.get("type")` raised
    AttributeError, killing the whole audit (identical traceback 09-18, 09-25,
    10-02 — three weekly runs produced nothing). A bare string carries no
    `type`, so it becomes a non-checkable process note rather than being
    dropped silently.
    """
    enf = lesson.get("enforcement") or []
    if isinstance(enf, (str, bytes)):
        enf = [enf]
    elif isinstance(enf, dict):
        enf = [enf]
    out = []
    for item in enf:
        out.append(item if isinstance(item, dict) else {"type": None, "ref": str(item)})
    return out


def audit() -> dict:
    reg = yaml.safe_load(REGISTRY.read_text())
    results = []
    for lesson in reg.get("lessons", []):
        checks = []
        for enf in _enforcements(lesson):
            if enf.get("type") in CHECKABLE:
                ok, detail = _check_ref(enf.get("ref", ""))
                checks.append({"type": enf["type"], "ref": enf.get("ref"),
                               "ok": ok, "detail": detail})
            else:
                checks.append({"type": enf.get("type"), "ref": enf.get("ref"),
                               "ok": None, "detail": "process (not checkable)"})
        mechanical = [c for c in checks if c["ok"] is not None]
        if mechanical and all(c["ok"] for c in mechanical):
            status = "ENFORCED"
        elif mechanical:
            status = "BROKEN"
        else:
            status = "RECALL-ONLY"
        flagged = lesson.get("recall_only", False)
        results.append({"id": lesson["id"], "status": status,
                        "recall_only_flag": flagged, "checks": checks,
                        "flag_mismatch": (status == "RECALL-ONLY") != flagged})
    summary = {s: sum(1 for r in results if r["status"] == s)
               for s in ("ENFORCED", "BROKEN", "RECALL-ONLY")}
    return {"summary": summary, "lessons": results,
            "ok": summary["BROKEN"] == 0}


def main() -> int:
    out = audit()
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
    else:
        s = out["summary"]
        print(f"LESSONS AUDIT: {s['ENFORCED']} enforced, "
              f"{s['BROKEN']} BROKEN, {s['RECALL-ONLY']} recall-only")
        for r in out["lessons"]:
            if r["status"] == "BROKEN":
                bad = [c["detail"] for c in r["checks"] if c["ok"] is False]
                print(f"  BROKEN   {r['id']}: {'; '.join(bad)}")
            elif r["status"] == "RECALL-ONLY":
                print(f"  RECALL   {r['id']} (no mechanical enforcement)")
            if r["flag_mismatch"]:
                print(f"  WARN     {r['id']}: recall_only flag disagrees with audit")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
