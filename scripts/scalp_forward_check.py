#!/usr/bin/env python3
"""SCALP-candidate forward tracker — progress vs config/scalp_forward_criteria.yaml.

Mirrors scripts/forward_acceptance_check.py (imports its math so the two gates
can never drift apart): Sharpe on daily sleeve P&L, profit factor, max drawdown,
top-2 concentration, adverse-regime coverage, plus the scalp-specific
per-structure split (0dte / debit_spread / premium_selling / fallback).

Evidence = closed '-TEST-' round trips ENTERED on/after window_start. While the
criteria are DRAFT (window_start null), runs in DRY-RUN mode on the motivating
sample — machinery demo only, explicitly NOT evidence.

Artifact-fill filter (2026-07-11 amendment): round trips whose ENTRY fill lies
outside the contemporaneous quoted band (bid/ask +- a detection-lag tolerance,
parameters in config) are sandbox artifacts and are EXCLUDED from evidence;
entries with no fill telemetry are excluded as unverifiable (fail-closed).
Exclusion counts are always reported — silent drops are forbidden.

Run: .venv/bin/python scripts/scalp_forward_check.py [--json]
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))          # for src.engine.paper_trader (lane classifier)

import forward_acceptance_check as fac  # noqa: E402  (shared math)


def load_criteria(v2: bool = False, v3: bool = False) -> dict:
    """v1 (default), the SCALP-v2 candidate (--v2) or SCALP-v3 (--v3). Separate
    files, separate windows, separate evidence — never evaluated as one."""
    if v3:
        return yaml.safe_load((ROOT / "config" / "scalp_v3_forward_criteria.yaml").read_text())["scalp_v3_forward"]
    if v2:
        return yaml.safe_load((ROOT / "config" / "scalp_v2_forward_criteria.yaml").read_text())["scalp_v2_forward"]
    return yaml.safe_load((ROOT / "config" / "scalp_forward_criteria.yaml").read_text())["scalp_forward"]



def active_candidate() -> str:
    """Which SCALP candidate is LIVE: 'v3' if its criteria file is FROZEN with no
    window_end, else 'v2', else 'v1'. Nightly callers follow this so a new
    candidate's window is never invisible to the report (the 09-14 lesson)."""
    for name, flag in (("v3", dict(v3=True)), ("v2", dict(v2=True))):
        try:
            c = load_criteria(**flag)
        except FileNotFoundError:
            continue
        if c.get("status") == "FROZEN" and not c.get("window_end"):
            return name
    return "v1"


def load_active_criteria() -> dict:
    a = active_candidate()
    c = load_criteria(v2=(a == "v2"), v3=(a == "v3"))
    c["_candidate"] = a          # lets reports label and compare against the RIGHT prereg
    return c


def _in_window(ts, start, end) -> bool:
    """tz-aware [start, end] test; naive ledger stamps are America/Los_Angeles."""
    if ts is None:
        return False
    import pandas as pd
    t = pd.Timestamp(str(ts))
    t = t.tz_localize("America/Los_Angeles") if t.tzinfo is None else t
    if start and t < pd.Timestamp(start):
        return False
    if end and t > pd.Timestamp(end):
        return False
    return True


def ioc_fill_rate(criteria: dict, trades: list[dict],
                  reject_ids: set[int] | None = None) -> dict | None:
    """SCALP-v2 only: fills / (fills + entry_ioc_unfilled) inside the window.
    The denominator is the whole point — an IOC that never fills is the
    hypothesis failing, not a quiet day."""
    # startswith: a marketable_day candidate's criteria file must still GET a
    # fill-rate check. Matching "marketable_ioc" exactly would return None and
    # the criterion would silently vanish from that candidate's gate.
    if not str((criteria.get("scope") or {}).get("fill_mode", "")).startswith("marketable"):
        return None
    if reject_ids is None:
        reject_ids = broker_reject_order_ids()
    ledger = ROOT / criteria["scope"]["ledger"]
    filled = unfilled = rejected = 0
    for line in ledger.read_text().splitlines():
        e = json.loads(line)
        # tz-aware window cut (naive PT vs aware UTC in the same ledger — the
        # 7-hour bug class); window_end closes a retired candidate's count
        if not _in_window(e.get("ts"), criteria.get("window_start"), criteria.get("window_end")):
            continue
        # a PARTIAL IOC executed contracts: it is a fill in the numerator.
        # startswith("marketable") so marketable_day counts here too — v1's
        # resting_mid is still excluded, so every historical number is unchanged.
        if e.get("event") in ("paper_fill", "partial_fill_booked") \
                and str(e.get("fill_mode", "")).startswith("marketable"):
            filled += 1
        elif e.get("event") == "entry_ioc_unfilled":
            unfilled += 1
        elif (e.get("event") == "entry_expired"
              and str(e.get("fill_mode", "")).startswith("marketable")):
            # marketable_day (2026-09-16): an entry cancelled at the timeout is a
            # completed ATTEMPT that did not fill — the same role entry_ioc_unfilled
            # plays for IOC. Legacy v1 expiries carry no fill_mode and are NOT
            # counted, so every number computed before today is unchanged.
            unfilled += 1
        else:
            continue
        if e.get("event") == "entry_ioc_unfilled" and is_broker_reject(e, reject_ids):
            rejected += 1
    n = filled + unfilled
    # DISCLOSURE ONLY (2026-09-15): `rate` above is the frozen computation and
    # still decides pass/fail. `market_rate` excludes broker refusals (IBKR 201,
    # IOC not accepted on SMART combos) so the operator sees both numbers.
    m = n - rejected
    return {"filled": filled, "unfilled": unfilled, "broker_rejects": rejected,
            "rate": (filled / n) if n else None,
            "market_rate": (filled / m) if m else None,
            "need": criteria.get("min_fill_rate", 0.25)}


# IB's order-scoped error lines, as the engine's stdout records them
_REJECT_CACHE: dict = {}
_REJECT_LINE = re.compile(r"Error (\d+), reqId (\d+): Order rejected")


def broker_reject_order_ids(log_path: Path | None = None) -> set[int]:
    """Order ids IB REFUSED (error 201 etc.), read from the engine's stdout.
    Back-fills ledger rows written before entry_ioc_unfilled carried a
    broker_reject field (before 2026-09-15). Never raises."""
    p = log_path or ROOT / "logs" / "agent_stdout.log"
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        if _REJECT_CACHE.get("key") != key:     # the log is ~300k lines: parse once per change
            _REJECT_CACHE.update(key=key, ids={int(m.group(2)) for m in
                                              _REJECT_LINE.finditer(p.read_text(errors="replace"))})
        return set(_REJECT_CACHE["ids"])
    except OSError:
        return set()


def is_broker_reject(e: dict, reject_ids: set[int]) -> bool:
    # the submit path has stamped terminal_status since v2 began (202 cancels
    # were remapped to 'canceled' before the window opened)
    if e.get("broker_reject") or e.get("terminal_status") == "rejected":
        return True
    try:
        return int(e.get("order_id")) in reject_ids
    except (TypeError, ValueError):
        return False


def mode_map() -> dict[str, str]:
    """rec_id -> EXECUTION-TEST | EXECUTION-TEST-FALLBACK (from observation logs)."""
    m = {}
    for f in sorted(glob.glob(str(ROOT / "logs/observation/2026-*.jsonl"))):
        for line in Path(f).read_text().splitlines():
            for t in (json.loads(line).get("paper_execution_tests") or []):
                if t.get("rec_id"):
                    m[t["rec_id"]] = t.get("mode", "EXECUTION-TEST")
    return m


def structure_of(rec_id: str, modes: dict) -> str:
    if modes.get(rec_id) == "EXECUTION-TEST-FALLBACK":
        return "fallback"
    return rec_id.rsplit("-", 1)[-1]  # strategy suffix in the rec_id


def _fill_verdict(entry: dict | None, cfg: dict) -> str:
    """'ok' | 'artifact' | 'unverifiable' for an entry-fill record.

    mid_at_fill is snapshotted at fill DETECTION (up to one poll cycle after
    the broker fill), so the quoted band gets a drift tolerance; only fills
    the tolerance cannot explain (sandbox fantasy fills) are artifacts."""
    if not entry or entry.get("mid_at_fill") is None:
        return "unverifiable"
    mid = entry["mid_at_fill"]
    spread = entry.get("spread_at_fill") or 0.0
    tol = max(cfg["tolerance_abs"], cfg["tolerance_frac_of_mid"] * abs(mid))
    lo, hi = mid - spread / 2 - tol, mid + spread / 2 + tol
    return "ok" if lo <= entry["fill_price"] <= hi else "artifact"


def _in_scope(rec_id: str, tag: str) -> bool:
    """Is this rec_id in the SCALP lane the frozen scope names?

    NOT AN AMENDMENT (2026-08-10). The frozen criteria say
    `rec_id_tag: "-TEST-"`, and config/scalp_forward_criteria.yaml is NOT
    edited here — the file is untouched, as the freeze requires. What changed
    underneath it is the MARKER, not the population: the 2026-08-08
    forward-only rename mints '-SCALP-' for the very same lane. Matching the
    literal string therefore silently dropped every round trip from 2026-08-10
    onward (10 trades, +$476.03 on the first day) and the frozen gate would
    have accrued zero evidence forever while appearing to work.

    is_scalp() accepts BOTH eras, so this preserves the frozen scope's meaning
    rather than widening it. If the configured tag is ever something other than
    a SCALP marker, fall back to the literal test so the freeze still governs.
    """
    from src.engine.paper_trader import SCALP_MARKER, LEGACY_SCALP_MARKER, is_scalp
    if tag in (SCALP_MARKER, LEGACY_SCALP_MARKER):
        return is_scalp(rec_id)
    return tag in str(rec_id or "")


def qualifying(criteria: dict) -> tuple[list[dict], dict]:
    """Returns (qualifying trades, exclusions). Exclusions carry per-reason
    counts and the excluded rows themselves so no drop is ever silent."""
    tag = criteria["scope"]["rec_id_tag"]
    start = criteria.get("window_start")
    filt = criteria.get("artifact_fill_filter") or {}
    modes = mode_map()
    out = []
    excluded = {"artifact": [], "unverifiable": [],
                "pending_enrichment": [], "no_market_data": [],
                "wrong_fill_mode": [],   # SCALP-v2: entries filled under another mode
                "outside_window": []}    # before window_start / after window_end — REPORTED, never vanish
    scope_mode = (criteria.get("scope") or {}).get("fill_mode")   # None => any
    enrichment = {}
    if criteria.get("scoring") == "market_mid_v1":   # L21, 2026-07-15 amendment
        enrichment = fac.load_enrichment(ROOT)
    ledger = ROOT / criteria["scope"]["ledger"]
    entry_fills = {}
    if filt.get("enabled") or scope_mode:
        for line in ledger.read_text().splitlines():
            e = json.loads(line)
            if e.get("event") in ("paper_fill", "partial_fill_booked") and e.get("rec_id"):
                entry_fills.setdefault(e["rec_id"], e)   # a partial IOC is still a v2 entry
    for line in ledger.read_text().splitlines():
        e = json.loads(line)
        if e.get("event") != "paper_exit" or not _in_scope(e.get("rec_id", ""), tag):
            continue
        rt = e.get("round_trip") or {}
        entered = rt.get("entry_filled_at") or e.get("ts")
        booked = e.get("pnl_total")
        if booked is None:
            booked = e.get("pnl_per_contract") or 0
        row = {"rec_id": e["rec_id"], "ticker": e.get("ticker"),
               "date": e.get("ts", "")[:10], "pnl": float(booked),
               "pnl_booked": float(booked),
               "structure": structure_of(e["rec_id"], modes)}
        if not _in_window(entered, start, criteria.get("window_end")):
            # tz-aware; a CLOSED candidate (window_end) accrues nothing after it —
            # but the drop is REPORTED, never silent
            excluded["outside_window"].append(row)
            continue
        if scope_mode:
            # SCALP-v2 (2026-09-12): the fill MECHANISM is the candidate. A trade
            # entered under v1's resting-mid limit is not v2 evidence even if it
            # carries the same rec_id marker — and vice versa. Never blend.
            ef = entry_fills.get(e["rec_id"]) or {}
            if ef.get("fill_mode", "resting_mid") != scope_mode:
                excluded["wrong_fill_mode"].append(row)
                continue
        if filt.get("enabled"):
            verdict = _fill_verdict(entry_fills.get(e["rec_id"]), filt)
            if verdict != "ok":
                excluded[verdict].append(row)
                continue
        if criteria.get("scoring") == "market_mid_v1":
            # L21: score market-mid truth, never the simulator's fills
            pnl, status = fac.market_truth_pnl(e, enrichment)
            if pnl is None:
                excluded[status].append(row)
                continue
            row["pnl"] = pnl
        cm = criteria.get("cost_model") or {}
        if cm.get("per_contract_round_trip_usd") is not None:
            # SCALP-v3 (2026-09-25): the gate is truth AFTER real costs. Quantity
            # comes from the exit row; a row without it cannot be costed ->
            # excluded as unverifiable (fail-closed, reported, never silent).
            qty = e.get("quantity") or rt.get("quantity")
            if not qty:
                excluded["unverifiable"].append(row)
                continue
            row["cost"] = float(qty) * float(cm["per_contract_round_trip_usd"])
            row["pnl"] = row["pnl"] - row["cost"]
        out.append(row)
    return out, excluded


def evaluate(v2: bool = False, v3: bool = False) -> dict:
    c = load_criteria(v2=v2, v3=v3)
    frozen = c.get("status") == "FROZEN" and c.get("window_start")
    trades, excluded = qualifying(c)
    pnls = [t["pnl"] for t in trades]
    days = sorted({t["date"] for t in trades})
    import pandas as pd
    daily = pd.Series(pnls, index=pd.to_datetime([t["date"] for t in trades])).groupby(level=0).sum() \
        if trades else pd.Series(dtype=float)
    sharpe = fac.annualized_sharpe(daily) if len(daily) > 1 else None
    pf = fac.profit_factor(pnls)
    dd = fac.max_drawdown(pnls)
    conc = fac.concentration_pnl(pnls)
    by_struct = {}
    for t in trades:
        by_struct.setdefault(t["structure"], []).append(t["pnl"])
    adverse = fac.adverse_regime(ROOT, days[0], days[-1]) if days else {"met": False, "adverse_weeks": 0}
    span = fac.trading_day_span(days[0], days[-1]) if days else 0

    checks = {
        "trades": {"value": len(trades), "need": c["min_trades"], "pass": len(trades) >= c["min_trades"]},
        "trading_days": {"value": span, "need": c["min_trading_days"], "pass": span >= c["min_trading_days"]},
        "adverse_week": {"value": adverse.get("adverse_weeks"), "need": ">=1", "pass": bool(adverse.get("met"))},
        "sharpe_ann": {"value": round(sharpe, 2) if sharpe is not None else None,
                       "need": c["min_forward_sharpe_ann"],
                       "pass": sharpe is not None and sharpe >= c["min_forward_sharpe_ann"]},
        "profit_factor": {"value": round(pf, 2) if pf is not None else None,
                          "need": c["min_profit_factor"],
                          "pass": pf is not None and pf >= c["min_profit_factor"]},
        "max_drawdown_usd": {"value": round(dd), "need": f"<= {c['max_drawdown_usd']}",
                             "pass": abs(dd) <= c["max_drawdown_usd"]},
        "per_structure_non_negative": {
            "value": {k: round(sum(v)) for k, v in sorted(by_struct.items())},
            "pass": all(sum(v) >= 0 for v in by_struct.values()) if by_struct else False},
        "survives_top2_removal": {"value": round(conc), "pass": conc > 0},
    }
    if c.get("cost_model"):
        mean = (sum(pnls) / len(pnls)) if pnls else None
        checks["truth_per_trade_after_costs"] = {
            "value": round(mean, 2) if mean is not None else None, "need": "> 0",
            "pass": mean is not None and mean > 0}
    fut = c.get("futility_stop") or {}
    if fut:
        k = int(fut["at_trades"])
        head = pnls[:k]   # qualifying() preserves ledger (chronological) order
        tripped = len(head) >= k and (sum(head) / k) < float(fut["min_truth_per_trade_usd"])
        checks["futility_stop"] = {
            "value": (f"{sum(head)/k:+.2f}/trade over first {k}" if len(head) >= k
                      else f"not reached ({len(head)}/{k})"),
            "need": f">= {fut['min_truth_per_trade_usd']} at {k} trades",
            "pass": not tripped,
            "tripped": tripped}
    fr = ioc_fill_rate(c, trades)
    if fr is not None:
        mkt = (f"; {fr['broker_rejects']} broker rejects (IOC refused on combos) — "
               f"{fr['market_rate']:.0%} on orders that reached the market"
               if fr["broker_rejects"] and fr["market_rate"] is not None else
               f"; all {fr['broker_rejects']} were broker rejects" if fr["broker_rejects"] else "")
        checks["ioc_fill_rate"] = {"value": (f"{fr['rate']:.0%} ({fr['filled']}/{fr['filled']+fr['unfilled']}){mkt}"
                                             if fr["rate"] is not None else "no IOC attempts yet"),
                                   "need": f">= {fr['need']:.0%}",
                                   "pass": fr["rate"] is not None and fr["rate"] >= fr["need"]}
    return {"status": c.get("status"), "window_start": c.get("window_start"),
            "window_end": c.get("window_end"),
            "evidence": bool(frozen), "n_trades": len(trades),
            "total_pnl": round(sum(pnls)),
            # booked beside truth: the two rulers must be visible together
            "total_pnl_booked": round(sum(t.get("pnl_booked", 0.0) for t in trades)),
            "checks": checks,
            "excluded": {k: {"count": len(v),
                             "pnl_not_counted": round(sum(r["pnl"] for r in v)),
                             "rec_ids": [r["rec_id"] for r in v]}
                         for k, v in excluded.items()},
            "all_pass": all(v["pass"] for v in checks.values())}


def main() -> int:
    r = evaluate(v2="--v2" in sys.argv, v3="--v3" in sys.argv)
    if "--json" in sys.argv:
        print(json.dumps(r, indent=1, default=str))
        return 0
    hdr = ("SCALP-v3 FORWARD TRACKER" if "--v3" in sys.argv else
           "SCALP-v2 FORWARD TRACKER" if "--v2" in sys.argv else "SCALP FORWARD TRACKER")
    if r.get("status") in (None, "DRAFT"):
        # only a DRAFT is a dry run; a CLOSED_* window is closed evidence, not a draft
        hdr += "  [DRY RUN — criteria DRAFT, window not frozen; this is the MOTIVATING sample, NOT evidence]"
    print(hdr)
    print(f"  status={r['status']}  window_start={r['window_start']}  "
          f"n={r['n_trades']}  total {r['total_pnl']:+}$")
    for reason, ex in r["excluded"].items():
        if ex["count"]:
            ids = ex["rec_ids"]
            shown = ", ".join(ids[:8]) + (f", … +{len(ids)-8} more" if len(ids) > 8 else "")
            print(f"  EXCLUDED ({reason}): {ex['count']} round trips, "
                  f"{ex['pnl_not_counted']:+}$ not counted — {shown}")
    print()
    for name, v in r["checks"].items():
        mark = "PASS" if v["pass"] else "----"
        print(f"  [{mark}] {name:28} {v['value']}  (need {v.get('need','')})")
    print(f"\n  => {'ALL CRITERIA MET' if r['all_pass'] else 'not met (expected while accruing)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
