"""Lane-local hermeticity (spec §16, F7) — layered ON TOP of the repo-wide O-20
seal in tests/conftest.py (which refuses any write under data/, logs/, config/).

1. `ALLOW_LIVE_GATEWAY_TESTS` must not be "1": the lane's client refuses a
   socket under pytest; this fixture makes sure nobody opted the suite in.
2. The FakeIB's class-level broker book / fill tape survive client instances
   by design (restart tests); they must NOT survive between tests.
3. Every module-level path constant in `src/rvol_exec/paths.py` (when the
   module exists) is redirected under tmp_path so a lane test can write its
   books without tripping the seal. A module that is not built yet is skipped
   — the seal still refuses production writes, which is the correct failure.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _rvol_exec_hermetic(tmp_path, monkeypatch):
    assert os.environ.get("ALLOW_LIVE_GATEWAY_TESTS") != "1", (
        "tests/rvol_exec must never run with ALLOW_LIVE_GATEWAY_TESTS=1")
    monkeypatch.delenv("ALLOW_LIVE_GATEWAY_TESTS", raising=False)

    # (2) fresh broker per test
    from tests.rvol_exec.fake_ib import FakeIB
    FakeIB.reset_broker()

    # (3) redirect lane paths (module may not exist yet on this branch)
    try:
        import src.rvol_exec.paths as paths
    except Exception:  # noqa: BLE001 — not built yet
        paths = None
    if paths is not None:
        lane_root = tmp_path / "lane_root"
        for name in dir(paths):
            if not name.isupper() or name == "ROOT":        # ROOT stays the repo (git sha, relative_to)
                continue
            val = getattr(paths, name)
            if not isinstance(val, Path):
                continue
            try:
                rel = val.resolve().relative_to(ROOT)
            except ValueError:
                continue
            monkeypatch.setattr(paths, name, lane_root / rel, raising=False)
    yield
    FakeIB.reset_broker()
