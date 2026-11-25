"""Shared runtime control state — a JSON file both the CLI and the agent
loop read/write (CLAUDE.md: keep it simple). Writes are atomic
(tmp + rename) so a half-written file can never be read."""

import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = ROOT / "data" / "agent_state.json"

DEFAULT_STATE = {
    "command": "stop",        # desired: run | pause | stop (written by CLI)
    "status": "stopped",      # actual: running | paused | stopped (written by loop)
    "pid": None,
    "watchlist_override": None,  # None = config default
    "sector": "all",
    "mode": "all",
    "risk_pct": None,            # None = config default
    "cycle_count": 0,
    "last_cycle": None,
    "last_regime": None,
}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return dict(DEFAULT_STATE)
    try:
        return {**DEFAULT_STATE, **json.loads(STATE_FILE.read_text())}
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_STATE)


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def update_state(**kwargs) -> dict:
    state = load_state()
    state.update(kwargs)
    save_state(state)
    return state
