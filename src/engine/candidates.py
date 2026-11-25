"""Deterministic candidate generation (Track B step 8) — replaces the Ollama
local-LLM synthesis. Ranks applicability-'viable' (ticker, strategy) pairs by
confirmation strength. Fully auditable: every candidate traces to explicit rules,
no opaque model, no per-cycle LLM latency. Claude still does the validation gate.

Returns the same shape the old synthesize() did, so step 9 is unchanged:
  {"synthesis": {"candidates": [...], "cycle_assessment": str}, "perf": {...}, "error": None}
Each candidate: {ticker, strategy, final_confidence, reasoning, conflicts}.
"""
from __future__ import annotations

# confidence heuristic: a 'viable' setup (all requires met) starts below the
# 0.75 invoke threshold and clears it with >=1 confirmation. Transparent + tunable.
CONF_BASE = 0.70
CONF_PER_CONFIRM = 0.06
CONF_CAP = 0.95
TOP_N = 3


def synthesize_deterministic(record: dict, top_n: int = TOP_N) -> dict:
    signals = record.get("signals", {})
    applicability = record.get("applicability", {})
    cands = []
    for ticker, verdicts in applicability.items():
        sig = signals.get(ticker)
        if not isinstance(sig, dict) or "error" in sig:
            continue
        for strat, v in verdicts.items():
            if v.get("verdict") != "viable":
                continue
            confirms = v.get("confirms_met", []) or []
            n = len(confirms)
            conf = round(min(CONF_CAP, CONF_BASE + CONF_PER_CONFIRM * n), 2)
            cands.append({
                "ticker": ticker, "strategy": strat,
                "final_confidence": conf,
                "reasoning": f"viable; {n} confirm(s)" + (f": {', '.join(confirms)}" if confirms else ""),
                "conflicts": None,
                "_n": n,
            })
    cands.sort(key=lambda c: (-c["_n"], -c["final_confidence"], c["ticker"]))
    top = cands[:top_n]
    for c in top:
        c.pop("_n", None)
    n_viable = sum(1 for vs in applicability.values()
                   for v in vs.values() if v.get("verdict") == "viable")
    return {
        "synthesis": {
            "candidates": top,
            "cycle_assessment": f"deterministic: {n_viable} viable setup(s), "
                                f"top {len(top)} by confirmation strength",
        },
        "perf": {"model": "deterministic", "latency_s": 0.0, "tokens_per_s": 0},
        "error": None,
    }
