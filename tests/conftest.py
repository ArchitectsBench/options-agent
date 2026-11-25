"""O-20 PRODUCTION-STATE SEAL — the suite may not read from or write to ANY
production state.

WHY (2026-08-06/07): two real specimens leaked test data into production files.
  * tests/test_broker_lost.py     -> wrote the production POSITION BOOK
  * tests/test_forward_acceptance.py -> wrote logs/recommendations.jsonl, a
    GATE-EVIDENCE surface, for five weeks (80 rows quarantined)
Per-test patching is not a guarantee: it only protects the tests that remember.

TWO INDEPENDENT LAYERS, each sufficient on its own:
  LAYER 1 (primary)  — autouse fixture redirects every production path to tmp.
                       No test opts out; nothing is asked to remember.
  LAYER 2 (backstop) — a write-refusal guard that RAISES if any code under
                       pytest opens a real production path for writing.
Both are proven against BOTH specimens independently in tests/test_o20_seal.py.
"""
import builtins
import io
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# Real production state the suite must never touch.
PROTECTED = (
    REPO / "data",
    REPO / "logs",
    REPO / "config",
)
_real_open = builtins.open


def _is_protected(path) -> bool:
    try:
        p = Path(os.fspath(path)).resolve()
    except Exception:
        return False
    for root in PROTECTED:
        try:
            p.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


class ProductionWriteRefused(RuntimeError):
    """LAYER 2: a test attempted to WRITE production state."""


@pytest.fixture(autouse=True)
def _o20_seal(tmp_path, monkeypatch):
    """LAYER 1 — redirect the data root, and LAYER 2 — refuse production writes."""
    # ---- LAYER 1: redirect module-level production paths to tmp ----
    import src.engine.paper_trader as pt
    lane = tmp_path / "lane"
    for ln in getattr(pt, "LANES", ("funnel", "scalp")):
        (lane / ln).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pt, "LANE_DIR", lane, raising=False)
    monkeypatch.setattr(pt, "FILLS_LOG", tmp_path / "fills.jsonl", raising=False)
    monkeypatch.setattr(pt, "LEGACY_POSITIONS", tmp_path / "paper_positions.json", raising=False)
    monkeypatch.setattr(pt, "LEGACY_PENDING", tmp_path / "paper_orders.json", raising=False)
    # Engine pages log per-target rows (2026-10-14); send them to tmp so no test
    # can append to the real logs/ops/notify_delivery.log the health sweep greps.
    monkeypatch.setenv("NTFY_SEND_LOG", str(tmp_path / "notify_delivery.log"))
    try:
        import src.engine.risk as risk
        monkeypatch.setattr(risk, "FILLS_LOG", tmp_path / "fills.jsonl", raising=False)
        monkeypatch.setattr(risk, "RISK_STATE", tmp_path / "risk_state.json", raising=False)
    except Exception:
        pass
    try:
        import src.engine.recommendation as rec
        monkeypatch.setattr(rec, "REC_LOG", tmp_path / "recommendations.jsonl", raising=False)
    except Exception:
        pass
    # Buy-zone radar ledgers (2026-09-13): logs/alerts/outcomes.jsonl (writer
    # scripts/radar_outcomes.py + reader src/api/watchlist_squeeze.py) and
    # logs/alerts/pushes.jsonl (scripts/watch_buyzone.py). radar_outcomes and
    # the API reader are import-light (no config/.env side effects) so they
    # are imported here; watch_buyzone loads .env on import and is redirected
    # only when a test has already imported it — never force-imported.
    import sys as _sys
    _scripts = str(REPO / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    try:
        import radar_outcomes as _ro
        monkeypatch.setattr(_ro, "OUTCOMES", tmp_path / "outcomes.jsonl", raising=False)
        monkeypatch.setattr(_ro, "LOCK", tmp_path / "watchlist_squeeze.lock", raising=False)
    except Exception:
        pass
    try:
        import src.api.watchlist_squeeze as _ws
        monkeypatch.setattr(_ws, "OUTCOMES", tmp_path / "outcomes.jsonl", raising=False)
        # the M5 population reader (events archive + hot) — tmp too, so a
        # test never counts production instances
        monkeypatch.setattr(_ws, "EVENTS_HOT", tmp_path / "events.jsonl", raising=False)
        monkeypatch.setattr(_ws, "ARCHIVE_DIR", tmp_path / "archive", raising=False)
    except Exception:
        pass
    _wb = _sys.modules.get("watch_buyzone")
    if _wb is not None:
        monkeypatch.setattr(_wb, "PUSHES", tmp_path / "pushes.jsonl", raising=False)
    # RVOL-EXEC lane (2026-09-13): its books live in data/lane_rvol/ and its
    # ledger in logs/observation/rvol_exec_paper.jsonl — both already inside
    # PROTECTED roots for LAYER 2. LAYER 1 redirects every module-level Path in
    # src/rvol_exec/paths.py under tmp so a lane test can never reach them,
    # whichever test directory it runs from (the lane-local conftest does the
    # same; this is the independent backstop). Skipped when the module is absent.
    try:
        import src.rvol_exec.paths as _rvx
        _rvx_root = tmp_path / "lane_rvol_root"
        for _name in dir(_rvx):
            if not _name.isupper() or _name == "ROOT":
                continue
            _val = getattr(_rvx, _name)
            if not isinstance(_val, Path):
                continue
            try:
                _rel = _val.resolve().relative_to(REPO)
            except ValueError:
                continue
            monkeypatch.setattr(_rvx, _name, _rvx_root / _rel, raising=False)
    except Exception:
        pass

    # ---- LAYER 2: refuse any WRITE to a protected production path ----
    def guarded_open(file, mode="r", *a, **k):
        if any(m in str(mode) for m in ("w", "a", "x", "+")) and _is_protected(file):
            raise ProductionWriteRefused(
                f"O-20: test attempted to WRITE production state: {file} "
                f"(mode={mode!r}). Use tmp_path — the suite must never touch "
                "data/, logs/ or config/.")
        return _real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    # Path.write_text / write_bytes resolve io.open SEPARATELY, so patching
    # builtins.open alone does NOT cover them. Demonstrated the hard way on
    # 2026-08-07: a test called RECS.write_text(...) and TRUNCATED the real
    # recommendations.jsonl. Patch the Path methods too.
    _real_wt, _real_wb = Path.write_text, Path.write_bytes

    def guarded_write_text(self, *a, **k):
        if _is_protected(self):
            raise ProductionWriteRefused(
                f"O-20: Path.write_text on production state: {self}")
        return _real_wt(self, *a, **k)

    def guarded_write_bytes(self, *a, **k):
        if _is_protected(self):
            raise ProductionWriteRefused(
                f"O-20: Path.write_bytes on production state: {self}")
        return _real_wb(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(Path, "write_bytes", guarded_write_bytes)
    yield
