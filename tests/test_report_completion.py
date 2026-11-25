"""Hermetic tests for scripts/ops/report_completion.sh.

The nightly chain's last line claimed "nightly ops complete: <report>, <journal>"
unconditionally, so 2026-10-29 printed it for two files that were never written
(agent step exited 1: "Your computer went to sleep mid-response"). These tests
pin the line to the FILES.

Every path is under tmp_path — never a production log (CLAUDE.md destructive-probe
rule: verify the guard, not the blast radius). No network, no .venv subprocess.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "ops" / "report_completion.sh"
CHAIN = ROOT / "scripts" / "nightly_ops.sh"


def _run(report: Path, journal: Path, lib: Path = LIB) -> tuple[int, str]:
    """Source the library under the chain's own `set -euo pipefail`."""
    script = (
        "set -euo pipefail\n"
        f". '{lib}'\n"
        f"completion_line '{report}' '{journal}' || rc=$?\n"
        'exit "${rc:-0}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.stderr == "", proc.stderr
    return proc.returncode, proc.stdout.strip()


def test_both_files_written_reports_complete(tmp_path):
    report = tmp_path / "report-2026-10-30.md"
    journal = tmp_path / "2026-10-30.md"
    report.write_text("READINESS: RED\n")
    journal.write_text("journal body\n")
    rc, out = _run(report, journal)
    assert rc == 0
    assert out == f"nightly ops complete: {report}, {journal}"


def test_the_2026_10_29_shape_is_reported_incomplete(tmp_path):
    # The real regression: the agent step failed, the empty-$OUT branch wrote
    # NEITHER file, and the chain still printed "complete".
    report = tmp_path / "report-2026-10-29.md"
    journal = tmp_path / "2026-10-29.md"
    rc, out = _run(report, journal)
    assert rc == 1
    assert out.startswith("NIGHTLY OPS INCOMPLETE")
    assert "complete:" not in out  # must not read as success to a grep or an eye
    assert str(report) in out and str(journal) in out
    assert "agent-errors.log" in out


def test_missing_report_alone_is_incomplete_and_names_only_it(tmp_path):
    report = tmp_path / "report.md"
    journal = tmp_path / "journal.md"
    journal.write_text("journal body\n")
    rc, out = _run(report, journal)
    assert rc == 1
    assert f"report={report}" in out
    assert "journal=" not in out


def test_missing_journal_alone_is_incomplete_and_names_only_it(tmp_path):
    report = tmp_path / "report.md"
    journal = tmp_path / "journal.md"
    report.write_text("READINESS: RED\n")
    rc, out = _run(report, journal)
    assert rc == 1
    assert f"journal={journal}" in out
    assert "report=" not in out


def test_empty_files_are_incomplete_not_complete(tmp_path):
    # The 2026-07-16 marker-less-output bug split into two EMPTY files; an
    # existence check (-e) would have called that a complete night.
    report = tmp_path / "report.md"
    journal = tmp_path / "journal.md"
    report.write_text("")
    journal.write_text("")
    rc, out = _run(report, journal)
    assert rc == 1
    assert out.startswith("NIGHTLY OPS INCOMPLETE")


def test_both_missing_names_both(tmp_path):
    rc, out = _run(tmp_path / "r.md", tmp_path / "j.md")
    assert rc == 1
    assert "report=" in out and "journal=" in out


def test_fallback_definition_keeps_the_chain_running_without_the_library(tmp_path):
    """Sourcing is optional: a missing library must not kill the chain."""
    absent = tmp_path / "absent.sh"
    script = (
        "set -euo pipefail\n"
        f"if [ ! -r '{absent}' ] || ! . '{absent}' 2>/dev/null; then\n"
        '    completion_line() { echo "nightly ops complete: $1, $2"; return 0; }\n'
        "fi\n"
        "completion_line a b\n"
        'echo "REACHED END"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0
    assert proc.stdout.split() [:1] != []  # produced output at all
    assert "nightly ops complete: a, b" in proc.stdout
    assert "REACHED END" in proc.stdout


def test_bare_source_of_a_missing_file_kills_the_shell(tmp_path):
    """Pins the measurement the readability guard exists for.

    On this host (bash 3.2.57, arm64-darwin25) `.` on an unreadable file exits a
    non-interactive shell outright — even inside an `if !` condition. The bare
    form therefore does NOT provide the optional-sourcing fail-safe that
    nightly_ops.sh's comments promise, which is why both source sites carry a
    `[ ! -r ... ] ||` guard. If a future bash makes the bare form safe, this
    test fails loudly and the guards can be revisited deliberately.
    """
    absent = tmp_path / "absent.sh"
    script = (
        "set -euo pipefail\n"
        f"if ! . '{absent}' 2>/dev/null; then\n"
        '    echo "FALLBACK RAN"\n'
        "fi\n"
        'echo "REACHED END"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode != 0
    assert "FALLBACK RAN" not in proc.stdout
    assert "REACHED END" not in proc.stdout


def test_chain_guards_both_optional_source_sites(tmp_path):
    """The latent single point of failure found 2026-10-30 stays fixed.

    A missing step_timing.sh would otherwise kill the whole nightly chain at the
    source line — before ops_checks runs and before step_alert is even defined,
    so with no page and no log line.
    """
    text = CHAIN.read_text()
    for lib in ("step_timing.sh", "report_completion.sh"):
        assert f'[ ! -r "$ROOT/scripts/ops/{lib}" ] || ! . "$ROOT/scripts/ops/{lib}"' in text, lib
        assert f'if ! . "$ROOT/scripts/ops/{lib}" 2>/dev/null; then' not in text, lib


def test_chain_sources_the_library_and_no_longer_claims_unconditionally():
    """Guard the wiring, not just the library."""
    text = CHAIN.read_text()
    assert "report_completion.sh" in text
    assert 'completion_line "$ROOT/logs/ops/report-$DATE.md"' in text
    # the unconditional claim must be gone from the chain's own last line
    assert 'echo "nightly ops complete: logs/ops/report-$DATE.md' not in text


def test_library_is_syntactically_valid_bash():
    proc = subprocess.run(
        ["bash", "-n", str(LIB)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr


def test_chain_is_syntactically_valid_bash():
    proc = subprocess.run(
        ["bash", "-n", str(CHAIN)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
