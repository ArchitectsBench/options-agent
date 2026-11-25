"""CLL Tier-1 metric-integrity invariants (the crown; distinct from observation).

Six cross-foots that MUST hold if the rulers are correct. A violation is an
INTEGRITY ALARM (never a hypothesis, never an observation). INV1 is the exact
check that would have caught the quantity-slip bug; INV3 the unclamped-MFE error.
Spec: docs/continuous_learning_loop_spec.md §6.

Pure functions over data the caller assembles — no writes here (nightly.py owns
writes via guard.py). Each check returns:
  {"invariant","status":"OK"|"ALARM","severity","detail"}
"""
from __future__ import annotations

import glob
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.util.atomic import read_jsonl  # noqa: E402


def ruleset_hash() -> str:
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "post_mortem.yaml").read_text())
    return hashlib.sha256(json.dumps(cfg["phase2"], sort_keys=True).encode()).hexdigest()[:12]


def _slip(e: dict):
    be, te = e.get("booked_entry"), e.get("true_mid_entry")
    bx, tx = e.get("booked_exit"), e.get("true_mid_exit")
    q = e.get("quantity") or 1
    if None in (be, te, bx, tx):
        return None
    return (abs(be - te) + abs(bx - tx)) * 100 * q


# ── INV1: D-share reconciliation (catches the quantity-slip bug) ──
def inv1_d_share(ledger, enriched, d1_share=0.5, tol=0.02):
    fails = [r for r in ledger if r["lane"] == "SCALP" and r["cohort"] == "failure"]
    if not fails:
        return {"invariant": "INV1_d_share_reconciliation", "status": "OK",
                "severity": "critical", "detail": {"note": "no SCALP failures yet"}}
    classified_d = sum(1 for r in fails if r["primary"] == "D") / len(fails)
    raw_hits = raw_n = 0
    for r in fails:
        e = enriched.get(r["rec_id"])
        if not e or e.get("true_pnl_mid") in (None, 0):
            continue
        s = _slip(e)
        if s is None:
            continue
        raw_n += 1
        if s >= d1_share * abs(e["true_pnl_mid"]):
            raw_hits += 1
    raw_d = raw_hits / raw_n if raw_n else None
    gap = abs(classified_d - raw_d) if raw_d is not None else None
    alarm = gap is not None and gap > tol
    return {"invariant": "INV1_d_share_reconciliation",
            "status": "ALARM" if alarm else "OK", "severity": "critical",
            "detail": {"classified_d_share": round(classified_d, 3),
                       "raw_d_share": None if raw_d is None else round(raw_d, 3),
                       "gap": None if gap is None else round(gap, 3), "tol": tol,
                       "n_fail": len(fails), "n_raw": raw_n}}


# ── INV2: %-of-risk consistency (num/denom quantity match) ──
def inv2_pct_consistency(ledger, tol=0.15):
    worst = 0.0; worst_rid = None; n = 0
    for r in ledger:
        # only failures carry a loss %-of-risk; controls store 0 by design
        if r.get("cohort") != "failure":
            continue
        mdr = r.get("max_dollar_risk")
        if not mdr:
            continue
        expected = r["truth_pnl"] / mdr * 100
        diff = abs(expected - r["truth_pnl_pct_of_risk"])
        n += 1
        if diff > worst:
            worst, worst_rid = diff, r["rec_id"]
    alarm = worst > tol
    return {"invariant": "INV2_pct_of_risk_consistency",
            "status": "ALARM" if alarm else "OK", "severity": "critical",
            "detail": {"worst_abs_diff": round(worst, 3), "worst_rid": worst_rid,
                       "tol": tol, "n_checked": n}}


# ── INV3: MFE/MAE window clamp + path consistency (catches unclamped-MFE) ──
def inv3_clamp(batch_pms, enr_mod):
    viol = []
    for pm in batch_pms:
        lc = pm["lifecycle"]
        path = lc.get("pnl_path_1min") or []
        risk = (pm.get("reconstructed_risk") or {}).get("max_dollar_risk")
        if not path or not risk:
            continue
        vals = [x["value"] for x in path]
        exp_mfe = round(max(vals) / risk * 100, 1)
        exp_mae = round(min(vals) / risk * 100, 1)
        if lc.get("mfe_pct_of_max_risk") not in (None, exp_mfe) or \
           lc.get("mae_pct_of_max_risk") not in (None, exp_mae):
            viol.append({"rid": pm["meta"]["rec_id"], "why": "mfe/mae != max/min(path)/risk",
                         "stored": [lc.get("mfe_pct_of_max_risk"), lc.get("mae_pct_of_max_risk")],
                         "expected": [exp_mfe, exp_mae]})
            continue
        # window clamp: every path minute within [entry, exit] (ET)
        try:
            e_et = enr_mod.pt_to_et_minute(lc.get("entry_ts", ""))
            x_et = enr_mod.pt_to_et_minute(lc.get("exit_ts", ""))
            day = pm["meta"]["rec_id"][:8]
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            lo, hi = f"{day}T{e_et}", f"{lc.get('exit_ts','')[:10] or day}T{x_et}"
            out = [x["ts"] for x in path if not (lo <= x["ts"] <= hi)]
            if out:
                viol.append({"rid": pm["meta"]["rec_id"], "why": "path bleeds outside holding window",
                             "window": [lo, hi], "outside": out[:3]})
        except Exception:
            pass
    return {"invariant": "INV3_mfe_mae_clamp",
            "status": "ALARM" if viol else "OK", "severity": "critical",
            "detail": {"violations": viol[:5], "n_violations": len(viol),
                       "n_checked": len(batch_pms)}}


# ── INV4: funnel is an untouched control — dist must not move without cause ──
def inv4_funnel_control(baseline, classify_fn, legs):
    ctrl = baseline.get("funnel_control") or {}
    if ruleset_hash() != baseline.get("ruleset_hash"):
        return {"invariant": "INV4_funnel_control", "status": "OK", "severity": "critical",
                "detail": {"note": "ruleset hash changed (declared cause) — funnel may move"}}
    rec_ids = set(ctrl.get("rec_ids") or [])
    p95 = (baseline.get("frozen_p95") or {}).get("funnel", 100.0)
    dist = Counter()
    for f in glob.glob(str(ROOT / "logs/post_mortems/*.json")):
        if "phase3_results" in f:
            continue
        try:
            pm = json.load(open(f))
        except Exception:
            continue
        if pm.get("meta", {}).get("rec_id") in rec_ids:
            dist[classify_fn(pm, p95, legs)["primary"]] += 1
    now = dict(dist)
    base = ctrl.get("dist") or {}
    alarm = now != base
    return {"invariant": "INV4_funnel_control",
            "status": "ALARM" if alarm else "OK", "severity": "critical",
            "detail": {"baseline_dist": base, "current_dist": now,
                       "note": "funnel moved with no ruleset change / new data" if alarm else "stable"}}


# ── INV5: shadow "actual" equals truth mid (no booked fiction) ──
def inv5_shadow_actual(enriched, tol=0.01):
    led = ROOT / "logs" / "observation" / "exit_variants.jsonl"
    if not led.exists():
        return {"invariant": "INV5_shadow_actual_is_truth", "status": "OK",
                "severity": "high", "detail": {"note": "no shadow ledger"}}
    bad = []; n = 0
    for row in read_jsonl(led):
        e = enriched.get(row.get("rec_id"))
        if not e or e.get("true_pnl_mid") is None:
            continue
        n += 1
        if abs(row.get("actual", 0) - round(e["true_pnl_mid"], 2)) > tol:
            bad.append(row.get("rec_id"))
    return {"invariant": "INV5_shadow_actual_is_truth",
            "status": "ALARM" if bad else "OK", "severity": "high",
            "detail": {"mismatches": bad[:5], "n_mismatch": len(bad), "n_checked": n}}


# ── INV6: ledger integrity (no dupes; stamped) ──
def inv6_ledger(ledger):
    seen = Counter((r["rec_id"], r["cohort"], r.get("ruleset"), r.get("baseline_pin")) for r in ledger)
    dupes = [k for k, c in seen.items() if c > 1]
    unstamped = [r["rec_id"] for r in ledger if not r.get("ruleset") or not r.get("baseline_pin")]
    alarm = bool(dupes or unstamped)
    return {"invariant": "INV6_ledger_integrity",
            "status": "ALARM" if alarm else "OK", "severity": "high",
            "detail": {"dupes": dupes[:5], "n_dupes": len(dupes),
                       "unstamped": unstamped[:5], "n_unstamped": len(unstamped)}}


# ── INV7: reconstructed_risk bound vs RAW fill data (shared-input check) ──
# INV1's classified side depends on reconstructed_risk (via classify's D1 qty);
# without this, a corruption of reconstructed_risk could move both INV1 sides
# together. INV7 checks the upstream field ITSELF against the independent raw
# enriched fill data, so the shared input is bounded, not only its derivatives.
DEBIT_STRATEGIES = {"0dte_directional", "debit_spread", "long_call", "long_put", "orb_breakout"}

def inv7_reconstructed_risk(ledger, enriched, rel_tol=0.02):
    qty_bad, risk_bad, n = [], [], 0
    for r in ledger:
        e = enriched.get(r["rec_id"])
        if not e:
            continue
        n += 1
        # (a) reconstructed qty MUST equal the raw fill qty (independent source)
        if e.get("quantity") is not None and r.get("reconstructed_qty") != e.get("quantity"):
            qty_bad.append((r["rec_id"], r.get("reconstructed_qty"), e.get("quantity")))
        # (b) truth-mid debit identity: max_dollar_risk ≈ qty * entry_mid * 100.
        # SCALP debit only — funnel risk is recorded/booked-based and legitimately
        # differs from the truth mid by entry slippage.
        strat = r.get("strategy") or ""
        emid, mdr, q = e.get("true_mid_entry"), r.get("max_dollar_risk"), r.get("reconstructed_qty")
        if r.get("lane") == "SCALP" and strat in DEBIT_STRATEGIES and emid and mdr and q:
            expect = q * emid * 100
            if abs(mdr - expect) / mdr > rel_tol:
                risk_bad.append((r["rec_id"], mdr, round(expect, 1)))
    alarm = bool(qty_bad or risk_bad)
    return {"invariant": "INV7_reconstructed_risk_vs_raw",
            "status": "ALARM" if alarm else "OK", "severity": "critical",
            "detail": {"qty_mismatch": qty_bad[:5], "n_qty_mismatch": len(qty_bad),
                       "risk_mismatch": risk_bad[:5], "n_risk_mismatch": len(risk_bad),
                       "n_checked": n, "rel_tol": rel_tol}}


# ── INV8: signal-data freshness (the blind spot INV1-INV7 all shared) ──
# INV1-INV7 reconcile DERIVED figures; NONE looked at how old the INPUT was.
# A 15-min-frozen signal feed would pass every one of them. INV8 stamps
# signal_data_age (decision wall-clock minus the bar timestamp the decision was
# computed from) and CRITICALs when it exceeds N. Added 2026-07-24 after the
# feed-latency blind spot was found by inspection, not by the integrity tier.
def inv8_signal_age(ledger):
    """Pre-gate LEAK detector (mark 5): re-applies the per-lane data_suspect
    verdict (pregate.verdict) to every active-ledger row and alarms if any would
    be data_suspect — i.e., a stale entry leaked past the pre-gate. This is
    per-lane and SEES one-bar staleness (drift>floor / cross-session / age
    fallback), yet never false-flags a drift-clean keep the pre-gate legitimately
    allowed. A global 30-min age bar could see none of this."""
    import importlib
    pg = importlib.import_module("pregate")
    fl = pg.floors()
    leaks = []
    for r in ledger:
        ds, reason = pg.verdict(r.get("lane"), r.get("underlying_drift_over_lag"),
                                r.get("signal_data_age_min"), r.get("missing_bars_over_lag"),
                                r.get("cross_session", False), fl)
        if ds:
            leaks.append((r["rec_id"], reason, r.get("signal_data_age_min")))
    return {"invariant": "INV8_signal_data_age",
            "status": "ALARM" if leaks else "OK", "severity": "critical",
            "detail": {"mode": "pregate_leak_detector", "n_checked": len(ledger),
                       "n_leaked": len(leaks), "leaks": leaks[:5]}}


def run_all(*, ledger, batch_pms, enriched, baseline, classify_fn, legs, enr_mod, cfg):
    ig = cfg.get("integrity", {})
    results = [
        inv1_d_share(ledger, enriched, tol=ig.get("inv1_d_share_tol", 0.02)),
        inv2_pct_consistency(ledger, tol=ig.get("inv2_pct_consistency_tol", 0.15)),
        inv3_clamp(batch_pms, enr_mod),
        inv4_funnel_control(baseline, classify_fn, legs),
        inv5_shadow_actual(enriched, tol=ig.get("inv5_shadow_actual_tol", 0.01)),
        inv6_ledger(ledger),
        inv7_reconstructed_risk(ledger, enriched, rel_tol=ig.get("inv7_reconstructed_risk_rel_tol", 0.02)),
        inv8_signal_age(ledger),   # per-lane floors from config/signal_freshness.yaml (mark 5)
    ]
    overall = "ALARM" if any(r["status"] == "ALARM" for r in results) else "OK"
    critical = any(r["status"] == "ALARM" and r["severity"] == "critical" for r in results)
    return {"overall": overall, "critical": critical, "results": results}


# ════════════════════════════════════════════════════════════════════════════
# ── INV9-INV12: LIVE (Track B / IBKR paper) invariants ──
# ════════════════════════════════════════════════════════════════════════════
# INV1-INV8 (above) reconcile DERIVED research-ledger figures against enriched
# offline fill data. INV9-INV12 extend the same alarm/hold pattern to the LIVE
# paper-trading loop: engine book vs broker book, order health, fill
# provenance, and rec_id lineage. Same {"invariant","status","severity",
# "detail"} shape. Deliberately NOT folded into run_all() (it runs on the
# offline research ledger and has no broker state) — see run_live() below.
# Added 2026-07-25, IBKR paper-execution migration
# (scratchpad/IBKR_MIGRATION_CONTRACT.md, TASK 3).
#
# Every function here is PURE: plain-dict inputs only (fixtures shaped like
# data/paper_positions.json, data/paper_orders.json, logs/observation/
# fills.jsonl rows, and PaperOrderClient.get_positions()/get_orders()), no
# broker client import, no I/O, no writes. The caller (future live-loop
# integration) assembles the inputs from disk + a live client and owns
# writing integrity_hold.flag / quarantine on ALARM, exactly as nightly.py
# does for run_all().
#
# NOTE (cross-task overlap, resolved): src/engine/reconcile_drift.py (a
# sibling TASK 2 file, NOT owned here) independently solves the same
# engine-vs-broker position cross-foot for DRIFT REPAIR purposes. inv9 below
# is a deliberately independent, self-contained implementation (no import of
# reconcile_drift) so this module has zero cross-task coupling with a file
# owned by a different parallel agent — it mirrors the same OCC-symbol /
# signed-qty / adopt_foreign conventions but answers a different question
# (ALARM-worthy or not, for the integrity tier) rather than reconcile_drift's
# richer (matched/drift/actions) repair-oriented output.

# Sign convention mirrors src/engine/reconcile_drift.py and
# src/engine/paper_trader.py: *_to_open / *_to_close on the "buy" side is a
# long leg (+1), "sell" side is a short leg (-1).
_LEG_SIGN = {
    "buy_to_open": 1, "buy_to_close": 1,
    "sell_to_open": -1, "sell_to_close": -1,
}

# The user's manual trades sharing the paper sandbox account (contract:
# "Manual positions are sacred"; config.yaml's real default is
# agent.adopt_foreign=false). These underlyings must NEVER trip a
# position-reconcile alarm, independent of the adopt_foreign flag's value at
# call time (defense in depth against a future config flip) — inv9's
# ignore-set parameter, overridable per-call.
MANUAL_UNDERLYINGS_DEFAULT = frozenset({"HON", "GOOGL", "DELL", "PLTR"})

# Mirrors src.data.tradier_orders.ORDER_TAG (not imported, same reason as
# above: zero cross-task import coupling). Every broker order this engine
# places carries this tag (contract, HARD SAFETY RAILS).
_ORDER_TAG_DEFAULT = "UNVALIDATED-PAPER-ONLY"

# fills.jsonl event types that represent something this engine itself placed
# and MUST therefore carry a rec_id.
_BOOKED_FILL_EVENTS = frozenset({"paper_fill", "paper_exit"})

# fills.jsonl event types emitted for broker-side orders/positions the engine
# never placed (crash orphans / the user's manual trades adopted defensively
# by reconcile_with_broker's order-cancel path). These carry NO rec_id BY
# DESIGN — see src/engine/paper_trader.py reconcile_with_broker() — and that
# is correct, not a violation, of INV12.
_ORPHAN_EVENT_TYPES = frozenset({"reconciled_orphan_order", "reconciled_orphan"})

_DEFAULT_REJECT_RATE_THRESHOLD = 0.10
_DEFAULT_REJECT_RATE_MIN_ORDERS = 5


def _occ_underlying(symbol: str) -> str:
    """underlying = symbol[:-15] (drops the YYMMDD+C/P+8-digit-strike OCC
    suffix), per the convention used throughout this migration (contract,
    paper_trader.reconcile_with_broker)."""
    return symbol[:-15] if symbol and len(symbol) > 15 else (symbol or "")


def _flatten_signed_positions(positions):
    """Reduce either position shape to {occ_symbol: signed_quantity}, summed
    across duplicate symbols:
      - broker shape (PaperOrderClient.get_positions()): flat {"symbol",
        "quantity" (already signed +long/-short), "cost_basis"} — no "legs".
      - engine shape (data/paper_positions.json rows): {"legs": [{"symbol",
        "action"}], "quantity": <contracts, unsigned>, ...} — signed here via
        _LEG_SIGN (an unrecognized/missing action defaults to +1, long).
    A flat dict with no "legs" key (broker rows, or pre-flattened fixtures) is
    treated as already-signed via its own "quantity" field.
    """
    out: dict = {}
    for p in positions or []:
        if p.get("legs"):
            qty = float(p.get("quantity") or 1)
            for leg in p["legs"]:
                sym = leg.get("symbol")
                if not sym:
                    continue
                sign = _LEG_SIGN.get(leg.get("action"), 1)
                out[sym] = out.get(sym, 0.0) + sign * qty
        else:
            sym = p.get("symbol")
            if not sym:
                continue
            out[sym] = out.get(sym, 0.0) + float(p.get("quantity") or 0)
    return out


# ── INV9: engine <-> broker position reconciliation ──
def inv9_position_reconcile(engine_positions, broker_positions, *,
                             adopt_foreign=False,
                             manual_underlyings=MANUAL_UNDERLYINGS_DEFAULT,
                             tol=1e-6):
    """ALARM if the engine's open book and the broker's reported positions
    don't cross-foot, matched on OCC symbol (underlying=symbol[:-15]), signed
    qty. Three violation classes, always alarm-worthy:
      - missing_at_broker: engine thinks it holds a leg; broker has none (the
        L20 sandbox-silently-deleted-the-position failure mode).
      - qty_mismatch: both sides know the symbol; signed quantities disagree
        (partial-fill / manual-adjust drift the engine hasn't reconciled).
      - missing_at_engine (only reachable when adopt_foreign=True): broker
        holds a leg the engine doesn't track, adopt_foreign says it SHOULD
        have been adopted already, and it wasn't — an adoption gap.
    NEVER alarm-worthy (manual-position protection, per contract "manual
    positions are sacred"):
      - any broker-only leg whose underlying is in manual_underlyings — the
        user's own manual trades sharing this paper sandbox account. Ignored
        UNCONDITIONALLY, regardless of adopt_foreign.
      - any other broker-only leg when adopt_foreign=False (the real
        config.yaml agent.adopt_foreign default) — intentionally left
        ungoverned by design (paper_trader.reconcile_with_broker), not drift.
    """
    eng = _flatten_signed_positions(engine_positions)
    brk = _flatten_signed_positions(broker_positions)
    manual = {u.upper() for u in (manual_underlyings or ())}

    missing_at_broker, missing_at_engine, qty_mismatch, ignored_foreign = [], [], [], []
    for sym in sorted(set(eng) | set(brk)):
        eq, bq = eng.get(sym), brk.get(sym)
        u = _occ_underlying(sym)
        if eq is not None and bq is None:
            missing_at_broker.append({"symbol": sym, "underlying": u, "engine_qty": eq})
        elif bq is not None and eq is None:
            if u.upper() in manual or not adopt_foreign:
                ignored_foreign.append({"symbol": sym, "underlying": u, "broker_qty": bq,
                                        "reason": "manual_position" if u.upper() in manual
                                                  else "adopt_foreign_false"})
            else:
                missing_at_engine.append({"symbol": sym, "underlying": u, "broker_qty": bq})
        elif eq is not None and bq is not None and abs(eq - bq) > tol:
            qty_mismatch.append({"symbol": sym, "underlying": u, "engine_qty": eq, "broker_qty": bq})

    alarms = missing_at_broker + missing_at_engine + qty_mismatch
    return {"invariant": "INV9_position_reconcile",
            "status": "ALARM" if alarms else "OK", "severity": "critical",
            "detail": {"missing_at_broker": missing_at_broker[:5], "n_missing_at_broker": len(missing_at_broker),
                       "missing_at_engine": missing_at_engine[:5], "n_missing_at_engine": len(missing_at_engine),
                       "qty_mismatch": qty_mismatch[:5], "n_qty_mismatch": len(qty_mismatch),
                       "ignored_foreign": ignored_foreign[:5], "n_ignored_foreign": len(ignored_foreign),
                       "adopt_foreign": adopt_foreign, "n_alarms": len(alarms)}}


# ── INV10: order-rejection rate ──
def inv10_order_reject_rate(fills, orders, *, threshold=None, critical_threshold=None,
                             order_tag=_ORDER_TAG_DEFAULT, min_orders=_DEFAULT_REJECT_RATE_MIN_ORDERS):
    """ALARM if this engine's order-rejection rate (rejected / total placed)
    exceeds `threshold` (cfg-configurable, default 0.10).

    `orders` is the broker's raw order list (PaperOrderClient.get_orders()
    shape: {"id","status","tag",...}, today's orders, ANY status) — in the
    shared paper sandbox this includes the user's own manual orders too, which
    must not dilute or pollute the metric. `fills` (fills.jsonl events) proves
    ownership for every order that reached a fill/expiry outcome, but a
    REJECTED order never produces a fill event (nothing filled) — so
    ownership there is proven the only other way available: the broker-side
    tag this engine stamps on every order it places (contract HARD SAFETY
    RAILS: "Every new broker order carries tag UNVALIDATED-PAPER-ONLY").
    denominator = orders with (id in fills' order_ids) OR (tag == order_tag).

    Severity is dynamic: `critical` once the rate exceeds critical_threshold
    (default 2x threshold — a broker/adapter-level failure, e.g. bad
    contract qualification or a stale buying-power check), else `warning`
    below that but still over threshold (elevated but not yet alarming-grade
    rejects, e.g. a few stale-quote misses). Below `min_orders` attributed
    orders the sample is too small to trust and never alarms.
    """
    threshold = _DEFAULT_REJECT_RATE_THRESHOLD if threshold is None else threshold
    critical_threshold = threshold * 2 if critical_threshold is None else critical_threshold

    fill_order_ids = {f.get("order_id") for f in (fills or []) if f.get("order_id") is not None}
    ours = [o for o in (orders or [])
            if o.get("id") in fill_order_ids or str(o.get("tag") or "") == order_tag]
    total = len(ours)
    rejected = [o for o in ours if o.get("status") == "rejected"]
    rate = (len(rejected) / total) if total else 0.0
    alarm = total >= min_orders and rate > threshold
    severity = "critical" if rate > critical_threshold else "warning"
    return {"invariant": "INV10_order_reject_rate",
            "status": "ALARM" if alarm else "OK", "severity": severity,
            "detail": {"rejected": len(rejected), "total_attributed": total,
                       "rate": round(rate, 4), "threshold": threshold,
                       "critical_threshold": critical_threshold, "min_orders": min_orders,
                       "rejected_order_ids": [o.get("id") for o in rejected][:5]}}


# ── INV11: every booked fill has a matching broker execution ──
def inv11_fill_has_execution(fills, broker_executions):
    """ALARM if any booked paper_fill/paper_exit event (logs/observation/
    fills.jsonl) lacks a matching broker execution record, matched on
    order_id. A booked fill with no broker-side execution is a phantom fill —
    either a broker/adapter normalization bug or a fabricated log entry;
    either way every downstream P&L number built on it is untrustworthy, so
    this is CRITICAL. `broker_executions` is the normalized IBKR execution
    list (ib_async Fill.execution, one row per execution, each carrying at
    least order_id — see IBKR PATTERN in the migration contract)."""
    exec_order_ids = {e.get("order_id") for e in (broker_executions or []) if e.get("order_id") is not None}
    missing = []
    n_checked = 0
    for f in (fills or []):
        if f.get("event") not in _BOOKED_FILL_EVENTS:
            continue
        n_checked += 1
        oid = f.get("order_id")
        if oid is None or oid not in exec_order_ids:
            missing.append({"rec_id": f.get("rec_id"), "event": f.get("event"), "order_id": oid})
    return {"invariant": "INV11_fill_has_execution",
            "status": "ALARM" if missing else "OK", "severity": "critical",
            "detail": {"missing": missing[:5], "n_missing": len(missing), "n_checked": n_checked}}


# ── INV12: every placed order / fill event carries a rec_id ──
def inv12_order_has_rec_id(pending_orders, fills, *, orphan_event_types=_ORPHAN_EVENT_TYPES):
    """ALARM if any placed order (data/paper_orders.json pending intents) or
    booked fill/lifecycle event (fills.jsonl) is missing rec_id — the thread
    that ties every broker action back to a recommendation. Excludes
    `_ORPHAN_EVENT_TYPES` (reconciled_orphan / reconciled_orphan_order):
    those are emitted for broker-side state this engine never placed
    (crash orphans / the user's manual trades) and carry no rec_id BY DESIGN
    — flagging them would be a false alarm on exactly the kind of foreign
    state the 'manual positions are sacred' rail says to leave alone."""
    missing = []
    for p in (pending_orders or []):
        if not p.get("rec_id"):
            missing.append({"source": "pending_order", "order_id": p.get("order_id"),
                            "intent_seq": p.get("intent_seq")})
    for f in (fills or []):
        if f.get("event") in orphan_event_types:
            continue
        if not f.get("rec_id"):
            missing.append({"source": "fill_event", "event": f.get("event"), "order_id": f.get("order_id")})
    return {"invariant": "INV12_order_has_rec_id",
            "status": "ALARM" if missing else "OK", "severity": "critical",
            "detail": {"missing": missing[:5], "n_missing": len(missing),
                       "n_checked": len(pending_orders or []) + len(fills or [])}}


def run_live(*, engine_positions, broker_positions, fills, pending_orders,
             broker_executions, orders, cfg):
    """LIVE-tier mirror of run_all(): builds INV9-INV12 into the same
    {"overall","critical","results"} shape. Never mixed into run_all() (no
    broker state there). Thresholds/ignore-sets read from cfg["integrity"]
    with the same defaults used when called with cfg={} — no config-file
    edits required to run this out of the box."""
    ig = cfg.get("integrity", {})
    results = [
        inv9_position_reconcile(
            engine_positions, broker_positions,
            adopt_foreign=ig.get("adopt_foreign", False),
            manual_underlyings=ig.get("manual_underlyings", MANUAL_UNDERLYINGS_DEFAULT),
        ),
        inv10_order_reject_rate(
            fills, orders,
            threshold=ig.get("inv10_order_reject_rate_threshold", _DEFAULT_REJECT_RATE_THRESHOLD),
            critical_threshold=ig.get("inv10_order_reject_rate_critical"),
            order_tag=ig.get("order_tag", _ORDER_TAG_DEFAULT),
            min_orders=ig.get("inv10_min_orders", _DEFAULT_REJECT_RATE_MIN_ORDERS),
        ),
        inv11_fill_has_execution(fills, broker_executions),
        inv12_order_has_rec_id(pending_orders, fills),
    ]
    overall = "ALARM" if any(r["status"] == "ALARM" for r in results) else "OK"
    critical = any(r["status"] == "ALARM" and r["severity"] == "critical" for r in results)
    return {"overall": overall, "critical": critical, "results": results}
