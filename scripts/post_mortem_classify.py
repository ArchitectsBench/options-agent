"""Phase 2 classifier — ruleset p2_v1 (approved 2026-07-23 with marks 1-5).

Deterministic; fills the null `classification` block only; atomic rewrite;
originals preserved in git. Every A-rule test records the mfe_guard result
and value (mark #1). B2 split into B2a (FOMC 14:00 ET) / B2b (CPI 08:30 ET)
(mark #4). A4 KEPT — iv_rank provenance is live chain-derived snapshots
(mark #3). E is per-lane empirical p95, exclusion-only; big unexplained
losses land in UNCLASSIFIED_TAIL (never E).
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config" / "post_mortem.yaml").read_text())
P2 = CFG["phase2"]
PM_DIR = ROOT / "logs" / "post_mortems"


def direction_of(pm: dict, legs_by_rid: dict) -> str | None:
    d = (pm["meta"].get("direction") or "").lower() or None
    if d in ("long", "short"):
        return d
    legs = legs_by_rid.get(pm["meta"]["rec_id"]) or []
    for leg in legs:                       # single-leg directional inference
        side = str(leg.get("side", "")).lower()
        sym = str(leg.get("symbol", ""))
        right = "C" if "C" in sym[-9:] else ("P" if "P" in sym[-9:] else None)
        if side.startswith("buy") and right:
            return "long" if right == "C" else "short"
    return None


def classify(pm: dict, e_p95: float, legs_by_rid: dict, skip_d: bool = False) -> dict:
    lc, ov = pm["lifecycle"], pm["market_truth_overlay"]
    rec = pm["entry_snapshot"]["recorded"]
    snap = rec.get("indicator_snapshot") or {}
    regime = rec.get("regime") or {}
    fd = pm["failure_definition"]
    risk_pct = abs(fd["truth_pnl_pct_of_risk"])
    mfe = lc.get("mfe_pct_of_max_risk")
    mae = lc.get("mae_pct_of_max_risk")
    mins = lc.get("minutes_held") or 0
    direction = direction_of(pm, legs_by_rid)
    tested, fired = [], []
    guard_val = P2["a_mfe_guard_pct"]
    mfe_guard_pass = (mfe is not None and mfe < guard_val)
    mfe_guard_str = f"mfe_guard(mfe={mfe}, threshold={guard_val}) -> {'PASS' if mfe_guard_pass else 'FAIL'}"

    def test(rule, cond, evidence):
        tested.append({"rule": rule, "fired": bool(cond), "evidence": evidence})
        if cond:
            fired.append(rule)

    # ---- D
    be, te = lc.get("booked_entry_fill"), lc.get("truth_mid_entry")
    bx, tx = lc.get("booked_exit_fill"), lc.get("truth_mid_exit")
    # batch-0 fix (2026-07-24): read the authoritative real fill quantity, not
    # rec.risk.quantity (None->1 for assembled SCALP recs, which under-fired D1).
    qty = (pm.get("reconstructed_risk") or {}).get("quantity") \
        or (rec.get("risk") or {}).get("quantity") or 1
    slip_usd = None
    if None not in (be, te, bx, tx):
        slip_usd = (abs(be - te) + abs(bx - tx)) * 100 * qty
    test("D1_slippage_dominant",
         (not skip_d) and slip_usd is not None and abs(fd["truth_pnl"]) > 0
         and slip_usd >= P2["d1_share"] * abs(fd["truth_pnl"]),
         f"slip_usd={None if slip_usd is None else round(slip_usd, 2)} vs "
         f"{P2['d1_share']}x|pnl|={round(P2['d1_share'] * abs(fd['truth_pnl']), 2)}")
    rk = rec.get("risk") or {}
    test("D2_sizing_breach",
         (not skip_d) and bool(rk) and rk.get("max_loss_per_contract") and rk.get("risk_budget")
         and rk.get("quantity", 0) * rk["max_loss_per_contract"]
         > rk["risk_budget"] * P2["d2_tolerance"],
         f"risk_block={'present' if rk else 'absent'}")

    # ---- A (each records the mfe_guard — mark #1)
    vd = snap.get("vwap_dist_atr")
    a1_geom = (vd is not None and direction is not None
               and abs(vd) >= P2["a1_vwap_atr"]
               and ((direction == "long" and vd > 0) or (direction == "short" and vd < 0)))
    test("A1_extension_chase", a1_geom and mfe_guard_pass,
         f"vwap_dist_atr={vd}, direction={direction or 'UNEVALUABLE'}; {mfe_guard_str}")
    vr = snap.get("volume_ratio")
    test("A2_no_participation",
         vr is not None and vr < P2["a2_volume_ratio"]
         and pm["meta"].get("strategy") in ("0dte_directional", "orb_breakout", "debit_spread")
         and mfe_guard_pass,
         f"volume_ratio={vr}, strategy={pm['meta'].get('strategy')}; {mfe_guard_str}")
    bias = regime.get("bias")
    a3_opp = (direction is not None and bias in ("bullish", "bearish")
              and ((bias == "bullish" and direction == "short")
                   or (bias == "bearish" and direction == "long")))
    test("A3_regime_opposition", a3_opp and mfe_guard_pass,
         f"bias={bias}, direction={direction or 'UNEVALUABLE'}; {mfe_guard_str}")
    ivr = (snap.get("iv_rank") or {}).get("rank") if isinstance(snap.get("iv_rank"), dict) else None
    is_debit = pm["meta"].get("strategy") in ("debit_spread", "0dte_directional", "long_call", "long_put")
    test("A4_iv_headwind_long_premium",
         ivr is not None and is_debit and ivr >= P2["a4_iv_rank"] and mfe_guard_pass,
         f"iv_rank={ivr} (provenance: live chain snapshots), debit={is_debit}; {mfe_guard_str}")

    # ---- B
    gaps = ov.get("gap_events_in_window") or []
    big_gap = any(g.get("move_pct", 0) >= P2["b1_move_pct"] for g in gaps)
    test("B1_adverse_gap_shock", big_gap,
         f"gaps={[(g['ts'], g['move_pct']) for g in gaps]}"
         f" (direction-opposition unresolvable without tick sign; move>=1% treated adverse on losers)")
    macro = (ov.get("known_events") or {}).get("macro")
    ent_hm = lc.get("entry_ts", "")[11:16]
    ex_hm = lc.get("exit_ts", "")[11:16]

    def _et(hm):  # ledger is PT; +3h
        try:
            h, m = hm.split(":"); return f"{int(h) + 3:02d}:{m}"
        except ValueError:
            return "00:00"
    test("B2a_fomc_flip", macro == "fomc" and _et(ent_hm) < "14:00" < _et(ex_hm),
         f"macro={macro}, entry_ET={_et(ent_hm)}, exit_ET={_et(ex_hm)}")
    test("B2b_cpi_open", macro == "cpi" and _et(ent_hm) <= "09:45",
         f"macro={macro}, entry_ET={_et(ent_hm)}")
    atr = snap.get("atr"); last = snap.get("last")
    umove = ov.get("underlying_move_entry_to_exit_pct")
    stack = snap.get("ema_stack") or {}
    agreed = (direction == "long" and stack.get("15m") == "bullish") or \
             (direction == "short" and stack.get("15m") == "bearish")
    b3 = (mfe is not None and mfe >= guard_val and atr and last and umove is not None
          and direction is not None and agreed
          and ((direction == "long" and umove < 0) or (direction == "short" and umove > 0))
          and abs(umove) / 100 * last >= P2["b3_atr_mult"] * atr)
    test("B3_trend_flip_against", bool(b3),
         f"mfe={mfe}, umove={umove}%, atr={atr}, stack15={stack.get('15m')}, direction={direction}")

    # ---- C
    test("C1_gave_back_winner", mfe is not None and mfe >= P2["c1_mfe_pct"],
         f"mfe={mfe} vs {P2['c1_mfe_pct']}")
    test("C2_clock_exit_loss",
         lc.get("exit_trigger") == "flatten_1545" and mfe is not None and mfe >= guard_val,
         f"trigger={lc.get('exit_trigger')}, mfe={mfe}")
    c3 = (lc.get("exit_trigger") == "vwap_invalidation" and mfe is not None
          and mfe >= guard_val and mae is not None and abs(mae) <= 25
          and mins < P2["c3_max_minutes"])
    test("C3_invalidation_whipsaw", bool(c3),
         f"trigger={lc.get('exit_trigger')}, mfe={mfe}, mae={mae}, mins={mins}")

    ORDER = ["D1_slippage_dominant", "D2_sizing_breach",
             "A1_extension_chase", "A2_no_participation", "A3_regime_opposition",
             "A4_iv_headwind_long_premium",
             "B1_adverse_gap_shock", "B2a_fomc_flip", "B2b_cpi_open",
             "B3_trend_flip_against",
             "C1_gave_back_winner", "C2_clock_exit_loss", "C3_invalidation_whipsaw"]
    fired_sorted = [r for r in ORDER if r in fired]
    if fired_sorted:
        primary_rule = fired_sorted[0]
        primary = primary_rule[0]
        secondary = fired_sorted[1] if len(fired_sorted) > 1 else None
        reasoning = (f"{primary_rule} fired (precedence D>A>B>C). "
                     f"All rules tested; {len(fired_sorted)} fired: {fired_sorted}.")
    elif risk_pct <= e_p95:
        primary_rule, primary, secondary = None, "E", None
        reasoning = (f"E by exclusion: all 13 A-D rules tested, none fired "
                     f"({mfe_guard_str}); loss {risk_pct:.1f}% of risk within "
                     f"lane p{P2['e_percentile']}={e_p95:.1f}%. pit_pending rules "
                     f"A5/A6 not yet run (ORATS mirror stale).")
    else:
        primary_rule, primary, secondary = None, "UNCLASSIFIED_TAIL", None
        reasoning = (f"TAIL: no A-D rule fired but loss {risk_pct:.1f}% exceeds "
                     f"lane p{P2['e_percentile']}={e_p95:.1f}% — not E by definition; "
                     f"surfaced for human review.")
    return {"primary": primary, "primary_rule": primary_rule,
            "secondary": secondary, "rules_tested": tested,
            "reasoning": reasoning, "pit_pending": True,
            "thresholds_version": P2["thresholds_version"]}


def main() -> None:
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib
    enr = importlib.import_module("enrich_trades_thetadata")
    legs_by_rid = enr.fallback_legs_map()
    files = sorted(glob.glob(str(PM_DIR / "*.json")))
    pms = [(f, json.load(open(f))) for f in files]
    # per-lane empirical p95 of loss share
    p95 = {}
    for lane in ("funnel", "SCALP"):
        losses = [abs(p["failure_definition"]["truth_pnl_pct_of_risk"])
                  for _, p in pms if p["meta"]["lane"] == lane]
        p95[lane] = float(np.percentile(losses, P2["e_percentile"])) if losses else 100.0
    counts: dict = {}
    for f, pm in pms:
        cl = classify(pm, p95[pm["meta"]["lane"]], legs_by_rid)
        pm["classification"] = cl
        d = direction_of(pm, legs_by_rid)
        if d and not pm["meta"].get("direction"):
            pm["meta"]["direction"] = d
            pm["meta"]["direction_source"] = "order_legs_backfill"
        if cl.get("primary_rule") == "D1_slippage_dominant":
            pm["classification"]["secondary_candidate"] = classify(
                pm, p95[pm["meta"]["lane"]], legs_by_rid, skip_d=True)
            pm["classification"]["secondary_candidate"]["note"] = (
                "NON-AUTHORITATIVE: A-E re-run ignoring D for post-migration validation")
        Path(f).write_text(json.dumps(pm, indent=1))
        key = (pm["meta"]["lane"], cl["primary"])
        counts[key] = counts.get(key, 0) + 1
    print("lane p95 loss%:", {k: round(v, 1) for k, v in p95.items()})
    for lane in ("funnel", "SCALP"):
        row = {c: n for (ln, c), n in counts.items() if ln == lane}
        tot = sum(row.values()) or 1
        e_share = row.get("E", 0) / tot
        print(f"{lane}: {row}  E-share={e_share:.0%}"
              f"{'  <-- FLAG (>70%)' if e_share > P2['e_share_flag'] else ''}")


if __name__ == "__main__":
    main()
