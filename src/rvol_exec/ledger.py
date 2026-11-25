"""Append-only jsonl ledger of the RVOL-EXEC lane (spec §15.1).
UNVALIDATED — PAPER ONLY on every line.

Required on every record: `ts_et`, `date`, `seq` (monotonic per process; a
restart CONTINUES from the last seq found in the file for that date), `pid`,
`sha`, `run_id`, `lane`, `event`, `stamp`, `criteria`. Only the canonical
event names in `EVENTS` may be emitted — `emit` raises on anything else so a
typo can never create a second vocabulary (D19). The file path is read from
`src.rvol_exec.paths.LEDGER` at call time (F7 redirect).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from pathlib import Path

import src.rvol_exec.paths as paths
from src.rvol_exec.constants import STAMP
from src.util.atomic import read_jsonl

logger = logging.getLogger("rvol_exec")

LANE = "rvol_exec"
CRITERIA = "docs/rvol_exec_lane.md#20-frozen-forward-criteria-draft"

EVENTS = frozenset({
    # session
    "session_start", "session_noop", "session_skipped", "restart", "late_start",
    "session_closed", "unflattened", "clock_jump", "heartbeat_gap",
    # data
    "snapshot_polled", "snapshot_error", "t_ready", "t_ready_timeout", "rank",
    "name_skipped", "quote_degraded",
    # orders
    "order_intent", "order_placed", "order_ambiguous", "order_bound", "intent_dropped",
    "order_rejected", "short_unavailable", "entry_cancelled", "guardrail_breached",
    "first10_full", "slot_overflow", "stop_amended", "stop_amend_failed",
    "stop_requantified", "stop_replaced", "stop_replace_failed", "stop_missing_replaced",
    "stop_missing_flattened", "flatten_placed", "flatten_failed", "orphan_order_cancelled",
    "reverse_position_closed",
    # executions
    "execution", "commission_report", "partial_fill", "slot_consumed", "entry_filled",
    "stop_partial", "stopped", "flattened", "reconcile_closed",
    # protection
    "kill_switch_observed", "hold_observed", "flatten_now_observed", "loss_halt_tripped",
    "position_bound_violation", "overnight_position", "restart_loop_breaker",
    "safety_abort_live_account", "connect_failed", "connected", "disconnected",
    "reconnected", "recovery_done", "recovery_divergence", "recovery_execution",
    "foreign_ignored",
})


def git_sha(root: Path | None = None) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root or paths.ROOT),
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


class Ledger:
    """One JSON object per line, additive schema, torn last line tolerated."""

    def __init__(self, date: str, clock, path: Path | None = None, run_id: str | None = None,
                 sha: str | None = None, pid: int | None = None, dry_run: bool = False):
        self.date = str(date)
        self.clock = clock
        self._path = Path(path) if path is not None else None
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.sha = sha or git_sha()
        self.pid = int(pid if pid is not None else os.getpid())
        self.dry_run = bool(dry_run)
        self.seq = self._last_seq_for_date()
        self.records: list[dict] = []          # this process's records (tests read these)

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else paths.LEDGER

    def _last_seq_for_date(self) -> int:
        last = 0
        try:
            for rec in read_jsonl(self.path):
                if rec.get("date") == self.date and rec.get("lane") == LANE:
                    try:
                        last = max(last, int(rec.get("seq") or 0))
                    except (TypeError, ValueError):
                        continue
        except Exception:  # noqa: BLE001
            return 0
        return last

    def emit(self, event: str, **fields) -> dict:
        if event not in EVENTS:
            raise ValueError(f"ledger: '{event}' is not a canonical event name (spec §15.1)")
        self.seq += 1
        rec = {"ts_et": self.clock.now_et().isoformat(timespec="seconds"), "date": self.date,
               "seq": self.seq, "pid": self.pid, "sha": self.sha, "run_id": self.run_id,
               "lane": LANE, "event": event, "stamp": STAMP, "criteria": CRITERIA}
        if self.dry_run:
            rec["dry_run"] = True
        for k, v in fields.items():
            rec[k] = _jsonable(v)
        line = json.dumps(rec, default=str)
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab+") as fh:
            # a crashed daemon can leave a TORN last line without its newline;
            # never append onto it (that would corrupt the next record too)
            fh.seek(0, os.SEEK_END)
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")
            fh.write((line + "\n").encode("utf-8"))
        self.records.append(rec)
        logger.info("[ledger] %s %s", event, {k: rec[k] for k in fields})
        return rec

    def read_today(self) -> list[dict]:
        return [r for r in read_jsonl(self.path) if r.get("date") == self.date and r.get("lane") == LANE]


def _jsonable(v):
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    if isinstance(v, Path):
        return str(v)
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:  # noqa: BLE001
            return str(v)
    if isinstance(v, float) and v != v:            # NaN
        return None
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v
