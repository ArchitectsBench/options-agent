"""Hermetic tests for scripts/ops/step_timing.sh (nightly-chain per-step timing).

Every test points TIMING_FILE at a tmp_path file — never a production log (the
CLAUDE.md destructive-probe rule: verify the guard, not the blast radius).
No network, no .venv subprocess, no production file read or written.
"""
import json
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "step_timing.sh"


def _run(body: str, timing_file: Path | None = None, extra_env: str = "") -> str:
    """Source the library under the chain's own `set -euo pipefail` and run body."""
    script = f"set -euo pipefail\n{extra_env}\n. '{LIB}'\n{body}\n"
    if timing_file is not None:
        script = f"TIMING_FILE='{timing_file}'\n" + script
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr}"
    return proc.stdout


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def test_emits_one_json_line_with_numeric_seconds(tmp_path):
    tf = tmp_path / "timing.jsonl"
    _run('start=$(timing_now); emit_timing ops_checks "$start" 0', tf)
    rows = _lines(tf)
    assert len(rows) == 1
    assert rows[0]["step"] == "ops_checks"
    assert isinstance(rows[0]["seconds"], int) and rows[0]["seconds"] >= 0
    assert rows[0]["rc"] == "0"
    assert rows[0]["finished_at"].startswith("20")


def test_measures_elapsed_wall_clock(tmp_path):
    tf = tmp_path / "timing.jsonl"
    _run('start=$(timing_now); sleep 2; emit_timing slow_step "$start" 0', tf)
    # The point of the whole library: a slow step must be nameable by duration.
    assert _lines(tf)[0]["seconds"] >= 2


def test_appends_per_step_so_a_partial_file_is_readable(tmp_path):
    # An overrun night is still running at the 19:10 checkpoint, so each line
    # must land as its step finishes rather than being buffered to chain end.
    tf = tmp_path / "timing.jsonl"
    _run(
        'for s in a b c; do start=$(timing_now); emit_timing "$s" "$start" 0; done',
        tf,
    )
    assert [r["step"] for r in _lines(tf)] == ["a", "b", "c"]


def test_records_failing_step_rc(tmp_path):
    tf = tmp_path / "timing.jsonl"
    _run('start=$(timing_now); emit_timing cll_nightly "$start" 7', tf)
    assert _lines(tf)[0]["rc"] == "7"


def test_unwritable_timing_path_does_not_fail_the_step(tmp_path):
    # Fail-safe contract: the chain is load-bearing, so a timing write that
    # cannot happen must not abort it under `set -euo pipefail`. _run asserts
    # rc==0, so reaching the echo at all is the proof.
    out = _run(
        'start=$(timing_now); emit_timing s "$start" 0; echo STEP_SURVIVED',
        tmp_path / "no_such_dir" / "timing.jsonl",
    )
    assert "STEP_SURVIVED" in out


def test_unset_timing_file_is_timing_off_not_an_error(tmp_path):
    out = _run('start=$(timing_now); emit_timing s "$start" 0; echo STEP_SURVIVED')
    assert "STEP_SURVIVED" in out


def test_non_numeric_start_cannot_abort_the_chain(tmp_path):
    # A non-integer bound would be a fatal arithmetic error under `set -e`;
    # the library validates both bounds before any arithmetic.
    tf = tmp_path / "timing.jsonl"
    out = _run('emit_timing s "not-a-number" 0; echo STEP_SURVIVED', tf)
    assert "STEP_SURVIVED" in out
    assert not tf.exists() or _lines(tf) == []


def test_step_name_defaults_rather_than_tripping_unset_var_guard(tmp_path):
    tf = tmp_path / "timing.jsonl"
    out = _run('emit_timing; echo STEP_SURVIVED', tf)
    assert "STEP_SURVIVED" in out


def test_chain_script_sources_the_library_and_times_every_step():
    """The wiring, not just the library: pin the four call sites."""
    chain = (LIB.parents[2] / "scripts" / "nightly_ops.sh").read_text()
    assert ". \"$ROOT/scripts/ops/step_timing.sh\"" in chain
    assert 'TIMING_FILE="$ROOT/logs/ops/nightly_timing-$DATE.jsonl"' in chain
    # run_step wrapper, the un-wrapped agent step, and the chain total.
    assert 'emit_timing "$name" "$start" "$rc" || true' in chain
    assert 'emit_timing agent_interpretation "$AGENT_START" "$agent_rc" || true' in chain
    assert 'emit_timing chain_total "$CHAIN_START" 0 || true' in chain


def test_agent_timestamps_are_taken_outside_the_command_substitution():
    """An echo inside OUT=$(...) would corrupt the ===READINESS=== split."""
    chain = (LIB.parents[2] / "scripts" / "nightly_ops.sh").read_text()
    start_at = chain.index("AGENT_START=$(timing_now")
    subst_at = chain.index("OUT=$(cd \"$ROOT\"")
    emit_at = chain.index("emit_timing agent_interpretation")
    assert start_at < subst_at < emit_at
