"""Phone-originated confirm/decline commands via ntfy (2026-07-02).

The CONFIRM? notification carries one-tap action buttons that publish
"confirm <rec_id> <token>" / "decline <rec_id> <token>" to the private
command topic (confirm.command_topic()). The agent polls that topic every
~30s while confirmations are pending and applies verified commands.

Security model: the command topic name is private but NOT authorization —
the one-time per-recommendation token generated at queue time is. A command
with a wrong/missing token is rejected and alerted, never applied. Tokens
travel only inside the user's own ntfy notification.

State: data/ntfy_cmd_state.json tracks the last-seen ntfy message id so
commands are processed exactly once across restarts. First poll anchors to
'now' so stale topic history is never replayed.
"""

import json
import logging
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = ROOT / "data" / "ntfy_cmd_state.json"

logger = logging.getLogger("trader")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def _fetch_messages(topic: str, since: str) -> list[dict]:
    url = f"https://ntfy.sh/{topic}/json?poll=1&since={since}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        lines = resp.read().decode().splitlines()
    out = []
    for line in lines:
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("event") == "message":
            out.append(m)
    return out


def parse_command(body: str) -> tuple[str, str, str] | None:
    """'confirm <rec_id> <token>' -> (action, rec_id, token); else None."""
    parts = (body or "").strip().split()
    if len(parts) == 3 and parts[0] in ("confirm", "decline"):
        return parts[0], parts[1], parts[2]
    return None


def apply_command(action: str, rec_id: str, token: str) -> dict:
    """Verify the token against the queue entry, then apply. Fail-closed."""
    from src.engine import confirm as cf
    entry = next((e for e in cf._load() if e["rec_id"] == rec_id), None)
    if entry is None:
        return {"applied": False, "detail": f"{rec_id} not in queue"}
    expected = entry.get("confirm_token")
    if not expected or token != expected:
        logger.warning("REJECTED %s command for %s: bad token", action, rec_id)
        try:
            from src.engine.notify import send
            send("Rejected phone command", f"{action} {rec_id}: token mismatch — "
                 "not applied", priority="high", tags="warning")
        except Exception:
            pass
        return {"applied": False, "detail": "token mismatch"}
    out = cf.confirm(rec_id) if action == "confirm" else cf.decline(rec_id)
    applied = out.endswith(("confirmed", "declined"))
    try:
        from src.engine.notify import send
        send(f"{action.capitalize()} received: {entry['ticker']}",
             out + ("" if not applied else
                    " — executing within ~30s" if action == "confirm" else ""),
             tags="white_check_mark" if applied else "warning")
    except Exception:
        pass
    return {"applied": applied, "detail": out}


def poll_and_apply() -> list[dict]:
    """One poll pass. Cheap no-ops: no command topic, or nothing pending."""
    from src.engine import confirm as cf
    topic = cf.command_topic()
    if not topic:
        return []
    if not any(e["status"] == "pending" for e in cf._load()):
        return []

    state = _load_state()
    since = state.get("last_id")
    if not since:  # first ever poll: anchor to now, never replay old history
        _save_state({"last_id": str(int(datetime.now().timestamp()))})
        since = _load_state()["last_id"]
    try:
        messages = _fetch_messages(topic, since)
    except Exception as exc:
        logger.warning("ntfy command poll failed: %s", exc)
        return []

    events = []
    for m in messages:
        state["last_id"] = m.get("id") or state.get("last_id")
        cmd = parse_command(m.get("message", ""))
        if cmd is None:
            continue
        result = apply_command(*cmd)
        events.append({"event": "phone_command", "action": cmd[0],
                       "rec_id": cmd[1], **result})
    _save_state(state)
    return events
