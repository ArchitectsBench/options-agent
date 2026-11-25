"""Hermetic tests for scripts/ops/cockpit_stale_kickstart.py.

No launchctl is ever invoked (the kickstart is injected), no production file is
written (every receipt lands under tmp_path) — CLAUDE.md destructive-probe
rule: prove the guard, not the blast radius.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))

import cockpit_stale_kickstart as csk  # noqa: E402

NOW = datetime(2026, 10, 28, 19, 10, 0)

STALE = {"stale": True,
         "trader": {"pid": 23175, "proc_started": "Wed Oct 28 06:20:03 2026",
                    "latest_code_commit": "2026-10-28T06:20:28", "stale": False},
         "cockpitapi": {"pid": 764, "proc_started": "Sat Oct 24 00:32:22 2026",
                        "latest_code_commit": "2026-10-28T06:20:28", "stale": True}}

FRESH = {"stale": False,
         "cockpitapi": {"pid": 9001, "proc_started": "Wed Oct 28 19:05:00 2026",
                        "latest_code_commit": "2026-10-28T06:20:28", "stale": False}}

NO_PID = {"stale": False,
          "cockpitapi": {"pid": None, "proc_started": None,
                         "latest_code_commit": None, "stale": None}}


class Spy:
    """Injected kickstart: records calls, returns a scripted rc."""

    def __init__(self, rc=0, out=""):
        self.rc, self.out, self.calls = rc, out, []

    def __call__(self, label):
        self.calls.append(label)
        return self.rc, self.out


def run(verdict, tmp_path, rc=0, out="", dry_run=False):
    spy = Spy(rc, out)
    receipt = csk.evaluate(stale_fn=lambda: verdict, kickstart=spy, now=NOW,
                           root=tmp_path, dry_run=dry_run)
    return receipt, spy


def test_stale_cockpit_is_kickstarted(tmp_path):
    receipt, spy = run(STALE, tmp_path)
    assert spy.calls == ["com.optionsagent.cockpitapi"]
    assert receipt["state"] == "restarted"
    assert receipt["kickstarted"] is True
    assert csk.exit_code(receipt) == 0


def test_fresh_cockpit_is_left_alone(tmp_path):
    receipt, spy = run(FRESH, tmp_path)
    assert spy.calls == []
    assert receipt["state"] == "fresh"
    assert receipt["kickstarted"] is False
    assert csk.exit_code(receipt) == 0


def test_dry_run_never_kickstarts_a_stale_cockpit(tmp_path):
    receipt, spy = run(STALE, tmp_path, dry_run=True)
    assert spy.calls == []
    assert receipt["state"] == "stale"
    assert receipt["dry_run"] is True
    assert csk.exit_code(receipt) == 0


def test_failed_kickstart_is_exit_1_not_a_silent_pass(tmp_path):
    receipt, spy = run(STALE, tmp_path, rc=3, out="Could not find service")
    assert spy.calls == ["com.optionsagent.cockpitapi"]
    assert receipt["state"] == "kickstart_failed"
    assert receipt["kickstarted"] is False
    assert receipt["kickstart_output"] == "Could not find service"
    assert csk.exit_code(receipt) == 1


def test_no_pid_is_unmeasurable_not_a_breach(tmp_path):
    """The service not running at all must not be reported as staleness, and
    must not provoke a kickstart — exit 2 says 'cannot measure'."""
    receipt, spy = run(NO_PID, tmp_path)
    assert spy.calls == []
    assert receipt["state"] == "unmeasurable"
    assert csk.exit_code(receipt) == 2


def test_missing_cockpitapi_key_is_unmeasurable(tmp_path):
    receipt, spy = run({"stale": False, "trader": {"stale": False}}, tmp_path)
    assert spy.calls == []
    assert receipt["state"] == "unmeasurable"
    assert csk.exit_code(receipt) == 2


def test_non_dict_verdict_does_not_raise(tmp_path):
    receipt, spy = run(["unexpected"], tmp_path)
    assert spy.calls == []
    assert receipt["state"] == "unmeasurable"
    assert csk.exit_code(receipt) == 2


def test_stale_code_raising_is_caught_and_recorded(tmp_path):
    def boom():
        raise RuntimeError("git unavailable")

    spy = Spy()
    receipt = csk.evaluate(stale_fn=boom, kickstart=spy, now=NOW, root=tmp_path)
    assert spy.calls == []
    assert receipt["state"] == "unmeasurable"
    assert "git unavailable" in receipt["detail"]
    assert csk.exit_code(receipt) == 2


@pytest.mark.parametrize("verdict,expected", [(STALE, "restarted"),
                                              (FRESH, "fresh"),
                                              (NO_PID, "unmeasurable")])
def test_dated_receipt_is_written_under_logs_ops(verdict, expected, tmp_path):
    receipt, _ = run(verdict, tmp_path)
    path = tmp_path / "logs" / "ops" / "cockpit_staleness-2026-10-28.json"
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["state"] == expected
    assert on_disk["at"] == "2026-10-28T19:10:00"
    assert on_disk["label"] == "com.optionsagent.cockpitapi"
    assert receipt["receipt_path"] == str(path)


def test_the_real_entrypoint_defaults_are_wired(tmp_path, monkeypatch):
    """main() must reach the real stale_code() and the real launchctl helper —
    asserted by substitution, never by invoking launchctl."""
    seen = {}

    def fake_kickstart(label):
        seen["label"] = label
        return 0, "ok"

    monkeypatch.setattr(csk, "_default_stale_fn", lambda: STALE)
    monkeypatch.setattr(csk, "_default_kickstart", fake_kickstart)
    monkeypatch.setattr(csk, "ROOT", tmp_path)
    rc = csk.main([])
    assert rc == 0
    assert seen["label"] == "com.optionsagent.cockpitapi"
    assert (tmp_path / "logs" / "ops" / f"cockpit_staleness-"
            f"{datetime.now().strftime('%Y-%m-%d')}.json").exists()
