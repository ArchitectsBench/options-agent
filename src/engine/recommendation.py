"""Recommendation formatting + logging (Track B step 9).

Every recommendation is stamped UNVALIDATED — PAPER ONLY on every surface
(console block, jsonl log) and appended to logs/recommendations.jsonl with
the full context chain for later performance review.
"""

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REC_LOG = ROOT / "logs" / "recommendations.jsonl"

_counter = {"n": 0}


def _entry_features_safe(ticker: str, signals: dict, regime: dict, trade: dict) -> dict:
    """Guarded wrapper: instrumentation must never break the record path (2026-08-03)."""
    try:
        from src.engine.entry_features import build_entry_features
        return build_entry_features(ticker, signals, regime, trade,
                                    datetime.now().isoformat(timespec="seconds"))
    except Exception:
        return {}


def next_rec_id() -> str:
    _counter["n"] += 1
    return f"{datetime.now():%Y-%m-%d}-{_counter['n']:03d}"


def build_recommendation(ticker: str, candidate: dict, trade: dict, signals: dict,
                         regime: dict, local_perf: dict | None, claude_result: dict,
                         cycle: int) -> dict:
    validated_by = "Claude (approved)" if claude_result["status"] == "approved" else \
        f"Claude validation skipped: {claude_result['reason']}"
    return {
        "rec_id": next_rec_id(),
        "stamp": "UNVALIDATED — PAPER ONLY",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "cycle": cycle,
        "ticker": ticker,
        "trade": trade,
        "local_confidence": candidate.get("final_confidence"),
        "local_reasoning": candidate.get("reasoning"),
        "validation_chain": {
            "local_model": local_perf.get("model") if local_perf else None,
            "claude": claude_result,
            "summary": validated_by,
        },
        "indicator_snapshot": signals,
        "regime_summary": {k: regime.get(k) for k in ("bias", "trend", "vol_regime", "vix", "gex")},
        # instrumentation only (2026-08-03); recorded, never evaluated
        "entry_features": _entry_features_safe(ticker, signals, regime, trade),
        "expiry_invalidation": {
            "signal_expires": "30 minutes if entry not triggered",
            "invalidation": "close beyond session VWAP against position",
        },
    }


def format_recommendation(rec: dict) -> str:
    t = rec["trade"]
    legs = "\n".join(
        f"    {leg['action']:<14}{leg['symbol']}  strike {leg['strike']}  "
        f"mid {leg['mid']}  delta {leg['delta']}  OI {leg['oi']}"
        for leg in t["legs"]
    )
    claude = rec["validation_chain"]["claude"]
    narrative = (claude.get("verdict") or {}).get("narrative", "(no Claude narrative)")
    return f"""
  ═══════════════════════════════════════════════════════
    TRADE RECOMMENDATION  #{rec['rec_id']}
    *** UNVALIDATED — PAPER ONLY (observation mode) ***
  ═══════════════════════════════════════════════════════
    Generated:    {rec['generated']}  (cycle #{rec['cycle']})
    Validated by: local conf {rec['local_confidence']} -> {rec['validation_chain']['summary']}

    Ticker:       {rec['ticker']}
    Structure:    {t['structure']}  exp {t['expiration']}
{legs}
    Net debit:    {t['net_debit']}   Max loss: {t['max_loss']}   Max profit: {t['max_profit']}
    Breakeven:    {t['breakeven']}

    Narrative:    {narrative}
    Regime:       {rec['regime_summary']['bias']} / {rec['regime_summary']['trend']} / VIX {rec['regime_summary']['vix']}
    Invalidation: {rec['expiry_invalidation']['invalidation']}
  ═══════════════════════════════════════════════════════"""


def log_recommendation(rec: dict):
    REC_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(REC_LOG, "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def recent_rec_exists(ticker: str, strategy: str, direction: str,
                      within_min: int = 30) -> str | None:
    """Unexpired recommendation for the same ticker+strategy+direction
    (covers recs whose paper order wasn't tracked, e.g. sandbox off)."""
    if not REC_LOG.exists():
        return None
    now = datetime.now()
    for line in REC_LOG.read_text().splitlines()[-200:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = rec.get("trade", {})
        if rec.get("ticker") != ticker or t.get("strategy") != strategy \
                or t.get("direction") != direction:
            continue
        age_min = (now - datetime.fromisoformat(rec["generated"])).total_seconds() / 60
        if age_min < within_min:
            return f"unexpired rec {rec['rec_id']} ({age_min:.0f}m old)"
    return None
