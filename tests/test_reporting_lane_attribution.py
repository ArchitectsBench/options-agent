"""Hermetic tests for the 2026-10-03 reporting fixes.

Two nightly-report defects, both reporting-only (never money-path):

1. `weekly_review.trading_week` attributed every `-SCALP-` round trip to the
   funnel sleeve, violating the operator's standing requirement that the
   funnel and the SCALP lane are reported as two separate streams.
2. `lessons_audit.audit` crashed with AttributeError whenever a registry
   entry wrote `enforcement:` as a bare string.

Both tests build synthetic inputs under tmp_path and never read production
logs or the real registry (CLAUDE.md destructive-probe / O-20 rule).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _exit_row(rec_id: str, pnl: float) -> str:
    return json.dumps({
        "ts": "2099-01-01T10:00:00-08:00",
        "event": "paper_exit",
        "rec_id": rec_id,
        "pnl_total": pnl,
        "exit_reason": "vwap_invalidation",
        "quantity": 1,
    })


def test_scalp_round_trips_are_not_folded_into_the_funnel(tmp_path):
    wr = _load("weekly_review")
    fills = tmp_path / "fills.jsonl"
    fills.write_text("\n".join([
        _exit_row("20261002-SCALP-5347-QQQ-0dte_directional", -100.0),
        _exit_row("20260930-SCALP-5193-IWM-0dte_directional", -118.0),
        _exit_row("20261002-0007-NVDA-call_debit_spread", 50.0),
    ]) + "\n")
    # WEEK_AGO is captured at import; the synthetic rows are dated far future
    # so the trailing-7-day filter keeps all three regardless of run date.
    out = wr.trading_week(fills_path=fills)

    assert out["by_sleeve"] == {"scalp": -218.0, "funnel": 50.0}
    assert out["test_vs_funnel"] == {"test": 0, "scalp": 2, "funnel": 1}
    assert out["round_trips"] == 3


@pytest.mark.parametrize("rec_id,expected", [
    ("20261002-SCALP-5347-QQQ-0dte_directional", "scalp"),
    ("20261002-0007-NVDA-call_debit_spread", "funnel"),
    ("20260914-TEST-0001-MSFT-rvol", "rvol"),
    ("", "funnel"),
])
def test_sleeve_of(rec_id, expected):
    assert _load("weekly_review").sleeve_of(rec_id) == expected


def test_lessons_audit_survives_a_bare_string_enforcement(tmp_path):
    la = _load("lessons_audit")
    registry = tmp_path / "lessons.yaml"
    registry.write_text(
        "lessons:\n"
        "  - id: L-BARE-STRING\n"
        "    enforcement: tests/test_thing.py::SomeCase + a prose note\n"
        "  - id: L-STRUCTURED-OK\n"
        "    enforcement:\n"
        "      - type: test\n"
        "        ref: tests/test_reporting_lane_attribution.py::test_sleeve_of\n"
        "  - id: L-STRUCTURED-BROKEN\n"
        "    enforcement:\n"
        "      - type: test\n"
        "        ref: tests/does_not_exist_anywhere.py\n"
    )
    la.REGISTRY = registry

    report = la.audit()  # before the fix: AttributeError: 'str' object has no attribute 'get'

    by_id = {r["id"]: r for r in report["lessons"]}
    assert by_id["L-BARE-STRING"]["status"] == "RECALL-ONLY"
    assert by_id["L-STRUCTURED-OK"]["status"] == "ENFORCED"
    assert by_id["L-STRUCTURED-BROKEN"]["status"] == "BROKEN"


def test_enforcements_normalises_every_shape():
    la = _load("lessons_audit")
    assert la._enforcements({}) == []
    assert la._enforcements({"enforcement": "a ref"}) == [{"type": None, "ref": "a ref"}]
    assert la._enforcements({"enforcement": {"type": "test", "ref": "x"}}) == [
        {"type": "test", "ref": "x"}]
    assert la._enforcements({"enforcement": [{"type": "test", "ref": "x"}, "bare"]}) == [
        {"type": "test", "ref": "x"}, {"type": None, "ref": "bare"}]
