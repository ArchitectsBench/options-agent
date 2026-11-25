"""Deadman library load is fail-LOUD, not fatal and not silent (2026-10-31).

`scripts/deadman.sh` sources `scripts/ops/lib_ops_sweep_stale.sh` for layer 2.
That source used to be bare. The script runs under `set -euo pipefail`
(line 27) and the plist invokes `/bin/bash deadman.sh`, so a missing or
unreadable library exited the shell at the source line — killing layers 2b, 3,
3e, 3f and 4 after layer 1 only, which is precisely the silent-watchman failure
the deadman exists to prevent. A *truncated* library is the other half: it
sources cleanly, `ops_sweep_stale` is then undefined, `if ops_sweep_stale ...`
is false via 127, and the staleness page is disabled with no trace.

These tests run a SANDBOXED COPY of the script with `ROOT` rewritten to a tmp
fixture tree (destructive-probe rule: the live `scripts/deadman.sh`, the real
throttle stamps under `data/deadman/` and the real ntfy sender are never
touched). Everything dangerous in the script hangs off `$ROOT`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REAL_DEADMAN = REPO / "scripts" / "deadman.sh"
REAL_LIB = REPO / "scripts" / "ops" / "lib_ops_sweep_stale.sh"

END_MARKER = "DEADMAN-REACHED-END"

# A python stub standing in for $ROOT/.venv/bin/python. deadman calls it for
# liveness.py helpers; it must answer `slug` and `throttle` so alert() produces
# a named ALERT line and never throttles.
PY_STUB = """#!/bin/bash
for a in "$@"; do
  case "$a" in
    slug)     echo "$3"; exit 0 ;;
    throttle) echo 0; exit 0 ;;
  esac
done
echo ""
exit 0
"""

NTFY_STUB = """#!/bin/bash
echo "NTFY $1" >> "$(dirname "$0")/../../ntfy_calls.log"
exit 0
"""


def _sandbox(tmp_path: Path, *, lib: str | None) -> Path:
    """Build a fixture ROOT and return the sandboxed deadman copy.

    lib=None        -> library absent
    lib="unreadable"-> present, chmod 000
    lib="truncated" -> present and readable, defines no functions
    lib="real"      -> the real library, copied
    """
    root = tmp_path / "root"
    for d in ("data/deadman", "logs", "scripts/ops", ".venv/bin"):
        (root / d).mkdir(parents=True, exist_ok=True)

    py = root / ".venv" / "bin" / "python"
    py.write_text(PY_STUB)
    py.chmod(0o755)

    ntfy = root / "scripts" / "ops" / "ntfy_send.sh"
    ntfy.write_text(NTFY_STUB)
    ntfy.chmod(0o755)

    # Minimal state the earlier layers read. No pid -> layer 1 alerts, which is
    # fine: these tests assert on layer 2's behaviour and on reaching the end.
    (root / "data" / "agent_state.json").write_text('{"pid": null, "last_cycle": null}')
    (root / "logs" / "launchd_nightlyops.log").write_text("")
    # The DEADMAN_URL line must EXIST: line 29 is `URL=$(grep '^DEADMAN_URL=' ...)`
    # and under `set -e` a grep that matches nothing aborts the script before it
    # prints anything at all. Empty value = layer 4 skips the external ping, so
    # the sandbox makes no network calls.
    (root / ".env").write_text("DEADMAN_URL=\n")

    target = root / "scripts" / "ops" / "lib_ops_sweep_stale.sh"
    if lib == "real":
        shutil.copyfile(REAL_LIB, target)
        target.chmod(0o644)
    elif lib == "truncated":
        target.write_text("# truncated: no function definitions\n")
    elif lib == "unreadable":
        target.write_text("ops_sweep_stale() { return 1; }\n")
        target.chmod(0o000)

    # Sandbox the whole script with one rewrite of the ROOT assignment, and
    # append an end marker so "did execution get past the source line?" is
    # observable without depending on the exit status (FAIL=1 is expected here).
    src = REAL_DEADMAN.read_text()
    sandboxed, n = re.subn(
        r'^ROOT="[^"]*"', f'ROOT="{root}"', src, count=1, flags=re.MULTILINE
    )
    assert n == 1, "could not rewrite ROOT= in deadman.sh — sandbox not safe"

    # The marker has to go BEFORE the script's own final `exit` (appending it at
    # the bottom is unreachable and silently asserts nothing).
    lines = sandboxed.splitlines()
    last_exit = max(
        i for i, ln in enumerate(lines) if re.match(r"^\s*exit\b", ln)
    )
    lines.insert(last_exit, f'echo "{END_MARKER}"')
    sandboxed = "\n".join(lines) + "\n"

    copy = root / "deadman_sandboxed.sh"
    copy.write_text(sandboxed)
    copy.chmod(0o755)
    return copy


def _run(copy: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(copy)],  # the plist's own interpreter
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "DEADMAN_TEST": "1"},
    )


def test_sandbox_never_points_at_the_repo(tmp_path: Path) -> None:
    """Guard on the guard: the probe must not be able to touch production."""
    copy = _sandbox(tmp_path, lib="real")
    body = copy.read_text()
    assert str(REPO) not in body
    assert copy != REAL_DEADMAN


@pytest.mark.parametrize("lib", [None, "unreadable", "truncated"])
def test_unloadable_library_pages_and_does_not_kill_the_sweep(
    tmp_path: Path, lib: str | None
) -> None:
    res = _run(_sandbox(tmp_path, lib=lib))
    out = res.stdout + res.stderr
    # (a) it pages, with its own slug
    assert "deadman-lib-missing" in out, out[-2000:]
    # (b) and it keeps going: the later layers (cycle, broker port, ephemeral
    #     ports, healthchecks ping) are the whole point of continuing.
    assert END_MARKER in out, out[-2000:]
    # (c) the page actually reached the sender
    calls = (tmp_path / "root" / "ntfy_calls.log").read_text()
    assert "deadman-lib-missing" in calls


def test_real_library_loads_silently(tmp_path: Path) -> None:
    res = _run(_sandbox(tmp_path, lib="real"))
    out = res.stdout + res.stderr
    assert "deadman-lib-missing" not in out, out[-2000:]
    assert END_MARKER in out, out[-2000:]


def test_bare_source_under_set_e_would_have_been_fatal(tmp_path: Path) -> None:
    """Pins the measured host behaviour the fix is built on.

    If a future bash makes a failed `source` non-fatal under `set -e`, this
    test fails and the comment block in deadman.sh needs rewording — the fix
    itself stays correct either way.
    """
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/bin/bash\nset -euo pipefail\necho BEFORE\n"
        f"source {tmp_path}/absent.sh\necho AFTER\n"
    )
    res = subprocess.run(
        ["/bin/bash", str(probe)], capture_output=True, text=True, timeout=30
    )
    assert res.returncode != 0
    assert "BEFORE" in res.stdout
    assert "AFTER" not in res.stdout

    # ...and WITHOUT set -e it merely warns and continues, which is why the
    # 2026-10-30 note's premise ("no set -e in the first 12 lines") was the
    # wrong half of the question to ask.
    probe2 = tmp_path / "probe2.sh"
    probe2.write_text(
        f"#!/bin/bash\necho BEFORE\nsource {tmp_path}/absent.sh\necho AFTER\n"
    )
    res2 = subprocess.run(
        ["/bin/bash", str(probe2)], capture_output=True, text=True, timeout=30
    )
    assert res2.returncode == 0
    assert "AFTER" in res2.stdout
