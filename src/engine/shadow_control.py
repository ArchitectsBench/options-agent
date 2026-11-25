"""SHADOW ABORT SWITCH — surgical halt of the shadow (IBKR) path only.

Deliberately distinct from the kill switch:

  data/KILL_SWITCH     -> halts EVERYTHING (live submits included). Operator's
                          blunt instrument. Exit management stays active.
  data/SHADOW_DISABLE  -> halts ONLY shadow submits. The live Tradier path is
                          completely untouched and keeps trading normally.

Rationale: the shadow path is the NEW, least-proven component in the system.
It must be removable mid-session without collateral damage to the live book —
otherwise the only way to stop a misbehaving shadow is the kill switch, which
would stop live trading too and make "just disable the new thing" cost a
trading day.

Same disk-authority pattern as the kill switch: a file on disk, re-read every
cycle (never cached), so it can be flipped by hand, by the pre-session go/no-go,
or by any operator tooling without touching the running process.

Semantics (frozen 2026-08-04):
  * Checked by the shadow loop EVERY cycle; takes effect on the NEXT cycle.
  * Every suppressed shadow submit is logged with the reason.
  * It suppresses SUBMITS. Shadow reconciliation/observation may continue so the
    book does not silently drift while disabled — a shadow order already live is
    still tracked, it just gets no new company.
  * Fail-safe: an unreadable/ambiguous flag file counts as DISABLED. When in
    doubt, the new component stops.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHADOW_DISABLE_FILE = ROOT / "data" / "SHADOW_DISABLE"

logger = logging.getLogger("trader")


def shadow_disabled() -> str | None:
    """Reason string when the shadow path is disabled, None when armed.

    Fail-safe: any error reading the flag returns a reason (disabled), never
    None — an unreadable flag must not silently re-arm the newest component.
    """
    try:
        if SHADOW_DISABLE_FILE.exists():
            try:
                return (SHADOW_DISABLE_FILE.read_text().strip()
                        or "shadow disabled (no reason recorded)")
            except OSError:
                return "shadow disabled (flag unreadable)"
    except OSError:
        return "shadow disabled (flag state indeterminate)"
    return None


def disable_shadow(reason: str, source: str = "manual") -> str:
    """Write the abort flag. Idempotent; refreshes the reason if already set."""
    SHADOW_DISABLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    detail = (f"{reason} [source={source}, "
              f"at={datetime.now().isoformat(timespec='seconds')}]")
    SHADOW_DISABLE_FILE.write_text(detail)
    logger.warning("SHADOW DISABLED: %s", detail)
    return detail


def enable_shadow() -> bool:
    """Clear the abort flag (re-arm). True if a flag was actually removed."""
    if SHADOW_DISABLE_FILE.exists():
        SHADOW_DISABLE_FILE.unlink()
        logger.warning("SHADOW RE-ARMED (SHADOW_DISABLE cleared)")
        return True
    return False


def guard_shadow_submit(intent_desc: str = "") -> str | None:
    """Call immediately before EVERY shadow submit.

    Returns None when the submit may proceed; otherwise returns the reason and
    logs the suppression (the frozen semantics require every suppressed shadow
    submit to be logged with its reason).

    Checks BOTH switches: the kill switch halts everything (so it halts shadow
    too — explicitly required), and SHADOW_DISABLE halts shadow alone.
    """
    try:
        from src.engine.live_gate import kill_switch_engaged
        ks = kill_switch_engaged()
    except Exception as exc:  # noqa: BLE001 - fail safe: unknown kill state = stop
        ks = f"kill state unreadable ({exc})"
    if ks:
        reason = f"KILL SWITCH engaged: {ks}"
        logger.warning("shadow submit SUPPRESSED (%s) — %s", reason, intent_desc)
        return reason
    sd = shadow_disabled()
    if sd:
        logger.warning("shadow submit SUPPRESSED (SHADOW_DISABLE) — %s | %s",
                       sd, intent_desc)
        return sd
    return None
