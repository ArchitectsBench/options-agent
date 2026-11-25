"""Recommend-and-confirm workflow — Phase L step 3 (2026-07-02).

The charter's execution model: the agent never fires a trade on its own in
live mode — it posts a fully-specified recommendation, a HUMAN confirms, the
agent executes and manages. This module is the queue between those steps.

Flow (one entry per approved recommendation):
  queue_recommendation()  — pipeline queues instead of submitting; pushes a
                            high-priority notification with the trade spec and
                            the confirm command
  trader confirm <rec_id> — human marks it confirmed (CLI); trader decline
                            <rec_id> / expiry (confirm_expiry_min) close it
  process_queue()         — the agent loop, each cycle: expires stale entries,
                            re-runs the RISK GATE on confirmed ones (conditions
                            can drift between queue and confirm — breaker,
                            caps), then executes via the injected submit
                            function and marks executed. Kill switch blocks
                            execution (never expiry bookkeeping).

Mode routing (config agent.confirm_mode):
  observation + confirm_mode=false (default) — pipeline auto-submits paper
    orders as before; this module is idle. Forward-test throughput must not
    depend on human availability.
  observation + confirm_mode=true — DRILL: funnel recs go through this queue
    and execute as sandbox paper orders on confirmation. Exact live UX,
    rehearsable today.
  live — ALWAYS through this queue (no auto-execute path exists in live);
    execution raises NotImplementedError until the live order-book lifecycle
    ships and LIVE_EXECUTION_BUILT flips (see live_gate.py).

State: data/pending_confirmations.json. Entries keep the FULL rec (trade legs
are needed at execution time). Statuses: pending -> confirmed -> executed,
or pending -> expired | declined, or confirmed -> blocked_by_risk.
"""

import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIRM_FILE = ROOT / "data" / "pending_confirmations.json"

# Serializes the read-modify-write in _transition so two concurrent POSTs
# (e.g. a double-clicked CONFIRM/DECLINE, handled on separate FastAPI threadpool
# threads) can't both read "pending" before either writes. The status guard
# then makes the second call an explicit already-processed no-op. In-process
# only — cross-process races with the scheduler's execute_confirmed are guarded
# separately by that status transition, not by this lock.
_TRANSITION_LOCK = threading.Lock()

logger = logging.getLogger("trader")

DEFAULT_EXPIRY_MIN = 15


def command_topic() -> str | None:
    """ntfy topic for phone-originated confirm/decline commands. Defaults to
    '<NTFY_TOPIC>-cmd' so no extra secret is needed; override with
    NTFY_COMMAND_TOPIC. The per-recommendation token (below) is what makes a
    command trustworthy — the topic name alone is not authorization."""
    explicit = os.environ.get("NTFY_COMMAND_TOPIC")
    base = os.environ.get("NTFY_TOPIC")
    return explicit or (f"{base}-cmd" if base else None)


def _load() -> list[dict]:
    if CONFIRM_FILE.exists():
        try:
            return json.loads(CONFIRM_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def _save(entries: list[dict]):
    CONFIRM_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIRM_FILE.write_text(json.dumps(entries, indent=2, default=str))


def _now() -> datetime:
    return datetime.now()


def queue_recommendation(rec: dict, quantity: int, expiry_min: int = DEFAULT_EXPIRY_MIN) -> dict:
    """Queue an approved recommendation for human confirmation."""
    trade = rec.get("trade", {})
    entry = {
        "rec_id": rec["rec_id"],
        "ticker": rec.get("ticker"),
        "structure": trade.get("structure"),
        "direction": trade.get("direction"),
        "net_debit": trade.get("net_debit"),
        "max_loss": trade.get("max_loss"),
        "quantity": quantity,
        "rec": rec,                     # full spec — needed at execution time
        "status": "pending",
        # one-time token: a phone command is honored only if it carries this,
        # so knowing the command topic name alone cannot authorize a trade
        "confirm_token": secrets.token_hex(6),
        "queued_at": _now().isoformat(timespec="seconds"),
        "expires_at": (_now() + timedelta(minutes=expiry_min)).isoformat(timespec="seconds"),
    }
    entries = [e for e in _load() if e["rec_id"] != rec["rec_id"]]
    entries.append(entry)
    _save(entries)
    logger.info("queued for confirmation: %s (%s x%d, expires %s)",
                rec["rec_id"], entry["ticker"], quantity, entry["expires_at"])
    try:
        from src.engine.notify import send
        actions = None
        topic = command_topic()
        if topic:  # one-tap phone buttons: publish the command back through ntfy
            actions = [
                f"http, CONFIRM x{quantity}, https://ntfy.sh/{topic}, method=POST, "
                f"body=confirm {rec['rec_id']} {entry['confirm_token']}",
                f"http, Decline, https://ntfy.sh/{topic}, method=POST, "
                f"body=decline {rec['rec_id']} {entry['confirm_token']}",
            ]
        send(f"CONFIRM? {entry['ticker']} {entry['structure']}",
             f"{entry['direction']} x{quantity} @ {entry['net_debit']} "
             f"(max loss {entry['max_loss']}/contract)\n"
             f"tap CONFIRM, or: trader confirm {rec['rec_id']}  — "
             f"expires {entry['expires_at'][11:16]}",
             priority="high", tags="bell", actions=actions)
    except Exception:
        pass
    return entry


def _transition(rec_id: str, from_status: str, to_status: str,
                require_unexpired: bool = True) -> str:
    with _TRANSITION_LOCK:
        entries = _load()
        for e in entries:
            if e["rec_id"] != rec_id:
                continue
            if e["status"] == to_status:
                # idempotent duplicate (e.g. double-clicked POST) — no-op
                return f"{rec_id} already {to_status} — no-op (already processed)"
            if e["status"] != from_status:
                return f"{rec_id} is {e['status']}, not {from_status}"
            if require_unexpired and _now() > datetime.fromisoformat(e["expires_at"]):
                e["status"] = "expired"
                _save(entries)
                return f"{rec_id} EXPIRED at {e['expires_at']} — not {to_status}"
            e["status"] = to_status
            e[f"{to_status}_at"] = _now().isoformat(timespec="seconds")
            _save(entries)
            return f"{rec_id} {to_status}"
        return f"{rec_id} not found in confirmation queue"


def confirm(rec_id: str) -> str:
    out = _transition(rec_id, "pending", "confirmed")
    logger.warning("confirmation: %s", out)
    return out


def decline(rec_id: str) -> str:
    return _transition(rec_id, "pending", "declined", require_unexpired=False)


def list_entries(active_only: bool = True) -> list[dict]:
    entries = _load()
    if active_only:
        entries = [e for e in entries if e["status"] in ("pending", "confirmed")]
    return [{k: v for k, v in e.items() if k != "rec"} for e in entries]


def expire_stale() -> list[str]:
    """Mark overdue pending entries expired. Runs every cycle."""
    entries, expired = _load(), []
    for e in entries:
        if e["status"] == "pending" and _now() > datetime.fromisoformat(e["expires_at"]):
            e["status"] = "expired"
            expired.append(e["rec_id"])
            logger.info("confirmation expired unanswered: %s", e["rec_id"])
    if expired:
        _save(entries)
    return expired


def process_queue(cfg: dict, submit_paper_order, mode: str = "observation") -> list[dict]:
    """Agent-loop entry point (each cycle, after exits, before new recs):
    expire stale entries, then execute confirmed ones. Returns events."""
    events = [{"event": "confirmation_expired", "rec_id": r} for r in expire_stale()]
    confirmed = [e for e in _load() if e["status"] == "confirmed"]
    if not confirmed:
        return events

    from src.engine.live_gate import kill_switch_engaged
    ks = kill_switch_engaged()
    if ks:
        logger.warning("kill switch engaged — %d confirmed order(s) NOT executed (%s)",
                       len(confirmed), ks)
        return events + [{"event": "confirmation_held_kill_switch", "rec_id": e["rec_id"]}
                         for e in confirmed]

    from src.engine.earnings import earnings_veto
    from src.engine.risk import gate_new_entry
    entries = _load()
    for e in entries:
        if e["status"] != "confirmed":
            continue
        # earnings announcements can APPEAR between queue and confirm —
        # the hard gate is re-checked before any money moves
        eveto = earnings_veto(e["ticker"], e["rec"]["trade"].get("expiration"))
        if eveto:
            e["status"] = "blocked_by_risk"
            e["blocked_reason"] = f"earnings_veto: {eveto}"
            events.append({"event": "confirmation_blocked_by_risk",
                           "rec_id": e["rec_id"], "reason": e["blocked_reason"]})
            logger.warning("confirmed %s blocked at execution: %s",
                           e["rec_id"], e["blocked_reason"])
            continue
        # risk conditions can drift between queue-time and confirm-time —
        # re-gate (breaker/caps/sizing) before any money moves
        gate = gate_new_entry(e["rec"]["trade"], "funnel", cfg)
        if not gate["allowed"]:
            e["status"] = "blocked_by_risk"
            e["blocked_reason"] = gate["reason"]
            events.append({"event": "confirmation_blocked_by_risk",
                           "rec_id": e["rec_id"], "reason": gate["reason"]})
            logger.warning("confirmed %s blocked at execution by risk gate: %s",
                           e["rec_id"], gate["reason"])
            try:
                from src.engine.notify import send
                send("Confirmed trade BLOCKED by risk gate",
                     f"{e['rec_id']}: {gate['reason']}", priority="high", tags="warning")
            except Exception:
                pass
            continue
        quantity = min(e["quantity"], gate["quantity"])  # never exceed re-gated size

        if mode == "live":
            # deliberately unreachable today: live_gate.LIVE_EXECUTION_BUILT is
            # False until the live order-book lifecycle ships and is reviewed
            raise NotImplementedError(
                "live execution path not built — recommend-and-confirm currently "
                "executes only against the sandbox paper book (drill mode)")
        result = submit_paper_order(e["rec"], quantity=quantity)
        e["status"] = "executed"
        e["executed_at"] = _now().isoformat(timespec="seconds")
        e["order_result"] = {k: v for k, v in (result or {}).items() if k != "response"}
        events.append({"event": "confirmation_executed", "rec_id": e["rec_id"],
                       "quantity": quantity, "result": e["order_result"]})
        logger.warning("confirmed order EXECUTED: %s x%d -> %s",
                       e["rec_id"], quantity, e["order_result"])
    _save(entries)
    return events
