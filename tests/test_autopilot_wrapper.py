"""Autopilot wrapper (scripts/ops/autopilot.sh, 2026-09-14).

The wrapper is the only thing between launchd and an unattended Claude session,
so its mechanics are pinned here against a stub `claude` and a stub sender in a
tmp root (never the real one): a completed run is recorded and silent; a run
that fails or omits the completion line pages the operator; the kill switch and
the lock skip the run without invoking Claude; a stale lock is broken; the
checkpoint reaches the prompt; git push is disallowed at the tool layer.
"""
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "ops" / "autopilot.sh"


def _setup(tmp_path, claude_body):
    root = tmp_path / "root"
    (root / "docs" / "autopilot").mkdir(parents=True)
    (root / "docs" / "autopilot" / "CHARTER.md").write_text("CHARTER TEXT\n")
    calls = tmp_path / "claude_calls.txt"
    sent = tmp_path / "sent.txt"
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" >> " + str(calls) + "\n" + claude_body + "\n")
    claude.chmod(0o755)
    send = tmp_path / "send.sh"
    send.write_text("#!/bin/bash\nprintf '%s|%s|%s\\n' \"$1\" \"$2\" \"$4\" >> " + str(sent) + "\n")
    send.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AUTOPILOT_ROOT": str(root),
           "CLAUDE_BIN": str(claude), "AUTOPILOT_NTFY": str(send),
           "AUTOPILOT_CHECKPOINT": "evening", "AUTOPILOT_TIMEOUT_S": "20"}
    return root, calls, sent, env


def _run(env):
    return subprocess.run(["bash", str(WRAPPER)], env=env, capture_output=True, text=True, timeout=60)


def _runs(root):
    p = root / "logs" / "autopilot" / "runs.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_completed_run_is_recorded_and_silent(tmp_path):
    root, calls, sent, env = _setup(tmp_path, 'echo "work done"; echo "===AUTOPILOT-DONE=== evening ok"')
    assert _run(env).returncode == 0
    assert _runs(root)[-1]["status"] == "ok" and _runs(root)[-1]["checkpoint"] == "evening"
    assert not sent.exists()
    args = calls.read_text()
    assert "CHARTER TEXT" in args and "CHECKPOINT: evening" in args
    assert "Bash(git push:*)" in args and "--max-budget-usd" in args
    assert not (root / "data" / "autopilot.lock").exists()          # lock released


def test_failed_or_incomplete_run_pages_the_operator(tmp_path):
    root, _, sent, env = _setup(tmp_path, 'echo "started, then died"; exit 3')
    assert _run(env).returncode == 1
    assert _runs(root)[-1]["status"] == "failed"
    title, prio, body = sent.read_text().strip().split("|", 2)
    assert "AUTOPILOT RUN FAILED: evening" in title and prio == "urgent" and "exit 3" in body

    root2, _, sent2, env2 = _setup(tmp_path / "b", 'echo "finished but forgot the line"')
    assert _run(env2).returncode == 1
    assert "no completion line" in sent2.read_text()


def test_kill_switch_and_fresh_lock_skip_without_calling_claude(tmp_path):
    root, calls, sent, env = _setup(tmp_path, 'echo "===AUTOPILOT-DONE=== evening ok"')
    (root / "ops").mkdir()
    (root / "ops" / "AUTOPILOT_OFF").write_text("")
    assert _run(env).returncode == 0 and _runs(root)[-1]["status"] == "skipped_off"
    (root / "ops" / "AUTOPILOT_OFF").unlink()
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "autopilot.lock").mkdir()
    assert _run(env).returncode == 0 and _runs(root)[-1]["status"] == "skipped_locked"
    assert not calls.exists() and not sent.exists()


def test_stale_lock_is_broken(tmp_path):
    root, calls, _, env = _setup(tmp_path, 'echo "===AUTOPILOT-DONE=== evening ok"')
    lock = root / "data" / "autopilot.lock"
    lock.mkdir(parents=True)
    old = time.time() - 60 * 60
    os.utime(lock, (old, old))
    assert _run(env).returncode == 0 and _runs(root)[-1]["status"] == "ok" and calls.exists()


def test_checkpoint_is_derived_from_the_clock_when_not_forced(tmp_path):
    root, calls, _, env = _setup(tmp_path, 'echo "===AUTOPILOT-DONE=== x ok"')
    env.pop("AUTOPILOT_CHECKPOINT")
    _run(env)
    cp = _runs(root)[-1]["checkpoint"]
    assert cp in {"pre_open", "post_open", "mid_session", "post_close", "evening", "weekend"}
    assert f"CHECKPOINT: {cp}" in calls.read_text()
