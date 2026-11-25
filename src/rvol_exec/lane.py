"""The RVOL-EXEC paper lane — session state machine (spec §4, §9-§11).
UNVALIDATED — PAPER ONLY.

`Lane.tick()` is a STEP function: the daemon calls it every `LOOP_S` seconds
with a real clock, the session simulator calls it with a fake clock between
`FakeIB.advance()` calls. Nothing in a tick sleeps. Every collaborator is
injected: `clock`, `client` (IbkrStockClient over ib_async or a FakeIB),
`polygon` (snapshot + per-ticker bars), `ledger`, `notify`.

Isolation (§0): this module imports NONE of `src.engine.paper_trader`,
`src.engine.pipeline`, `src.scheduler`, `src.data.ibkr_orders`,
`src.data.ibkr_thread`, `src.data.order_client` (asserted by
tests/rvol_exec/test_isolation.py). Files live under `data/lane_rvol/`; the
ledger is `logs/observation/rvol_exec_paper.jsonl`; the only shared file is
`data/KILL_SWITCH`, READ-ONLY.

Ordering invariants (copied patterns, §10): intent written -> POST -> bind
ids -> ledger event. Own positions come from own EXECUTIONS. A recorded stop
is not a live stop: the reconciler (§11.4) re-reads broker truth every loop.
Flatten = MKT in the child's OCA group FIRST, then verify the child died
(§8.6) — never cancel-then-MKT.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import src.rvol_exec.paths as paths
from src.rvol_exec import intents as intents_mod
from src.rvol_exec import rules
from src.rvol_exec.constants import (CLOCK_JUMP_S, CONNECT_RETRY_S, ET, FLATTEN_RETRY_S,
                                     FLATTEN_UNFILLED_S, GAP_CONFIRM_LOOPS, INTENT_RESOLVE_AGE_S,
                                     LOSS_HALT_USD, MAX_ENTRIES, MAX_HELD, NOTIONAL_CAP_USD,
                                     OCA_VERIFY_S, PARENT_TIF, PART_CAP, RESTART_BREAKER,
                                     SLOTS, SNAPSHOT_REF, STAGE2_EXACT_BAR, STAMP,
                                     T_DAEMON_START, T_ENTRY_END, T_FLATTEN, T_HARD_STOP,
                                     T_NO_ENTRY_AFTER, T_READY_DEADLINE, T_SNAPSHOT, TAG)
from src.rvol_exec.ledger import Ledger
from src.rvol_exec.post_result import AMBIGUOUS, OK, classify_post_result
from src.rvol_exec.prep import freshness, is_trading_day, read_meta, read_prep
from src.rvol_exec.snapshot import TReadyPoller, rank_universe
from src.rvol_exec.state import (CANCELLED, CANDIDATE, DROPPED, FLATTENED, FLATTENING, HELD,
                                 INTENT, OPEN_POSITION_STATES, PARTIAL, RECONCILE_CLOSED, REJECTED,
                                 RESTING, SHORT_UNAVAILABLE, SKIPPED, STOPPED, UNBOUND, NameState,
                                 SessionState, TRANSITIONS)
from src.util.atomic import atomic_save

logger = logging.getLogger("rvol_exec")

PHASES = ("boot", "preflight", "wait_snapshot", "ranking", "entry_window", "manage", "flatten",
          "done", "skipped", "unflattened")
QUOTE_MAX_AGE_S = 10
POLYGON_MARK_EVERY_S = 60
CONNECT_FAILED_EVERY_N = 30            # `connect_failed` ledgered on the 1st failure, on a reason change,
                                       # then every 30th attempt (~15 min at CONNECT_RETRY_S) — never 2/min all day
UNKNOWN_FATE_STATES = frozenset({INTENT, UNBOUND, RESTING, PARTIAL})   # a parent the broker may have filled

# the one deliberate extra edge: an intent DROPPED after an ambiguous POST may
# be re-placed ONCE (§10.6) — it goes back to candidate
TRANSITIONS[DROPPED] = frozenset({CANDIDATE})


def _t(now: datetime):
    return now.timetz().replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if isinstance(dt, datetime) else dt


def default_kill_switch_reader() -> str | None:
    """Same semantics as `src.engine.live_gate.kill_switch_engaged` on
    `paths.KILL_SWITCH_FILE` (missing -> None; unreadable -> engaged)."""
    p = paths.KILL_SWITCH_FILE
    if p.exists():
        try:
            return p.read_text().strip() or "engaged (no reason recorded)"
        except OSError:
            return "engaged (unreadable)"
    return None


def default_notify(title: str, body: str, priority: str = "default") -> None:
    try:
        from src.engine.notify import send                      # allowed reuse (§0)
        send(title, body, priority=priority, tags="rotating_light")
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify failed: %s", exc)


class Lane:
    def __init__(self, date_str: str, clock, client, polygon, ledger: Ledger | None = None,
                 notify=None, kill_switch_reader=None, dry_run: bool = False,
                 parent_tif: str = PARENT_TIF, stage2: bool = STAGE2_EXACT_BAR,
                 prep_rows=None, prep_meta: dict | None = None, pid: int | None = None,
                 sha: str | None = None):
        self.date = str(date_str)
        self.clock = clock
        self.client = client
        self.polygon = polygon
        self.notify = notify or default_notify
        self._kill_reader = kill_switch_reader or default_kill_switch_reader
        self.dry_run = bool(dry_run)
        self.tif = parent_tif
        self.stage2 = bool(stage2)
        self.pid = int(pid if pid is not None else os.getpid())
        self.sdir: Path = paths.session_dir(self.date)
        self.ledger = ledger or Ledger(self.date, clock, pid=self.pid, sha=sha, dry_run=self.dry_run)
        self._prep_rows = list(prep_rows) if prep_rows is not None else None
        self._prep_meta = prep_meta
        self._prep_symbols: list[str] = [r["ticker"] for r in self._prep_rows] if self._prep_rows else []
        loaded = SessionState.load(self.sdir / "state.json")
        # a session dir with an unreadable/missing state.json is STILL a resume:
        # orders may be live at the broker (crash between POST and persist)
        self._state_unreadable = loaded is None and self.sdir.exists()
        self.resumed = loaded is not None or self._state_unreadable
        self.s = loaded or SessionState(date=self.date)
        self._booted = False
        self.s.run_id = self.ledger.run_id
        self.s.pid = self.pid
        self.s.dry_run = self.dry_run
        self._poller: TReadyPoller | None = None
        self._last_tick: datetime | None = None
        self._last_connect_try: datetime | None = None
        self._connected_once = False
        self._needs_recovery = self.resumed
        self._recovering = False
        self._exec_seen: set[str] = set(e for n in self.s.names.values() for e in n.exec_ids)
        self._orders_cache: list[dict] | None = None
        self._orders_read_ok_at: datetime | None = None
        self._quotes_subscribed = False
        self._quote_degraded_logged = False
        self._pending_cancel_reason: str | None = None
        self._paged: set[str] = set()
        self._foreign_logged: set[str] = set()
        self._polygon_marks: dict[str, tuple[datetime, float | None]] = {}
        self._positions_without_live_stop = 0
        self._last_flatten_try: dict[str, datetime] = {}
        self._session_close_stats: dict = {}
        self._rank_meta: dict = {}
        self._flags_seen: set[str] = set()
        self._entries_deferred = False
        self._reverse_orders: dict[str, int] = {}
        self._connect_fail_count = 0
        self._connect_fail_reason: str | None = None

    # ── helpers ──────────────────────────────────────────────────────────────
    @property
    def intents_path(self) -> Path:
        return self.sdir / "intents.json"

    def _emit(self, event: str, **fields) -> dict:
        return self.ledger.emit(event, **fields)

    def _page(self, key: str, title: str, body: str, priority: str = "high") -> None:
        if key in self._paged:
            return
        self._paged.add(key)
        try:
            self.notify(f"RVOL-EXEC {STAMP}: {title}", body, priority)
        except Exception as exc:  # noqa: BLE001
            logger.warning("page failed: %s", exc)

    def _persist(self) -> None:
        try:
            self.s.save(self.sdir / "state.json")
        except Exception as exc:  # noqa: BLE001
            logger.warning("state persist failed: %s", exc)

    def _finish(self, code: int, phase: str, reason: str | None = None) -> None:
        self.s.exit_code = int(code)
        self.s.phase = phase
        self.s.close_reason = reason
        self._persist()

    def _now_dt(self, t) -> datetime:
        d = date.fromisoformat(self.date)
        return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=ET)

    def _side_is_long(self, n: NameState) -> bool:
        return n.side == "long"

    def _exit_action(self, n: NameState) -> str:
        return "SELL" if self._side_is_long(n) else "BUY"

    # ── the tick ─────────────────────────────────────────────────────────────
    def tick(self) -> int | None:
        if self.s.exit_code is not None:
            return self.s.exit_code
        now = self.clock.now_et()
        self._clock_jump(now)
        self._read_flags(now)
        if not self._booted:
            self._booted = True
            self._boot(now)
            if self.s.exit_code is not None:
                self._heartbeat(now)
                return self.s.exit_code
        self._ensure_connected(now)
        if self.s.exit_code is not None:
            self._heartbeat(now)
            return self.s.exit_code
        avail = self.client.available()
        if avail:
            if self._needs_recovery:
                self._recover(now)
            self._ingest(now)
            if self._pending_cancel_reason:
                self._cancel_working(now, self._pending_cancel_reason)
                self._pending_cancel_reason = None
        ph = self.s.phase
        if ph == "wait_snapshot":
            self._phase_wait_snapshot(now)
        elif ph == "entry_window":
            self._phase_entry_window(now)
        elif ph == "manage":
            self._phase_manage(now)
        elif ph == "flatten":
            self._phase_flatten(now)
        if self.s.phase in ("entry_window", "manage", "flatten") and self.client.available():
            self._stop_reconciler(now)
            self._loss_halt(now)
            if self.s.phase == "flatten":
                self._flatten_done_check(now)
        elif self.s.phase in ("entry_window", "manage", "flatten") and _t(now) >= T_HARD_STOP:
            self._hard_stop_without_broker(now)
        self._heartbeat(now)
        self._persist()
        self._last_tick = now
        return self.s.exit_code

    # ── clock / flags ────────────────────────────────────────────────────────
    def _clock_jump(self, now: datetime) -> None:
        self._entries_blocked_this_loop = False
        if self._last_tick is not None:
            gap = (now - self._last_tick).total_seconds()
            if abs(gap) > CLOCK_JUMP_S:
                self._emit("clock_jump", gap_s=round(gap, 1), prev=_iso(self._last_tick), now=_iso(now))
                self._entries_blocked_this_loop = True
                if gap > 60:
                    self._emit("heartbeat_gap", gap_s=round(gap, 1))
                    self._needs_recovery = True

    def _read_flags(self, now: datetime) -> None:
        ks = self._kill_reader()
        if ks and not self.s.killed:
            self.s.killed = True
            self._emit("kill_switch_observed", reason=str(ks)[:200])
            self._close_entries("kill_switch")
        hold = paths.HOLD_FILE.exists()
        if hold and not self.s.hold:
            self.s.hold = True
            reason = None
            try:
                reason = paths.HOLD_FILE.read_text()[:200]
            except OSError:
                reason = "unreadable"
            self._emit("hold_observed", reason=reason)
            self._close_entries("hold")
        if paths.FLATTEN_NOW_FILE.exists() and not self.s.flatten_now:
            self.s.flatten_now = True
            self._emit("flatten_now_observed")
            self._close_entries("flatten_now")
            if self.s.phase in ("entry_window", "manage"):
                self.s.phase = "flatten"

    def _close_entries(self, reason: str) -> None:
        """No new entries for the rest of the session; cancel resting parents
        (now if the client is available, else when it is)."""
        if not self.s.entries_closed:
            self.s.entries_closed = True
            self.s.entries_closed_reason = reason
        if self.s.phase in ("entry_window", "manage", "flatten"):
            if self.client.available():
                self._cancel_working(self.clock.now_et(), reason)
            else:
                self._pending_cancel_reason = reason

    # ── boot (§4, §11.5, §11.6, §11.12) ──────────────────────────────────────
    def _boot(self, now: datetime) -> None:
        t = _t(now)
        d = date.fromisoformat(self.date)
        if not is_trading_day(d):
            self._emit("session_noop", reason="weekend" if d.weekday() >= 5 else "holiday")
            self._finish(0, "done", "noop")
            return
        if not self.resumed and (t < T_DAEMON_START or t >= T_HARD_STOP):
            self._emit("session_noop", reason="outside_window", now_et=_iso(now))
            self._finish(0, "done", "noop")
            return
        self.sdir.mkdir(parents=True, exist_ok=True)
        n_restarts = self._bump_restarts()
        self.s.restarts = n_restarts
        if self._prep_meta is None:
            self._prep_meta = read_meta(paths.PREP_DIR, self.date)
        if self.resumed:
            prior_pid = self.s.pid
            if self._prep_rows is None:
                df = read_prep(paths.PREP_DIR, self.date)
                self._prep_rows = df.to_dict("records") if df is not None else []
                self._prep_symbols = [r["ticker"] for r in self._prep_rows]
            if self._state_unreadable:
                self._emit("recovery_divergence", detail="state.json unreadable or missing on resume — "
                           "rebuilding from rank.json + intents.json + broker truth")
                self._rebuild_from_disk(now)
            if self.s.phase in ("done", "skipped"):
                self._emit("restart", reason="already_done", prior_pid=prior_pid,
                           recovered_orders=0, recovered_executions=0)
                self._finish(0, "done", self.s.close_reason)
                return
            self.s.started_at_et = self.s.started_at_et or _iso(now)
            self._emit("restart", reason="resume", prior_pid=prior_pid, restarts=n_restarts,
                       phase=self.s.phase, recovered_orders=None, recovered_executions=None)
            if n_restarts >= RESTART_BREAKER and not self.s.breaker:
                self.s.breaker = True
                self._write_hold("restart_loop")
                self._emit("restart_loop_breaker", restarts=n_restarts)
                self._page("breaker", "restart loop breaker", f"{n_restarts} starts today — HOLD written, flattening")
                self._close_entries("hold")
                self.s.flatten_now = True
            if self.s.phase == "boot":
                self.s.phase = "wait_snapshot"
            if self.s.phase == "flatten" or self.s.flatten_now:
                self.s.phase = "flatten"
            return
        # fresh session
        self.s.started_at_et = _iso(now)
        self._emit("session_start", started_at_et=_iso(now), dry_run=self.dry_run, parent_tif=self.tif,
                   stage2=self.stage2, restarts=n_restarts)
        ks = self._kill_reader()
        if ks:
            self._emit("session_skipped", reason="kill_switch", detail=str(ks)[:200])
            self._finish(0, "skipped", "kill_switch")
            return
        if paths.HOLD_FILE.exists():
            self._emit("session_skipped", reason="hold")
            self._finish(0, "skipped", "hold")
            return
        if t >= T_READY_DEADLINE:
            self._emit("session_skipped", reason="late", now_et=_iso(now))
            self._finish(0, "skipped", "late")
            return
        ok, why = freshness(self._prep_meta, self.date)
        if not ok:
            self._emit("session_skipped", reason="prep_stale", detail=why)
            self._page("prep_stale", "prep stale", why, "urgent")
            self._finish(0, "skipped", "prep_stale")
            return
        if self._prep_rows is None:
            df = read_prep(paths.PREP_DIR, self.date)
            self._prep_rows = df.to_dict("records") if df is not None else []
            self._prep_symbols = [r["ticker"] for r in self._prep_rows]
        self.s.prep_file = str(paths.PREP_DIR / f"{self.date}.parquet")
        if paths.UNFLATTENED_FILE.exists():
            self.s.overnight = True
            self._emit("overnight_position", reason="unflattened_marker")
            self._page("overnight", "UNFLATTENED marker present", "entries refused; flattening own net at 09:35", "urgent")
            self._close_entries("overnight")
        if t > T_SNAPSHOT:
            self.s.late_start = True
            self._emit("late_start", planned_ready_et=_iso(self._now_dt(T_SNAPSHOT)), actual_start_et=_iso(now))
        self.s.phase = "wait_snapshot"

    def _rebuild_from_disk(self, now: datetime) -> None:
        """state.json is gone but the session ran: restore the PLAN from
        rank.json (written before any order), the order ids from intents.json
        (write-ahead, bound after POST), and let broker truth (§10) settle
        every name on the first ingest. Slots are recomputed from executions."""
        rank_rec = None
        try:
            rank_rec = json.loads((self.sdir / "rank.json").read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            rank_rec = None
        if rank_rec:
            tr = rank_rec.get("t_ready") or {}
            self.s.t_ready_et = tr.get("t_ready_et")
            self.s.snapshot_mode = tr.get("mode") or "none"
            self.s.contamination_s = float(tr.get("contamination_s") or 0.0)
            self.s.prep_file = rank_rec.get("prep_file")
            for row in rank_rec.get("top20") or []:
                sym = row.get("ticker")
                if not sym:
                    continue
                n = NameState(symbol=sym, rank=int(row.get("rank") or 0))
                for k in ("rvol", "baseline14", "atr14", "v930_used", "orb_source", "o", "h", "l", "c"):
                    setattr(n, k, row.get(k))
                n.oca_group = rules.oca_group_for(self.date.replace("-", ""), sym)
                if row.get("status") == SKIPPED or row.get("skip_reason"):
                    n.skip_reason = row.get("skip_reason")
                    n.state = SKIPPED
                else:
                    n.side, n.level, n.limit_px = row.get("side"), row.get("level"), row.get("limit_px")
                    n.stop_dist, n.stop_provisional = row.get("stop_dist"), row.get("stop_provisional")
                    n.shares = int(row.get("shares_live") or 0)
                    n.shares_sim = row.get("shares_sim")
                n.notes.append("rebuilt_from_rank_json")
                self.s.names[sym] = n
            self.s.rank_written = True
            self.s.planned = sum(1 for n in self.s.names.values() if n.state == CANDIDATE)
        placed = 0
        for it in intents_mod.load(self.intents_path):
            n = self.s.names.get(it.get("symbol"))
            if n is None:
                continue
            if it.get("role") == "entry":
                placed += 1
                n.intent_seq = it.get("intent_seq")
                n.intent_at_et = n.placed_at_et = it.get("submitted_at")
                if n.state == CANDIDATE:
                    n.state = INTENT
                if it.get("order_id") or it.get("perm_id"):
                    n.parent_id = int(it.get("order_id") or 0) or None
                    n.parent_perm = int(it.get("perm_id") or 0) or None
                    n.child_id = int(it.get("child_order_id") or 0) or None
                    n.child_perm = int(it.get("child_perm_id") or 0) or None
                    n.stop_px, n.stop_qty = n.stop_provisional, float(n.shares)
                    if n.state == INTENT:
                        n.state = RESTING
                elif n.state == INTENT:
                    n.state = UNBOUND
            elif it.get("role") == "flatten":
                n.flatten_intent_seq = it.get("intent_seq")
                n.flatten_order_id = int(it.get("order_id") or 0) or None
                n.flatten_perm = int(it.get("perm_id") or 0) or None
                n.flatten_placed_at_et = it.get("submitted_at")
        self.s.placed = max(self.s.placed, placed)
        if (self.sdir / "HALTED").exists():
            self.s.halted = True
        t = _t(now)
        if not self.s.rank_written:
            self.s.phase = "wait_snapshot"
        elif t < T_ENTRY_END:
            self.s.phase = "entry_window"
        elif t < T_FLATTEN and not self.s.halted:
            self.s.phase = "manage"
        else:
            self.s.phase = "flatten"
        if t >= T_ENTRY_END:
            self.s.entries_closed = True
            self.s.entries_closed_reason = self.s.entries_closed_reason or "window_close"
        self._emit("recovery_divergence", detail="rebuilt from disk", names=len(self.s.names),
                   placed=self.s.placed, phase=self.s.phase, rank_written=self.s.rank_written)

    def _adopt_from_execution(self, e: dict, now: datetime) -> NameState:
        """An OWN execution (our tag, STK) on a symbol absent from state: it is
        ours whatever the state file says (L20). Adopt the symbol so its
        position is managed (stop reconciled, flattened at 15:55) — never
        treat an own execution as foreign."""
        sym = e.get("symbol")
        side = "long" if e.get("side") == "BOT" else "short"
        n = NameState(symbol=sym, rank=0, side=side)
        n.state = HELD
        n.shares = int(float(e.get("cum_qty") or e.get("shares") or 0))
        n.oca_group = rules.oca_group_for(self.date.replace("-", ""), sym)
        n.notes.append("adopted_from_execution")
        self.s.names[sym] = n
        self.s.planned = sum(1 for x in self.s.names.values() if x.state == CANDIDATE)
        self._emit("recovery_divergence", symbol=sym, detail="own execution on a name absent from state -> adopted",
                   exec_id=e.get("exec_id"), side=side)
        self._page(f"adopt-{sym}", "own execution on an unknown name", f"{sym} adopted from broker truth", "high")
        return n

    def _own_entry_at_broker(self, sym: str) -> dict | None:
        """§11.11 one parent per symbol per session: any own entry order for
        `sym` already at the broker today (any status). None = none known;
        the caller must treat an unreadable cache as 'unknown' (no placement)."""
        if self._orders_cache is None:
            return None
        n = self.s.names.get(sym)
        ignore = set(n.ignore_order_ids) if n is not None else set()
        for o in self._orders_cache:
            if o.get("symbol") == sym and o.get("role") == "entry":
                if o.get("id") in ignore or o.get("perm_id") in ignore:
                    continue
                return o
        return None

    def _adopt_existing_parent(self, n: NameState, o: dict, now: datetime) -> None:
        intent = intents_mod.add(self.intents_path, {
            "symbol": n.symbol, "role": "entry", "side": n.side, "qty": n.shares, "level": n.level,
            "limit": n.limit_px, "stop_provisional": n.stop_provisional, "oca_group": n.oca_group,
            "tif": self.tif, "submitted_at": _iso(now), "rank": n.rank, "adopted": True})
        n.intent_seq = intent["intent_seq"]
        n.intent_at_et = _iso(now)
        n.transition(INTENT)
        n.parent_id, n.parent_perm = o.get("id") or None, o.get("perm_id") or None
        child = None
        for c in self._orders_cache or []:
            if c.get("symbol") == n.symbol and c.get("role") == "stop" and \
                    (int(c.get("parent_id") or 0) == int(n.parent_id or -1) or c.get("oca_group") == n.oca_group):
                child = c
                break
        if child is not None:
            n.child_id, n.child_perm = child.get("id") or None, child.get("perm_id") or None
        n.placed_at_et = o.get("placed_at_et") or _iso(now)
        n.stop_px, n.stop_qty = n.stop_provisional, float(n.shares)
        intents_mod.bind(self.intents_path, n.intent_seq, order_id=n.parent_id, perm_id=n.parent_perm,
                         child_order_id=n.child_id, child_perm_id=n.child_perm)
        n.transition(RESTING)
        self.s.placed += 1
        self._emit("recovery_divergence", symbol=n.symbol, detail="own entry already at the broker -> bound, not re-placed",
                   order_id=n.parent_id, perm_id=n.parent_perm, status=o.get("status"))
        self._emit("order_bound", symbol=n.symbol, order_id=n.parent_id, perm_id=n.parent_perm,
                   child_order_id=n.child_id, intent_seq=n.intent_seq, during="placement_guard")

    def _bump_restarts(self) -> int:
        p = self.sdir / "restarts.json"
        n = 0
        try:
            if p.exists():
                n = int(json.loads(p.read_text()).get("starts", 0))
        except (json.JSONDecodeError, OSError, ValueError, AttributeError):
            n = 0
        n += 1
        try:
            atomic_save(p, json.dumps({"starts": n, "last_pid": self.pid,
                                       "last_at_et": _iso(self.clock.now_et())}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("restarts.json write failed: %s", exc)
        return n

    def _write_hold(self, reason: str) -> None:
        try:
            if not paths.HOLD_FILE.exists():
                paths.HOLD_FILE.parent.mkdir(parents=True, exist_ok=True)
                atomic_save(paths.HOLD_FILE, json.dumps({"reason": reason, "source": "rvol_exec_daemon",
                                                         "at": _iso(self.clock.now_et())}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("HOLD write failed: %s", exc)

    # ── connection (§13.3) ───────────────────────────────────────────────────
    def _ensure_connected(self, now: datetime) -> None:
        if self.client.available():
            if not self._connected_once:
                self._connected_once = True
                self._emit("connected", account=self.client.account, client_id=self.client.client_id)
            return
        if self._last_connect_try is not None and \
                (now - self._last_connect_try).total_seconds() < CONNECT_RETRY_S:
            return
        self._last_connect_try = now
        was_connected = self._connected_once
        ok = self.client.connect()
        if ok:
            self._emit("reconnected" if was_connected else "connected", account=self.client.account,
                       client_id=self.client.client_id)
            self._connected_once = True
            if was_connected or self.resumed or self.s.rank_written:
                self._needs_recovery = True
            return
        reason = getattr(self.client, "last_connect_failure", None) or "unknown"
        if reason == "non_paper_account":
            self._emit("safety_abort_live_account", account=self.client.account)
            self._page("live_account", "SAFETY ABORT", "non-D account reported by the gateway — never trading", "urgent")
            self._finish(0, "skipped", "safety_abort_live_account")
            return
        if was_connected:
            if "disconnected" not in self._flags_seen:
                self._flags_seen.add("disconnected")
                self._emit("disconnected", reason=reason)
        else:
            self._connect_fail_count += 1
            if (self._connect_fail_count == 1 or reason != self._connect_fail_reason
                    or self._connect_fail_count % CONNECT_FAILED_EVERY_N == 0):
                self._emit("connect_failed", reason=reason, attempts=self.client.connect_attempts,
                           failures=self._connect_fail_count)
            self._connect_fail_reason = reason
            if _t(now) >= datetime.strptime("09:45", "%H:%M").time():
                self._page("connect_failed", "gateway not connected", f"clientId {self.client.client_id}: {reason}")

    # ── phases ───────────────────────────────────────────────────────────────
    def _phase_wait_snapshot(self, now: datetime) -> None:
        t = _t(now)
        if self.s.overnight:
            if t >= T_SNAPSHOT:
                self.s.phase = "flatten"
            return
        if self._poller is None:
            self._poller = TReadyPoller(self.clock, self.polygon, self.ledger, self._prep_symbols, self.sdir)
        status, snap, meta = self._poller.poll()
        if status == "timeout":
            self._emit("session_skipped", reason="no_t_ready")
            self._page("no_t_ready", "no T_ready by 10:10", "session skipped, zero entries", "urgent")
            self._finish(0, "skipped", "no_t_ready")
            return
        if status != "ready":
            return
        self.s.t_ready_et = meta["t_ready_et"]
        self.s.snapshot_mode = meta["mode"]
        self.s.contamination_s = meta["contamination_s"]
        self._rank_meta = meta
        self._rank(snap, meta, now)
        self.s.phase = "entry_window"
        self._place_entries(now)

    def _phase_entry_window(self, now: datetime) -> None:
        t = _t(now)
        if t >= T_ENTRY_END:
            self._close_window(now)
            self.s.phase = "manage"
            return
        if not self.client.available():
            if t >= T_ENTRY_END:
                return
            return
        self._place_entries(now)
        self._breach_cancels(now)
        self._locate_timeouts(now)

    def _phase_manage(self, now: datetime) -> None:
        t = _t(now)
        if self.s.flatten_now or self.s.halted or t >= T_FLATTEN:
            self.s.phase = "flatten"
            return
        if self.client.available() and not self.s.open_positions() and self._all_names_terminal():
            open_orders = self.client.get_own_orders(open_only=True)
            if open_orders == []:
                self._session_closed(now, "nothing_held")

    def _phase_flatten(self, now: datetime) -> None:
        if not self.client.available():
            return
        self._cancel_working(now, "halt" if self.s.halted else ("flatten_now" if self.s.flatten_now else "window_close"))
        for n in self.s.by_rank():
            if n.frozen or n.held_qty <= 1e-9:
                continue
            self._flatten_name(n, now)
        self._verify_children_after_flatten(now)

    def _flatten_done_check(self, now: datetime) -> None:
        t = _t(now)
        held = [n for n in self.s.names.values() if n.held_qty > 1e-9 and not n.frozen]
        frozen_held = [n for n in self.s.names.values() if n.held_qty > 1e-9 and n.frozen]
        if not held and not frozen_held:
            open_orders = self.client.get_own_orders(open_only=True)
            if open_orders is None:
                return
            if open_orders:
                for o in open_orders:
                    if o["role"] in ("stop", "other") or o["role"] == "entry":
                        r = self.client.cancel(o["id"])
                        self._emit("orphan_order_cancelled", symbol=o["symbol"], order_id=o["id"],
                                   role=o["role"], result=str(r.get("order", {}).get("status") if r else r))
                return
            self._session_closed(now, "flat")
            return
        if t >= T_HARD_STOP and self.s.exit_code is None:
            syms = {n.symbol: n.held_qty for n in held + frozen_held}
            try:
                paths.UNFLATTENED_FILE.parent.mkdir(parents=True, exist_ok=True)
                atomic_save(paths.UNFLATTENED_FILE, json.dumps({"date": self.date, "symbols": syms,
                                                                 "at": _iso(now), "stamp": STAMP}))
            except Exception as exc:  # noqa: BLE001
                logger.warning("UNFLATTENED marker write failed: %s", exc)
            self.s.unflattened = True
            self._emit("unflattened", symbols=syms)
            self._page("unflattened", "UNFLATTENED at 16:00", json.dumps(syms), "urgent")
            self._finish(2, "unflattened", "unflattened")

    def _hard_stop_without_broker(self, now: datetime) -> None:
        """16:00 with the gateway unreachable (§11.8): broker truth is UNKNOWN,
        so the daemon must still terminate — never loop into the next morning
        holding the pid file. A recorded position, or a parent whose fate the
        broker never confirmed, is treated as HELD: UNFLATTENED marker + page +
        exit 2 (the morning go/no-go r7 and `--flatten-now` take it from
        there). Nothing recorded as open -> `session_closed{gateway_down}`,
        exit 0 (the 09:45 `connect_failed` page already went out)."""
        if self.s.exit_code is not None:
            return
        names = list(self.s.names.values())
        held = {n.symbol: n.held_qty for n in names if n.held_qty > 1e-9 or n.state in OPEN_POSITION_STATES}
        unknown = {n.symbol: float(n.shares) for n in names if n.state in UNKNOWN_FATE_STATES and n.symbol not in held}
        if not held and not unknown:
            self._session_closed(now, "gateway_down")
            return
        syms = dict(held, **unknown)
        try:
            paths.UNFLATTENED_FILE.parent.mkdir(parents=True, exist_ok=True)
            atomic_save(paths.UNFLATTENED_FILE, json.dumps({"date": self.date, "symbols": syms,
                                                             "unknown_fate": sorted(unknown), "at": _iso(now),
                                                             "reason": "gateway_down", "stamp": STAMP}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("UNFLATTENED marker write failed: %s", exc)
        self.s.unflattened = True
        self._emit("unflattened", symbols=syms, unknown_fate=sorted(unknown), reason="gateway_down")
        self._page("unflattened", "UNFLATTENED at 16:00 (gateway down)", json.dumps(syms), "urgent")
        self._finish(2, "unflattened", "unflattened")

    def _all_names_terminal(self) -> bool:
        return all(n.is_terminal for n in self.s.names.values())

    def _session_closed(self, now: datetime, how: str) -> None:
        names = list(self.s.names.values())
        fills = sum(1 for n in names if n.filled_qty > 0)
        self._emit("session_closed", how=how,
                   held_closed=sum(1 for n in names if n.state in (STOPPED, FLATTENED)),
                   realized=round(self.s.realized, 2), commissions=round(self.s.commissions, 2),
                   slots_used=self.s.slots_used, slot_order=list(self.s.slot_order), fills=fills,
                   cancels=sum(1 for n in names if n.state == CANCELLED),
                   rejects=sum(1 for n in names if n.state == REJECTED),
                   short_unavailable=sum(1 for n in names if n.state == SHORT_UNAVAILABLE),
                   skipped=sum(1 for n in names if n.state == SKIPPED),
                   unflattened=False, killed=self.s.killed, held_flag=self.s.hold, halted=self.s.halted,
                   flatten_now=self.s.flatten_now, late_start=self.s.late_start,
                   t_ready_et=self.s.t_ready_et, snapshot_mode=self.s.snapshot_mode)
        try:
            self.client.unsubscribe_all()
        except Exception:  # noqa: BLE001
            pass
        self._finish(0, "done", how)

    # ── §6 rank + §7 per-name rules ──────────────────────────────────────────
    def _rank(self, snap: dict, meta: dict, now: datetime) -> None:
        res = rank_universe(snap, self._prep_rows or [], self.polygon, self.date,
                            contaminated=bool(meta.get("contaminated")), stage2=self.stage2,
                            retry_sleep=self.clock.sleep)
        foreign = set(self.s.foreign)
        f2 = self.client.foreign_stk_symbols() if self.client.available() else None
        if f2:
            foreign |= set(f2)
        own_today = {n.symbol for n in self.s.names.values() if n.filled_qty > 0}
        foreign -= own_today
        self.s.foreign = sorted(foreign)
        top_rows = []
        for row in res["top20"]:
            sym = row["ticker"]
            plan = rules.plan_name(row["o"], row["h"], row["l"], row["c"], row["v930_used"], row["atr14"])
            n = self.s.names.get(sym) or NameState(symbol=sym)
            n.rank = row["rank"]
            n.rvol, n.baseline14, n.atr14, n.v930_used, n.orb_source = (
                row["rvol"], row["baseline14"], row["atr14"], row["v930_used"], row["orb_source"])
            n.o, n.h, n.l, n.c = row["o"], row["h"], row["l"], row["c"]
            n.oca_group = rules.oca_group_for(self.date.replace("-", ""), sym)
            rec = {"rank": n.rank, "ticker": sym, "rvol": round(row["rvol"], 4),
                   "v930_used": row["v930_used"], "orb_source": row["orb_source"],
                   "baseline14": row["baseline14"], "n_baseline": row["n_baseline"],
                   "atr14": row["atr14"], "avgvol14": row["avgvol14"], "open": row["open"],
                   "o": row["o"], "h": row["h"], "l": row["l"], "c": row["c"],
                   "doji": plan.get("doji"), "side": plan.get("side"), "level": plan.get("level"),
                   "stop_dist": plan.get("stop_dist"), "shares_live": plan.get("shares_live"),
                   "shares_sim": plan.get("shares_sim"), "limit_px": plan.get("limit_px"),
                   "stop_provisional": plan.get("stop_provisional"), "day": row["day"],
                   "min_t": row["min_t"], "min_av": row["min_av"], "min_c": row["min_c"],
                   "updated": row["updated"], "v_snap_source": row["v_snap_source"]}
            if n.state == CANDIDATE:
                if plan["status"] != "ok":
                    n.skip_reason = plan["reason"]
                    n.transition(SKIPPED)
                    # `cap` is a CONSTRUCTION DEVIATION (§2 row 19: notional = RISK/sd * price binds
                    # whenever atr14 < 1% of price), ledgered and excluded by the comparator — not paged
                    self._emit("name_skipped", symbol=sym, reason=plan["reason"], rank=n.rank,
                               orb_source=n.orb_source, notional=(plan.get("shares_live") or 0) * (plan.get("level") or 0))
                elif sym in foreign:
                    n.skip_reason = "foreign"
                    n.transition(SKIPPED)
                    self._emit("name_skipped", symbol=sym, reason="foreign", rank=n.rank)
                    if sym not in self._foreign_logged:
                        self._foreign_logged.add(sym)
                        self._emit("foreign_ignored", symbol=sym, scope="position")
                else:
                    n.side, n.level, n.limit_px, n.stop_dist = (plan["side"], plan["level"], plan["limit_px"],
                                                                 plan["stop_dist"])
                    n.stop_provisional, n.shares, n.shares_sim = (plan["stop_provisional"], plan["shares_live"],
                                                                   plan["shares_sim"])
            rec["status"] = n.state
            rec["skip_reason"] = n.skip_reason
            top_rows.append(rec)
            self.s.names[sym] = n
        for ex in res["excluded"]:
            self._emit("name_skipped", symbol=ex["ticker"], reason=ex["reason"])
        self.s.planned = sum(1 for n in self.s.names.values() if n.state == CANDIDATE)
        rank_rec = {"top20": top_rows, "excluded": res["excluded"], "n_candidates": res["n_candidates"],
                    "stage1_k": res["stage1_k"], "stage2": res["stage2"], "prep_file": self.s.prep_file,
                    "daily_loss_halt_usd": LOSS_HALT_USD,
                    "caps": {"max_entries": MAX_ENTRIES, "max_held": MAX_HELD, "slots": SLOTS,
                             "part_cap": PART_CAP, "notional_cap_usd": NOTIONAL_CAP_USD},
                    "parent_tif": self.tif, "foreign_excluded": sorted(foreign),
                    "t_ready": {k: meta.get(k) for k in ("mode", "t_ready_et", "delay_s", "contaminated",
                                                         "contamination_s")},
                    "gap_rule": "active", "stamp": STAMP}
        try:
            atomic_save(self.sdir / "rank.json", json.dumps(rank_rec, indent=1, default=str))
            atomic_save(self.sdir / "orb_bars.json", json.dumps(
                {r["ticker"]: {k: r[k] for k in ("o", "h", "l", "c", "v930_used", "orb_source")} for r in top_rows},
                indent=1))
        except Exception as exc:  # noqa: BLE001
            logger.warning("rank.json write failed: %s", exc)
        self._emit("rank", **rank_rec)           # the plan record — BEFORE any order
        self.s.rank_written = True

    # ── placement (§7.5-7.7, §8.1, §10 write-ahead) ──────────────────────────
    def _place_entries(self, now: datetime) -> None:
        if self.dry_run or not self.s.rank_written or not self.client.available():
            return
        if self._entries_blocked_this_loop:
            return
        t = _t(now)
        for n in self.s.by_rank():
            if n.state != CANDIDATE:
                continue
            ok, why = rules.may_place(now, T_ENTRY_END, T_NO_ENTRY_AFTER, self.s.placed,
                                      self.s.slots_used, self.s.entries_closed)
            if not ok:
                break
            if not self._quotes_subscribed:
                self._subscribe_quotes()
            contract = self.client.qualify(n.symbol)
            if contract is None:
                n.skip_reason = "qualify"
                n.transition(SKIPPED)
                self._emit("name_skipped", symbol=n.symbol, reason="qualify", rank=n.rank)
                continue
            n.primary_exchange = getattr(contract, "primaryExchange", None)
            self._guard_ref(n, now)
            if not rules.stale_trigger_ok(n.side, n.guard_ref, n.limit_px):
                n.skip_reason = "through_guardrail"
                n.transition(SKIPPED)
                self._emit("name_skipped", symbol=n.symbol, reason="through_guardrail", rank=n.rank,
                           guard_ref=n.guard_ref, guard_ref_source=n.guard_ref_source, limit=n.limit_px)
                continue
            # hard assertion (§4, §11.11): never at/after 10:25:00 nor 15:40:00
            if t >= T_ENTRY_END or t >= T_NO_ENTRY_AFTER:
                raise RuntimeError(f"entry placement attempted at {now.isoformat()} — forbidden")
            # §11.11 one parent per symbol per session, by BROKER truth (a lost
            # or corrupt state file must never produce a second parent)
            if self._orders_cache is None:
                break                                    # orders unknown this loop -> no placement
            existing = self._own_entry_at_broker(n.symbol)
            if existing is not None:
                self._adopt_existing_parent(n, existing, now)
                continue
            self._place_one(n, now)

    def _guard_ref(self, n: NameState, now: datetime) -> None:
        q = self.client.last(n.symbol)
        if q and q.get("mark") is not None and (q.get("age_s") is None or q["age_s"] <= QUOTE_MAX_AGE_S):
            n.guard_ref = float(q.get("last") if q.get("last") is not None else q["mark"])
            n.guard_ref_source = q.get("source", "ibkr")
            n.gap_rule = "active"
        else:
            n.guard_ref = float(n.c) if n.c is not None else None
            n.guard_ref_source = "snapshot"
            n.gap_rule = "inactive_no_quote"
            self._quote_degraded_once("stale_trigger_guard", "snapshot min.c")

    def _quote_degraded_once(self, mechanism: str, fallback: str) -> None:
        if not self._quote_degraded_logged:
            self._quote_degraded_logged = True
            self.s.quote_degraded = True
            self._emit("quote_degraded", scope="session", mechanism=mechanism, fallback=fallback)

    def _subscribe_quotes(self) -> None:
        self._quotes_subscribed = True
        syms = [n.symbol for n in self.s.by_rank() if n.state == CANDIDATE] + list(SNAPSHOT_REF)
        try:
            self.client.subscribe_quotes(syms)
        except Exception as exc:  # noqa: BLE001
            logger.warning("quote subscription failed: %s", exc)

    def _place_one(self, n: NameState, now: datetime) -> None:
        gtd = self._now_dt(T_ENTRY_END)
        intent = intents_mod.add(self.intents_path, {
            "symbol": n.symbol, "role": "entry", "side": n.side, "qty": n.shares, "level": n.level,
            "limit": n.limit_px, "stop_provisional": n.stop_provisional, "oca_group": n.oca_group,
            "tif": self.tif, "submitted_at": _iso(now), "rank": n.rank})
        n.intent_seq = intent["intent_seq"]
        n.intent_at_et = _iso(now)
        n.transition(INTENT)
        self._emit("order_intent", symbol=n.symbol, intent_seq=n.intent_seq, side=n.side, qty=n.shares,
                   level=n.level, limit=n.limit_px, stop_provisional=n.stop_provisional,
                   oca_group=n.oca_group, tif=self.tif, rank=n.rank)
        self.s.placed += 1
        env = self.client.place_stop_limit_entry_with_stop_child(
            n.symbol, n.side, n.shares, n.level, n.limit_px, n.stop_provisional, n.oca_group,
            tif=self.tif, gtd_et=gtd)
        res = classify_post_result(env)
        if res["status"] == OK:
            o = env["order"]
            n.parent_id, n.child_id = int(o["parent_id"]), int(o.get("child_id") or 0) or None
            n.parent_perm = int(o.get("perm_id") or 0) or None
            n.child_perm = int(o.get("child_perm_id") or 0) or None
            n.placed_at_et = _iso(now)
            n.stop_px, n.stop_qty = n.stop_provisional, float(n.shares)
            intents_mod.bind(self.intents_path, n.intent_seq, order_id=n.parent_id, perm_id=n.parent_perm,
                             child_order_id=n.child_id, child_perm_id=n.child_perm)
            n.transition(RESTING)
            self._emit("order_placed", symbol=n.symbol, order_id=n.parent_id, perm_id=n.parent_perm,
                       child_order_id=n.child_id, child_perm_id=n.child_perm, role="entry",
                       oca_group=n.oca_group, placed_at_et=n.placed_at_et, tif=self.tif,
                       held=bool(o.get("held")), guard_ref=n.guard_ref, guard_ref_source=n.guard_ref_source,
                       gap_rule=n.gap_rule, primary_exchange=n.primary_exchange, rank=n.rank)
        elif res["status"] == AMBIGUOUS:
            n.transition(UNBOUND)
            n.placed_at_et = _iso(now)
            self._emit("order_ambiguous", symbol=n.symbol, intent_seq=n.intent_seq, reason=res.get("reason"))
            if sum(1 for x in self.s.names.values() if x.state == UNBOUND) > 3:
                self._page("ambiguous", "ambiguous POSTs", "> 3 in a session")
        else:
            intents_mod.drop(self.intents_path, n.intent_seq)
            code = (env or {}).get("code") if isinstance(env, dict) else None
            reason = res.get("reason")
            n.reject_code, n.reject_reason = code, str(reason)[:200] if reason else None
            if isinstance(env, dict) and env.get("short_unavailable"):
                n.transition(SHORT_UNAVAILABLE)
                self._emit("short_unavailable", symbol=n.symbol, code=code, reason=n.reject_reason, rank=n.rank)
            else:
                n.transition(REJECTED)
                self._emit("order_rejected", symbol=n.symbol, code=code, reason=n.reject_reason, rank=n.rank)
                if sum(1 for x in self.s.names.values() if x.state == REJECTED) > 5:
                    self._page("rejects", "many rejects", "> 5 in a session")

    # ── ingest broker truth every loop (§9, §10) ─────────────────────────────
    def _ingest(self, now: datetime) -> None:
        orders = self.client.get_own_orders()
        if orders is not None:
            self._orders_cache = orders
            self._orders_read_ok_at = now
        execs = self.client.get_own_executions()
        if execs is not None:
            self._book_executions(execs, now)
        if orders is not None:
            self._sync_order_states(orders, now)
        if self._recovering:
            self._recompute_slots()
            self._recovering = False
            stats = getattr(self, "_recovery_stats", {}) or {}
            self._emit("recovery_done", **stats,
                       positions={n.symbol: n.held_qty for n in self.s.open_positions()},
                       slots_used=self.s.slots_used)

    def _name_for_exec(self, e: dict) -> tuple[NameState | None, str | None]:
        sym = e.get("symbol")
        n = self.s.names.get(sym)
        if n is None:
            return None, None
        oid, perm = e.get("order_id") or 0, e.get("perm_id") or 0
        if sym in self._reverse_orders and oid and oid == self._reverse_orders[sym]:
            return n, "reverse"                       # F27 close of a double-fill: recorded, never re-booked
        if (oid and oid == n.parent_id) or (perm and perm == n.parent_perm):
            return n, "entry"
        if (oid and oid == n.child_id) or (perm and perm == n.child_perm):
            return n, "stop"
        if (oid and oid == n.flatten_order_id) or (perm and perm == n.flatten_perm):
            return n, "flatten"
        role = e.get("role")
        if role in ("entry", "stop", "flatten"):
            return n, role
        # unknown ids (a prior-session record): infer from direction
        entry_side = "BOT" if n.side == "long" else "SLD"
        if e.get("side") == entry_side:
            return n, "entry"
        return n, "flatten" if n.state == FLATTENING else "stop"

    def _book_executions(self, execs: list[dict], now: datetime) -> None:
        for e in sorted(execs, key=lambda x: (x.get("time_et") or "", x.get("exec_id") or "")):
            xid = e.get("exec_id")
            if not xid or xid in self._exec_seen:
                if xid and e.get("commission") and xid not in getattr(self, "_comm_seen", set()):
                    self._comm_seen = getattr(self, "_comm_seen", set())
                    self._comm_seen.add(xid)
                continue
            n, role = self._name_for_exec(e)
            if n is None:
                # get_own_executions() already filtered STK + our tag: this IS ours
                self._adopt_from_execution(e, now)
                n, role = self._name_for_exec(e)
            self._exec_seen.add(xid)
            n.exec_ids.append(xid)
            self._emit("recovery_execution" if self._recovering else "execution",
                       exec_id=xid, perm_id=e.get("perm_id"), order_id=e.get("order_id"), role=role,
                       symbol=n.symbol, side=e.get("side"), shares=e.get("shares"), price=e.get("price"),
                       time_et=e.get("time_et"), cum_qty=e.get("cum_qty"), avg_price=e.get("avg_price"),
                       commission=e.get("commission"))
            if role == "entry":
                self._on_entry_exec(n, e, now)
            elif role == "reverse":
                n.notes.append(f"reverse_closed_exec:{xid}")
            else:
                self._on_exit_exec(n, e, role, now)

    def _on_entry_exec(self, n: NameState, e: dict, now: datetime) -> None:
        shares = float(e.get("shares") or 0)
        first = n.filled_qty <= 0
        n.filled_qty += shares
        n.held_qty += shares
        cum = float(e.get("cum_qty") or 0)
        n.avg_fill = float(e.get("avg_price") or e.get("price") or 0) if cum >= n.filled_qty - 1e-9 else \
            round(((n.avg_fill or 0) * (n.filled_qty - shares) + float(e.get("price") or 0) * shares) / n.filled_qty, 4)
        if first:
            n.first_exec_time_et = e.get("time_et") or _iso(now)
            if self.s.slots_used < SLOTS:
                self.s.slots_used += 1
                n.slot_index = self.s.slots_used
                self.s.slot_order.append(n.symbol)
                self._emit("slot_consumed", symbol=n.symbol, slot_index=n.slot_index,
                           first_exec_time_et=n.first_exec_time_et, rank=n.rank)
            else:
                n.slot_overflow = True
                self._emit("slot_overflow", symbol=n.symbol, first_exec_time_et=n.first_exec_time_et,
                           cancel_reason=n.cancel_reason)
        if n.filled_qty >= float(n.shares) - 1e-9:
            if n.state != HELD:
                if n.state in (INTENT, UNBOUND, RESTING, PARTIAL):
                    n.transition(HELD)
            n.parent_terminal = True
            n.fill_time_et = e.get("time_et") or _iso(now)
            self._emit("entry_filled", symbol=n.symbol, qty=n.filled_qty, avg_fill=n.avg_fill,
                       fill_time_et=n.fill_time_et, slip_bps_vs_level=rules.slip_bps(n.side, n.avg_fill, n.level),
                       slot_index=n.slot_index, slot_overflow=n.slot_overflow)
            self._amend_stop_final(n, requantify=False)
        else:
            if n.state in (INTENT, UNBOUND, RESTING):
                n.transition(PARTIAL)
            self._emit("partial_fill", symbol=n.symbol, filled=n.filled_qty,
                       remaining=float(n.shares) - n.filled_qty, avg=n.avg_fill)
        if self.s.slots_used >= SLOTS and not self.s.first10_done:
            self._first10_cancel(now)

    def _on_exit_exec(self, n: NameState, e: dict, role: str, now: datetime) -> None:
        shares = float(e.get("shares") or 0)
        px = float(e.get("price") or 0)
        if n.state in (STOPPED, FLATTENED) and n.held_qty <= 1e-9:
            # the stop and the flatten BOTH executed (F27): own net is now reversed
            # by this execution's quantity; close it, book nothing else
            self._close_reverse(n, shares, now)
            return
        n.exit_qty += shares
        n.exit_notional += shares * px
        n.exit_avg = round(n.exit_notional / n.exit_qty, 4) if n.exit_qty else None
        n.held_qty -= shares
        if n.held_qty <= 1e-9:
            reverse = -n.held_qty if n.held_qty < -1e-9 else 0.0
            n.held_qty = 0.0
            exit_reason = "stop" if role == "stop" else ("halt" if self.s.halted else
                                                          ("flatten_now" if self.s.flatten_now else "flatten"))
            n.exit_reason = exit_reason
            n.r_booked = rules.r_booked(n.side, n.avg_fill, n.exit_avg, n.stop_dist)
            if role == "stop":
                if n.state in (PARTIAL, HELD):
                    n.transition(STOPPED)
                elif n.state == FLATTENING:
                    n.state = STOPPED
                self._emit("stopped", symbol=n.symbol, exit_vwap=n.exit_avg,
                           slip_bps_vs_stop=rules.slip_bps("short" if n.side == "long" else "long",
                                                            n.exit_avg, n.stop_px or n.stop_provisional),
                           r_booked=n.r_booked, qty=n.exit_qty)
                if n.parent_id and not n.parent_terminal:
                    r = self.client.cancel(n.parent_id)
                    n.cancel_reason = n.cancel_reason or "stop_hit"
            else:
                if n.state in (PARTIAL, HELD):
                    n.transition(FLATTENING)
                if n.state == FLATTENING:
                    n.transition(FLATTENED)
                self._emit("flattened", symbol=n.symbol, exit_vwap=n.exit_avg, r_booked=n.r_booked,
                           qty=n.exit_qty, reason=exit_reason, mode=n.flatten_mode)
                n.flatten_placed_at_et = None
            if reverse > 1e-9:
                self._close_reverse(n, reverse, now)
        else:
            if role == "stop":
                n.stop_qty = n.held_qty
                self._emit("stop_partial", symbol=n.symbol, filled=shares, held_qty=n.held_qty, price=px)

    def _close_reverse(self, n: NameState, qty: float, now: datetime) -> None:
        """F27: a stop and a flatten both executed -> own net reversed; close
        the reverse quantity at MKT (own net, never the account position)."""
        n.reverse_closed = True
        side = "short" if n.side == "long" else "long"     # reversed direction
        env = self.client.flatten_market(n.symbol, int(round(qty)), side, None)
        res = classify_post_result(env)
        self._emit("reverse_position_closed", symbol=n.symbol, qty=qty, result=res["status"],
                   order_id=res.get("order_id"))
        self._page(f"reverse-{n.symbol}", "reverse position closed", f"{n.symbol} qty {qty}", "urgent")
        if res["status"] == OK:
            n.exec_ids.append(f"reverse-order-{res['order_id']}")
            self._reverse_orders[n.symbol] = int(res["order_id"])

    def _amend_stop_final(self, n: NameState, requantify: bool) -> None:
        if n.child_id is None or n.avg_fill is None or n.stop_dist is None:
            return
        target = rules.stop_final_of(n.side, n.avg_fill, n.stop_dist)
        qty = int(round(n.held_qty)) if requantify else None
        if not requantify and n.stop_amended and n.stop_px == target:
            return
        r = self.client.amend_stop(n.child_id, target, new_qty=qty)
        ok = isinstance(r, dict) and "order" in r and "error" not in r
        if ok:
            prev_px, prev_qty = n.stop_px, n.stop_qty
            n.stop_px, n.stop_amended = target, True
            if requantify:
                n.stop_qty = float(qty)
                self._emit("stop_requantified", symbol=n.symbol, qty_from=prev_qty, qty_to=n.stop_qty,
                           px_from=prev_px, px_to=target, child_id=n.child_id)
            else:
                n.stop_qty = float(r["order"].get("qty") or n.stop_qty or n.shares)
                self._emit("stop_amended", symbol=n.symbol, **{"from": prev_px, "to": target}, child_id=n.child_id)
        else:
            err = (r or {}).get("error") if isinstance(r, dict) else "unavailable"
            if requantify:
                self._replace_stop(n, target, int(round(n.held_qty)), why=f"requantify_failed: {err}")
            else:
                self._emit("stop_amend_failed", symbol=n.symbol, error=str(err)[:200], child_id=n.child_id,
                           kept=n.stop_px)

    def _replace_stop(self, n: NameState, px: float, qty: int, why: str) -> None:
        """Cancel the child and place a fresh STP for `qty` in the same OCA
        group; two failures -> MKT flatten the symbol (§8.2, §11.4)."""
        if n.child_id is not None:
            try:
                self.client.cancel(n.child_id)
            except Exception:  # noqa: BLE001
                pass
        env = self.client.place_stop_order(n.symbol, self._exit_action(n), qty, px, n.oca_group)
        res = classify_post_result(env)
        if res["status"] == OK:
            n.stop_failures = 0
            old = n.child_id
            n.child_id = int(env["order"]["id"])
            n.child_perm = int(env["order"].get("perm_id") or 0) or None
            n.stop_px, n.stop_qty = px, float(qty)
            self._emit("stop_replaced", symbol=n.symbol, old_child_id=old, child_id=n.child_id, px=px,
                       qty=qty, why=why)
            return
        n.stop_failures += 1
        self._emit("stop_replace_failed", symbol=n.symbol, error=str(res.get("reason"))[:200],
                    failures=n.stop_failures, why=why)
        if n.stop_failures >= 2:
            self._emit("stop_missing_flattened", symbol=n.symbol, qty=n.held_qty, failures=n.stop_failures)
            self._page(f"stopmiss-{n.symbol}", "stop missing — flattening", n.symbol, "urgent")
            self._flatten_name(n, self.clock.now_et(), forced=True)

    def _first10_cancel(self, now: datetime) -> None:
        self.s.first10_done = True
        cancelled = 0
        for n in self.s.by_rank():
            if n.state == RESTING and n.filled_qty <= 0 and n.parent_id is not None:
                self._cancel_parent(n, "first10_full", now)
                cancelled += 1
        self._emit("first10_full", n_cancelled=cancelled, at=_iso(now), slot_order=list(self.s.slot_order))
        self.s.entries_closed = True
        self.s.entries_closed_reason = self.s.entries_closed_reason or "first10_full"

    def _cancel_parent(self, n: NameState, reason: str, now: datetime) -> None:
        if n.parent_id is None or n.cancel_requested_at:
            return
        n.cancel_reason = reason
        n.cancel_requested_at = _iso(now)
        r = self.client.cancel(n.parent_id)
        status = (r or {}).get("order", {}).get("status")
        if status == "filled":
            # the cancel RACED a fill (10148 "cannot be cancelled, state: Filled"): the
            # name is held, not cancelled — no `entry_cancelled`; the execution arrives
            # through ingest as `entry_filled` and the slot is consumed there
            n.cancel_reason = None
            n.notes.append(f"cancel_raced_fill:{reason}")
            return
        self._emit("entry_cancelled", symbol=n.symbol, reason=reason, order_id=n.parent_id,
                   result=(r or {}).get("code") or status, rank=n.rank)

    def _cancel_working(self, now: datetime, reason: str) -> None:
        for n in self.s.by_rank():
            if n.state == RESTING or (n.state == PARTIAL and not n.parent_terminal):
                self._cancel_parent(n, reason, now)

    def _close_window(self, now: datetime) -> None:
        """10:25 (§8.5): explicit cancel of every non-terminal parent; names
        never placed are recorded."""
        if self.client.available():
            self._cancel_working(now, "window_close")
        else:
            self._pending_cancel_reason = "window_close"
        for n in self.s.by_rank():
            if n.state == CANDIDATE:
                n.skip_reason = "window_closed"
                n.transition(SKIPPED)
                self._emit("name_skipped", symbol=n.symbol, reason="window_closed", rank=n.rank)
        self.s.entries_closed = True
        self.s.entries_closed_reason = self.s.entries_closed_reason or "window_close"

    def _sync_order_states(self, orders: list[dict], now: datetime) -> None:
        by_id = {o["id"]: o for o in orders if o.get("id")}
        by_perm = {o["perm_id"]: o for o in orders if o.get("perm_id")}
        t = _t(now)
        for n in list(self.s.names.values()):
            if n.state == UNBOUND:
                self._resolve_unbound(n, orders, now)
                continue
            parent = (by_id.get(n.parent_id) if n.parent_id else None) or \
                     (by_perm.get(n.parent_perm) if n.parent_perm else None)
            if n.state in (RESTING, PARTIAL) and parent is not None:
                st = parent["status"]
                if n.parent_perm is None and parent.get("perm_id"):
                    n.parent_perm = parent["perm_id"]
                if st == "rejected":
                    if n.filled_qty > 0:
                        n.parent_terminal = True
                        self._partial_terminal(n, now)
                    elif parent.get("short_unavailable"):
                        n.reject_code, n.reject_reason = parent.get("reject_code"), parent.get("reject_reason")
                        n.transition(SHORT_UNAVAILABLE)
                        self._emit("short_unavailable", symbol=n.symbol, code=n.reject_code,
                                   reason=n.reject_reason, rank=n.rank)
                    else:
                        n.reject_code, n.reject_reason = parent.get("reject_code"), parent.get("reject_reason")
                        n.transition(REJECTED)
                        self._emit("order_rejected", symbol=n.symbol, code=n.reject_code,
                                   reason=n.reject_reason, rank=n.rank)
                elif st == "canceled":
                    n.parent_terminal = True
                    if n.filled_qty > 0:
                        self._partial_terminal(n, now)
                    else:
                        reason = n.cancel_reason or ("window_close" if t >= T_ENTRY_END else "gtd")
                        n.cancel_reason = reason
                        if not n.cancel_requested_at:
                            self._emit("entry_cancelled", symbol=n.symbol, reason=reason, order_id=n.parent_id,
                                       rank=n.rank, broker_initiated=True)
                        n.transition(CANCELLED)
                elif st == "filled":
                    n.parent_terminal = True
                    if n.state == PARTIAL and n.filled_qty >= float(n.shares) - 1e-9:
                        n.transition(HELD)
            elif n.state in (RESTING, PARTIAL) and parent is None and n.parent_id is not None \
                    and self._recovering:
                self._emit("recovery_divergence", symbol=n.symbol, detail="parent order not found at broker",
                           order_id=n.parent_id)
                if n.filled_qty > 0:
                    n.parent_terminal = True
                    self._partial_terminal(n, now)
                else:
                    n.cancel_reason = "lost_intent"
                    n.transition(CANCELLED)
                    self._emit("entry_cancelled", symbol=n.symbol, reason="lost_intent", order_id=n.parent_id)
            if n.state == FLATTENING and n.flatten_order_id:
                fo = by_id.get(n.flatten_order_id) or by_perm.get(n.flatten_perm)
                if fo is not None and fo["status"] in ("rejected", "canceled") and n.held_qty > 1e-9:
                    self._emit("flatten_failed", symbol=n.symbol, reason=fo.get("reject_reason") or fo["status"],
                               order_id=n.flatten_order_id)
                    self._page(f"flatten-{n.symbol}", "flatten failed", f"{n.symbol}: {fo.get('reject_reason')}", "urgent")
                    n.flatten_order_id = n.flatten_perm = None
                    n.transition(HELD)

    def _partial_terminal(self, n: NameState, now: datetime) -> None:
        """Parent terminal with 0 < filled < qty (§8.2): the position is the
        executed part; the child must be repriced and requantified."""
        if n.state == PARTIAL:
            n.transition(HELD)
        if n.held_qty > 1e-9:
            self._amend_stop_final(n, requantify=True)

    def _resolve_unbound(self, n: NameState, orders: list[dict], now: datetime) -> None:
        m = self.client.match_own_order(n.symbol, "entry", action="BUY" if n.side == "long" else "SELL",
                                        qty=n.shares, lmt_price=n.limit_px, aux_price=n.level)
        if m is not None:
            n.parent_id, n.parent_perm = m["id"] or None, m["perm_id"] or None
            child = self.client.match_own_order(n.symbol, "stop", parent_id=m["id"]) or \
                self.client.match_own_order(n.symbol, "stop", oca_group=n.oca_group)
            if child is not None:
                n.child_id, n.child_perm = child["id"] or None, child["perm_id"] or None
            elif float(m.get("filled") or 0) <= 0 and (m.get("raw_status") == "PendingSubmit"
                                                        or m.get("transmit") is False):
                # the POST died between the parent and its child: the parent sits
                # un-transmitted with NO stop attached. Never let it go live
                # stop-less — cancel it, drop the intent, re-place the pair once.
                self.client.cancel(n.parent_id)
                n.ignore_order_ids += [x for x in (n.parent_id, n.parent_perm) if x]
                intents_mod.drop(self.intents_path, n.intent_seq)
                self._emit("entry_cancelled", symbol=n.symbol, reason="lost_intent", order_id=n.parent_id,
                           rank=n.rank, detail="parent found un-transmitted without its stop child")
                self._emit("intent_dropped", symbol=n.symbol, intent_seq=n.intent_seq, reason="untransmitted_parent")
                n.transition(DROPPED)
                if "replaced" not in n.notes and _t(now) < T_ENTRY_END and self.s.slots_used < SLOTS \
                        and not self.s.entries_closed:
                    n.notes.append("replaced")
                    n.transition(CANDIDATE)
                    n.parent_id = n.parent_perm = n.child_id = n.child_perm = None
                    n.intent_seq = None
                return
            intents_mod.bind(self.intents_path, n.intent_seq, order_id=n.parent_id, perm_id=n.parent_perm,
                             child_order_id=n.child_id, child_perm_id=n.child_perm)
            if n.filled_qty > 0:
                n.transition(HELD if n.filled_qty >= n.shares else PARTIAL)
            else:
                n.transition(RESTING)
            self._emit("order_bound", symbol=n.symbol, order_id=n.parent_id, perm_id=n.parent_perm,
                       child_order_id=n.child_id, intent_seq=n.intent_seq)
            return
        age = (now - datetime.fromisoformat(n.placed_at_et)).total_seconds() if n.placed_at_et else 0
        if age >= INTENT_RESOLVE_AGE_S and n.filled_qty <= 0:
            intents_mod.drop(self.intents_path, n.intent_seq)
            n.transition(DROPPED)
            self._emit("intent_dropped", symbol=n.symbol, intent_seq=n.intent_seq, age_s=round(age))
            replaced = int(getattr(n, "_replaced", 0))
            if "replaced" not in n.notes and _t(now) < T_ENTRY_END and self.s.slots_used < SLOTS \
                    and not self.s.entries_closed:
                n.notes.append("replaced")
                n.transition(CANDIDATE)
                n.parent_id = n.parent_perm = n.child_id = n.child_perm = None
                n.intent_seq = None

    # ── §8.4 breach cancel, §13.2 locate ─────────────────────────────────────
    def _breach_cancels(self, now: datetime) -> None:
        for n in self.s.names.values():
            if n.state not in (RESTING, PARTIAL) or n.parent_terminal or n.cancel_requested_at:
                continue
            q = self.client.last(n.symbol)
            if not q or q.get("last") is None:
                if n.gap_rule != "inactive_no_quote":
                    n.gap_rule = "inactive_no_quote"
                    self._quote_degraded_once("breach_cancel", "inert")
                continue
            if rules.breached(n.side, q["last"], n.limit_px):
                n.breach_loops += 1
                if n.breach_loops >= GAP_CONFIRM_LOOPS:
                    self._emit("guardrail_breached", symbol=n.symbol, last=q["last"], limit=n.limit_px,
                               loops=n.breach_loops)
                    self._cancel_parent(n, "guardrail", now)
            else:
                n.breach_loops = 0

    def _locate_timeouts(self, now: datetime) -> None:
        for n in self.s.names.values():
            if n.state == RESTING and n.parent_id is not None and self.client.locate_expired(n.parent_id):
                r = self.client.cancel(n.parent_id)
                n.cancel_reason = "short_unavailable"
                n.cancel_requested_at = _iso(now)
                n.reject_code, n.reject_reason = 404, "held while securities are located (timeout)"
                n.transition(SHORT_UNAVAILABLE)
                self._emit("short_unavailable", symbol=n.symbol, code=404, reason=n.reject_reason, rank=n.rank,
                           waited_s=self.client.held_for_s(n.parent_id))

    # ── §8.6 flatten ─────────────────────────────────────────────────────────
    def _flatten_name(self, n: NameState, now: datetime, forced: bool = False) -> None:
        if n.held_qty <= 1e-9 or n.frozen:
            return
        if n.state == FLATTENING and n.flatten_order_id is not None:
            placed = datetime.fromisoformat(n.flatten_placed_at_et) if n.flatten_placed_at_et else now
            if (now - placed).total_seconds() >= FLATTEN_UNFILLED_S and not n.flatten_failed_paged:
                n.flatten_failed_paged = True
                self._emit("flatten_failed", symbol=n.symbol, reason="unfilled", order_id=n.flatten_order_id,
                           waited_s=FLATTEN_UNFILLED_S)
                self._page(f"flatten-unfilled-{n.symbol}", "flatten unfilled", n.symbol, "urgent")
            return
        last = self._last_flatten_try.get(n.symbol)
        if last is not None and (now - last).total_seconds() < FLATTEN_RETRY_S and not forced:
            return
        self._last_flatten_try[n.symbol] = now
        qty = int(round(n.held_qty))
        intent = intents_mod.add(self.intents_path, {"symbol": n.symbol, "role": "flatten", "side": n.side,
                                                     "qty": qty, "oca_group": n.oca_group,
                                                     "submitted_at": _iso(now)})
        n.flatten_intent_seq = intent["intent_seq"]
        n.flatten_attempts += 1
        env = self.client.flatten_market(n.symbol, qty, n.side, n.oca_group)
        res = classify_post_result(env)
        if res["status"] == OK:
            n.flatten_order_id = int(env["order"]["id"])
            n.flatten_perm = int(env["order"].get("perm_id") or 0) or None
            n.flatten_mode = env.get("mode", "oca")
            n.child_cancel_required = bool(env.get("child_cancel_required"))
            n.flatten_placed_at_et = _iso(now)
            n.flatten_qty = qty
            intents_mod.bind(self.intents_path, n.flatten_intent_seq, order_id=n.flatten_order_id,
                             perm_id=n.flatten_perm)
            if n.state in (PARTIAL, HELD):
                n.transition(FLATTENING)
            self._emit("flatten_placed", symbol=n.symbol, qty=qty, oca_group=n.oca_group, mode=n.flatten_mode,
                       order_id=n.flatten_order_id, forced=forced, attempt=n.flatten_attempts)
        elif res["status"] == AMBIGUOUS:
            self._emit("order_ambiguous", symbol=n.symbol, role="flatten", intent_seq=n.flatten_intent_seq,
                       reason=res.get("reason"))
            if n.state in (PARTIAL, HELD):
                n.transition(FLATTENING)
        else:
            intents_mod.drop(self.intents_path, n.flatten_intent_seq)
            self._emit("flatten_failed", symbol=n.symbol, reason=str(res.get("reason"))[:200], attempt=n.flatten_attempts)
            self._page(f"flatten-{n.symbol}", "flatten rejected", f"{n.symbol}: {res.get('reason')}", "urgent")

    def _verify_children_after_flatten(self, now: datetime) -> None:
        if self._orders_cache is None:
            return
        by_id = {o["id"]: o for o in self._orders_cache if o.get("id")}
        for n in self.s.names.values():
            if n.state != FLATTENED or n.child_id is None:
                continue
            child = by_id.get(n.child_id)
            if child is None or child["status"] in ("filled", "canceled", "rejected"):
                continue
            done_at = datetime.fromisoformat(n.fill_time_et) if False else None
            elapsed = OCA_VERIFY_S if n.child_cancel_required else 0
            # give the OCA `OCA_VERIFY_S` seconds, then force-cancel
            key = f"orphan-{n.symbol}"
            first = self._last_flatten_try.get(key)
            if first is None:
                self._last_flatten_try[key] = now
                if not n.child_cancel_required:
                    continue
            elif (now - first).total_seconds() < OCA_VERIFY_S and not n.child_cancel_required:
                continue
            r = self.client.cancel(n.child_id)
            self._emit("orphan_order_cancelled", symbol=n.symbol, order_id=n.child_id, role="stop",
                       result=(r or {}).get("code") or (r or {}).get("order", {}).get("status"))
            n.child_cancel_required = False

    # ── §11.4 stop reconciler ────────────────────────────────────────────────
    def _stop_reconciler(self, now: datetime) -> None:
        if self._orders_cache is None:
            return
        live_stops: dict[str, list[dict]] = {}
        for o in self._orders_cache:
            if o["role"] == "stop" and o["status"] in ("open", "pending", "partially_filled"):
                live_stops.setdefault(o["symbol"], []).append(o)
        missing = 0
        for n in self.s.names.values():
            if n.held_qty <= 1e-9 or n.frozen or n.state == FLATTENING or n.state in (STOPPED, FLATTENED):
                continue
            if n.symbol in self._reverse_orders:
                continue
            stops = live_stops.get(n.symbol, [])
            mine = [o for o in stops if o["id"] == n.child_id or (n.child_perm and o["perm_id"] == n.child_perm)]
            if not mine:
                if stops:
                    mine = stops[:1]
                    n.child_id, n.child_perm = mine[0]["id"], mine[0]["perm_id"]
            if not mine:
                missing += 1
                if n.stop_dist is None and n.stop_provisional is None:
                    # adopted name: no plan to price a stop from -> never hold it stop-less
                    self._emit("stop_missing_flattened", symbol=n.symbol, qty=n.held_qty, failures=n.stop_failures,
                               why="no_stop_geometry")
                    self._page(f"stopmiss-{n.symbol}", "stop missing — flattening", n.symbol, "urgent")
                    self._flatten_name(n, now, forced=True)
                    continue
                px = rules.stop_final_of(n.side, n.avg_fill, n.stop_dist) if (n.avg_fill and n.stop_dist) \
                    else n.stop_provisional
                env = self.client.place_stop_order(n.symbol, self._exit_action(n), int(round(n.held_qty)), px,
                                                   n.oca_group)
                res = classify_post_result(env)
                if res["status"] == OK:
                    n.child_id = int(env["order"]["id"])
                    n.child_perm = int(env["order"].get("perm_id") or 0) or None
                    n.stop_px, n.stop_qty, n.stop_failures = px, float(int(round(n.held_qty))), 0
                    self._emit("stop_missing_replaced", symbol=n.symbol, child_id=n.child_id, px=px,
                               qty=n.stop_qty)
                    missing -= 1
                else:
                    n.stop_failures += 1
                    self._emit("stop_replace_failed", symbol=n.symbol, error=str(res.get("reason"))[:200],
                               failures=n.stop_failures, why="missing")
                    if n.stop_failures >= 2:
                        self._emit("stop_missing_flattened", symbol=n.symbol, qty=n.held_qty,
                                   failures=n.stop_failures)
                        self._page(f"stopmiss-{n.symbol}", "stop missing — flattening", n.symbol, "urgent")
                        self._flatten_name(n, now, forced=True)
                continue
            o = mine[0]
            if n.parent_terminal and abs(float(o["remaining"]) - n.held_qty) > 1e-9:
                self._amend_stop_final(n, requantify=True)
        self._positions_without_live_stop = max(0, missing)

    # ── §11.3 loss halt + §11.10 marks ───────────────────────────────────────
    def _mark(self, sym: str, now: datetime) -> tuple[float | None, str | None]:
        q = self.client.last(sym)
        if q and q.get("mark") is not None:
            return float(q["mark"]), q.get("source")
        cached = self._polygon_marks.get(sym)
        if cached is None or (now - cached[0]).total_seconds() >= POLYGON_MARK_EVERY_S:
            px = None
            try:
                px = self.polygon.last_price(sym)
            except Exception:  # noqa: BLE001
                px = None
            self._polygon_marks[sym] = (now, px)
            cached = self._polygon_marks[sym]
        if cached[1] is not None:
            self._quote_degraded_once("loss_halt_marks", "polygon")
            return float(cached[1]), "polygon"
        return None, None

    def _loss_halt(self, now: datetime) -> None:
        pos = self.client.get_own_positions()
        if pos is None:
            return
        realized = sum(float(p.get("realized_net") or 0) for p in pos.values())
        comm = sum(float(p.get("commissions") or 0) for p in pos.values())
        unreal = 0.0
        missing = 0
        for sym, p in pos.items():
            q = float(p.get("qty") or 0)
            if abs(q) < 1e-9:
                continue
            mark, _src = self._mark(sym, now)
            if mark is None:
                missing += 1
                continue
            unreal += q * (mark - float(p.get("avg_px") or 0))
        self.s.realized, self.s.commissions, self.s.unrealized, self.s.marks_missing = (
            round(realized, 2), round(comm, 2), round(unreal, 2), missing)
        if not self.s.halted and (realized + unreal) <= LOSS_HALT_USD:
            self.s.halted = True
            try:
                atomic_save(self.sdir / "HALTED", json.dumps({"realized": realized, "unrealized": unreal,
                                                              "at": _iso(now)}))
            except Exception as exc:  # noqa: BLE001
                logger.warning("HALTED latch failed: %s", exc)
            self._emit("loss_halt_tripped", realized=round(realized, 2), unrealized=round(unreal, 2),
                       threshold=LOSS_HALT_USD, marks_missing=missing)
            self._page("halt", "daily loss halt", f"realized {realized:.2f} unrealized {unreal:.2f}", "urgent")
            self._close_entries("halt")
            if self.s.phase in ("entry_window", "manage"):
                self.s.phase = "flatten"

    # ── §10 recovery ─────────────────────────────────────────────────────────
    def _recover(self, now: datetime) -> None:
        orders = self.client.get_own_orders()
        execs = self.client.get_own_executions()
        if orders is None or execs is None:
            return                                   # None != []: retry next loop
        if self.sdir.exists() and (self.sdir / "HALTED").exists():
            self.s.halted = True
            self._close_entries("halt")
        bound = dropped = 0
        by_perm = {o["perm_id"]: o for o in orders if o.get("perm_id")}
        for it in intents_mod.unbound(self.intents_path):
            n = self.s.names.get(it.get("symbol"))
            m = None
            if it.get("perm_id") and it["perm_id"] in by_perm:
                m = by_perm[it["perm_id"]]
            elif it.get("role") == "entry":
                m = self.client.match_own_order(it["symbol"], "entry",
                                                action="BUY" if it.get("side") == "long" else "SELL",
                                                qty=it.get("qty"), lmt_price=it.get("limit"), aux_price=it.get("level"))
            elif it.get("role") == "flatten":
                m = self.client.match_own_order(it["symbol"], "flatten")
            if m is not None:
                intents_mod.bind(self.intents_path, it["intent_seq"], order_id=m["id"], perm_id=m["perm_id"])
                bound += 1
                if n is not None and it.get("role") == "entry":
                    n.parent_id, n.parent_perm = m["id"] or None, m["perm_id"] or None
                    if n.state in (INTENT, UNBOUND):
                        n.transition(RESTING)
                    self._emit("order_bound", symbol=n.symbol, order_id=n.parent_id, perm_id=n.parent_perm,
                               intent_seq=it["intent_seq"], during="recovery")
                elif n is not None and it.get("role") == "flatten":
                    n.flatten_order_id, n.flatten_perm = m["id"] or None, m["perm_id"] or None
            else:
                sub = it.get("submitted_at")
                age = (now - datetime.fromisoformat(sub)).total_seconds() if sub else INTENT_RESOLVE_AGE_S
                has_fill = any(e.get("symbol") == it.get("symbol") for e in execs)
                if age >= INTENT_RESOLVE_AGE_S and not has_fill:
                    intents_mod.drop(self.intents_path, it["intent_seq"])
                    dropped += 1
                    self._emit("intent_dropped", symbol=it.get("symbol"), intent_seq=it["intent_seq"],
                               during="recovery")
                    if n is not None and n.state in (INTENT, UNBOUND):
                        n.transition(DROPPED)
        # names whose recorded state the broker contradicts
        by_id = {o["id"]: o for o in orders if o.get("id")}
        for n in self.s.names.values():
            if n.state == INTENT and n.parent_id is None:
                m = self.client.match_own_order(n.symbol, "entry")
                if m is None:
                    n.state = CANDIDATE            # crash before POST: place again if the window is open
                    self._emit("recovery_divergence", symbol=n.symbol, detail="intent without order -> candidate")
            if n.child_id is not None and n.held_qty > 1e-9:
                c = by_id.get(n.child_id) or by_perm.get(n.child_perm or -1)
                if c is None or c["status"] in ("canceled", "rejected"):
                    self._emit("recovery_divergence", symbol=n.symbol, detail="recorded stop not live",
                               child_id=n.child_id)
        self._recovering = True
        self._needs_recovery = False
        n_exec_new = sum(1 for e in execs if e.get("exec_id") not in self._exec_seen)
        # `recovery_done` is emitted by the ingest that follows, once the
        # executions are booked (positions and slots come from broker truth)
        self._recovery_stats = {"bound": bound, "dropped": dropped, "orders": len(orders),
                                "executions": len(execs), "new_executions": n_exec_new}

    def _recompute_slots(self) -> None:
        filled = [n for n in self.s.names.values() if n.filled_qty > 0]
        filled.sort(key=lambda n: (n.first_exec_time_et or "", n.rank))
        self.s.slot_order = [n.symbol for n in filled[:SLOTS]]
        self.s.slots_used = len(self.s.slot_order)
        for i, n in enumerate(filled, 1):
            n.slot_index = i if i <= SLOTS else None
            n.slot_overflow = i > SLOTS
        if self.s.slots_used >= SLOTS and not self.s.first10_done and self.client.available():
            self._first10_cancel(self.clock.now_et())

    # ── heartbeat (§15.3) ────────────────────────────────────────────────────
    def _heartbeat(self, now: datetime) -> None:
        hb = {"ts_et": _iso(now), "now_et": _iso(now), "monotonic": round(self.clock.monotonic(), 3),
              "date": self.date, "pid": self.pid, "sha": self.ledger.sha, "started_at_et": self.s.started_at_et,
              "phase": self.s.phase,
              "broker": "connected" if self.client.available() else ("unknown" if self._connected_once else "disconnected"),
              "account": self.client.account, "t_ready_et": self.s.t_ready_et,
              "snapshot_mode": self.s.snapshot_mode, "contamination_s": self.s.contamination_s,
              "planned": self.s.planned, "placed": self.s.placed,
              "held": sum(1 for n in self.s.names.values() if n.held_qty > 1e-9),
              "slots_used": self.s.slots_used,
              "positions_without_live_stop": self._positions_without_live_stop,
              "realized": self.s.realized, "unrealized": self.s.unrealized, "marks_missing": self.s.marks_missing,
              "halted": self.s.halted, "kill_switch": self.s.killed, "hold": self.s.hold,
              "last_exec_poll_ok_et": _iso(self._orders_read_ok_at), "restarts_today": self.s.restarts,
              "exit_code": self.s.exit_code, "stamp": STAMP}
        self.last_heartbeat = hb
        try:
            atomic_save(paths.HEARTBEAT, json.dumps(hb))
        except Exception as exc:  # noqa: BLE001
            logger.warning("heartbeat write failed: %s", exc)

    # ── driver ───────────────────────────────────────────────────────────────
    def run(self, loop_s: float, sleep=None, max_ticks: int | None = None) -> int:
        """Blocking loop for the daemon: tick, sleep `loop_s`, until done."""
        sleep = sleep or self.clock.sleep
        ticks = 0
        while True:
            code = self.tick()
            ticks += 1
            if code is not None:
                return int(code)
            if max_ticks is not None and ticks >= max_ticks:
                return -1
            sleep(loop_s)
