"""Second phone channel (2026-09-13).

ntfy.sh's free quota is per visitor IP: when it is exhausted every ntfy.sh topic
returns 429 at once, which took ALL phone alerts (deadman included) down. The fix
is a second ntfy SERVER. These tests pin the two properties that matter:

  1. one target failing does not stop the other from being tried;
  2. delivery is reported, never assumed — a sender whose every target fails
     says so (Python raises into send()'s logged warning; the shell helper
     prints per-target HTTP codes and exits non-zero).

No test touches the network: Python stubs urlopen, the shell test points both
targets at a local stub server.
"""

import json
import subprocess
import sys
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import src.engine.notify as n

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ops" / "ntfy_send.sh"

ENV = {"NTFY_TOPIC": "primary-topic",
       "NTFY_ALT_SERVER": "https://alt.example",
       "NTFY_ALT_TOPIC": "alt-topic"}


def _fake_urlopen(fail_hosts):
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        calls.append(req.full_url)
        if any(h in req.full_url for h in fail_hosts):
            raise urllib.error.HTTPError(req.full_url, 429, "quota", {}, None)
        return _Resp()
    return fake, calls


def test_engine_pages_are_logged_per_target_like_the_shell_helper(monkeypatch, tmp_path):
    """2026-10-14: engine pages wrote NOTHING to notify_delivery.log, so the
    autopilot health sweep read an ops-scripts-only log as a complete record of
    every page sent. One row per target now, in the shell's exact format."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    log = tmp_path / "delivery.log"
    monkeypatch.setenv("NTFY_SEND_LOG", str(log))
    fake, _ = _fake_urlopen(fail_hosts=[])
    with patch("urllib.request.urlopen", fake):
        n._ntfy_notify("BROKER DOWN in session", "b")
    rows = log.read_text().strip().splitlines()
    assert len(rows) == 2
    for row, host in zip(rows, ("ntfy.sh", "alt.example")):
        assert f"target={host}" in row and "http=200" in row
        assert 'title="BROKER DOWN in session"' in row
        assert row.startswith(datetime.now().strftime("%Y-%m-%d"))   # local time, not UTC


def test_a_429_is_logged_as_429_not_dropped(monkeypatch, tmp_path):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    log = tmp_path / "delivery.log"
    monkeypatch.setenv("NTFY_SEND_LOG", str(log))
    fake, _ = _fake_urlopen(fail_hosts=["ntfy.sh"])
    with patch("urllib.request.urlopen", fake):
        n._ntfy_notify("t", "b")
    rows = log.read_text().strip().splitlines()
    assert "target=ntfy.sh http=429" in rows[0]
    assert "target=alt.example http=200" in rows[1]


def test_an_unwritable_log_never_blocks_a_page(monkeypatch, tmp_path, caplog):
    """The actual danger of this edit: this file is the paging backbone during an
    outage, so delivery bookkeeping must not be able to break delivery."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    blocker = tmp_path / "blocker"                   # a FILE where a dir must be,
    blocker.write_text("")                           # so mkdir(parents=True) raises
    monkeypatch.setenv("NTFY_SEND_LOG", str(blocker / "sub" / "delivery.log"))
    fake, calls = _fake_urlopen(fail_hosts=[])
    with patch("urllib.request.urlopen", fake), \
            patch.object(n, "_macos_notify", lambda *a: None):
        n.send("t", "b")                             # must not raise
    assert len(calls) == 2                           # both targets still paged
    assert "all 2 ntfy targets failed" not in caplog.text


def test_targets_are_primary_then_second_server(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    assert n._ntfy_targets() == [("ntfy.sh", "https://ntfy.sh/primary-topic"),
                                 ("alt.example", "https://alt.example/alt-topic")]


def test_primary_429_still_delivers_on_second_server(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    fake, calls = _fake_urlopen(fail_hosts=["ntfy.sh"])
    with patch("urllib.request.urlopen", fake):
        n._ntfy_notify("t", "b")                     # must not raise
    assert calls == ["https://ntfy.sh/primary-topic", "https://alt.example/alt-topic"]


def test_all_targets_failing_is_reported_not_swallowed_silently(monkeypatch, caplog):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    fake, _ = _fake_urlopen(fail_hosts=["ntfy.sh", "alt.example"])
    with patch("urllib.request.urlopen", fake), \
            patch.object(n, "_macos_notify", lambda *a: None):
        n.send("t", "b")                             # send() never raises...
    assert "all 2 ntfy targets failed" in caplog.text   # ...but it says so


# The stub server runs in its OWN process: the SQZBZ analysis modules block
# socket creation process-wide once imported (analysis/squeeze_buyzone/common.py),
# so an in-process server fails whenever the full suite has loaded them first.
_STUB = r"""
import http.server, json, sys
codes = json.loads(sys.argv[1])
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(codes.get(self.path.strip("/"), 200))
        self.end_headers()
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
print(srv.server_port, flush=True)
srv.serve_forever()
"""


def _run_helper(tmp_path, codes):
    stub = subprocess.Popen([sys.executable, "-c", _STUB, json.dumps(codes)],
                            stdout=subprocess.PIPE, text=True)
    try:
        base = f"http://127.0.0.1:{stub.stdout.readline().strip()}"
        env_file = tmp_path / "env"
        env_file.write_text(f"NTFY_SERVER={base}\nNTFY_TOPIC=primary\n"
                            f"NTFY_ALT_SERVER={base}\nNTFY_ALT_TOPIC=alt\n")
        return subprocess.run(
            ["bash", str(HELPER), "Title — dash", "high", "warning", "body"],
            capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "NTFY_SEND_ENV": str(env_file),
                 "NTFY_SEND_LOG": str(tmp_path / "delivery.log")})
    finally:
        stub.kill()
        stub.wait()


def test_shell_helper_delivers_via_second_server_when_primary_429s(tmp_path):
    r = _run_helper(tmp_path, {"primary": 429, "alt": 200})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "http=429" in r.stdout and "http=200" in r.stdout
    assert (tmp_path / "delivery.log").read_text().count("ntfy target=") == 2


def test_shell_helper_exits_nonzero_when_every_target_fails(tmp_path):
    r = _run_helper(tmp_path, {"primary": 429, "alt": 503})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "http=429" in r.stdout and "http=503" in r.stdout
    # NARROW SEMANTICS (2026-10-19): a server that ANSWERED is not a lost page,
    # so neither code is retried — one line each, no attempt= suffix anywhere.
    assert r.stdout.count("ntfy target=") == 2
    assert "attempt=" not in r.stdout


_DEAD_PORT = r"""
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0))
print(s.getsockname()[1], flush=True); s.close()
"""


def _dead_port():
    """A loopback port nothing listens on: bind, read the port, close. curl then
    fails connect-refused -> http=000 with no network and no waiting.

    In a SUBPROCESS for the same reason _STUB is: once the SQZBZ analysis
    modules are imported, socket creation raises process-wide, so an in-process
    bind fails whenever the full suite has loaded them first (it did, 10-19)."""
    return subprocess.run([sys.executable, "-c", _DEAD_PORT],
                          capture_output=True, text=True, timeout=30).stdout.strip()


def test_shell_helper_retries_a_000_and_still_delivers_on_the_live_target(tmp_path):
    """2026-10-19: both targets returned http=000 in the same second (host
    network blip) and one DEADMAN page was LOST — nothing re-sent it. A 000 is
    no HTTP response at all, so it is the one code worth retrying."""
    stub = subprocess.Popen([sys.executable, "-c", _STUB, json.dumps({"alt": 200})],
                            stdout=subprocess.PIPE, text=True)
    try:
        live = f"http://127.0.0.1:{stub.stdout.readline().strip()}"
        env_file = tmp_path / "env"
        env_file.write_text(f"NTFY_SERVER=http://127.0.0.1:{_dead_port()}\n"
                            f"NTFY_TOPIC=primary\n"
                            f"NTFY_ALT_SERVER={live}\nNTFY_ALT_TOPIC=alt\n")
        r = subprocess.run(
            ["bash", str(HELPER), "t", "high", "warning", "body"],
            capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin", "NTFY_SEND_ENV": str(env_file),
                 "NTFY_SEND_LOG": str(tmp_path / "delivery.log"),
                 "NTFY_RETRY_SLEEP": "0"})
    finally:
        stub.kill()
        stub.wait()
    assert r.returncode == 0, r.stdout + r.stderr      # the live target delivered
    assert r.stdout.count("http=000") == 3             # 1 + 2 retries, not 1
    assert "attempt=2/3" in r.stdout and "attempt=3/3" in r.stdout
    assert "http=200" in r.stdout
    log = (tmp_path / "delivery.log").read_text()
    assert log.count("attempt=") == 2                  # retries are distinguishable
    assert log.count("http=200") == 1                  # a success is never retried


# ---------------------------------------------------------------------------
# 2026-10-20: the PYTHON half of the lost-page fix. The shell helper got the
# retry on 2026-10-19; `src/engine/notify.py` still posted once per target, so
# an ENGINE page could be lost the same way the DEADMAN page was.
#
# Retry class is STRUCTURAL, not an exception enumeration: anything that logs
# 000 (no HTTP code came back) is retried; an HTTPError carries a code, which
# means a server answered, so it is never retried. Enumerating `URLError` would
# have missed raw read-phase failures — urllib wraps only request-phase OSError,
# so a read timeout on an accepted-then-stalled socket arrives as a bare
# TimeoutError (that is the 2026-10-05 gateway-stall shape).
# ---------------------------------------------------------------------------

def _urlopen_raising_then_ok(exc, fail_times):
    """Raise `exc` for the first `fail_times` calls, then succeed."""
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) <= fail_times:
            raise exc
        return _Resp()
    return fake, calls


def _retry_env(monkeypatch, tmp_path, topics_only=True):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    if topics_only:          # single target keeps the attempt arithmetic readable
        monkeypatch.delenv("NTFY_ALT_SERVER", raising=False)
        monkeypatch.delenv("NTFY_ALT_TOPIC", raising=False)
    log = tmp_path / "delivery.log"
    monkeypatch.setenv("NTFY_SEND_LOG", str(log))
    monkeypatch.setenv("NTFY_ATTEMPTS", "3")
    monkeypatch.setenv("NTFY_RETRY_SLEEP", "0")
    return log


def test_a_000_is_retried_and_the_page_still_gets_through(monkeypatch, tmp_path):
    """The PASS criterion: URLError then success delivers, and the attempt rows
    are in the log. 2026-10-19: both targets 000'd in the same second and one
    DEADMAN page was lost outright with nothing re-sending it."""
    log = _retry_env(monkeypatch, tmp_path)
    fake, calls = _urlopen_raising_then_ok(urllib.error.URLError("connection refused"), 1)
    slept = []
    with patch("urllib.request.urlopen", fake), patch.object(n, "_sleep", slept.append):
        n._ntfy_notify("DEADMAN: broker-port-dead-in-session", "b")
    assert len(calls) == 2                      # failed once, then delivered
    assert slept == [0.0]                       # slept between tries, not after the last
    rows = log.read_text().strip().splitlines()
    assert len(rows) == 2
    assert "http=000" in rows[0] and " attempt=" not in rows[0]   # first try: NO suffix
    assert "http=200" in rows[1] and "attempt=2/3" in rows[1]


def test_a_raw_timeout_is_retried_too_not_just_urlerror(monkeypatch, tmp_path):
    """urllib wraps only request-phase OSError in URLError, so a read timeout
    arrives as a bare TimeoutError. It logs 000 — no code came back — so it is
    in the same lost-page class and MUST be retried."""
    log = _retry_env(monkeypatch, tmp_path)
    fake, calls = _urlopen_raising_then_ok(TimeoutError("timed out"), 1)
    with patch("urllib.request.urlopen", fake), patch.object(n, "_sleep", lambda s: None):
        n._ntfy_notify("t", "b")
    assert len(calls) == 2
    assert "http=000" in log.read_text()
    assert "http=200" in log.read_text()


def test_an_http_error_is_never_retried_because_a_server_answered(monkeypatch, tmp_path):
    """A 429 is ntfy.sh's daily per-IP quota: re-posting cannot help (the second
    SERVER is the answer). One attempt, one row, no attempt= suffix."""
    log = _retry_env(monkeypatch, tmp_path)
    fake, calls = _fake_urlopen(fail_hosts=["ntfy.sh"])
    slept = []
    with patch("urllib.request.urlopen", fake), patch.object(n, "_sleep", slept.append):
        try:
            n._ntfy_notify("t", "b")
        except RuntimeError:
            pass                                 # the only target 429'd
    assert len(calls) == 1 and slept == []
    rows = log.read_text().strip().splitlines()
    assert len(rows) == 1
    assert "http=429" in rows[0] and " attempt=" not in rows[0]


def test_every_attempt_exhausted_logs_each_try_and_reports_failure(monkeypatch, tmp_path):
    """A page that never gets through must still be VISIBLE as 000 rows — and
    count as PAGES not LINES, which is what the attempt= suffix is for."""
    log = _retry_env(monkeypatch, tmp_path)
    fake, calls = _urlopen_raising_then_ok(urllib.error.URLError("down"), 99)
    with patch("urllib.request.urlopen", fake), patch.object(n, "_sleep", lambda s: None):
        try:
            n._ntfy_notify("t", "b")
            raise AssertionError("a total failure must not be swallowed here")
        except RuntimeError:
            pass
    assert len(calls) == 3                       # NTFY_ATTEMPTS
    rows = log.read_text().strip().splitlines()
    assert len(rows) == 3
    # One PAGE, three LINES: the shell's own counting rule applies to this log.
    assert len([r for r in rows if "http=000" in r and " attempt=" not in r]) == 1


def test_retry_attempts_mirror_the_shell_env_knobs(monkeypatch, tmp_path):
    """NTFY_ATTEMPTS / NTFY_RETRY_SLEEP are the same knobs ntfy_send.sh reads,
    so both paths can be tuned together; a junk value falls back, never raises."""
    _retry_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NTFY_ATTEMPTS", "1")
    fake, calls = _urlopen_raising_then_ok(urllib.error.URLError("down"), 99)
    with patch("urllib.request.urlopen", fake), patch.object(n, "_sleep", lambda s: None):
        try:
            n._ntfy_notify("t", "b")
        except RuntimeError:
            pass
    assert len(calls) == 1                       # no retry when attempts=1
    monkeypatch.setenv("NTFY_ATTEMPTS", "not-a-number")
    monkeypatch.setenv("NTFY_RETRY_SLEEP", "not-a-number")
    assert n._ntfy_attempts() == 3 and n._ntfy_retry_sleep() == 2.0
