#!/usr/bin/env python3
"""SCALP-v2 mechanics — descriptive telemetry on the v1 -> v2 fill-mechanism
change, per session and cumulative, beside the PRE-COMMITTED predictions.

What changed v1 -> v2 (config/scalp_v2_forward_criteria.yaml, frozen
2026-09-12): ONLY the entry fill mechanism — immediate-or-cancel at
mid + 0.5*spread, a $1.00 net-debit floor, 10 fills / 30 attempts per day.
Signals, sizing and EXIT RULES are unchanged from v1. So the only things worth
measuring are the mechanism's own moving parts:

  * does the IOC fill at all (fill rate; frozen floor 25 %)
  * what it pays to fill (spreads over the at-fill mid)
  * where the attempt budget goes (per-ticker concentration, when the cap binds)
  * whether the trade population changed (exit-reason mix vs the predicted 82 %
    VWAP-invalidation share) and what it earns on the TRUTH ruler (NBBO mid),
    with booked P&L shown beside it and pending enrichment counted, never hidden

This is NOT a verdict. The frozen criteria forbid mid-stream calls; the gate
is scripts/scalp_forward_check.py --v2 at 60 sessions / 100 trades. Everything
here is UNVALIDATED — PAPER ONLY.

Run: .venv/bin/python scripts/scalp_v2_mechanics.py [--json] [--notify] [--write]
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scalp_forward_check as sfc  # noqa: E402  (window + scope rules, shared)

STAMP = "UNVALIDATED — PAPER ONLY"

# Pre-window predictions from the counterfactual replay of v2 on v1's own
# 1,588 attempts (docs/scalp_v2_counterfactual_20260912.md). Frozen here as the
# comparison column; the forward window is a test of these numbers.
PREDICTIONS = {
    "source": "docs/scalp_v2_counterfactual_20260912.md",
    "truth_per_trade_usd": -20.8,
    "win_rate": 0.28,
    "vwap_invalidation_exit_share": 0.82,
    "fill_rate_floor": 0.25,          # falsification clause, frozen criteria
}
# Each candidate is compared with ITS OWN pre-registered numbers (2026-09-27: the
# v3 window was being reported under v2's label and v2's predictions).
PREDICTIONS_BY_CANDIDATE = {
    "v2": PREDICTIONS,
    "v3": {"source": "docs/scalp_v3_prereg.md §5 (central estimate)",
           "truth_per_trade_usd": 1.0, "win_rate": None,
           "vwap_invalidation_exit_share": None, "fill_rate_floor": 0.60},
}


def _cand(criteria: dict) -> str:
    return str(criteria.get("_candidate") or "v2")


def _v2_events(ledger: Path, criteria: dict) -> list[dict]:
    start, end = criteria.get("window_start"), criteria.get("window_end")
    tag = criteria["scope"]["rec_id_tag"]
    out = []
    for line in ledger.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if not sfc._in_scope(e.get("rec_id", ""), tag):
            continue
        if not sfc._in_window(e.get("ts"), start, end):
            continue
        out.append(e)
    return out


def _is_v2_fill(e: dict) -> bool:
    # startswith: marketable_ioc and marketable_day are both v2-family fills;
    # v1's resting_mid is not, so historical numbers are unchanged.
    return (e.get("event") in ("paper_fill", "partial_fill_booked")
            and str(e.get("fill_mode", "")).startswith("marketable"))


def mechanics(ledger: Path, criteria: dict, truth_fn, today: str | None = None,
              reject_ids: set[int] | None = None) -> dict:
    """truth_fn(exit_row) -> (pnl or None, status) — the same market-truth
    scorer the gate uses (fac.market_truth_pnl bound to loaded enrichment).
    reject_ids: order ids IB refused (back-fill for rows logged before the
    broker_reject field existed); the recorded fill rate is unchanged, the
    market-reaching rate is reported beside it."""
    if reject_ids is None:
        reject_ids = sfc.broker_reject_order_ids()
    events = _v2_events(ledger, criteria)
    cap = int(criteria.get("rules_frozen", {}).get("max_attempts_per_day") or 30)
    by_day: dict[str, dict] = defaultdict(lambda: {
        "attempts": 0, "fills": 0, "broker_rejects": 0, "tickers": Counter(), "cap_bound_at": None,
        "spreads_over_mid": [], "round_trips": 0, "booked": 0.0,
        "truth": 0.0, "truth_scored": 0, "pending": 0, "exit_reasons": Counter()})
    v2_entries = {e["rec_id"]: e for e in events if _is_v2_fill(e) and e.get("rec_id")}

    for e in events:
        is_attempt = (e.get("event") == "entry_ioc_unfilled" or _is_v2_fill(e)
                      # marketable_day: a timeout-cancelled entry is an attempt
                      or (e.get("event") == "entry_expired"
                          and str(e.get("fill_mode", "")).startswith("marketable")))
        is_v2_exit = e.get("event") == "paper_exit" and e.get("rec_id") in v2_entries
        if not (is_attempt or is_v2_exit):
            continue   # a v1-mode fill in the lane is not a v2 session, so no day row
        day = str(e.get("ts", ""))[:10]
        d = by_day[day]
        if is_attempt:
            d["attempts"] += 1
            d["tickers"][e.get("ticker", "?")] += 1
            if d["attempts"] == cap and d["cap_bound_at"] is None:
                d["cap_bound_at"] = str(e.get("ts", ""))[11:16]
            if e.get("event") == "entry_ioc_unfilled" and sfc.is_broker_reject(e, reject_ids):
                d["broker_rejects"] += 1
            if _is_v2_fill(e):
                d["fills"] += 1
                cap_vs_mid, spread = e.get("capture_vs_mid"), e.get("spread_at_fill")
                if cap_vs_mid is not None and spread:
                    d["spreads_over_mid"].append(-float(cap_vs_mid) / float(spread))
        else:
            d["round_trips"] += 1
            d["booked"] += float(e.get("pnl_total") if e.get("pnl_total") is not None
                                 else (e.get("pnl_per_contract") or 0))
            d["exit_reasons"][str(e.get("exit_reason") or e.get("close_reason") or "?")] += 1
            pnl, _status = truth_fn(e)
            if pnl is None:
                d["pending"] += 1
            else:
                d["truth"] += float(pnl)
                d["truth_scored"] += 1
                d.setdefault("truth_wins", 0)
                if pnl > 0:
                    d["truth_wins"] += 1

    days = []
    for day in sorted(by_day):
        d = by_day[day]
        top = d["tickers"].most_common(1)[0] if d["tickers"] else (None, 0)
        mkt = d["attempts"] - d["broker_rejects"]
        days.append({
            "date": day, "attempts": d["attempts"], "fills": d["fills"],
            "fill_rate": round(d["fills"] / d["attempts"], 3) if d["attempts"] else None,
            "broker_rejects": d["broker_rejects"], "market_attempts": mkt,
            "fill_rate_market": round(d["fills"] / mkt, 3) if mkt else None,
            "top_ticker": top[0], "top_ticker_attempts": top[1],
            "top_ticker_share": round(top[1] / d["attempts"], 2) if d["attempts"] else None,
            "cap_bound_at": d["cap_bound_at"],
            "spreads_over_mid_mean": (round(sum(d["spreads_over_mid"]) / len(d["spreads_over_mid"]), 2)
                                      if d["spreads_over_mid"] else None),
            "round_trips": d["round_trips"], "booked": round(d["booked"], 2),
            "truth": round(d["truth"], 2), "truth_scored": d["truth_scored"],
            "truth_wins": d.get("truth_wins", 0),
            "pending_enrichment": d["pending"],
            "exit_reasons": dict(d["exit_reasons"]),
        })

    att = sum(x["attempts"] for x in days)
    fills = sum(x["fills"] for x in days)
    rts = sum(x["round_trips"] for x in days)
    scored = sum(x["truth_scored"] for x in days)
    wins = sum(x["truth_wins"] for x in days)
    tick = Counter()
    for d in by_day.values():
        tick.update(d["tickers"])
    som = [s for d in by_day.values() for s in d["spreads_over_mid"]]
    reasons = Counter()
    for d in by_day.values():
        reasons.update(d["exit_reasons"])
    vwap = sum(n for r, n in reasons.items() if "vwap" in r.lower())
    pending = sum(x["pending_enrichment"] for x in days)
    # a trade still unscored after the night that should have enriched it
    stale = sum(x["pending_enrichment"] for x in days if today and x["date"] < today)
    booked = sum(x["booked"] for x in days)
    truth = sum(x["truth"] for x in days)
    rej = sum(x["broker_rejects"] for x in days)
    cum = {
        "sessions": len(days), "attempts": att, "fills": fills,
        "fill_rate": round(fills / att, 3) if att else None,
        "fill_rate_floor": PREDICTIONS_BY_CANDIDATE.get(_cand(criteria), PREDICTIONS)["fill_rate_floor"],
        "broker_rejects": rej, "market_attempts": att - rej,
        "fill_rate_market": round(fills / (att - rej), 3) if att - rej else None,
        "attempts_by_ticker": dict(tick.most_common(5)),
        "top_ticker_share": round(tick.most_common(1)[0][1] / att, 2) if att else None,
        "cap_bound_sessions": sum(1 for x in days if x["cap_bound_at"]),
        "spreads_over_mid_mean": round(sum(som) / len(som), 2) if som else None,
        "round_trips": rts,
        "exit_reasons": dict(reasons),
        "vwap_invalidation_exit_share": round(vwap / rts, 2) if rts else None,
        "booked_total": round(booked, 2),
        "booked_per_trade": round(booked / rts, 2) if rts else None,
        "truth_scored": scored, "truth_total": round(truth, 2),
        "truth_per_trade": round(truth / scored, 2) if scored else None,
        "truth_win_rate": round(wins / scored, 2) if scored else None,
        "pending_enrichment": pending, "stale_pending_enrichment": stale,
    }
    return {"stamp": STAMP, "as_of": today or datetime.now().strftime("%Y-%m-%d"),
            "window_start": criteria.get("window_start"), "candidate": _cand(criteria),
            "unchanged_from_v1": ["entry_signals", "exit_rules", "sizing"],
            "predictions": PREDICTIONS_BY_CANDIDATE.get(_cand(criteria), PREDICTIONS),
            "cumulative": cum, "days": days}


def _pct(x):
    return "—" if x is None else f"{x:.0%}"


def summary_line(m: dict) -> str:
    c = m["cumulative"]
    d = m["days"][-1] if m["days"] else None
    # `days[-1]` is the latest day with ACTIVITY, not necessarily today. Saying
    # "today N att" on a day the ledger never moved reported 2026-10-08's numbers
    # as today's for ~4 weeks (queue item 2026-11-07 weekend, fixed post_close
    # 11-04). When the latest active day is not as_of, name the date instead.
    as_of = m.get("as_of")
    if d is None:
        today = f"no {m.get('candidate', 'v2')} activity"
    elif as_of and d["date"] != as_of:
        today = (f"no activity today ({as_of}); last active {d['date']} "
                 f"{d['attempts']} att / {d['fills']} fill ({_pct(d['fill_rate'])})")
    else:
        today = (f"today {d['attempts']} att / {d['fills']} fill ({_pct(d['fill_rate'])})"
                 + (f", cap bound {d['cap_bound_at']}" if d["cap_bound_at"] else "")
                 + (f", top {d['top_ticker']} {d['top_ticker_attempts']}" if d["top_ticker"] else ""))
    P = m.get("predictions", PREDICTIONS); cand = m.get("candidate", "v2")
    truth = (f"truth {c['truth_per_trade']:+.0f}$/trade n={c['truth_scored']} (pred {P['truth_per_trade_usd']:+.1f})"
             if c["truth_scored"] else f"truth pending {c['pending_enrichment']}")
    rej = (f"; {c['broker_rejects']} broker rejects -> {_pct(c['fill_rate_market'])} "
           f"({c['fills']}/{c['market_attempts']}) on orders that reached the market"
           if c.get("broker_rejects") else "")
    return (f"SCALP-{cand} s{c['sessions']}: {today} | cum fill {_pct(c['fill_rate'])} "
            f"({c['fills']}/{c['attempts']}, floor {_pct(c['fill_rate_floor'])}{rej}) | {truth} | "
            f"booked {c['booked_total']:+.0f}$ | vwap-exit {_pct(c['vwap_invalidation_exit_share'])} (pred {_pct(P.get('vwap_invalidation_exit_share'))})")


def render_md(m: dict) -> str:
    c, p = m["cumulative"], m["predictions"]
    cand = m.get("candidate", "v2")
    L = [f"# SCALP-{cand} mechanics — as of {m['as_of']}", "",
         f"**{m['stamp']}.** Descriptive telemetry on the v1→v2 fill-mechanism change; "
         f"NOT a verdict (gate: `scalp_forward_check.py --{cand}` at 60 sessions / 100 trades). "
         f"Unchanged from v1: {', '.join(m['unchanged_from_v1'])}. Window start {m['window_start']}.", "",
         "## Cumulative vs pre-committed predictions", "",
         "| measure | observed | prediction / floor |", "|---|---|---|",
         f"| IOC fill rate (as recorded — the frozen computation) | {_pct(c['fill_rate'])} ({c['fills']}/{c['attempts']}) | floor {_pct(p['fill_rate_floor'])} (falsified below) |",
         f"| of which broker rejects (IBKR refuses IOC on combos, never reached the market) | {c['broker_rejects']} | — |",
         f"| IOC fill rate on orders that reached the market (disclosure) | {_pct(c['fill_rate_market'])} ({c['fills']}/{c['market_attempts']}) | — |",
         f"| truth P&L / trade | {c['truth_per_trade'] if c['truth_per_trade'] is not None else '—'} $ (n={c['truth_scored']}) | {p['truth_per_trade_usd']} $ |",
         f"| truth win rate | {_pct(c['truth_win_rate'])} | {_pct(p['win_rate'])} |",
         f"| VWAP-invalidation exit share | {_pct(c['vwap_invalidation_exit_share'])} of {c['round_trips']} | {_pct(p['vwap_invalidation_exit_share'])} |",
         f"| booked P&L (not the ruler) | {c['booked_total']:+.2f} $ ({c['booked_per_trade'] if c['booked_per_trade'] is not None else '—'} /trade) | — |",
         f"| spreads paid over at-fill mid | {c['spreads_over_mid_mean'] if c['spreads_over_mid_mean'] is not None else '—'} | 0.5 sent |",
         f"| attempt concentration | top ticker {_pct(c['top_ticker_share'])} of attempts: {c['attempts_by_ticker']} | no per-ticker cap exists |",
         f"| attempt cap bound | {c['cap_bound_sessions']} of {c['sessions']} sessions | — |",
         f"| pending enrichment | {c['pending_enrichment']} (stale: {c['stale_pending_enrichment']}) | 0 stale |",
         "", "## Per session", "",
         "| date | attempts | fills | rate | top ticker | cap bound | spreads/mid | round trips | booked | truth (n) | pending | exits |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in m["days"]:
        L.append(f"| {d['date']} | {d['attempts']} | {d['fills']} | {_pct(d['fill_rate'])} | "
                 f"{d['top_ticker'] or '—'} {d['top_ticker_attempts']} | {d['cap_bound_at'] or '—'} | "
                 f"{d['spreads_over_mid_mean'] if d['spreads_over_mid_mean'] is not None else '—'} | "
                 f"{d['round_trips']} | {d['booked']:+.0f} | {d['truth']:+.0f} ({d['truth_scored']}) | "
                 f"{d['pending_enrichment']} | {d['exit_reasons'] or '—'} |")
    L += ["", f"Predictions source: `{p['source']}`. Truth = ThetaData NBBO mid (enriched nightly); "
          "booked = simulator fills, shown only so the two rulers are visible together."]
    return "\n".join(L) + "\n"


def main() -> int:
    import forward_acceptance_check as fac
    c = sfc.load_active_criteria()          # follows the LIVE window: v3 since 2026-09-25
    enrichment = fac.load_enrichment(ROOT) if c.get("scoring") == "market_mid_v1" else {}
    today = datetime.now().strftime("%Y-%m-%d")
    m = mechanics(ROOT / c["scope"]["ledger"], c,
                  lambda e: fac.market_truth_pnl(e, enrichment), today=today,
                  reject_ids=sfc.broker_reject_order_ids())
    if "--write" in sys.argv:
        out = ROOT / "logs" / "ops"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"scalp_v2_mechanics-{today}.json").write_text(json.dumps(m, indent=1, default=str))
        (out / f"scalp_v2_mechanics-{today}.md").write_text(render_md(m))   # file name kept: the nightly + digest read it
    if "--json" in sys.argv:
        print(json.dumps(m, indent=1, default=str))
    else:
        print(render_md(m))
        print(summary_line(m))
    if "--notify" in sys.argv:
        from src.engine.notify import send
        send(f"SCALP-{m.get('candidate', 'v2')} mechanics", summary_line(m) + "\n" + STAMP, priority="default", tags="bar_chart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
