"""Live-mode gate — Phase L step 2 (2026-07-02, roadmap-to-live).

THE gate the charter mandates: switching from observation to live mode is
impossible unless EVERY condition below proves out at startup. On failure the
agent REFUSES to start (LiveModeRefused) — it does not warn-and-continue.

Conditions for agent.mode: live (config.yaml):
  1. >= 1 Stage-6-validated strategy in config/validated_strategies.yaml
     (promoter.load_validated_strategies — the only source of truth)
  2. forward acceptance PASS — the frozen config/forward_acceptance.yaml
     criteria, evaluated by scripts/forward_acceptance_check.evaluate
     (two-stage promotion, CLAUDE.md 2026-07-01 amendment)
  3. kill switch NOT engaged (data/KILL_SWITCH absent)
  4. live credentials present (TRADIER_LIVE_TOKEN / TRADIER_LIVE_ACCOUNT_ID)
     and the live account id is DISTINCT from the sandbox account id
  5. the live execution layer exists — it deliberately does NOT yet
     (LIVE_EXECUTION_BUILT = False until recommend-and-confirm is built and
     reviewed), so live mode is structurally impossible even if 1-4 held.

Every check is fail-closed: an error evaluating a condition counts as unmet.

Kill switch (independent of mode): data/KILL_SWITCH. When the file exists the
pipeline places NO NEW orders of any kind — funnel or execution-test —
checked EVERY cycle, so it can be engaged mid-session (`trader kill-switch
on`). Exits and position management are NEVER blocked: the switch stops
digging, it must not prevent flattening.
"""

import json
import os
import logging
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KILL_SWITCH_FILE = ROOT / "data" / "KILL_SWITCH"

logger = logging.getLogger("trader")

# The recommend-and-confirm WORKFLOW shipped 2026-07-02 (src/engine/confirm.py,
# drill-able on paper via agent.confirm_mode). This constant still flips ONLY
# when the LIVE order-book lifecycle ships (fills/positions/exits through
# tradier_live.LiveOrderClient against a separate live book) AND is reviewed —
# the remaining deliberate step, taken once forward acceptance is within reach.
LIVE_EXECUTION_BUILT = False


class LiveModeRefused(RuntimeError):
    """Raised at startup when live mode is requested but the gate fails."""


def kill_switch_engaged() -> str | None:
    """Reason string when engaged, None otherwise."""
    if KILL_SWITCH_FILE.exists():
        try:
            return KILL_SWITCH_FILE.read_text().strip() or "engaged (no reason recorded)"
        except OSError:
            return "engaged (unreadable)"
    return None


def engage_kill_switch(reason: str = "") -> str:
    KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    detail = json.dumps({"engaged_at": datetime.now().isoformat(timespec="seconds"),
                         "reason": reason or "manual"})
    KILL_SWITCH_FILE.write_text(detail)
    logger.warning("KILL SWITCH ENGAGED: %s", detail)
    return detail


def release_kill_switch() -> bool:
    if KILL_SWITCH_FILE.exists():
        KILL_SWITCH_FILE.unlink()
        logger.warning("kill switch released")
        return True
    return False


def _check_validated_strategies() -> tuple[bool, str]:
    try:
        from src.research.promoter import load_validated_strategies
        strategies = load_validated_strategies()
    except Exception as exc:  # fail-closed
        return False, f"validated_strategies unreadable ({exc})"
    if not strategies:
        return False, "no Stage-6-validated strategy in config/validated_strategies.yaml"
    ids = [s.get("id") or s.get("family", "?") for s in strategies]
    return True, f"{len(strategies)} validated: {ids}"


def _check_forward_acceptance() -> tuple[bool, str]:
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from scripts.forward_acceptance_check import evaluate
        result = evaluate(ROOT)
    except Exception as exc:  # fail-closed
        return False, f"forward-acceptance evaluation failed ({exc})"
    overall = result.get("overall", {})
    ok = bool(overall.get("pass"))
    return ok, overall.get("summary", "no summary")


def _check_live_credentials() -> tuple[bool, str]:
    token = os.environ.get("TRADIER_LIVE_TOKEN")
    account = os.environ.get("TRADIER_LIVE_ACCOUNT_ID")
    if not token or not account:
        return False, "TRADIER_LIVE_TOKEN / TRADIER_LIVE_ACCOUNT_ID not set"
    if account == os.environ.get("TRADIER_SANDBOX_ACCOUNT_ID"):
        return False, "live account id equals the SANDBOX account id — refusing"
    return True, "live credentials present and distinct from sandbox"


def resolve_mode(cfg: dict | None, strict: bool = True) -> dict:
    """Resolve the agent mode from config against the gate.

    strict=True (startup): a requested-but-unmet 'live' RAISES LiveModeRefused
    — the agent refuses to start, per charter.
    strict=False (per-cycle): returns observation with the failed checks
    attached, for a loud per-cycle warning when config was flipped mid-run
    (a restart through the strict gate is required to change modes).
    """
    requested = ((cfg or {}).get("agent") or {}).get("mode", "observation")
    if requested not in ("observation", "live"):
        raise LiveModeRefused(f"unknown agent.mode {requested!r} — valid: observation | live")
    if requested == "observation":
        return {"mode": "observation", "requested": requested, "checks": []}

    ks = kill_switch_engaged()
    checks = [
        ("validated_strategy", *_check_validated_strategies()),
        ("forward_acceptance", *_check_forward_acceptance()),
        ("kill_switch_clear", ks is None, ks or "clear"),
        ("live_credentials", *_check_live_credentials()),
        ("live_execution_layer", LIVE_EXECUTION_BUILT,
         "built" if LIVE_EXECUTION_BUILT else
         "NOT BUILT — recommend-and-confirm (Phase L step 3) must ship and be reviewed first"),
    ]
    failed = [(name, detail) for name, ok, detail in checks if not ok]
    if failed:
        summary = "; ".join(f"{name}: {detail}" for name, detail in failed)
        if strict:
            raise LiveModeRefused(
                f"live mode REFUSED — {len(failed)}/{len(checks)} gate condition(s) unmet: {summary}")
        logger.warning("agent.mode=live requested mid-run but gate unmet (%s) — "
                       "staying in observation; restart required to change modes", summary)
        return {"mode": "observation", "requested": requested,
                "checks": checks, "refused": summary}
    return {"mode": "live", "requested": requested, "checks": checks}
