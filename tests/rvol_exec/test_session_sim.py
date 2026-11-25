"""Session-simulator scenarios (spec §16.3): the REAL `Lane` + the REAL
`IbkrStockClient` over the FakeIB, driven by a fake clock through a recorded
day (`fixtures/2026-09-11/`, vendored bars5m_fresh) or hand-built synthetic
bars. Hermetic: no socket, no API, every path under tmp (conftests).
UNVALIDATED — PAPER ONLY.

Every scenario runs the simulator's per-tick invariants: every ledger line
stamped, seq monotonic, every order tagged, no order on a foreign symbol,
no entry parent placed at/after 10:25:00, `positions_without_live_stop == 0`
outside the flatten phase.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

import src.rvol_exec.paths as paths
from src.rvol_exec import causal
from src.rvol_exec.clock import at_et
from src.rvol_exec.constants import LOSS_HALT_USD, SLOTS, STAMP, TAG
from src.rvol_exec.liveness_rules import HEARTBEAT_FIELDS
from tests.rvol_exec.fake_ib import FakeIB
from tests.rvol_exec.session_sim import SessionSim, fixture_day, make_bars, synthetic_prep

DAY = "2026-09-11"
TERMINAL = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


# ── helpers ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def recorded():
    return fixture_day(DAY)


def _fixture_sim(recorded, **kw) -> SessionSim:
    bars, prep, meta = recorded
    return SessionSim(bars, prep, day=DAY, prep_meta=meta, **kw)


def _events(sim, name=None):
    return sim.events(name)


def _count(sim, name, **match):
    return sum(1 for r in _events(sim, name) if all(r.get(k) == v for k, v in match.items()))


def _own_parents():
    return [t for t in FakeIB.book() if t.order.orderType == "STP LMT" and not t.order.parentId
            and t.order.orderRef == TAG]


def _dup_parents() -> dict:
    c = Counter(t.contract.symbol for t in _own_parents())
    return {s: n for s, n in c.items() if n > 1}


def _live_stops():
    return [t for t in FakeIB.book() if t.order.orderType == "STP" and t.order.orderRef == TAG
            and t.orderStatus.status not in TERMINAL]


def _cross_bar(gap: bool = False, open_cross: bool = False):
    """A crossing bar for orb (10.0, 10.5, 9.9, 10.4): level 10.51, limit
    10.55 (atr 2 -> sd 0.4). Prints o->l->h->c: the in-limit shape fills at
    10.53 on the bar's :30 print in the FakeIB and at 10.51 in the sim
    (raw = max(level, open)); the open-cross shape (c < o, prints o->h->l->c)
    fills on the :00 print at 10.52 in both; the gap shape opens beyond the
    limit and is the guardrail cancel."""
    if gap:
        return (10.75, 10.9, 10.70, 10.8)
    if open_cross:
        return (10.52, 10.53, 10.40, 10.45)
    return (10.45, 10.53, 10.40, 10.52)


def _synth_bars(cross_hm: str | None, gap: bool = False, later: dict | None = None, open_cross: bool = False):
    path = {}
    if cross_hm:
        path[cross_hm] = _cross_bar(gap, open_cross)
    if later:
        path.update(later)
    path.setdefault("15:55", (10.9, 10.95, 10.85, 10.92))
    return make_bars((10.0, 10.5, 9.9, 10.4, 100_000), path)


def _synthetic_sim(bars: dict, atr: dict | None = None, **kw) -> SessionSim:
    """`SPY` is added as a doji (skipped) so the snapshot carries a REF name
    for the T_ready `min.t` test (§5) without ever trading it."""
    bars = dict(bars)
    bars.setdefault("SPY", make_bars((500.0, 501.0, 499.0, 500.0, 1_000_000)))
    atr = dict(atr or {}) or {s: 2.0 for s in bars}
    atr.setdefault("SPY", 5.0)
    prep = synthetic_prep(bars, atr)
    return SessionSim(bars, prep, day=DAY, **kw)


# ── 1. real-time T_ready, the recorded day end to end ───────────────────────
def test_realtime_full_day_on_the_recorded_fixture(recorded):
    sim = _fixture_sim(recorded)
    assert sim.run() == 0
    tr = _events(sim, "t_ready")
    assert len(tr) == 1 and tr[0]["mode"] == "realtime" and not tr[0]["contaminated"]
    assert tr[0]["t_ready_et"].startswith(f"{DAY}T09:35:0")
    # the plan record precedes every order
    rank = _events(sim, "rank")[0]
    placed = _events(sim, "order_placed")
    assert placed and all(r["seq"] > rank["seq"] for r in placed)
    assert rank["daily_loss_halt_usd"] == LOSS_HALT_USD and rank["stamp"] == STAMP
    assert len(rank["top20"]) == 20
    closed = _events(sim, "session_closed")[-1]
    entries = {t.contract.symbol for t in _own_parents() if t.orderStatus.filled > 0}
    assert closed["fills"] == len(entries) == _count(sim, "entry_filled")
    assert closed["how"] == "flat" and closed["slots_used"] == len(entries) <= SLOTS
    # flat by executions AND the broker's tape agree
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())
    assert not _live_stops() and not _dup_parents()
    assert sim.max_pos_without_stop == 0 and sim.pages == []
    hb = sim.heartbeats[-1]
    assert all(f in hb for f in HEARTBEAT_FIELDS) and hb["stamp"] == STAMP
    # state.json persisted with the terminal phase
    st = json.loads((paths.session_dir(DAY) / "state.json").read_text())
    assert st["phase"] == "done" and st["exit_code"] == 0
    # every exit is a stop or a flatten; r_booked present on each
    exits = _events(sim, "stopped") + _events(sim, "flattened")
    assert len(exits) == len(entries) and all(e["r_booked"] is not None for e in exits)


# ── 2. delayed T_ready path ──────────────────────────────────────────────────
def test_delayed_snapshot_polls_until_0934_then_ranks_at_0950(recorded):
    sim = _fixture_sim(recorded, delay_s=900)
    assert sim.run() == 0
    tr = _events(sim, "t_ready")[0]
    assert tr["mode"] == "delayed" and tr["t_ready_et"] == at_et(DAY, 9, 50).isoformat(timespec="seconds")
    assert not tr["contaminated"] and tr["contamination_s"] == 0
    polls = _events(sim, "snapshot_polled")
    assert len(polls) >= 20 and polls[0]["wall_et"].startswith(f"{DAY}T09:35:00")
    # nothing placed before T_ready; names already through the limit are skipped, not placed
    assert all(r["ts_et"] >= tr["t_ready_et"] for r in _events(sim, "order_placed"))
    assert _count(sim, "name_skipped", reason="through_guardrail") > 0
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())


# ── 3 + 33. causal-first-10 and sim<->lane agreement ─────────────────────────
def _fourteen_crossings():
    """Ranks 1-9 cross on distinct bars 09:35..10:15; rank 10 crosses on the
    10:20 bar's OPEN print (10:20:00); ranks 11-14 cross on the same 10:20
    bar but only at its :30 print — the lane's first-10 cancel (at the
    10:20:00 tick) must beat that print, and the sim's tie -> rank rule
    picks the same ten."""
    hms = ["09:35", "09:40", "09:45", "09:50", "09:55", "10:00", "10:05", "10:10", "10:15"]
    bars = {f"C{i:02d}": _synth_bars(hm) for i, hm in enumerate(hms)}
    bars["C09"] = _synth_bars("10:20", open_cross=True)
    for i in range(10, 14):
        bars[f"C{i:02d}"] = _synth_bars("10:20")
    return bars


def test_causal_first10_exactly_ten_held_rest_cancelled_partial_counts():
    bars = _fourteen_crossings()
    sim = _synthetic_sim(bars)
    sim.fake.partial("C02", 40)                       # first execution 40 of 125, remainder never fills
    assert sim.run() == 0
    assert sim.lane.s.slots_used == SLOTS and _count(sim, "slot_consumed") == SLOTS
    full = _events(sim, "first10_full")
    assert len(full) == 1 and full[0]["n_cancelled"] == 4
    assert _count(sim, "entry_cancelled", reason="first10_full") == 4
    assert _count(sim, "slot_overflow") == 0
    # the partial occupied its slot and was requantified when the parent went terminal
    c02 = sim.names()["C02"]
    assert c02.slot_index is not None and c02.filled_qty == 40
    assert _count(sim, "partial_fill", symbol="C02") == 1
    # exactly ten symbols ever executed an entry; everything flat at the end
    entered = {f.contract.symbol for f in FakeIB.executions(TAG)
               if FakeIB._BROKER_BOOK[f.execution.permId].order.orderType == "STP LMT"}
    assert len(entered) == SLOTS
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())
    # §3 agreement on the lane's own ledgered rank/skips: same names, same order
    # (crossings on distinct bars, so the order is decidable), same cut bar
    rank_rows = [{"ticker": r["ticker"], "rank": r["rank"], "entry_start": "09:35"}
                 for r in _events(sim, "rank")[0]["top20"]]
    skips = [r["symbol"] for r in _events(sim, "name_skipped")]
    atr = {s: 2.0 for s in bars}
    taken, cancelled = causal.replay_causal_first10(rank_rows, bars, atr, skips)
    assert [t["ticker"] for t in taken] == sim.lane.s.slot_order
    assert {c[0] for c in cancelled} == {r["symbol"] for r in _events(sim, "entry_cancelled")
                                         if r["reason"] == "first10_full"}
    assert cancelled and cancelled[0][2] == "10:20"


# ── 4. guardrail ─────────────────────────────────────────────────────────────
def test_guardrail_gap_bar_cancels_and_never_fills_partial_keeps_held_part():
    bars = {"GAP": _synth_bars("09:40", gap=True, later={"10:00": (10.50, 10.52, 10.45, 10.50)}),
            "PART": _synth_bars("09:40", later={"09:50": (10.75, 10.9, 10.70, 10.8)}),
            "OK": _synth_bars("09:45")}
    sim = _synthetic_sim(bars)
    sim.fake.partial("PART", 30)
    assert sim.run() == 0
    # GAP: breach after 2 loops, cancelled, zero executions even when price pulls back inside the limit
    assert _count(sim, "guardrail_breached", symbol="GAP") == 1
    assert _count(sim, "entry_cancelled", symbol="GAP", reason="guardrail") == 1
    assert not [f for f in FakeIB.executions(TAG) if f.contract.symbol == "GAP"]
    assert sim.names()["GAP"].state == "cancelled"
    # the sim's guardrail refuses the same name
    tr = causal.replay_k18_trigger_only(bars["GAP"], 2.0)
    assert tr is None
    # PART: 30 filled, the remainder rests, a later breach cancels the remainder only; held part kept + stopped/flattened
    p = sim.names()["PART"]
    assert p.filled_qty == 30 and _count(sim, "entry_cancelled", symbol="PART", reason="guardrail") == 1
    assert p.state in ("flattened", "stopped") and abs(FakeIB.net_by_symbol(TAG)["PART"]) < 1e-9
    assert sim.names()["OK"].filled_qty == 125


# ── 5. stop child live while the process is dead; restart recovers from broker truth
def test_stop_hits_while_dead_restart_books_it_no_duplicate_stop(recorded):
    sim = _fixture_sim(recorded)
    sim.run_until(12, 0)
    held = {s: n.held_qty for s, n in sim.names().items() if n.held_qty > 0}
    assert "CLX" in held and len(held) >= 2
    sim.kill_lane()
    sim.run_until(12, 40, start_lane=False)             # the broker alone: CLX's stop fills at 12:30
    assert FakeIB.net_by_symbol(TAG)["CLX"] == 0 and FakeIB.net_by_symbol(TAG)["VIXY"] != 0
    sim.lane_start = at_et(DAY, 12, 40)
    sim.start_lane()
    assert sim.run() == 0
    recs = sim.lane.ledger.records                       # the restarted process only
    ev = Counter(r["event"] for r in recs)
    assert ev["restart"] == 1 and ev["recovery_done"] == 1
    rd = [r for r in recs if r["event"] == "recovery_done"][0]
    assert set(rd["positions"]) == set(held) - {"CLX"} and rd["new_executions"] == 1
    assert ev["recovery_execution"] == 1 and _count(sim, "stopped", symbol="CLX") == 1
    assert sim.lane.s.restarts == 2 and not _dup_parents()
    # one live stop per held name after recovery, none at the end
    assert len(_live_stops()) == 0 and all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())
    assert sim.max_pos_without_stop == 0


# ── 6. 10:25 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tif,gtd_rejected", [("GTD", False), ("DAY", True)])
def test_1025_cancels_every_unfilled_parent_and_nothing_placed_after(recorded, tif, gtd_rejected):
    sim = _fixture_sim(recorded, tif=tif)
    sim.fake.gtd_rejected = gtd_rejected
    sim.run_until(10, 24, 45)
    resting = {s for s, n in sim.names().items() if n.state == "resting"}
    assert resting
    sim.run_until(10, 26)
    assert all(t.orderStatus.status in TERMINAL for t in _own_parents())
    assert _count(sim, "entry_cancelled", reason="window_close") == len(resting)
    assert _count(sim, "order_rejected") == 0
    assert sim.run() == 0
    last_placed = max(r["ts_et"] for r in _events(sim, "order_placed"))
    assert last_placed < at_et(DAY, 10, 25).isoformat(timespec="seconds")
    assert all(t.order.tif == tif for t in _own_parents())


# ── 7 + 22. 15:55 flatten ordering ───────────────────────────────────────────
def test_1555_flatten_mkt_joins_the_stop_oca_before_any_cancel(recorded):
    sim = _fixture_sim(recorded)
    assert sim.run() == 0
    flats = _events(sim, "flatten_placed")
    assert flats and all(f["mode"] == "oca" for f in flats)
    assert all(f["ts_et"] >= at_et(DAY, 15, 55).isoformat(timespec="seconds") for f in flats)
    for f in flats:
        mkt = [t for t in FakeIB.book() if t.order.orderId == f["order_id"] and t.order.orderType == "MKT"][0]
        child = [t for t in FakeIB.book() if t.order.orderType == "STP" and t.order.ocaGroup == mkt.order.ocaGroup
                 and t.contract.symbol == f["symbol"]][0]
        assert child.orderStatus.status == "Cancelled"
        cancelled_at = [e.time for e in child.log if e.status == "Cancelled"][0]
        assert cancelled_at >= FakeIB._META[mkt.order.permId]["placed_at"]     # never cancel-then-MKT
    assert _count(sim, "orphan_order_cancelled") == 0
    assert sim.max_pos_without_stop == 0


def test_orphan_stop_after_flatten_is_force_cancelled(recorded):
    sim = _fixture_sim(recorded)
    sim.fake.orphan_stop_after_flatten = True
    assert sim.run() == 0
    assert _count(sim, "orphan_order_cancelled") >= 1
    assert not _live_stops()


# ── 14. kill switch ──────────────────────────────────────────────────────────
def test_kill_switch_cancels_resting_keeps_stops_and_release_does_not_rearm(recorded):
    flag = {"on": None}
    sim = _fixture_sim(recorded, kill_reader=lambda: flag["on"])
    sim.run_until(10, 0)
    resting = {s for s, n in sim.names().items() if n.state == "resting"}
    held = {s for s, n in sim.names().items() if n.held_qty > 0}
    assert resting and held
    flag["on"] = "test"
    sim.run_until(10, 1)
    assert _count(sim, "kill_switch_observed") == 1
    assert _count(sim, "entry_cancelled", reason="kill_switch") == len(resting)
    assert {t.contract.symbol for t in _live_stops()} >= held      # stops ride
    flag["on"] = None                                               # released -> still no entries today
    assert sim.run() == 0
    assert all(r["ts_et"] < at_et(DAY, 10, 0).isoformat(timespec="seconds") for r in _events(sim, "order_placed"))
    assert _count(sim, "flattened") >= 1 and all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())


# ── 15. HOLD ─────────────────────────────────────────────────────────────────
def test_hold_at_boot_skips_session_and_places_nothing(recorded):
    paths.HOLD_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.HOLD_FILE.write_text(json.dumps({"reason": "test", "source": "test"}))
    sim = _fixture_sim(recorded)
    assert sim.run() == 0
    assert _count(sim, "session_skipped", reason="hold") == 1
    assert FakeIB.book() == [] and _count(sim, "order_placed") == 0


def test_hold_mid_session_behaves_like_kill_switch(recorded):
    sim = _fixture_sim(recorded)
    sim.run_until(10, 5)
    resting = {s for s, n in sim.names().items() if n.state == "resting"}
    paths.HOLD_FILE.write_text(json.dumps({"reason": "test"}))
    assert sim.run() == 0
    assert _count(sim, "hold_observed") == 1
    assert _count(sim, "entry_cancelled", reason="hold") == len(resting)


# ── 12. restart with a corrupted / missing state file ───────────────────────
def test_restart_with_corrupted_state_json_rebuilds_from_disk_no_duplicate_parent(recorded):
    sim = _fixture_sim(recorded)
    sim.run_until(9, 50)
    held = {s: n.held_qty for s, n in sim.names().items() if n.held_qty > 0}
    resting = {s for s, n in sim.names().items() if n.state == "resting"}
    assert held and len(resting) >= 5
    sim.kill_lane()
    (paths.session_dir(DAY) / "state.json").write_text("{corrupt")
    sim.lane_start = at_et(DAY, 9, 52)
    sim.start_lane()
    assert sim.run() == 0
    recs = sim.lane.ledger.records
    details = [r.get("detail") for r in recs if r["event"] == "recovery_divergence"]
    assert any("rebuilding" in d for d in details) and "rebuilt from disk" in details
    rd = [r for r in recs if r["event"] == "recovery_done"][0]
    assert rd["positions"] == held
    assert not _dup_parents() and _count(sim, "foreign_ignored") == 0
    assert sim.lane.s.placed >= len(resting) + len(held)
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())
    assert _count(sim, "session_closed") == 1


def test_restart_with_only_intents_adopts_own_executions_and_binds_parents(recorded):
    sim = _fixture_sim(recorded)
    sim.run_until(9, 50)
    held = {s for s, n in sim.names().items() if n.held_qty > 0}
    sim.kill_lane()
    (paths.session_dir(DAY) / "state.json").unlink()
    (paths.session_dir(DAY) / "rank.json").unlink()
    sim.lane_start = at_et(DAY, 9, 52)
    sim.start_lane()
    assert sim.run() == 0
    recs = sim.lane.ledger.records
    adopted = {r["symbol"] for r in recs if r["event"] == "recovery_divergence" and "adopted" in str(r.get("detail"))}
    assert held <= adopted
    assert any(r["event"] == "order_bound" and r.get("during") == "placement_guard" for r in recs)
    assert not _dup_parents() and _count(sim, "foreign_ignored") == 0
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())
    assert not _live_stops()


# ── 16. loss halt ────────────────────────────────────────────────────────────
def test_loss_halt_trips_on_realized_flattens_and_latches_across_restart():
    stop_bar = {"09:45": (10.52, 10.53, 10.0, 10.05)}
    bars = {f"H{i}": _synth_bars("09:40", later=stop_bar) for i in range(6)}
    bars["KEEP"] = _synth_bars("09:40")
    bars["LATE"] = _synth_bars("10:10")
    sim = _synthetic_sim(bars)
    sim.run_until(9, 50)
    halt = _events(sim, "loss_halt_tripped")
    assert len(halt) == 1 and halt[0]["realized"] <= LOSS_HALT_USD and halt[0]["threshold"] == LOSS_HALT_USD
    assert (paths.session_dir(DAY) / "HALTED").exists()
    assert _count(sim, "entry_cancelled", reason="halt", symbol="LATE") == 1
    assert _count(sim, "stopped") == 6
    assert _count(sim, "flattened", symbol="KEEP") == 1
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())
    # a restart after the halt never re-enters
    sim.kill_lane()
    sim.lane_start = at_et(DAY, 10, 0)
    sim.start_lane()
    assert sim.run() == 0
    assert sim.lane.s.halted and all(r["event"] != "order_placed" for r in sim.lane.ledger.records)
    assert not [f for f in FakeIB.executions(TAG) if f.contract.symbol == "LATE"]


# ── 17. foreign-held exclusion ───────────────────────────────────────────────
def test_foreign_held_symbols_are_skipped_and_never_touched(recorded):
    sim = _fixture_sim(recorded, foreign_symbols=("VIXY", "ILF"))
    sim.fake.foreign_positions([("VIXY", "STK", 100, 17.0), ("ILF", "STK", 50, 36.0),
                                ("IEF", "STK", 200, 95.0), ("HON", "STK", 10, 200.0)])
    assert sim.run() == 0
    assert _count(sim, "name_skipped", reason="foreign", symbol="VIXY") == 1
    assert _count(sim, "name_skipped", reason="foreign", symbol="ILF") == 1
    rank = _events(sim, "rank")[0]
    assert {"VIXY", "ILF"} <= set(rank["foreign_excluded"])
    assert not [t for t in FakeIB.book() if t.contract.symbol in ("VIXY", "ILF", "IEF", "HON")]
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())


# ── 9. short unavailable ─────────────────────────────────────────────────────
def test_short_unavailable_201_and_404_timeout_do_not_consume_slots_release_trades(recorded):
    sim = _fixture_sim(recorded)
    sim.fake.short_unavailable("VIXY", "201")
    sim.fake.short_unavailable("ILF", "404")                       # never released
    sim.fake.short_unavailable("CLX", "404", release_after_s=10)   # released -> trades
    assert sim.run() == 0
    su = {r["symbol"]: r for r in _events(sim, "short_unavailable")}
    assert set(su) == {"VIXY", "ILF"} and su["VIXY"]["code"] == 201 and su["ILF"]["code"] == 404
    assert sim.names()["VIXY"].slot_index is None and sim.names()["ILF"].slot_index is None
    assert sim.names()["CLX"].filled_qty > 0 and sim.names()["CLX"].slot_index is not None
    assert not [f for f in FakeIB.executions(TAG) if f.contract.symbol in ("VIXY", "ILF")]
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())


# ── 23. FLATTEN_NOW ──────────────────────────────────────────────────────────
def test_flatten_now_file_flattens_at_once_and_exits_clean(recorded):
    sim = _fixture_sim(recorded)
    sim.run_until(11, 10)
    held = {s for s, n in sim.names().items() if n.held_qty > 0}
    assert held
    paths.FLATTEN_NOW_FILE.write_text(json.dumps({"reason": "test"}))
    code = sim.run_until(11, 15)
    assert code == 0 and _count(sim, "flatten_now_observed") == 1
    assert {r["symbol"] for r in _events(sim, "flattened")} == held
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values()) and not _live_stops()


# ── 24. UNFLATTENED ──────────────────────────────────────────────────────────
def test_halted_name_cannot_flatten_marker_and_exit_2(recorded):
    sim = _fixture_sim(recorded)
    sim.run_until(15, 50)
    held = [s for s, n in sim.names().items() if n.held_qty > 0]
    assert "VIXY" in held
    sim.fake.halted("VIXY")
    code = sim.run()
    assert code == 2 and sim.lane.s.phase == "unflattened"
    assert paths.UNFLATTENED_FILE.exists() and "VIXY" in json.loads(paths.UNFLATTENED_FILE.read_text())["symbols"]
    assert _count(sim, "flatten_failed", symbol="VIXY") >= 1 and _count(sim, "unflattened") == 1
    assert any(p[3] == "urgent" for p in sim.pages)


# ── 29-ish. dry run (the Tuesday SHADOW mode) ────────────────────────────────
def test_dry_run_ranks_ledgers_and_places_nothing(recorded):
    sim = _fixture_sim(recorded, dry_run=True)
    assert sim.run() == 0
    assert _count(sim, "rank") == 1 and _count(sim, "order_placed") == 0 and FakeIB.book() == []
    assert all(r.get("dry_run") is True for r in _events(sim))
    assert _count(sim, "session_closed", how="nothing_held") == 1


# ── 10. ambiguous POST ───────────────────────────────────────────────────────
def test_ambiguous_post_untransmitted_parent_is_cancelled_and_replaced_once_lost_is_dropped():
    """`LIVE`: the socket died AFTER the parent was booked (transmit=False,
    no child) — the lane finds it un-transmitted and stop-less, cancels it,
    drops the intent and re-places the pair ONCE. `LOST`: nothing reached the
    broker — dropped at 120 s and re-placed once. Neither is ever placed
    twice; both fill and flatten normally."""
    bars = {"LIVE": _synth_bars("10:00"), "LOST": _synth_bars("10:05"), "X": _synth_bars(None)}
    sim = _synthetic_sim(bars)
    sim.fake.transport_error_on_place("LIVE", live=True)
    sim.fake.transport_error_on_place("LOST", live=False)
    assert sim.run() == 0
    assert _count(sim, "order_ambiguous", symbol="LIVE") == 1
    assert _count(sim, "entry_cancelled", symbol="LIVE", reason="lost_intent") == 1
    assert _count(sim, "intent_dropped", symbol="LIVE") == 1 and _count(sim, "order_placed", symbol="LIVE") == 1
    assert _count(sim, "order_ambiguous", symbol="LOST") == 1 and _count(sim, "intent_dropped", symbol="LOST") == 1
    assert _count(sim, "order_placed", symbol="LOST") == 1        # re-placed exactly once
    dropped_lost = _events(sim, "intent_dropped")
    assert any(r["symbol"] == "LOST" and r["age_s"] >= 120 for r in dropped_lost)
    live_parents = [t for t in _own_parents() if t.contract.symbol == "LIVE"]
    assert len(live_parents) == 2 and [t.orderStatus.status for t in live_parents] == ["Cancelled", "Filled"]
    assert live_parents[0].order.transmit is False and live_parents[0].orderStatus.filled == 0
    assert len([t for t in _own_parents() if t.contract.symbol == "LOST"]) == 1
    assert sim.names()["LIVE"].filled_qty == 125 and sim.names()["LOST"].filled_qty == 125
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())
    assert sim.max_pos_without_stop == 0


# ── 11. disconnect mid-session; stop hits during the outage; reconnect on 61 ─
def test_disconnect_at_1040_reconnect_recovers_no_entries_in_outage(recorded):
    sim = _fixture_sim(recorded)
    sim.run_until(10, 0)
    held = {s for s, n in sim.names().items() if n.held_qty > 0}
    assert held
    sim.fake.disconnect_at(at_et(DAY, 10, 40), 900)                 # down 10:40 -> 10:55
    assert sim.run() == 0
    down_from, up_at = at_et(DAY, 10, 40).isoformat(timespec="seconds"), at_et(DAY, 10, 55).isoformat(timespec="seconds")
    assert not [r for r in _events(sim, "order_placed") if down_from <= r["ts_et"] < up_at]
    assert _count(sim, "disconnected") == 1 and _count(sim, "reconnected") == 1
    rec = [r for r in _events(sim, "recovery_done")]
    assert len(rec) == 1 and rec[0]["ts_et"] >= up_at
    ids = {int(c) for f in [sim.fake] for c in f.connect_calls}
    assert ids == {61}                                               # never another clientId
    assert not _dup_parents() and len(_live_stops()) == 0
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())
    assert sim.max_pos_without_stop == 0


# ── gateway down: fail-closed for orders AND for exit (finding 2026-09-13) ───
def test_gateway_down_all_day_places_nothing_pages_once_and_exits_clean_at_1600(recorded):
    sim = _fixture_sim(recorded)
    sim.fake.down = True
    code = sim.run(until=(16, 10, 0))
    assert code == 0 and sim.lane.s.phase == "done"
    assert FakeIB.book() == [] and _count(sim, "order_placed") == 0
    gw = [p for p in sim.pages if "gateway" in p[1].lower()]
    assert len(gw) == 1 and len(sim.pages) == 1
    assert _count(sim, "session_closed", how="gateway_down") == 1
    assert _count(sim, "connect_failed") <= 30                # bounded: first, reason change, every 30th attempt
    assert not paths.UNFLATTENED_FILE.exists()
    # the T_ready/rank plan was still recorded (Polygon is independent of the gateway)
    assert _count(sim, "rank") == 1 and _count(sim, "name_skipped", reason="window_closed") >= 15
    assert all(n.state == "skipped" for n in sim.names().values())     # every rank name closed at 10:25, unplaced


def test_gateway_down_at_restart_with_positions_marks_unflattened_and_exits_2(recorded):
    sim = _fixture_sim(recorded)
    sim.run_until(10, 30)
    held = {s for s, n in sim.names().items() if n.held_qty > 0}
    assert held
    sim.kill_lane()
    sim.fake.down = True
    sim.lane_start = at_et(DAY, 15, 50)
    code = sim.run(until=(16, 15, 0))
    assert code == 2 and sim.lane.s.phase == "unflattened"
    assert paths.UNFLATTENED_FILE.exists()
    marker = json.loads(paths.UNFLATTENED_FILE.read_text())
    assert marker["reason"] == "gateway_down" and held <= set(marker["symbols"])
    assert any("UNFLATTENED" in p[1] for p in sim.pages)
    unfl = _events(sim, "unflattened")
    assert len(unfl) == 1 and unfl[0]["reason"] == "gateway_down"
    assert _count(sim, "order_placed") == _count(sim, "order_placed")   # no placement after the restart
    assert not [r for r in sim.lane.ledger.records if r["event"] == "order_placed"]


# ── (a) generic parent reject through the real lane ─────────────────────────
def test_generic_parent_reject_is_ledgered_once_and_consumes_no_slot():
    bars = {"X": _synth_bars("09:40"), "OK": _synth_bars("09:45")}
    sim = _synthetic_sim(bars)
    sim.fake.reject("X", 201, "Order rejected - reason: insufficient buying power")
    assert sim.run() == 0
    assert _count(sim, "order_rejected", symbol="X") == 1 and _count(sim, "short_unavailable") == 0
    x = sim.names()["X"]
    assert x.state == "rejected" and x.slot_index is None and x.filled_qty == 0 and x.reject_code == 201
    assert not [f for f in FakeIB.executions(TAG) if f.contract.symbol == "X"]
    assert sim.names()["OK"].filled_qty == 125 and sim.lane.s.slots_used == 1
    assert _count(sim, "session_closed") == 1 and all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())


# ── (b) the 10:25 cancel races a fill: HELD with a live stop, no entry_cancelled ─
def test_1025_cancel_racing_a_fill_is_held_with_a_live_stop_not_cancelled():
    bars = {"Y": _synth_bars(None), "OK": _synth_bars("09:45")}
    sim = _synthetic_sim(bars, tif="DAY")              # DAY: only the explicit 10:25 cancel closes Y
    sim.fake.cancel_races_fill("Y")
    sim.run_until(10, 24, 45)
    assert sim.names()["Y"].state == "resting"
    sim.run_until(10, 27)
    y = sim.names()["Y"]
    assert y.filled_qty == 125 and y.state == "held" and y.slot_index is not None
    assert _count(sim, "entry_cancelled", symbol="Y") == 0         # pinned: a raced fill is never "cancelled"
    assert _count(sim, "entry_filled", symbol="Y") == 1 and "cancel_raced_fill:window_close" in y.notes
    assert "Y" in {t.contract.symbol for t in _live_stops()}
    assert sim.run() == 0
    assert _count(sim, "flattened", symbol="Y") == 1
    assert all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values()) and sim.max_pos_without_stop == 0


# ── (c) scenario 8: partial stop fill then the 15:55 flatten of the residual ─
def test_partial_stop_fill_then_1555_flattens_the_residual_only():
    stop_bar = {"10:00": (10.52, 10.53, 10.10, 10.50)}          # dips through hard (~10.13) once, closes above
    bars = {"Z": _synth_bars("09:40", later=stop_bar)}
    sim = _synthetic_sim(bars)
    sim.fake.stop_partial("Z", 40)
    assert sim.run() == 0
    assert _count(sim, "stop_partial", symbol="Z") == 1 and _count(sim, "stopped", symbol="Z") == 0
    sp = _events(sim, "stop_partial")[0]
    assert sp["filled"] == 40 and sp["held_qty"] == 85
    fl = _events(sim, "flattened")
    assert len(fl) == 1 and fl[0]["symbol"] == "Z" and fl[0]["qty"] == 125 and fl[0]["r_booked"] is not None
    z = sim.names()["Z"]
    assert z.exit_qty == 125 and z.held_qty == 0 and z.state == "flattened"
    stop_fills = [f for f in FakeIB.executions(TAG) if f.contract.symbol == "Z"
                  and FakeIB._BROKER_BOOK[f.execution.permId].order.orderType == "STP"]
    mkt_fills = [f for f in FakeIB.executions(TAG) if f.contract.symbol == "Z"
                 and FakeIB._BROKER_BOOK[f.execution.permId].order.orderType == "MKT"]
    assert [f.execution.shares for f in stop_fills] == [40] and [f.execution.shares for f in mkt_fills] == [85]
    assert abs(FakeIB.net_by_symbol(TAG)["Z"]) < 1e-9 and sim.max_pos_without_stop == 0


# ── (d) scenario 22 edges: stop AND flatten both fill; OCA-join rejected ─────
def test_stop_and_flatten_both_fill_reverse_is_closed_once_and_net_is_zero():
    later = {"15:55": (10.9, 10.95, 10.0, 10.05)}               # MKT fills at :15, the stop at the :30 print
    bars = {"B": _synth_bars("09:40", later=later)}
    sim = _synthetic_sim(bars)
    sim.fake.stop_and_mkt_both_fill = True
    assert sim.run() == 0
    assert _count(sim, "reverse_position_closed", symbol="B") == 1
    # the first exit execution booked closes the name (stop or flatten — both fill on the
    # same print); the SECOND is the double fill and is closed as a reverse, never re-booked
    assert _count(sim, "flattened", symbol="B") + _count(sim, "stopped", symbol="B") == 1
    b = sim.names()["B"]
    assert b.reverse_closed and any(n.startswith("reverse_closed_exec:") for n in b.notes)
    fills = [f for f in FakeIB.executions(TAG) if f.contract.symbol == "B"]
    assert len(fills) == 4                                       # entry, flatten, stop, reverse close
    assert abs(FakeIB.net_by_symbol(TAG)["B"]) < 1e-9
    assert any("reverse" in p[1] for p in sim.pages)
    assert not _live_stops() and sim.max_pos_without_stop == 0


def test_oca_join_rejected_falls_back_to_plain_mkt_then_cancels_the_child(recorded):
    sim = _fixture_sim(recorded)
    sim.fake.oca_join_rejected = True
    assert sim.run() == 0
    flats = _events(sim, "flatten_placed")
    plain = [f for f in flats if f["mode"] == "plain"]
    assert len(plain) == 1 and all(f["mode"] == "oca" for f in flats if f is not plain[0])   # one race, then normal
    assert _count(sim, "orphan_order_cancelled", symbol=plain[0]["symbol"], role="stop") == 1
    assert _count(sim, "flattened", symbol=plain[0]["symbol"]) == 1
    assert not _live_stops() and all(abs(q) < 1e-9 for q in FakeIB.net_by_symbol(TAG).values())
    assert sim.max_pos_without_stop == 0
