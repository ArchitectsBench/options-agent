#!/usr/bin/env python3
"""Restart the cockpit API when it is running code older than the code it serves.

Lesson L14: a long-running process runs the code it was STARTED with. The
cockpit API has been found stale on 2026-09-29, 10-02, 10-05, 10-08 and
2026-10-28 — every time it was fixed by a HUMAN (or an autopilot run) typing
`launchctl kickstart`, which is why it kept recurring unseen. This script is
the automatic hand: it reads the existing `ops_checks.stale_code()` verdict and
kickstarts `com.optionsagent.cockpitapi` when that verdict says the running pid
predates the newest commit touching the code it imports.

Deliberately NOT narrowed to `src/api/` + `ui/` (suggested 2026-10-08, rejected
2026-10-28): the original L14 catch was the cockpit serving four-day-old
*evidence math* out of `scripts/`, which a narrowed path set would stop
detecting. With an automatic restart the asymmetry decides it — a false
positive costs one harmless post-close kickstart, a false negative costs
another week of a stale UI. So `stale_code()` is read as-is and not touched.

Read-only except for the kickstart itself and a dated receipt
(`logs/ops/cockpit_staleness-<date>.json`), so a later run can verify what
happened without re-deriving it.

Exit codes: 0 = fresh, or stale and successfully kickstarted; 1 = stale and the
kickstart failed; 2 = could not measure (no pid / unreadable state). The
caller in the nightly chain must guard this under `if` so a non-zero here
cannot kill the rest of the chain.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "com.optionsagent.cockpitapi"


def _default_stale_fn():
    sys.path.insert(0, str(ROOT / "scripts"))
    import ops_checks  # noqa: E402

    return ops_checks.stale_code()


def _default_kickstart(label: str) -> tuple[int, str]:
    """Returns (returncode, combined output). Never raises."""
    target = f"gui/{os.getuid()}/{label}"
    try:
        p = subprocess.run(["launchctl", "kickstart", "-k", target],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:  # pragma: no cover - defensive
        return 1, f"{type(exc).__name__}: {exc}"


def evaluate(stale_fn=None, kickstart=None, now=None, root=None, dry_run=False):
    """Decide, act, and return the receipt dict. Never raises."""
    stale_fn = stale_fn or _default_stale_fn
    kickstart = kickstart or _default_kickstart
    now = now or datetime.now()
    root = Path(root) if root else ROOT

    receipt = {
        "at": now.isoformat(timespec="seconds"),
        "label": LABEL,
        "dry_run": bool(dry_run),
        "state": "unmeasurable",
        "pid": None,
        "proc_started": None,
        "latest_code_commit": None,
        "kickstarted": False,
        "kickstart_rc": None,
        "kickstart_output": None,
        "detail": None,
    }

    try:
        verdict = stale_fn() or {}
    except Exception as exc:
        receipt["detail"] = f"stale_code() raised: {type(exc).__name__}: {exc}"
        _write(receipt, now, root)
        return receipt

    row = (verdict.get("cockpitapi") or {}) if isinstance(verdict, dict) else {}
    receipt["pid"] = row.get("pid")
    receipt["proc_started"] = row.get("proc_started")
    receipt["latest_code_commit"] = row.get("latest_code_commit")
    stale = row.get("stale")

    if stale is True:
        if dry_run:
            receipt["state"] = "stale"
            receipt["detail"] = "stale; dry-run, no kickstart attempted"
        else:
            rc, out = kickstart(LABEL)
            receipt["kickstart_rc"] = rc
            receipt["kickstart_output"] = out or None
            receipt["kickstarted"] = rc == 0
            receipt["state"] = "restarted" if rc == 0 else "kickstart_failed"
            receipt["detail"] = (
                f"pid {row.get('pid')} started {row.get('proc_started')} predates code "
                f"{row.get('latest_code_commit')}"
            )
    elif stale is False:
        receipt["state"] = "fresh"
        receipt["detail"] = (
            f"pid {row.get('pid')} started {row.get('proc_started')} is at or after code "
            f"{row.get('latest_code_commit')}"
        )
    else:
        receipt["detail"] = (
            "stale_code() returned no cockpitapi verdict (service not running, or "
            "process start time unreadable) — not a staleness breach"
        )

    _write(receipt, now, root)
    return receipt


def _write(receipt: dict, now: datetime, root: Path) -> None:
    try:
        out = root / "logs" / "ops" / f"cockpit_staleness-{now.strftime('%Y-%m-%d')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(receipt, indent=1) + "\n")
        receipt["receipt_path"] = str(out)
    except Exception as exc:  # pragma: no cover - defensive
        receipt["receipt_write_error"] = f"{type(exc).__name__}: {exc}"


def exit_code(receipt: dict) -> int:
    return {"fresh": 0, "restarted": 0, "stale": 0, "kickstart_failed": 1}.get(
        receipt.get("state"), 2)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    receipt = evaluate(dry_run=dry_run)
    print(f"[cockpit-stale] {receipt['state']}: {receipt['detail']}")
    return exit_code(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
