"""O-20 LAYER 0 — the repo-root seal that no test may write a production
evidence ledger, from ANY directory.

Background (2026-10-21): `tests/conftest.py`'s seal binds only tests collected
at or below `tests/`. Two stray debug copies in `_to_delete/` (left 2026-09-25)
matched pytest's default `python_files` patterns, were collected by a bare
`pytest -q` from the repo root, and ran the real SCALP session simulator with
no seal — appending a verbatim 170-row block of `entry_blocked_vwap_consistency`
to `logs/observation/fills.jsonl` on every full-suite run.

DESTRUCTIVE-PROBE RULE (CLAUDE.md, 2026-08-07): these tests prove the guard
against a CANARY in tmp that is registered as sealed for the duration of the
test — never against the real ledgers. The real paths are only ever asserted
*about*, never opened.

UNVALIDATED — PAPER ONLY.
"""
import builtins
import configparser
import pathlib
import unittest

import pytest

import conftest as rootconf  # the REPO-ROOT conftest, not tests/conftest.py

REPO = pathlib.Path(rootconf.__file__).resolve().parent


@pytest.fixture
def canary(tmp_path, monkeypatch):
    """A disposable file that the ALREADY-INSTALLED layer-0 guard treats as a
    sealed ledger. `_is_sealed` reads both module globals at call time, so
    swapping them re-aims the live guard at tmp without touching production."""
    c = (tmp_path / "fills_canary.jsonl").resolve()
    c.write_text('{"event": "pre-existing"}\n')
    monkeypatch.setattr(rootconf, "SEALED_EVIDENCE", frozenset({c}))
    monkeypatch.setattr(rootconf, "_SEALED_NAMES", (c.name,))
    # tests/conftest.py's layer-2 patch of builtins.open lands on top of layer 0
    # for anything inside tests/. Re-install so these tests see the ordering a
    # file OUTSIDE tests/ sees — which is the case layer 0 exists for.
    rootconf.install_layer0(monkeypatch)
    return c


def test_real_evidence_ledgers_are_in_the_sealed_set():
    """The two irreplaceable ledgers — asserted about, never opened."""
    assert rootconf.SEALED_EVIDENCE == {
        (REPO / "logs" / "observation" / "fills.jsonl").resolve(),
        (REPO / "logs" / "recommendations.jsonl").resolve(),
    }


@pytest.mark.parametrize("mode", ["a", "w", "x", "r+", "ab", "w+b"])
def test_builtin_open_in_write_mode_is_refused(canary, mode):
    with pytest.raises(rootconf.ProductionEvidenceWriteRefused):
        builtins.open(canary, mode)


@pytest.mark.parametrize("mode", ["a", "w"])
def test_path_open_in_write_mode_is_refused(canary, mode):
    with pytest.raises(rootconf.ProductionEvidenceWriteRefused):
        pathlib.Path(canary).open(mode)


def test_path_write_text_and_write_bytes_are_refused(canary):
    with pytest.raises(rootconf.ProductionEvidenceWriteRefused):
        pathlib.Path(canary).write_text("x")
    with pytest.raises(rootconf.ProductionEvidenceWriteRefused):
        pathlib.Path(canary).write_bytes(b"x")


def test_a_refused_write_leaves_the_file_untouched(canary):
    """Refusal must happen BEFORE the descriptor opens — `open(p, "w")` that
    merely raised afterwards would already have truncated the ledger, which is
    the exact 2026-08-07 failure mode."""
    before = canary.read_bytes()
    for attempt in (lambda: builtins.open(canary, "w"),
                    lambda: pathlib.Path(canary).write_text("clobber")):
        with pytest.raises(rootconf.ProductionEvidenceWriteRefused):
            attempt()
    assert canary.read_bytes() == before


def test_reads_are_not_blocked(canary):
    """The seal is about writes; reading evidence in a test stays legal."""
    assert builtins.open(canary).read() == '{"event": "pre-existing"}\n'
    assert pathlib.Path(canary).read_text() == '{"event": "pre-existing"}\n'


def test_unrelated_paths_are_untouched(tmp_path, canary):
    p = tmp_path / "ordinary.jsonl"
    p.write_text("fine\n")
    with builtins.open(p, "a") as fh:
        fh.write("also fine\n")
    assert p.read_text() == "fine\nalso fine\n"


def test_non_path_open_targets_do_not_crash_the_guard(tmp_path, canary):
    """A file descriptor is a legal first argument to open(); the guard must
    fall through rather than raise TypeError on os.fspath."""
    import os
    fd = os.open(tmp_path / "byfd.txt", os.O_CREAT | os.O_WRONLY)
    with builtins.open(fd, "w") as fh:
        fh.write("ok")
    assert (tmp_path / "byfd.txt").read_text() == "ok"


def test_pytest_ini_scopes_collection_to_the_tests_tree():
    """Layer 0's companion: stray `*_test.py` outside tests/ is never collected
    in the first place. Both layers are required — testpaths is overridden by
    naming a path explicitly, and the root conftest covers that case."""
    cp = configparser.ConfigParser()
    cp.read(REPO / "pytest.ini")
    paths = cp["pytest"]["testpaths"].split()
    assert "tests" in paths
    # Pinned because a bare `testpaths = tests` silently dropped these 10
    # lookahead-bias guards when this seal was first written.
    assert "analysis/squeeze_buyzone/tests" in paths
    for rel in paths:
        assert (REPO / rel).is_dir(), f"testpaths entry does not exist: {rel}"
    assert "_to_delete" not in cp["pytest"]["testpaths"]


def test_the_session_simulators_write_target_is_not_production():
    """The simulator writes through `paper_trader.FILLS_LOG`; inside a test it
    must already be redirected by tests/conftest.py's seal. This is the check
    that would have failed for `_to_delete/dbg_test.py`."""
    import src.engine.paper_trader as pt
    prod = (REPO / "logs" / "observation" / "fills.jsonl").resolve()
    assert pathlib.Path(pt.FILLS_LOG).resolve() != prod


class StraySuiteFiles(unittest.TestCase):
    def test_no_collectible_test_files_live_outside_the_tests_tree(self):
        """Fails if another `_to_delete/`-style scratch file appears at the
        repo root. Informational on top of the two layers: the point is that a
        stray file is visible, not merely harmless."""
        stray = sorted({p.name for p in REPO.glob("*_test.py")}
                       | {p.name for p in REPO.glob("test_*.py")})
        self.assertEqual(stray, [], f"collectible test files in the repo root: {stray}")
