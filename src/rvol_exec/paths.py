"""Module-level path constants of the RVOL-EXEC lane (spec §0, F7).

Every path the lane reads or writes is a MODULE ATTRIBUTE here so the test
seals (`tests/conftest.py` O-20 and `tests/rvol_exec/conftest.py`) can redirect
each one under tmp with `monkeypatch.setattr(paths, NAME, ...)`. Lane modules
must therefore reference `paths.LEDGER` etc. AT CALL TIME — never
`from src.rvol_exec.paths import LEDGER` (that captures the production path
before the redirect).

Isolation invariant asserted at import: the lane's books live in
`data/lane_rvol/`, a SIBLING of `data/lane/` — `data/lane/*/*.json` is globbed
by the options go/no-go `check_d` and a stock row inside it would disarm the
OPTIONS money path.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# lane root (books, markers, heartbeat) — sibling of data/lane, never inside it
LANE_DIR = ROOT / "data" / "lane_rvol"
SESSIONS_DIR = LANE_DIR / "sessions"
PREP_DIR = LANE_DIR / "prep"
V930_CACHE = LANE_DIR / "v930_history.parquet"
HEARTBEAT = LANE_DIR / "heartbeat.json"
HOLD_FILE = LANE_DIR / "HOLD"
SHADOW_FILE = LANE_DIR / "SHADOW"        # present -> the daemon FORCES --dry-run (rank + would-be orders, zero placement)
FLATTEN_NOW_FILE = LANE_DIR / "FLATTEN_NOW"
UNFLATTENED_FILE = LANE_DIR / "UNFLATTENED"
PID_FILE = LANE_DIR / "rvol_exec.pid"

# shared, READ-ONLY for the lane (same file the options engine honours, F9)
KILL_SWITCH_FILE = ROOT / "data" / "KILL_SWITCH"

# ledger + process log — never logs/agent.log (deadman 3b greps it)
LEDGER = ROOT / "logs" / "observation" / "rvol_exec_paper.jsonl"
LOG_DIR = ROOT / "logs" / "rvol_exec"
GO_NOGO_LOG_DIR = ROOT / "logs" / "ops"

# research stores the nightly prep reads (read-only)
RESEARCH_ORB = ROOT / "data" / "research_orb"
GROUPED_DIR = RESEARCH_ORB / "grouped"
BARS5M_DIR = RESEARCH_ORB / "bars5m"
BARS5M_FRESH_DIR = RESEARCH_ORB / "bars5m_fresh"

# the nightly sim's ledger the comparator reads (read-only)
SLEEVE_LEDGER = ROOT / "logs" / "observation" / "rvolrank_sleeve.jsonl"

_OPTIONS_LANE_DIR = ROOT / "data" / "lane"
assert not LANE_DIR.resolve().is_relative_to(_OPTIONS_LANE_DIR.resolve()), (
    "data/lane_rvol must be a SIBLING of data/lane (options go/no-go globs data/lane/*/*.json)")


def session_dir(date_str: str) -> Path:
    """`data/lane_rvol/sessions/<date>/` — resolved at call time so a
    redirected SESSIONS_DIR is honoured."""
    import src.rvol_exec.paths as _p
    return _p.SESSIONS_DIR / str(date_str)
