"""CLL ledger seed — hypothesis_registry, graveyard (with basis_valid handling),
adopted_rules_monitor. One-time bootstrap from the Round-1 record
(post_mortem_program_state.md §3/§5). Spec §3, §11.

basis_valid handling (per 2026-07-24 directive): a graveyard entry whose evidence
was undermined by a later integrity fix is marked basis_invalidated and RETURNED
TO THE QUEUE for a clean re-test — never resurrected as adopted. A3 is the
showcase: association evidence invalidated (never computable on sandbox data),
economics rejection stands but flagged pending honest-fills recompute.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "learning"))
guard = importlib.import_module("guard")

DEFLATE_STUB = {"method": "deflated_sharpe", "note": "recompute per round from current lineage/family N"}

# ── hypothesis_registry — every hypothesis/variant ever tested ──
REGISTRY = [
    {"hyp_id": "A3", "lineage": "regime_opposition", "family": "A_regime", "sleeve": "SCALP",
     "round_id": "R1", "primary": True, "declared_before_results": True,
     "test_procedure": "winner_control_blocked_economics",
     "multiplicity": {"lineage_trials": 1, "family_trials": 3}, "deflated": DEFLATE_STUB,
     "result": {"decision": "rejected", "see": "graveyard#A3"}},
    {"hyp_id": "A1", "lineage": "extension_chase", "family": "A_geom", "sleeve": "SCALP",
     "round_id": "R1", "primary": False, "declared_before_results": True,
     "test_procedure": "winner_control_blocked_economics",
     "multiplicity": {"lineage_trials": 1, "family_trials": 3}, "deflated": DEFLATE_STUB,
     "result": {"decision": "not_promoted", "see": "graveyard#A1"}},
    {"hyp_id": "A4", "lineage": "iv_headwind_long_premium", "family": "A_vol", "sleeve": "SCALP",
     "round_id": "R1", "primary": False, "declared_before_results": True,
     "test_procedure": "winner_control_blocked_economics",
     "multiplicity": {"lineage_trials": 1, "family_trials": 3}, "deflated": DEFLATE_STUB,
     "result": {"decision": "out_of_scope", "see": "graveyard#A4"}},
    {"hyp_id": "looser_target_50", "lineage": "exit_target", "family": "exit_variant", "sleeve": "SCALP",
     "round_id": "R1", "primary": False, "test_procedure": "shadow_variant_vs_actual",
     "multiplicity": {"lineage_trials": 1, "family_trials": 3}, "deflated": DEFLATE_STUB,
     "result": {"decision": "rejected", "see": "graveyard#looser_target_50"}},
    {"hyp_id": "vwap_leak", "lineage": "give_back_vwap", "family": "give_back", "sleeve": "SCALP",
     "round_id": "R1", "primary": False, "test_procedure": "give_back_gb_v1",
     "multiplicity": {"lineage_trials": 1, "family_trials": 2}, "deflated": DEFLATE_STUB,
     "result": {"decision": "rejected", "see": "graveyard#vwap_leak"}},
    {"hyp_id": "peak_trail", "lineage": "exit_trail", "family": "exit_variant", "sleeve": "SCALP",
     "round_id": "R1(G3)", "primary": False, "test_procedure": "shadow_variant_vs_actual",
     "multiplicity": {"lineage_trials": 1, "family_trials": 3}, "deflated": DEFLATE_STUB,
     "result": {"decision": "promoted_to_shadow", "slot": 1,
                "frozen_criteria_ref": "config/exit_variant_shadow.yaml#peak_trail",
                "status": "filling (texture-only until >=150 prospective trades)"}},
]

# ── graveyard — rejected hypotheses with basis_valid + re-test conditions ──
GRAVEYARD = [
    {"hyp_id": "A3", "lineage": "regime_opposition", "family": "A_regime", "sleeve": "SCALP",
     "rejected_at": "2026-07-23", "rejected_in_round": "R1",
     "rejection": {
         "association": {"verdict": "inconclusive",
                         "evidence": "R1 2x2 (slip-capped): OR 1.31, Fisher p=0.38, CI[0.50,3.38]; 31 vs 36 evaluable",
                         "basis_valid": False,
                         "basis_invalidated_reason": "computed on the quantity-buggy slip metric (batch-0a); on the corrected metric the honest 40% slip cap leaves 1 failure / 10 controls -> NOT COMPUTABLE on sandbox data",
                         "action": "returned_to_queue_for_clean_retest"},
         "economics": {"verdict": "rejected",
                       "evidence": "blocked-set +$46.1/trade (avoids 15 losers -$1,895, forgoes 15 winners +$3,279) -> gate destroys value",
                       "basis_valid": True, "flag": "pending_honest_fills_recompute",
                       "note": "truth-P&L based (size-consistent); rejection stands, reconfirm on honest fills since blocked-set membership used the slip cap"}},
     "retest_condition": {"type": "honest_fills", "min_sessions": 30, "declared_at": "2026-07-24",
                          "auto_eligible_when": "post-migration re-pin complete"},
     "notes": [{"date": "2026-07-24", "text": "today -$91.2 sign-flip echoed A3; on corrected metric those 4 trades classify D and A3 not computable — NOTE, not a test"}]},
    {"hyp_id": "A1", "lineage": "extension_chase", "family": "A_geom", "sleeve": "SCALP",
     "rejected_at": "2026-07-23", "rejected_in_round": "R1",
     "rejection": {
         "association": {"verdict": "no_association", "evidence": "OR 0.90, p=0.68",
                         "basis_valid": False,
                         "basis_invalidated_reason": "same slip-cap/metric dependency as A3 (batch-0a); evidence set re-partitions on the corrected metric",
                         "action": "returned_to_queue_for_clean_retest"},
         "economics": {"verdict": "not_promoted", "evidence": "blocked expectancy +$39.8/trade",
                       "basis_valid": True, "flag": "pending_honest_fills_recompute"}},
     "retest_condition": {"type": "honest_fills", "min_sessions": 30, "auto_eligible_when": "post-migration re-pin complete"},
     "notes": []},
    {"hyp_id": "A4", "lineage": "iv_headwind_long_premium", "family": "A_vol", "sleeve": "SCALP",
     "rejected_at": "2026-07-23", "rejected_in_round": "R1",
     "rejection": {"structural": {"verdict": "out_of_scope",
                                  "evidence": "0 occurrences on intraday SCALP; retained for DTE books only",
                                  "basis_valid": True,
                                  "note": "not slip/metric-dependent — survives the batch-0a fix"}},
     "retest_condition": {"type": "sleeve_scope", "eligible_when": "tested on a 2-7 DTE (funnel) book, not SCALP"},
     "notes": []},
    {"hyp_id": "looser_target_50", "lineage": "exit_target", "family": "exit_variant", "sleeve": "SCALP",
     "rejected_at": "2026-07-23", "rejected_in_round": "R1",
     "rejection": {"continuation": {"verdict": "refuted",
                                    "evidence": "post-exit fetch (exit->EOD, 30 trades): EOD median -7.2%, only 10/30 above exit — holding past target loses on average",
                                    "basis_valid": True,
                                    "note": "ratio/count-based (post-exit %), qty-invariant — survives the batch-0a fix"}},
     "retest_condition": {"type": "regime_change", "eligible_when": "declared vol-regime shift (post-migration)"},
     "notes": []},
    {"hyp_id": "vwap_leak", "lineage": "give_back_vwap", "family": "give_back", "sleeve": "SCALP",
     "rejected_at": "2026-07-23", "rejected_in_round": "R1",
     "rejection": {"give_back": {"verdict": "refuted",
                                 "evidence": "vwap_invalidation median give-back 0.9% of risk; winners capture median 71% of sustained peak — winners are efficient",
                                 "basis_valid": True,
                                 "note": "give-back %-of-risk (ratio), qty-invariant — survives the batch-0a fix"}},
     "retest_condition": {"type": "none", "eligible_when": "not re-testable without new give-back signal"},
     "notes": []},
]


def main():
    reg = guard.guarded_path("seed", "hypothesis_registry.jsonl")
    with reg.open("w") as f:
        for r in REGISTRY:
            f.write(json.dumps(r) + "\n")
    gy = guard.guarded_path("seed", "graveyard.jsonl")
    with gy.open("w") as f:
        for r in GRAVEYARD:
            f.write(json.dumps(r) + "\n")
    # adopted_rules_monitor: EMPTY — the loop never adopts; no human adoption yet.
    guard.guarded_path("seed", "adopted_rules_monitor.jsonl").write_text("")

    inval = [g["hyp_id"] for g in GRAVEYARD
             if any(v.get("basis_valid") is False for v in g["rejection"].values())]
    print(f"seeded hypothesis_registry ({len(REGISTRY)}), graveyard ({len(GRAVEYARD)}), adopted_rules_monitor (0)")
    print(f"basis_invalidated -> returned to queue: {inval}")
    print("A3: association basis_invalidated (not computable on sandbox); economics stands, pending honest-fills recompute")


if __name__ == "__main__":
    main()
