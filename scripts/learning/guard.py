"""CLL write-guard — permissions are structural, not conventional.

Each tier may write ONLY its allow-listed ledgers under logs/learning/. A
cross-tier write raises PermissionError and aborts the run. This is what makes
"nightly MAY NOT propose" a property of the code, not a promise.
Spec: docs/continuous_learning_loop_spec.md §10 (G7).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEARN = ROOT / "logs" / "learning"

# top-level file or subdir each tier may write under logs/learning/
TIER_WRITES = {
    "nightly": {"classified_ledger.jsonl", "classified_quarantine.jsonl",
                "quarantine_resolved.jsonl", "integrity_alarms.jsonl", "integrity_hold.flag"},
    "weekly":  {"weekly", "hypothesis_registry.jsonl"},   # registry: queue/register only
    "round":   {"hypothesis_registry.jsonl", "graveyard.jsonl",
                "multiplicity.jsonl", "rounds"},
    "adoption": {"adopted_rules_monitor.jsonl"},
    "repin":   {"baselines", "ACTIVE", "repin_state.json"},
    "seed":    {"baselines", "ACTIVE", "repin_state.json", "classified_ledger.jsonl",
                "quarantine_resolved.jsonl",
                "hypothesis_registry.jsonl", "graveyard.jsonl", "adopted_rules_monitor.jsonl"},
}


def guarded_path(tier: str, name: str) -> Path:
    """Return logs/learning/<name>, asserting `tier` is allowed to write it."""
    allowed = TIER_WRITES.get(tier, set())
    top = name.split("/", 1)[0]
    if name not in allowed and top not in allowed:
        raise PermissionError(
            f"CLL guard: tier '{tier}' may not write '{name}' "
            f"(allowed: {sorted(allowed)})")
    p = LEARN / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def append_jsonl(tier: str, name: str, row: dict) -> None:
    import json
    with guarded_path(tier, name).open("a") as f:
        f.write(json.dumps(row) + "\n")
