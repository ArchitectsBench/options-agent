"""Weekly review fact collector (2026-07-02) — deterministic input for the
Friday check-in agent (scripts/weekly_checkin.sh). Fault-isolated sections in
the ops_checks style; output is one JSON document on stdout.

Covers the trailing 7 days: paper trading results (quantity-aware, by sleeve
and exit reason, execution-test vs funnel split, exit give-back via the fixed
MFE marks), forward-acceptance progress, safety-system events (breaker, kill
switch, quote-outage flattens, earnings vetoes, escalations, fill guards),
throughput vs caps, log health, and labeler/calibration status.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WEEK_AGO = datetime.now() - timedelta(days=7)


def section(fn):
    try:
        return fn()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _week_lines(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = str(e.get("ts", ""))
        if ts and ts >= WEEK_AGO.isoformat(timespec="seconds"):
            out.append(e)
    return out


def sleeve_of(rec_id: str) -> str:
    """Lane attribution for a rec_id (2026-10-03 fix).

    The operator's standing requirement is that the funnel and the SCALP lane
    are reported as two separate streams; SCALP is never folded into the
    funnel. Before this, only the `-TEST-` marker was recognised and every
    `-SCALP-` rec_id defaulted to "funnel" (weekly-facts 2026-10-02 reported
    by_sleeve {"funnel": -218.0} for three SCALP round trips).
    """
    rid = rec_id or ""
    if "-TEST-" in rid:
        return rid.rsplit("-", 1)[-1]
    if "-SCALP-" in rid:
        return "scalp"
    return "funnel"


def trading_week(fills_path: Path | None = None):
    events = _week_lines(fills_path or ROOT / "logs" / "observation" / "fills.jsonl")
    exits = [e for e in events if e.get("event") == "paper_exit"]
    pnl = lambda e: e.get("pnl_total") if e.get("pnl_total") is not None else e.get("pnl_per_contract", 0)  # noqa: E731
    by_day, by_sleeve, by_reason = {}, {}, {}
    giveback = []
    for e in exits:
        day = e["ts"][:10]
        sleeve = sleeve_of(e.get("rec_id", ""))
        by_day[day] = round(by_day.get(day, 0) + pnl(e), 2)
        by_sleeve[sleeve] = round(by_sleeve.get(sleeve, 0) + pnl(e), 2)
        by_reason[e.get("exit_reason", "?")] = round(by_reason.get(e.get("exit_reason", "?"), 0) + pnl(e), 2)
        rt = e.get("round_trip") or {}
        if rt.get("mfe_value") is not None and rt.get("exit_net_credit") is not None:
            gb = round((rt["mfe_value"] - rt["exit_net_credit"]) * 100
                       * int(e.get("quantity", 1)), 2)
            giveback.append(gb)
    wins = sum(1 for e in exits if pnl(e) > 0)
    return {
        "round_trips": len(exits), "wins": wins,
        "total_pnl": round(sum(pnl(e) for e in exits), 2),
        "by_day": by_day, "by_sleeve": by_sleeve, "by_exit_reason": by_reason,
        "avg_giveback_vs_mfe": round(sum(giveback) / len(giveback), 2) if giveback else None,
        "worst_giveback": max(giveback) if giveback else None,
        "test_vs_funnel": {
            "test": sum(1 for e in exits if "-TEST-" in e.get("rec_id", "")),
            "scalp": sum(1 for e in exits if sleeve_of(e.get("rec_id", "")) == "scalp"),
            "funnel": sum(1 for e in exits if sleeve_of(e.get("rec_id", "")) == "funnel"),
        },
    }


def forward_clock():
    from scripts.forward_acceptance_check import evaluate
    r = evaluate(ROOT)
    return {"summary": r.get("overall", {}).get("summary"),
            "criteria_met": r.get("overall", {}).get("criteria_met"),
            "criteria_total": r.get("overall", {}).get("criteria_total"),
            "qualifying_trades": r.get("scope", {}).get("qualifying"),
            "total_pnl": r.get("total_pnl")}


def safety_events():
    events = _week_lines(ROOT / "logs" / "observation" / "fills.jsonl")
    counts = {}
    for e in events:
        k = e.get("event")
        counts[k] = counts.get(k, 0) + 1
    guards = sum(1 for e in events if e.get("fill_guard"))
    log = (ROOT / "logs" / "agent.log")
    week_cut = WEEK_AGO.strftime("%Y-%m-%d")
    lines = [l for l in (log.read_text().splitlines() if log.exists() else [])
             if l[:10] >= week_cut]
    count = lambda pat: sum(1 for l in lines if pat in l)  # noqa: E731
    return {
        "fill_events": counts,
        "fill_guards_fired": guards,
        # NOTE: counts before 2026-07-02 evening may include test-suite noise
        # (tests wrote to agent.log until the unittest logger guard shipped)
        "breaker_trips": count("DAILY-LOSS BREAKER TRIPPED"),
        "quote_outage_flattens": count("QUOTE-OUTAGE FLATTEN"),
        "kill_switch_engagements": count("KILL SWITCH ENGAGED:"),
        "stale_close_escalations": count("escalating stale close"),
        "earnings_vetoes": count("earnings_veto"),
        "intent_recoveries": count("resolve_intents: bound"),
    }


def throughput():
    # CORRECTION 2026-08-07: n_recs counts recommendation rows DIRECTLY, so it
    # was inflated by 80 pytest-fixture rows written to recommendations.jsonl
    # between 07-24 and 08-07 (+69% on the 7d window, +80 rows on 14-35d).
    # Those rows are now quarantined and O-20 makes the leak structurally
    # impossible. Reports generated BEFORE 2026-08-07 overstate this count;
    # every P&L / outcome figure in them was fills-derived and is unaffected.
    recs = _week_lines(ROOT / "logs" / "recommendations.jsonl")
    # recommendations use 'generated' not 'ts'
    recs_log = ROOT / "logs" / "recommendations.jsonl"
    n_recs = 0
    if recs_log.exists():
        for line in recs_log.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(r.get("generated", "")) >= WEEK_AGO.isoformat(timespec="seconds"):
                n_recs += 1
    budget = {}
    bfile = ROOT / "data" / "claude_budget.json"
    if bfile.exists():
        b = json.loads(bfile.read_text())
        today = f"{datetime.now():%Y-%m-%d}"
        budget = {k: v for k, v in b.items() if isinstance(v, (int, float)) and today in str(k)} or \
                 {"note": "see data/claude_budget.json"}
    return {"funnel_recs_this_week": n_recs, "daily_cap": 6, "claude_budget": budget}


def health():
    log = ROOT / "logs" / "agent.log"
    if not log.exists():
        return {"error": "no agent.log"}
    week_cut = WEEK_AGO.strftime("%Y-%m-%d")
    errors = warnings = cycles = 0
    for line in log.read_text().splitlines():
        if line[:10] >= week_cut:
            if " ERROR " in line:
                errors += 1
            elif " WARNING " in line:
                warnings += 1
            elif "cycle #" in line:
                cycles += 1
    state = json.loads((ROOT / "data" / "agent_state.json").read_text())
    return {"cycles_this_week": cycles, "errors": errors, "warnings": warnings,
            "agent_status": state.get("status"), "pid": state.get("pid"),
            "last_cycle": state.get("last_cycle")}


def execution_quality():
    from scripts.execution_quality import evaluate
    return evaluate(ROOT)


def labeler_status():
    hist = ROOT / "logs" / "research" / "calibration_history.jsonl"
    if not hist.exists():
        return {"error": "no calibration history"}
    last = json.loads(hist.read_text().splitlines()[-1])
    return {k: last.get(k) for k in ("ready_to_fit", "n", "days", "asof") if k in last}


if __name__ == "__main__":
    print(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": f"{WEEK_AGO:%Y-%m-%d} .. {datetime.now():%Y-%m-%d}",
        "gate_learning": section(lambda: __import__("gate_learning").readiness()),
        "trading_week": section(trading_week),
        "forward_clock": section(forward_clock),
        "safety_events": section(safety_events),
        "throughput": section(throughput),
        "health": section(health),
        "labeler": section(labeler_status),
        "execution_quality": section(execution_quality),
    }, indent=1, default=str))
