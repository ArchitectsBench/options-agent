#!/usr/bin/env python3
"""Batch-0a one-time migration: persist `reconstructed_risk` (real fill quantity
+ qty-scaled max_dollar_risk) into every stored post-mortem record.

Why: the slip term in D1 (`classify.py`) and phase3 features read a quantity
that was `None -> 1` for assembled SCALP recs, while `truth_pnl` carries the
real size — so slip was compared to full-size P&L on a mismatched basis and D1
under-fired as a function of position size (audit: post_mortem_program_state.md
§8). The engine now emits `reconstructed_risk` on every build; this script
backfills records generated before that change so they don't need a full
(slow, API-touching) engine re-run.

Additive ONLY — classification blocks are never touched. Idempotent. Mirrors
`post_mortem_engine.build_one`'s risk formula (rec.risk.max_dollar_risk, else
truth_mid_entry*100*qty, else |booked_pnl|, else 1). Real qty comes from the
enriched truth ledger (same source the engine uses via tr.quantity).

Run: .venv/bin/python scripts/backfill_reconstructed_risk.py
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _enriched_qty() -> dict:
    q = {}
    for line in (ROOT / "logs/observation/enriched_trades_v1.jsonl").read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("status") == "ok":
            q[r.get("rec_id")] = r.get("quantity")
    return q


def backfill(paths, enq) -> tuple[int, int]:
    n = fixed = 0
    for f in paths:
        pm = json.load(open(f))
        rid = pm["meta"]["rec_id"]
        rec_risk = pm["entry_snapshot"]["recorded"].get("risk") or {}
        qty = enq.get(rid) or rec_risk.get("quantity") or 1
        e_mid = pm["lifecycle"].get("truth_mid_entry")
        fd = pm["failure_definition"]
        tp, pct = fd.get("truth_pnl"), fd.get("truth_pnl_pct_of_risk")
        lane = pm["meta"]["lane"]
        if lane == "SCALP" and e_mid:
            # SCALP: build_one risk = truth-mid debit = e_mid*100*qty (exact).
            # Matches the engine AND reconciles with raw fill data (INV7);
            # pct stays consistent within rounding (INV2).
            mdr = float(e_mid) * 100 * qty
        else:
            # funnel: risk is recorded (booked-based, differs from truth-mid by
            # entry slippage) — recover build_one's exact denominator from pct
            # so INV2 holds; INV7's truth-mid identity does not apply to funnel.
            mdr = rec_risk.get("max_dollar_risk")
            if mdr is None and pct not in (None, 0) and tp is not None:
                mdr = abs(tp) / (abs(pct) / 100.0)
            if mdr is None and e_mid:
                mdr = float(e_mid) * 100 * qty
        if mdr is None:
            mdr = abs(float(tp or 0)) or 1.0
        pm["reconstructed_risk"] = {"quantity": int(qty),
                                    "max_dollar_risk": round(float(mdr), 2),
                                    "basis": "backfill: enriched quantity + max_dollar_risk"}
        Path(f).write_text(json.dumps(pm, indent=1))
        n += 1
        if pm["meta"]["lane"] == "SCALP" and rec_risk.get("quantity") is None:
            fixed += 1
    return n, fixed


def main():
    enq = _enriched_qty()
    fails = [f for f in glob.glob(str(ROOT / "logs/post_mortems/*.json")) if "phase3_results" not in f]
    ctrls = glob.glob(str(ROOT / "logs/post_mortems/controls/*.json"))
    nf, sf = backfill(fails, enq)
    nc, sc = backfill(ctrls, enq)
    print(f"backfilled failures={nf} (SCALP qty=None -> real: {sf})")
    print(f"backfilled controls={nc} (SCALP qty=None -> real: {sc})")


if __name__ == "__main__":
    main()
