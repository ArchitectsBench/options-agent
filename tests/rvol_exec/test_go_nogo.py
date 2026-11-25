"""Go/no-go checks against FakeIB scenarios (spec §16.2 `test_go_nogo.py`).
Hermetic: paths under tmp, no socket. UNVALIDATED — PAPER ONLY.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

import src.rvol_exec.paths as paths
from src.rvol_exec.clock import FakeClock, at_et
from src.rvol_exec.constants import CLIENT_ID, GO_NOGO_CLIENT_ID, TAG
from tests.rvol_exec.fake_ib import FakeIB

ROOT = Path(__file__).resolve().parents[2]
DAY = "2026-09-14"


@pytest.fixture
def gn():
    spec = importlib.util.spec_from_file_location("rvol_exec_go_nogo", ROOT / "scripts" / "ops" / "rvol_exec_go_nogo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh_prep(day=DAY):
    paths.PREP_DIR.mkdir(parents=True, exist_ok=True)
    (paths.PREP_DIR / f"{day}.meta.json").write_text(json.dumps({
        "prep_for": day, "built_from_through": "2026-09-11",
        "coverage": {"candidates": 900, "with_baseline14": 880}}))


def test_offline_fail_writes_hold_under_the_lane_dir_advisory_does_not(gn, capsys):
    rep = gn.run_checks(advisory=True, offline=True, today=DAY, write_report=False)
    assert "r1" in rep.failed and "r0" not in rep.failed
    assert not paths.HOLD_FILE.exists()
    rep = gn.run_checks(advisory=False, offline=True, today=DAY, write_report=False)
    assert paths.HOLD_FILE.exists() and "go_nogo_fail" in paths.HOLD_FILE.read_text()
    assert str(paths.HOLD_FILE).startswith(str(paths.LANE_DIR))
    out = capsys.readouterr().out
    assert "verdict|NO-GO" in out and "r0|PASS" in out
    paths.HOLD_FILE.unlink()
    _fresh_prep()
    rep = gn.run_checks(advisory=False, offline=True, today=DAY, write_report=True)
    assert rep.failed == [] and not paths.HOLD_FILE.exists()
    assert (paths.GO_NOGO_LOG_DIR / f"rvol_exec_go_nogo-{DAY}.txt").exists()


def test_broker_probes_use_161_and_61_only_for_the_availability_check(gn):
    _fresh_prep()
    clock = FakeClock(at_et(DAY, 11, 0))
    fakes = []

    def factory():
        f = FakeIB(clock=clock)
        f.set_mark("IBM", 100.0).set_mark("AAPL", 200.0)
        fakes.append(f)
        return f

    rep = gn.run_checks(advisory=True, offline=False, today=DAY, ib_factory=factory, write_report=False,
                        broker=True)
    status = {c: s for c, s, _ in rep.lines}
    assert status["r2"] == "PASS" and status["r2a"] == "PASS" and status["r3"] == "PASS"
    assert status["r4"] == "PASS" and status["r4x"] == "PASS"
    assert status["r7"] == "PASS" and status["r8"] == "PASS" and status["r9"] == "PASS"
    ids = [cid for f in fakes for cid in f.connect_calls]
    assert set(ids) == {GO_NOGO_CLIENT_ID, CLIENT_ID} and ids.count(CLIENT_ID) == 1
    # the probe order was tagged, never fillable, and is gone
    probes = [t for t in FakeIB.book() if t.contract.symbol == "IBM"]
    assert probes and all(t.order.orderRef == TAG for t in probes)
    assert all(t.orderStatus.status == "Cancelled" and t.orderStatus.filled == 0 for t in probes)
    assert FakeIB.executions(TAG) == []
    assert not paths.HOLD_FILE.exists()


def test_non_paper_account_and_stuck_61_fail(gn):
    _fresh_prep()
    clock = FakeClock(at_et(DAY, 11, 0))
    rep = gn.run_checks(advisory=True, offline=False, today=DAY,
                        ib_factory=lambda: FakeIB(accounts=["U1234567"], clock=clock), write_report=False,
                        broker=True)
    status = {c: s for c, s, _ in rep.lines}
    assert status["r2"] == "FAIL"

    def stuck():
        f = FakeIB(clock=clock)
        f.in_use = {CLIENT_ID}
        f.set_mark("IBM", 100.0).set_mark("AAPL", 200.0)
        return f
    rep = gn.run_checks(advisory=True, offline=False, today=DAY, ib_factory=stuck, write_report=False, broker=True)
    status = {c: s for c, s, _ in rep.lines}
    assert status["r2"] == "PASS" and status["r2a"] == "FAIL"


def test_r7_flags_unflattened_marker_and_own_open_orders(gn):
    _fresh_prep()
    paths.UNFLATTENED_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.UNFLATTENED_FILE.write_text(json.dumps({"symbols": {"X": 1}}))
    rep = gn.run_checks(advisory=True, offline=True, today=DAY, write_report=False)
    assert "r7" in rep.failed
    paths.UNFLATTENED_FILE.unlink()
    rep = gn.run_checks(advisory=True, offline=True, today=DAY, write_report=False)
    assert "r7" not in rep.failed


def test_cli_refuses_under_pytest(gn):
    assert gn.main(["--offline", "--advisory"]) == 3


def test_default_invocation_never_constructs_a_client_or_connects(gn):
    """Without `--i-mean-it` (broker=False) the gateway probes are NA: no client,
    no connect, no order — an accidental bare run touches nothing."""
    _fresh_prep()
    calls = []

    def factory():
        calls.append(1)
        raise AssertionError("ib_factory must never be called without --i-mean-it")

    rep = gn.run_checks(advisory=True, today=DAY, ib_factory=factory, write_report=False)
    status = {c: s for c, s, _ in rep.lines}
    assert calls == [] and FakeIB.book() == []
    assert all(status[c] == "NA" for c in ("r2", "r2a", "r3", "r4", "r5", "r10"))
    assert "--i-mean-it" in [d for c, s, d in rep.lines if c == "r2"][0]
    assert status["r0"] == "PASS" and status["r1"] == "PASS" and "r7" in status


def test_arm_round_trip_against_the_fake_broker(gn):
    clock = FakeClock(at_et(DAY, 11, 0))
    fakes = []

    def factory():
        f = FakeIB(clock=clock)
        f.mkt_fill_immediately = True
        f.set_mark("F", 10.0)
        fakes.append(f)
        return f

    rep = gn.Report()
    gn.run_arm(rep, ib_factory=factory, sym="F", rth_check=False)
    status = {c: s for c, s, _ in rep.lines}
    assert status["arm1"] == "PASS" and status["arm2"] == "PASS" and status["arm3"] == "PASS"
    assert status["arm4"] == "PASS" and status["arm5"] == "PASS" and status["arm6"] == "PASS"
    assert status["arm7"] == "NA" and status["arm_cleanup"] == "PASS" and "armx" not in status
    # only clientId 61 was ever used; exactly 2 shares round-tripped; net 0; every order tagged; STP cancelled by OCA
    assert {cid for f in fakes for cid in f.connect_calls} == {CLIENT_ID}
    fills = FakeIB.executions(TAG)
    assert [f.execution.side for f in fills] == ["BOT", "SLD"] and all(f.contract.symbol == "F" for f in fills)
    assert FakeIB.net_by_symbol(TAG) == {"F": 0}
    assert all(t.order.orderRef == TAG for t in FakeIB.book())
    stp = [t for t in FakeIB.book() if t.order.orderType == "STP"]
    assert len(stp) == 1 and stp[0].orderStatus.status == "Cancelled"
    mkt = [t for t in FakeIB.book() if t.order.orderType == "MKT"]
    assert len(mkt) == 2 and mkt[1].order.ocaGroup == stp[0].order.ocaGroup


def test_arm_refuses_beside_a_live_daemon_pid(gn):
    import os
    paths.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.PID_FILE.write_text(str(os.getpid()))
    rep = gn.Report()
    gn.run_arm(rep, ib_factory=lambda: FakeIB(), sym="F", rth_check=False)
    assert [c for c, s, _ in rep.lines] == ["arm1"] and rep.failed == ["arm1"]
    assert FakeIB.book() == []


# ── bounded run: a HUNG probe must fail closed (queue item 2026-10-10) ────────
# The 2026-10-05 incident: port 4002 ACCEPTED and then stalled, so the
# post-connect waits hung ~15 h 21 m and NO-GO was written only when the
# gateway returned. A merely-absent gateway (ECONNREFUSED) fails fast and does
# NOT reproduce it, so these stubs hang a POST-CONNECT check, never connect.

def _hang_forever(*a, **kw):
    threading.Event().wait()          # never returns, never raises


def test_hung_post_connect_probe_writes_nogo_and_hold(gn, monkeypatch):
    _fresh_prep()
    monkeypatch.setattr(gn, "check_r3", _hang_forever)
    clock = FakeClock(at_et(DAY, 8, 31))
    t0 = time.monotonic()
    rep = gn.run_checks_bounded(
        timeout_s=0.5, advisory=False, offline=False, today=DAY,
        ib_factory=lambda: FakeIB(accounts=["U1234567"], clock=clock), broker=True)
    assert rep is None                                  # abandoned, not awaited
    assert time.monotonic() - t0 < 10                   # bounded, not ~15 h
    out = paths.GO_NOGO_LOG_DIR / f"rvol_exec_go_nogo-{DAY}.txt"
    assert out.exists(), "a hung probe must still leave a dated verdict"
    body = out.read_text()
    assert "verdict|NO-GO" in body and "timeout" in body
    assert paths.HOLD_FILE.exists()
    assert "go_nogo_timeout" in json.loads(paths.HOLD_FILE.read_text())["reason"]


def test_hung_probe_advisory_writes_nogo_but_no_hold(gn, monkeypatch):
    _fresh_prep()
    monkeypatch.setattr(gn, "check_r3", _hang_forever)
    clock = FakeClock(at_et(DAY, 8, 31))
    rep = gn.run_checks_bounded(
        timeout_s=0.5, advisory=True, offline=False, today=DAY,
        ib_factory=lambda: FakeIB(accounts=["U1234567"], clock=clock), broker=True)
    assert rep is None
    assert "verdict|NO-GO" in (paths.GO_NOGO_LOG_DIR / f"rvol_exec_go_nogo-{DAY}.txt").read_text()
    assert not paths.HOLD_FILE.exists(), "--advisory must never write HOLD, timeout included"


def test_bounded_run_is_transparent_when_checks_complete(gn):
    _fresh_prep()
    rep = gn.run_checks_bounded(timeout_s=30, advisory=True, offline=True, today=DAY,
                                write_report=False)
    assert rep is not None
    assert [c for c, s, _ in rep.lines if s not in ("PASS", "FAIL", "NA")] == []


def test_write_timeout_verdict_honours_write_report_false(gn):
    gn.write_timeout_verdict(DAY, advisory=True, timeout_s=1.0, write_report=False)
    assert not (paths.GO_NOGO_LOG_DIR / f"rvol_exec_go_nogo-{DAY}.txt").exists()
