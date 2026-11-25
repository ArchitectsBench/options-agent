"""R10 regression — DST correctness of truth-enrichment timestamp conversion.

The truth-scoring foundation converts trade timestamps to ET minutes to sample
the market-truth NBBO at the right minute. The pre-fix code applied a hardcoded
`- timedelta(hours=4)` to broker-UTC ('Z') timestamps — an EDT-only offset that
silently sampled the WRONG minute (off by one hour) for every 'Z' timestamp from
the November EST rollover onward (EST is UTC-5, not UTC-4), corrupting the
scoring foundation right at DST cutover.

These assertions pin BOTH regimes (dual-regime regression the audit called for):
a summer date (EDT, UTC-4) and a winter date (EST, UTC-5). The winter-Z case is
the load-bearing guard — the old bug produced 10:30 where the correct answer is
09:30.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_enrich():
    spec = importlib.util.spec_from_file_location(
        "enrich_mod_r10", ROOT / "scripts" / "enrich_trades_thetadata.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"enrich module not importable in this env: {exc}")
    return mod


pt_to_et_minute = _load_enrich().pt_to_et_minute


# ── broker-UTC ('Z') branch: must convert through the real ET tz database ──

def test_utc_z_summer_is_edt_utc_minus_4():
    # 2026-07-15 observes EDT (UTC-4): 14:30Z -> 10:30 ET
    assert pt_to_et_minute("2026-07-15T14:30:00Z") == "10:30"


def test_utc_z_winter_is_est_utc_minus_5():
    # 2026-12-15 observes EST (UTC-5): 14:30Z -> 09:30 ET.
    # THE REGRESSION: the old hardcoded -4h produced 10:30 (wrong by one hour).
    assert pt_to_et_minute("2026-12-15T14:30:00Z") == "09:30"


def test_utc_z_dst_boundary_both_sides():
    # Same wall-clock UTC either side of the fall-back boundary (DST ends
    # 2026-11-01) lands on a DIFFERENT ET minute — proof the conversion is
    # DST-aware, not fixed-offset. Under the old hardcoded -4h both would be
    # 09:30; the fix makes the EST side 08:30.
    edt = pt_to_et_minute("2026-10-31T13:30:00Z")  # EDT (offset -4) -> 09:30
    est = pt_to_et_minute("2026-11-02T13:30:00Z")  # EST (offset -5) -> 08:30
    assert edt == "09:30"
    assert est == "08:30"
    assert edt != est


# ── PT-naive branch: +3h is genuinely constant year-round (PT and ET both
#    observe DST simultaneously), so it is correctly regime-independent ──

def test_pt_naive_summer_plus_3():
    assert pt_to_et_minute("2026-07-15T07:30:00") == "10:30"


def test_pt_naive_winter_plus_3():
    assert pt_to_et_minute("2026-12-15T07:30:00") == "10:30"
