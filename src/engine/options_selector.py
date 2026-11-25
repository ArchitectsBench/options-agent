"""Strike/expiry selection (Track B step 9). Deterministic — no LLM.

Directional structures only at this stage (premium selling stays blocked by
the applicability filter until IV-rank history exists):
  - debit_spread: buy ~0.60 delta, sell ~0.30 delta, 2-7 DTE
  - long_call / long_put: single leg ~0.60 delta, 2-7 DTE
  - 0dte_directional: single leg ~0.50 delta (ATM), 0 DTE

Returns (trade, None) on success, or (None, skip_detail) with the SPECIFIC
cause: tradier_unavailable | no_expiration_in_bucket | chain_empty |
liquidity_filters (with per-leg rejection stats) | degenerate_structure.
"""

import json
from datetime import datetime
from pathlib import Path

from src.data.options_chain import (DELTA_TARGETS, MAX_SPREAD, MIN_OI, filter_contracts_stats,
                                    get_chain, select_expiration)
from src.data.tradier import get_client

ROOT = Path(__file__).resolve().parents[2]
SPREAD_TELEMETRY_LOG = ROOT / "logs" / "observation" / "spread_telemetry.jsonl"


def _leg_summary(c: dict) -> dict:
    return {k: c[k] for k in ("symbol", "type", "strike", "expiration", "bid", "ask",
                              "mid", "spread", "oi", "delta", "iv")}


def _skip(cause: str, **detail) -> tuple[None, dict]:
    return None, {"cause": cause, "detail": detail,
                  "filters": {"max_spread": MAX_SPREAD, "min_oi": MIN_OI}}


def _telemetry_rel_spread(chain: list[dict], option_type: str, leg_role: str) -> float | None:
    """Nearest-to-target-delta contract in the RAW chain (ignoring liquidity
    filters) — describes spread quality for telemetry only. Never consulted
    by the actual leg-selection/gating logic elsewhere in this module."""
    target, _ = DELTA_TARGETS[leg_role]
    cands = [c for c in chain if c.get("type") == option_type and c.get("delta") is not None and c.get("mid")]
    if not cands:
        return None
    best = min(cands, key=lambda c: abs(abs(c["delta"]) - target))
    if not best.get("mid"):
        return None
    return round((best["ask"] - best["bid"]) / best["mid"], 4)


def _log_spread_telemetry(ticker: str, strategy: str, chain: list[dict],
                          option_type: str, leg_role: str, proceeded: bool) -> None:
    """Item 3 of the 2026-07-19 observation-instrumentation batch (see
    docs/external_edge_map.md). Logs spread quality for EVERY candidate that
    reaches this point (a chain was fetched — quotes are in hand) whether or
    not it proceeds to a trade.

    Hook point (chosen; documented per task instruction — the natural spot
    was ambiguous): build_trade()/_single_leg() below, right after
    `chain = get_chain(...)` succeeds. This is the LATEST point where quotes
    for a candidate are in hand before/at leg selection; pipeline.py (the
    only caller) never sees the raw chain itself, only build_trade's final
    (trade, skip_detail) result, so instrumenting here is both natural and
    unambiguous. Not logged for the three earlier skip causes
    (tradier_unavailable / no_expiration_in_bucket / chain_empty) — quotes
    never existed for those, so there is nothing to measure.

    Pure logging, appended best-effort; never raises into the caller."""
    try:
        rel_spread = _telemetry_rel_spread(chain, option_type, leg_role)
        SPREAD_TELEMETRY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SPREAD_TELEMETRY_LOG, "a") as fh:
            fh.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "ticker": ticker, "strategy": strategy,
                "rel_spread": rel_spread, "proceeded": proceeded,
            }) + "\n")
    except Exception:
        pass


def _single_leg(ticker: str, strategy: str, direction: str, option_type: str,
                bucket: str, leg_role: str, exp: str, chain: list[dict]):
    legs, stats = filter_contracts_stats(chain, option_type, leg_role)
    if not legs:
        _log_spread_telemetry(ticker, strategy, chain, option_type, leg_role, proceeded=False)
        return _skip("liquidity_filters", expiration=exp, leg_stats=[stats])
    leg = legs[0]
    _log_spread_telemetry(ticker, strategy, chain, option_type, leg_role, proceeded=True)
    return {
        "structure": f"{'0DTE ' if bucket == '0dte' else ''}long {option_type}",
        "strategy": strategy,
        "direction": direction,
        "expiration": exp,
        "legs": [{"action": "buy_to_open", **_leg_summary(leg)}],
        "net_debit": leg["mid"],
        "max_loss": leg["mid"],
        "max_profit": None,  # unbounded for long options
        "breakeven": leg["strike"] + leg["mid"] if option_type == "call" else leg["strike"] - leg["mid"],
    }, None


def build_trade(ticker: str, strategy: str, direction: str, spot: float,
                z_overlay: bool = False) -> tuple[dict | None, dict | None]:
    """direction: 'long' (bullish) or 'short' (bearish).
    Returns (trade, None) or (None, skip_detail).

    z_overlay (2026-07-06, from the SVI study): re-rank equally-acceptable
    strikes by price-space cheapness vs the smile — buy the cheap leg, sell
    the rich leg. Currently enabled ONLY for execution-test orders (an entry
    -rule change would reset the funnel's frozen forward clock; adoption
    into the funnel is a deliberate later decision on measured benefit)."""
    if not get_client().available():
        return _skip("tradier_unavailable", note="no TRADIER_PROD_TOKEN or API unreachable")
    option_type = "call" if direction == "long" else "put"

    bucket = "0dte" if strategy == "0dte_directional" else "2-7dte"
    exp = select_expiration(ticker, bucket)
    if exp is None and bucket != "0dte":
        exp = select_expiration(ticker, "7-21dte")
    if exp is None:
        return _skip("no_expiration_in_bucket", bucket=bucket)
    chain = get_chain(ticker, exp)
    if not chain:
        return _skip("chain_empty", expiration=exp)

    if strategy == "0dte_directional":
        return _single_leg(ticker, strategy, direction, option_type, bucket,
                           "directional_atm", exp, chain)
    if strategy in ("long_call", "long_put"):
        return _single_leg(ticker, strategy, direction, option_type, bucket,
                           "long_leg", exp, chain)

    # default: debit spread (CLAUDE.md preferred defined-risk directional)
    longs, long_stats = filter_contracts_stats(chain, option_type, "long_leg")
    shorts, short_stats = filter_contracts_stats(chain, option_type, "short_leg")
    if not longs or not shorts:
        _log_spread_telemetry(ticker, strategy, chain, option_type, "long_leg", proceeded=False)
        return _skip("liquidity_filters", expiration=exp, leg_stats=[long_stats, short_stats])
    long_leg, short_leg = longs[0], shorts[0]
    overlay_note = None
    if z_overlay:
        from src.engine.smile import price_z_scores, rank_candidates
        z = price_z_scores([c for c in chain if c["type"] == option_type])
        for cands, stats_ in ((longs, long_stats), (shorts, short_stats)):
            target = stats_.get("delta_target") or 0.5
            for c in cands:
                c["_delta_dist"] = abs(abs(c.get("delta") or 0) - target)
        zl, tl = rank_candidates(longs, z, "buy")
        zs, ts = rank_candidates(shorts, z, "sell")
        if zl["strike"] != zs["strike"]:  # never degrade into a degenerate structure
            long_leg, short_leg = zl, zs
        overlay_note = {"buy": tl, "sell": ts}
    if long_leg["strike"] == short_leg["strike"]:
        _log_spread_telemetry(ticker, strategy, chain, option_type, "long_leg", proceeded=False)
        return _skip("degenerate_structure", note="long and short legs landed on the same strike",
                     expiration=exp)
    net_debit = round(long_leg["mid"] - short_leg["mid"], 3)
    if net_debit <= 0:
        _log_spread_telemetry(ticker, strategy, chain, option_type, "long_leg", proceeded=False)
        return _skip("degenerate_structure", note=f"non-positive net debit {net_debit}",
                     expiration=exp)
    width = abs(short_leg["strike"] - long_leg["strike"])
    structure = f"{option_type} debit spread" + (" (ORB)" if strategy == "orb_breakout" else "")
    _log_spread_telemetry(ticker, strategy, chain, option_type, "long_leg", proceeded=True)
    return {
        "structure": structure,
        "strategy": strategy,  # preserves orb_breakout vs debit_spread for lineage/logging
        "direction": direction,
        "expiration": exp,
        "legs": [
            {"action": "buy_to_open", **_leg_summary(long_leg)},
            {"action": "sell_to_open", **_leg_summary(short_leg)},
        ],
        "net_debit": net_debit,
        "max_loss": net_debit,
        "max_profit": round(width - net_debit, 3),
        "breakeven": long_leg["strike"] + net_debit if option_type == "call" else long_leg["strike"] - net_debit,
        **({"z_overlay": overlay_note} if overlay_note else {}),
    }, None
