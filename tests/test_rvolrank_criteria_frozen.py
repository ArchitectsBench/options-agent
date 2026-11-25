"""The frozen RVOL-RANK criteria file may only GROW by dated amendment blocks.
Everything before the A2a block (original + A1, 110 lines) is pinned by the
sha256 of the file as it stood before A2a was appended (2026-09-13)."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
F = ROOT / "config" / "rvolrank_forward_criteria.yaml"
PRE_A2A_SHA256 = "1044b07c3f457afb0675b71d53482f4299e35ad82e6cb9ca0f83508bdf096d18"


def _pre_a2a(s: str) -> str:
    i = s.index("# AMENDMENT A2a")
    j = s.rfind("\n# ═", 0, i)          # the blank-line newline before the A2a separator
    return s[:j]                         # = the file exactly as it stood before A2a (ends with its own newline)


def test_original_plus_a1_text_is_byte_identical():
    pre = _pre_a2a(F.read_text())
    assert hashlib.sha256(pre.encode()).hexdigest() == PRE_A2A_SHA256


def test_a2a_registers_lanes_and_keeps_gate():
    import yaml
    d = yaml.safe_load(F.read_text())["amendment_a2a_2026_09_13"]
    # Ratification records facts and registrations; it must never become a
    # route to an easier bar, so the gate flag and the raised bar are pinned
    # alongside the signature.
    assert d["gate_unchanged"] is True
    assert d["operator_ratification"] == "RATIFIED_2026_09_13"
    assert set(d["measurement_lanes_registered"]) >= {"selection_top10_only", "selection_causal_first10", "control_exante"}
    assert d["registry"]["trial_count_N"] == 30 and d["registry"]["deflated_t_bar"] == 2.94


def test_ratification_block_covers_both_amendments_and_moves_no_bar():
    """The operator's signature (2026-09-13) is recorded in the file itself, not
    only in git. It must state what it covers, leave the gate untouched, and
    exclude A2b, which was never written."""
    import yaml
    r = yaml.safe_load(F.read_text())["ratification_2026_09_13"]
    assert r["ratified_by"] == "operator" and r["date"] == "2026-09-13"
    assert set(r["covers"]) == {"amendment_a2a_2026_09_13", "amendment_a2a_c_2026_09_13"}
    assert r["gate_unchanged"] is True
    assert "A2b" in r["not_covered"]


def test_the_bar_only_ever_moved_up():
    """A1 froze 2.90 at N=27; A2a re-froze 2.94 at N=30. A ratified amendment
    that LOWERED the bar would be the one thing this whole discipline exists to
    prevent, so it is asserted rather than trusted."""
    import yaml
    d = yaml.safe_load(F.read_text())
    a1 = d["amendment_a1_2026_07_23"]["deflation"]["selection_t_min_DEFLATED"]
    a2a = d["amendment_a2a_2026_09_13"]["registry"]["deflated_t_bar"]
    assert a2a > a1 and a1 == 2.9 and a2a == 2.94


def test_worst_month_primary_is_still_the_originally_frozen_reading():
    """A2a briefly relaxed this AFTER the interim numbers were seen; A2a-c
    reversed it. The primary must remain 'any calendar month'."""
    import yaml
    wm = yaml.safe_load(F.read_text())["amendment_a2a_c_2026_09_13"]["worst_month"]
    assert "ANY calendar month" in wm["primary"]
    assert "complete calendar months only" in wm["registered_sensitivity"]
