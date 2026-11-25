"""Price-space smile ranking — the z-signal entry overlay (2026-07-06).

Born from the SVI surface study (logs/research/svi_rv_20260706.md): strike
dislocations vs a name's own smile are real and converge, but too small to
trade as a standalone strategy at retail costs. As an ENTRY OVERLAY they are
free basis points: when a structure has several acceptable strikes, buy the
one trading CHEAP vs its neighbors and sell the one trading RICH.

Live variant fits in PRICE space on raw mids (quadratic in strike over the
neighborhood), NOT in IV space — Tradier's live chain IVs are ORATS-smoothed,
and fitting a smooth curve to smoothed values measures the smoother, not the
market (the exact tautology the research run had to avoid). Price-space
residuals are direct dollars cheap/rich, which is what an entry cares about.

z > 0 = rich vs neighbors (prefer for SELL legs)
z < 0 = cheap vs neighbors (prefer for BUY legs)
"""

import logging

logger = logging.getLogger("trader")

MIN_NEIGHBORS = 7      # need a real neighborhood to fit a curve
FIT_WINDOW = 11        # strikes centered on the candidate region


def price_z_scores(chain_rows: list[dict]) -> dict[float, float]:
    """z per strike from a quadratic fit of mid price vs strike over the
    contiguous strike ladder. chain_rows: one option_type, one expiration,
    each row needs 'strike' and 'mid'. Returns {} when the ladder is too
    thin to say anything — the overlay then simply does nothing."""
    rows = sorted((r for r in chain_rows
                   if r.get("mid") and r["mid"] > 0 and r.get("strike")),
                  key=lambda r: r["strike"])
    if len(rows) < MIN_NEIGHBORS:
        return {}
    try:
        import numpy as np
        ks = np.array([r["strike"] for r in rows], dtype=float)
        ms = np.array([r["mid"] for r in rows], dtype=float)
        # quadratic in strike on log(mid): option prices are convex and
        # positive; log damps the deep-ITM dominance
        coef = np.polyfit(ks, np.log(ms), 2)
        fitted = np.exp(np.polyval(coef, ks))
        resid = ms - fitted
        std = float(resid.std(ddof=1))
        if std <= 1e-9:
            return {}
        return {float(k): round(float(r) / std, 2) for k, r in zip(ks, resid)}
    except Exception as exc:
        logger.debug("smile fit failed: %s", exc)
        return {}


def rank_candidates(candidates: list[dict], z: dict[float, float],
                    side: str, delta_tolerance: float = 0.05) -> tuple[dict, dict]:
    """Re-rank liquidity/delta-passed candidates by the z overlay.

    candidates: already sorted best-first by delta closeness (index 0 = the
    default pick). Only candidates within delta_tolerance of the default's
    delta distance are eligible — the overlay improves the price of the
    INTENDED structure, never changes the structure.

    side 'buy'  -> prefer the CHEAPEST (lowest z)
    side 'sell' -> prefer the RICHEST (highest z)

    Returns (chosen, telemetry)."""
    default = candidates[0]
    if not z or len(candidates) < 2:
        return default, {"applied": False, "reason": "no fit or single candidate"}
    # eligibility band relative to the default pick's delta quality
    def delta_dist(c):
        return c.get("_delta_dist", 0.0)
    band = delta_dist(default) + delta_tolerance
    eligible = [c for c in candidates if delta_dist(c) <= band and c["strike"] in z]
    if len(eligible) < 2:
        return default, {"applied": False, "reason": "band too narrow"}
    keyed = sorted(eligible, key=lambda c: z[c["strike"]],
                   reverse=(side == "sell"))
    chosen = keyed[0]
    telemetry = {
        "applied": chosen["strike"] != default["strike"],
        "side": side,
        "default_strike": default["strike"],
        "default_mid": default.get("mid"),
        "default_z": z.get(default["strike"]),
        "chosen_strike": chosen["strike"],
        "chosen_mid": chosen.get("mid"),
        "chosen_z": z.get(chosen["strike"]),
        "n_eligible": len(eligible),
    }
    return chosen, telemetry
