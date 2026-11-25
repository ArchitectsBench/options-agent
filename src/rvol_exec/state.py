"""Per-symbol and session state of the RVOL-EXEC lane (spec §9) with
`state.json` persistence (atomic_save). One vocabulary: the states below are
the only ones any module may use; an illegal transition raises
`IllegalTransition` (tested).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.util.atomic import atomic_save

# ── per-symbol states (terminal in TERMINAL) ─────────────────────────────────
CANDIDATE = "candidate"
SKIPPED = "skipped"
INTENT = "intent"
UNBOUND = "unbound"
RESTING = "resting"
PARTIAL = "partial"
HELD = "held"
STOPPED = "stopped"
FLATTENING = "flattening"
FLATTENED = "flattened"
CANCELLED = "cancelled"
REJECTED = "rejected"
SHORT_UNAVAILABLE = "short_unavailable"
DROPPED = "dropped"
RECONCILE_CLOSED = "reconcile_closed"

TERMINAL = frozenset({SKIPPED, STOPPED, FLATTENED, CANCELLED, REJECTED, SHORT_UNAVAILABLE,
                      DROPPED, RECONCILE_CLOSED})
OPEN_POSITION_STATES = frozenset({PARTIAL, HELD, FLATTENING})
WORKING_PARENT_STATES = frozenset({RESTING, PARTIAL})

TRANSITIONS: dict[str, frozenset] = {
    CANDIDATE: frozenset({SKIPPED, INTENT}),
    INTENT: frozenset({RESTING, UNBOUND, REJECTED, SHORT_UNAVAILABLE, DROPPED, PARTIAL, HELD}),
    UNBOUND: frozenset({RESTING, DROPPED, PARTIAL, HELD, CANCELLED}),
    RESTING: frozenset({PARTIAL, HELD, CANCELLED, REJECTED, SHORT_UNAVAILABLE}),
    PARTIAL: frozenset({HELD, STOPPED, FLATTENING, RECONCILE_CLOSED}),
    HELD: frozenset({STOPPED, FLATTENING, RECONCILE_CLOSED}),
    FLATTENING: frozenset({FLATTENED, RECONCILE_CLOSED, HELD}),
}

CANCEL_REASONS = ("window_close", "first10_full", "guardrail", "kill_switch", "hold", "halt",
                  "flatten_now", "lost_intent", "gtd", "breaker", "overnight")
SKIP_REASONS = ("doji", "size", "cap", "foreign", "qualify", "orb_unavailable",
                "through_guardrail")


class IllegalTransition(RuntimeError):
    pass


@dataclass
class NameState:
    symbol: str
    rank: int = 0
    state: str = CANDIDATE
    # plan (from the rank record)
    side: str | None = None
    level: float | None = None
    limit_px: float | None = None
    stop_dist: float | None = None
    stop_provisional: float | None = None
    shares: int = 0
    shares_sim: float | None = None
    v930_used: float | None = None
    orb_source: str | None = None
    atr14: float | None = None
    baseline14: float | None = None
    rvol: float | None = None
    o: float | None = None
    h: float | None = None
    l: float | None = None
    c: float | None = None
    guard_ref: float | None = None
    guard_ref_source: str | None = None
    gap_rule: str = "active"
    primary_exchange: str | None = None
    skip_reason: str | None = None
    oca_group: str | None = None
    # broker ids
    intent_seq: int | None = None
    parent_id: int | None = None
    parent_perm: int | None = None
    child_id: int | None = None
    child_perm: int | None = None
    placed_at_et: str | None = None
    intent_at_et: str | None = None
    # entry fills
    filled_qty: float = 0.0
    avg_fill: float | None = None
    first_exec_time_et: str | None = None
    fill_time_et: str | None = None
    slot_index: int | None = None
    slot_overflow: bool = False
    parent_terminal: bool = False
    cancel_reason: str | None = None
    cancel_requested_at: str | None = None
    reject_code: int | None = None
    reject_reason: str | None = None
    # position / stop
    held_qty: float = 0.0
    stop_px: float | None = None
    stop_qty: float | None = None
    stop_amended: bool = False
    stop_failures: int = 0
    frozen: bool = False
    # exits
    exit_qty: float = 0.0
    exit_notional: float = 0.0
    exit_reason: str | None = None
    exit_avg: float | None = None
    r_booked: float | None = None
    # flatten
    flatten_order_id: int | None = None
    flatten_perm: int | None = None
    flatten_intent_seq: int | None = None
    flatten_placed_at_et: str | None = None
    flatten_mode: str | None = None
    flatten_qty: float = 0.0
    flatten_attempts: int = 0
    flatten_failed_paged: bool = False
    child_cancel_required: bool = False
    reverse_closed: bool = False
    # bookkeeping
    breach_loops: int = 0
    ignore_order_ids: list = field(default_factory=list)   # cancelled un-transmitted parents (one re-place)
    exec_ids: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def transition(self, new: str) -> None:
        allowed = TRANSITIONS.get(self.state, frozenset())
        if new not in allowed:
            raise IllegalTransition(f"{self.symbol}: {self.state} -> {new} is not a legal transition")
        self.state = new

    @property
    def open_qty(self) -> float:
        return self.held_qty

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL


@dataclass
class SessionState:
    date: str
    phase: str = "boot"
    run_id: str | None = None
    pid: int | None = None
    started_at_et: str | None = None
    t_ready_et: str | None = None
    snapshot_mode: str = "none"
    contamination_s: float = 0.0
    late_start: bool = False
    planned: int = 0
    placed: int = 0
    slots_used: int = 0
    slot_order: list = field(default_factory=list)
    first10_done: bool = False
    entries_closed: bool = False
    entries_closed_reason: str | None = None
    killed: bool = False
    hold: bool = False
    halted: bool = False
    flatten_now: bool = False
    overnight: bool = False
    breaker: bool = False
    unflattened: bool = False
    dry_run: bool = False
    rank_written: bool = False
    orders_placed: bool = False
    foreign: list = field(default_factory=list)
    quote_degraded: bool = False
    marks_missing: int = 0
    realized: float = 0.0
    commissions: float = 0.0
    unrealized: float = 0.0
    restarts: int = 0
    exit_code: int | None = None
    close_reason: str | None = None
    prep_file: str | None = None
    names: dict = field(default_factory=dict)      # symbol -> NameState (as dict when serialized)

    # ── persistence ──────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        d["names"] = {s: (asdict(n) if isinstance(n, NameState) else n) for s, n in self.names.items()}
        return d

    def save(self, path: Path) -> None:
        atomic_save(Path(path), json.dumps(self.to_dict(), indent=1, default=str))

    @classmethod
    def from_dict(cls, d: dict) -> "SessionState":
        names = d.pop("names", {}) or {}
        known = {f for f in cls.__dataclass_fields__}
        s = cls(**{k: v for k, v in d.items() if k in known})
        nk = set(NameState.__dataclass_fields__)
        for sym, nd in names.items():
            s.names[sym] = NameState(**{k: v for k, v in nd.items() if k in nk})
        return s

    @classmethod
    def load(cls, path: Path) -> "SessionState | None":
        p = Path(path)
        if not p.exists():
            return None
        try:
            return cls.from_dict(json.loads(p.read_text()))
        except (json.JSONDecodeError, TypeError, ValueError, OSError):
            return None

    # ── views ────────────────────────────────────────────────────────────────
    def by_rank(self) -> list[NameState]:
        return sorted(self.names.values(), key=lambda n: (n.rank or 10**6, n.symbol))

    def open_positions(self) -> list[NameState]:
        return [n for n in self.names.values() if abs(n.held_qty) > 1e-9]

    def working_parents(self) -> list[NameState]:
        return [n for n in self.names.values() if n.state in WORKING_PARENT_STATES]
