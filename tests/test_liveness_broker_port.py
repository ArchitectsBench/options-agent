"""Hermetic cover for deadman layer 3e (broker-port-dead, 2026-10-12).

Pure decision function, no socket and no production file is touched — the
probe itself lives in deadman.sh and is injected here as a bool
(CLAUDE.md destructive-probe rule: never exercise a guard against production).
"""
import importlib.util
from datetime import datetime
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "liveness_mod", Path(__file__).resolve().parents[1] / "scripts" / "ops" / "liveness.py")
L = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(L)


def _pt(h, m, day=12):          # 2026-10-12 is a Monday
    return datetime(2026, 10, day, h, m, tzinfo=L.PT)


def test_dead_port_past_grace_alerts():
    # the 10-09 / 10-12 case: live session, nothing listening on 4002
    assert L.broker_port_dead(_pt(10, 0), port_open=False) is True


def test_live_port_is_quiet():
    assert L.broker_port_dead(_pt(10, 0), port_open=True) is False


def test_grace_window_belongs_to_await_broker_activation():
    # before open+30 the at-open layer owns the page; we must not double-page
    assert L.broker_port_dead(_pt(6, 35), port_open=False) is False
    assert L.broker_port_dead(_pt(6, 59), port_open=False) is False
    assert L.broker_port_dead(_pt(7, 0), port_open=False) is True


def test_out_of_session_is_quiet():
    assert L.broker_port_dead(_pt(5, 0), port_open=False) is False
    assert L.broker_port_dead(_pt(13, 30), port_open=False) is False
    assert L.broker_port_dead(_pt(10, 0, day=11), port_open=False) is False   # Sunday


def test_condition_is_session_critical():
    # locks the O-21 membership: distinct in-session slug + 30-min throttle,
    # so an out-of-session alert can never consume the window that matters
    now = _pt(10, 0)
    assert L.slug_for("broker-port-dead", now) == "broker-port-dead-in-session"
    assert L.throttle_seconds("broker-port-dead", now) == L.IN_SESSION_THROTTLE_S
