"""REPO-ROOT conftest — LAYER 0 of the O-20 write seal.

Why this file exists (2026-10-21). `tests/conftest.py` holds the O-20 seal, but
a conftest only binds tests collected **at or below its own directory**. On
2026-09-25 a scratch directory `_to_delete/` was left in the repo root holding
`dbg_test.py` / `test_zz_dbg_tmp.py` — stray debug copies that import the real
SCALP session simulator and run it against the recorded 2026-09-09 day. They
match pytest's default `python_files` patterns, so a bare `pytest -q` from the
root collected them, and because they live OUTSIDE `tests/` the seal never
applied: each full-suite run appended a verbatim 170-row block of
`entry_blocked_vwap_consistency` to the LIVE evidence ledger
`logs/observation/fills.jsonl` (4,551 of its 14,563 lines, 31%, all stamped
2026-09-09). Proven by interception on 2026-10-21 — 170 writes caught, the
single stack `_to_delete/dbg_test.py -> tests/sim/session_sim.py ->
pipeline._vwap_entry_ok -> paper_trader._log_event`, production byte-identical.

Two layers, because either alone is bypassable:
  * `testpaths = tests` in pytest.ini keeps collection out of stray directories
    — but it is overridden the moment anyone names a path explicitly.
  * THIS file is autouse at the repo root, so it binds EVERY collected test no
    matter where it lives, and it REFUSES rather than redirects: a test that
    reaches a production evidence ledger fails loudly instead of silently
    polluting the record.

Reads are untouched; only writes are sealed. The guarded set is deliberately
narrow — the two irreplaceable evidence ledgers, including
`logs/recommendations.jsonl`, the file a probe truncated on 2026-08-07.

UNVALIDATED — PAPER ONLY.
"""
import builtins
import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent

#: Production evidence ledgers no test may ever write. Resolved absolute paths.
SEALED_EVIDENCE = frozenset({
    (REPO / "logs" / "observation" / "fills.jsonl").resolve(),
    (REPO / "logs" / "recommendations.jsonl").resolve(),
})

#: Cheap pre-filter so the guard costs a string compare, not a stat(), on the
#: overwhelming majority of opens in a 1,200-test suite.
_SEALED_NAMES = tuple(sorted({p.name for p in SEALED_EVIDENCE}))

_WRITE_CHARS = "wax+"


class ProductionEvidenceWriteRefused(RuntimeError):
    """LAYER 0: a test reached a production evidence ledger in write mode."""


def _is_sealed(target) -> bool:
    """True when `target` names a sealed production evidence ledger."""
    try:
        raw = os.fspath(target)
    except TypeError:
        return False                      # fd, file-like, anything non-path
    if isinstance(raw, bytes):
        try:
            raw = raw.decode()
        except UnicodeDecodeError:
            return False
    if not raw.endswith(_SEALED_NAMES):
        return False
    try:
        return pathlib.Path(raw).resolve() in SEALED_EVIDENCE
    except OSError:
        return False


def _refuse(how: str, target) -> None:
    raise ProductionEvidenceWriteRefused(
        f"O-20 LAYER 0: {how} tried to WRITE the production evidence ledger "
        f"{target!s}. Tests must never touch it — redirect to tmp_path (see "
        f"tests/conftest.py::_o20_seal). If this fired from a file outside "
        f"tests/, that file is being collected by accident."
    )


def install_layer0(monkeypatch):
    """Install the refusal guards onto the CURRENT builtins/pathlib callables.

    Exposed (rather than inlined in the fixture) for two reasons: the tests can
    re-install it on top of a canary to reproduce the ordering a test *outside*
    `tests/` sees, and `tests/conftest.py`'s own layer-2 patch of
    `builtins.open` legitimately lands on top of this one for in-tree tests —
    both refuse production, so whichever wins is correct."""
    real_open = builtins.open
    real_path_open = pathlib.Path.open
    real_write_text = pathlib.Path.write_text
    real_write_bytes = pathlib.Path.write_bytes

    def guarded_open(file, mode="r", *args, **kwargs):
        if _is_sealed(file) and any(c in mode for c in _WRITE_CHARS):
            _refuse("open()", file)
        return real_open(file, mode, *args, **kwargs)

    def guarded_path_open(self, mode="r", *args, **kwargs):
        if _is_sealed(self) and any(c in mode for c in _WRITE_CHARS):
            _refuse("Path.open()", self)
        return real_path_open(self, mode, *args, **kwargs)

    def guarded_write_text(self, *args, **kwargs):
        if _is_sealed(self):
            _refuse("Path.write_text()", self)
        return real_write_text(self, *args, **kwargs)

    def guarded_write_bytes(self, *args, **kwargs):
        if _is_sealed(self):
            _refuse("Path.write_bytes()", self)
        return real_write_bytes(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(pathlib.Path, "open", guarded_path_open)
    monkeypatch.setattr(pathlib.Path, "write_text", guarded_write_text)
    monkeypatch.setattr(pathlib.Path, "write_bytes", guarded_write_bytes)


@pytest.fixture(autouse=True)
def _o20_layer0(monkeypatch):
    """Refuse every write-mode reach at a sealed evidence ledger, from any test
    in any directory — including ones `tests/conftest.py` cannot see."""
    install_layer0(monkeypatch)
    yield
