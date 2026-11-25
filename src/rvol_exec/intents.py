"""Write-ahead intents (spec §10) — a COPY of paper_trader's
`submit_paper_order` / `_bind_intent` / `_drop_intent` pattern, never an
import. Invariant: **intent written -> POST -> bind ids -> ledger event.
Never POST without a written intent.** The file is `intents.json` in the
session dir; every write is `atomic_save`.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.util.atomic import atomic_save


def load(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "[]")
    except (json.JSONDecodeError, OSError):
        return []
    return list(data) if isinstance(data, list) else []


def save(path: Path, intents: list[dict]) -> None:
    atomic_save(Path(path), json.dumps(intents, indent=1, default=str))


def add(path: Path, intent: dict) -> dict:
    """Append a new intent with the next `intent_seq`; written BEFORE any POST."""
    items = load(path)
    seq = 1 + max([int(i.get("intent_seq") or 0) for i in items] or [0])
    rec = {"intent_seq": seq, "order_id": None, "perm_id": None, "child_order_id": None,
           "child_perm_id": None, "bound": False}
    rec.update(intent)
    items.append(rec)
    save(path, items)
    return rec


def bind(path: Path, intent_seq: int, **ids) -> dict | None:
    items = load(path)
    hit = None
    for i in items:
        if int(i.get("intent_seq") or 0) == int(intent_seq):
            i.update({k: v for k, v in ids.items()})
            i["bound"] = True
            hit = i
    if hit is not None:
        save(path, items)
    return hit


def drop(path: Path, intent_seq: int) -> bool:
    items = load(path)
    keep = [i for i in items if int(i.get("intent_seq") or 0) != int(intent_seq)]
    if len(keep) == len(items):
        return False
    save(path, keep)
    return True


def unbound(path: Path) -> list[dict]:
    return [i for i in load(path) if not i.get("bound") and i.get("order_id") is None]
