"""Forward-acceptance progress evaluator — reports, never gates (exit code 0 always).

Evaluates progress against the FROZEN criteria in config/forward_acceptance.yaml:

  Scope (qualifying trade set):
    - paper_exit events from the current logs/observation/fills.jsonl
      (post-2026-06-25 reset ledger; the .archived file is never read)
    - rec_ids containing "-TEST-" (execution-test fills) excluded
    - only recs Claude-approved in logs/recommendations.jsonl
      (validation_chain.claude.status == "approved" and verdict.approve true)

  Criteria: min_trades, min_trading_days, annualized Sharpe of daily P&L,
  profit factor, max drawdown vs risk capital, per-sleeve non-negativity,
  concentration (P&L after removing top 2 winners), adverse-regime coverage
  (>=1 calendar week in the sample span containing a day where SPY drew down
  >=3% from its 20-day high OR VIX closed > 25, from the daily parquets).

Zero qualifying trades is a valid, honest result ("0/100, not evaluable yet"),
not an error.

Usage:
    .venv/bin/python scripts/forward_acceptance_check.py [--json]
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]

TRADING_DAYS_PER_YEAR = 252
ADVERSE_SPY_DRAWDOWN = -0.03   # >= 3% below the 20-day high
ADVERSE_VIX_CLOSE = 25.0       # VIX close above this


# ── data loading ────────────────────────────────────────────────────────────

def load_criteria(root: Path) -> dict:
    raw = yaml.safe_load((root / "config" / "forward_acceptance.yaml").read_text())
    return raw["forward_acceptance"]


def load_risk_capital(root: Path) -> dict:
    risk = yaml.safe_load((root / "config" / "config.yaml").read_text())["risk"]
    account = float(risk["account_size"])
    per_trade_pct = float(risk["max_risk_per_trade_pct"])
    return {"account_size": account,
            "max_risk_per_trade_pct": per_trade_pct,
            "risk_capital": account * per_trade_pct / 100.0}


def load_approved_recs(root: Path) -> dict:
    """rec_id -> {"strategy": ...} for recs with a live Claude approval."""
    path = root / "logs" / "recommendations.jsonl"
    approved = {}
    if not path.exists():
        return approved
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        claude = (rec.get("validation_chain") or {}).get("claude") or {}
        verdict = claude.get("verdict") or {}
        if claude.get("status") == "approved" and verdict.get("approve"):
            approved[rec["rec_id"]] = {
                "strategy": (rec.get("trade") or {}).get("strategy")}
    return approved


def _sleeve(rec_id: str, approved: dict) -> str:
    """Sleeve = strategy from recommendations.jsonl, else rec_id suffix."""
    meta = approved.get(rec_id) or {}
    if meta.get("strategy"):
        return meta["strategy"]
    parts = rec_id.split("-")
    return parts[-1] if len(parts) > 4 else "unknown"


def load_enrichment(root: Path) -> dict:
    """rec_id -> enrichment row (T2a market-truth data). L21: gates score
    market-mid P&L, never the simulator's booked fills."""
    p = root / "logs" / "observation" / "enriched_trades_v1.jsonl"
    out = {}
    if p.exists():
        for line in p.read_text().splitlines():
            r = json.loads(line)
            out[r["rec_id"]] = r
    return out


def market_truth_pnl(event: dict, enrichment: dict) -> tuple[float | None, str]:
    """(pnl, status) under market_mid_v1 scoring: ('ok', pnl) when enriched;
    'pending_enrichment' / 'no_market_data' -> excluded fail-closed."""
    row = enrichment.get(event.get("rec_id", ""))
    if row is None:
        return None, "pending_enrichment"
    if row.get("status") != "ok" or row.get("true_pnl_mid") is None:
        return None, "no_market_data"
    return float(row["true_pnl_mid"]), "ok"


def qualifying_trades(root: Path, criteria: dict) -> tuple[list[dict], dict]:
    """Closed round-trips in scope, plus the scope-filtering audit counts."""
    ledger = root / criteria.get("scope", {}).get(
        "ledger", "logs/observation/fills.jsonl")
    approved = load_approved_recs(root)
    stats = {"ledger": str(ledger), "ledger_exists": ledger.exists(),
             "exit_events": 0, "excluded_execution_test": 0,
             "excluded_not_claude_approved": 0, "excluded_pre_window": 0,
             "excluded_pending_enrichment": 0, "excluded_no_market_data": 0,
             "qualifying": 0, "scoring": criteria.get("scoring", "booked"),
             "booked_pnl_diagnostic": 0.0}
    window_start = criteria.get("window_start")   # 2026-07-11 amendment
    start_ts = pd.Timestamp(window_start) if window_start else None
    enrichment = (load_enrichment(root)
                  if criteria.get("scoring") == "market_mid_v1" else {})
    trades: list[dict] = []
    if not ledger.exists():
        return trades, stats
    for line in ledger.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event") != "paper_exit":
            continue
        stats["exit_events"] += 1
        rec_id = event.get("rec_id", "")
        if criteria.get("scope", {}).get("exclude_execution_tests", True) \
                and "-TEST-" in rec_id:
            stats["excluded_execution_test"] += 1
            continue
        if criteria.get("scope", {}).get("claude_approved_only", True) \
                and rec_id not in approved:
            stats["excluded_not_claude_approved"] += 1
            continue
        if start_ts is not None:
            # tz-aware entry-time filter (same convention as the scalp gate:
            # the ledger mixes UTC '...Z' and naive local stamps — naive is
            # America/Los_Angeles; string comparison is the L-series footgun)
            entered = (event.get("round_trip") or {}).get("entry_filled_at") \
                or event.get("ts")
            ent = pd.Timestamp(entered)
            ent = ent.tz_localize("America/Los_Angeles") if ent.tzinfo is None else ent
            if ent < start_ts:
                stats["excluded_pre_window"] += 1
                continue
        booked = float(event.get("pnl_total") if event.get("pnl_total") is not None
                       else event.get("pnl_per_contract") or 0.0)
        if criteria.get("scoring") == "market_mid_v1":
            # L21 (2026-07-15 amendment): score market truth, never the
            # simulator's booked fills; unenriched trades excluded fail-closed
            pnl, status = market_truth_pnl(event, enrichment)
            if pnl is None:
                stats[f"excluded_{status}"] += 1
                continue
            stats["booked_pnl_diagnostic"] += booked
        else:
            pnl = booked
        trades.append({
            "rec_id": rec_id,
            "ticker": event.get("ticker"),
            "ts": event.get("ts"),
            "date": (event.get("ts") or "")[:10],
            "pnl": pnl,
            "pnl_booked": booked,
            "sleeve": _sleeve(rec_id, approved),
            "exit_reason": event.get("exit_reason"),
        })
    trades.sort(key=lambda t: t["ts"] or "")
    stats["qualifying"] = len(trades)
    return trades, stats


# ── metric computations ─────────────────────────────────────────────────────

def trading_day_span(first: str, last: str) -> int:
    """Business days from first to last trade date, inclusive (approximation
    that ignores market holidays — conservative in the right direction)."""
    d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
    return int(np.busday_count(d0, d1)) + 1


def daily_pnl_series(trades: list[dict]) -> pd.Series:
    """Daily P&L over every business day in the sample span (flat days = 0)."""
    by_day: dict[str, float] = {}
    for t in trades:
        by_day[t["date"]] = by_day.get(t["date"], 0.0) + t["pnl"]
    days = pd.bdate_range(min(by_day), max(by_day))
    return pd.Series([by_day.get(d.strftime("%Y-%m-%d"), 0.0) for d in days],
                     index=days)


def annualized_sharpe(daily: pd.Series):
    if len(daily) < 2 or float(daily.std(ddof=1)) == 0.0:
        return None
    return float(daily.mean() / daily.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))


def profit_factor(pnls: list[float]):
    gross_wins = sum(p for p in pnls if p > 0)
    gross_losses = -sum(p for p in pnls if p < 0)
    if gross_losses == 0:
        return None  # undefined (no losing trades yet)
    return gross_wins / gross_losses


def max_drawdown(pnls_chronological: list[float]) -> float:
    """Peak-to-trough on the cumulative P&L curve (dollars, >= 0)."""
    equity = np.concatenate([[0.0], np.cumsum(pnls_chronological)])
    peaks = np.maximum.accumulate(equity)
    return float((peaks - equity).max())


def concentration_pnl(pnls: list[float]) -> float:
    """Total P&L after removing the top 2 winning trades."""
    winners = sorted((p for p in pnls if p > 0), reverse=True)[:2]
    return sum(pnls) - sum(winners)


def _load_daily_close(root: Path, names: list[str]):
    """Concatenated daily Close series from the first existing parquets
    (index = Date, cols include Close; NaN closes dropped)."""
    frames = []
    for name in names:
        p = root / "data" / "historical" / f"{name}.parquet"
        if p.exists():
            df = pd.read_parquet(p, columns=["Close"])
            frames.append(df["Close"].dropna())
    if not frames:
        return None
    s = pd.concat(frames)
    return s[~s.index.duplicated(keep="last")].sort_index()


def adverse_regime(root: Path, first: str, last: str) -> dict:
    """Was there >=1 calendar week within [first, last] containing a day where
    SPY sat >=3% below its 20-day high OR VIX closed > 25?"""
    spy = _load_daily_close(root, ["SPY"])
    vix = _load_daily_close(root, ["VIX", "VIX_1d"])
    out = {"required": True, "adverse_days": None, "adverse_weeks": None,
           "met": None, "data_coverage_end": None, "coverage_complete": None,
           "note": None}
    if spy is None and vix is None:
        out["note"] = "no SPY/VIX daily parquets found in data/historical"
        return out
    d0, d1 = pd.Timestamp(first), pd.Timestamp(last)
    adverse_dates = set()
    if spy is not None:
        drawdown = spy / spy.rolling(20, min_periods=1).max() - 1.0
        hits = drawdown[(drawdown.index >= d0) & (drawdown.index <= d1)
                        & (drawdown <= ADVERSE_SPY_DRAWDOWN)]
        adverse_dates.update(hits.index)
    if vix is not None:
        hits = vix[(vix.index >= d0) & (vix.index <= d1)
                   & (vix > ADVERSE_VIX_CLOSE)]
        adverse_dates.update(hits.index)
    coverage_end = max(
        [s.index.max() for s in (spy, vix) if s is not None]) if (
        spy is not None or vix is not None) else None
    out["adverse_days"] = len(adverse_dates)
    out["adverse_weeks"] = len({d.isocalendar()[:2] for d in adverse_dates})
    out["met"] = out["adverse_weeks"] >= 1
    out["data_coverage_end"] = str(coverage_end.date()) if coverage_end is not None else None
    out["coverage_complete"] = bool(coverage_end is not None and coverage_end >= d1)
    if not out["coverage_complete"]:
        out["note"] = ("daily SPY/VIX parquets end before the sample span ends; "
                       "adverse-regime check is based on partial coverage")
    return out


# ── evaluation ──────────────────────────────────────────────────────────────

def evaluate(root: Path = ROOT) -> dict:
    criteria = load_criteria(root)
    risk = load_risk_capital(root)
    trades, scope = qualifying_trades(root, criteria)
    pnls = [t["pnl"] for t in trades]

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "criteria_source": "config/forward_acceptance.yaml (frozen 2026-07-01)",
        "scope": scope,
        "evaluable": len(trades) > 0,
        "total_pnl": round(sum(pnls), 2),
        "trades": [{k: t[k] for k in ("rec_id", "ticker", "date", "pnl", "sleeve")}
                   for t in trades],
    }

    prog: dict = {}
    prog["trades"] = {"n": len(trades), "required": criteria["min_trades"],
                      "met": len(trades) >= criteria["min_trades"]}

    if not trades:
        # Honest zero-sample report: nothing else is evaluable yet.
        for key in ("trading_day_span", "sharpe_ann", "profit_factor",
                    "max_drawdown", "per_sleeve", "concentration",
                    "adverse_regime"):
            prog[key] = {"met": None, "note": "not evaluable: 0 qualifying trades"}
        result["progress"] = prog
        result["overall"] = {"pass": False,
                             "summary": "0 qualifying trades — criteria not evaluable yet"}
        return result

    first, last = trades[0]["date"], trades[-1]["date"]
    span = trading_day_span(first, last)
    prog["trading_day_span"] = {
        "first_trade": first, "last_trade": last, "trading_days": span,
        "required": criteria["min_trading_days"],
        "met": span >= criteria["min_trading_days"]}

    sharpe = annualized_sharpe(daily_pnl_series(trades))
    prog["sharpe_ann"] = {
        "value": round(sharpe, 3) if sharpe is not None else None,
        "required": criteria["min_forward_sharpe_ann"],
        "met": (sharpe >= criteria["min_forward_sharpe_ann"])
               if sharpe is not None else None,
        "note": ("estimate is statistically meaningless below "
                 f"{criteria['min_trading_days']} trading days")
                if span < criteria["min_trading_days"] else None}

    pf = profit_factor(pnls)
    prog["profit_factor"] = {
        "value": round(pf, 3) if pf is not None else None,
        "required": criteria["min_profit_factor"],
        "met": (pf >= criteria["min_profit_factor"]) if pf is not None else None,
        "note": "undefined: no losing trades yet" if pf is None else None}

    dd = max_drawdown(pnls)
    dd_pct = dd / risk["risk_capital"] * 100.0
    prog["max_drawdown"] = {
        "dollars": round(dd, 2),
        "risk_capital": risk["risk_capital"],
        "pct_of_risk_capital": round(dd_pct, 2),
        "required_max_pct": criteria["max_drawdown_pct_of_risk_capital"],
        "met": dd_pct <= criteria["max_drawdown_pct_of_risk_capital"]}

    sleeves: dict[str, float] = {}
    for t in trades:
        sleeves[t["sleeve"]] = round(sleeves.get(t["sleeve"], 0.0) + t["pnl"], 2)
    prog["per_sleeve"] = {"totals": sleeves,
                          "required": "every sleeve total >= 0",
                          "met": all(v >= 0 for v in sleeves.values())}

    conc = concentration_pnl(pnls)
    prog["concentration"] = {"pnl_after_top2_winners_removed": round(conc, 2),
                             "required": "> 0", "met": conc > 0}

    prog["adverse_regime"] = adverse_regime(root, first, last)

    result["progress"] = prog
    gates = [v.get("met") for v in prog.values()]
    result["overall"] = {
        "criteria_met": sum(1 for g in gates if g is True),
        "criteria_total": len(gates),
        "criteria_unknown": sum(1 for g in gates if g is None),
        "pass": all(g is True for g in gates),
        "summary": (f"{len(trades)}/{criteria['min_trades']} qualifying trades, "
                    f"{span}/{criteria['min_trading_days']} trading days — "
                    + ("ALL CRITERIA MET" if all(g is True for g in gates)
                       else "forward acceptance NOT met yet")),
    }
    return result


# ── rendering ───────────────────────────────────────────────────────────────

def _fmt_met(met) -> str:
    return {True: "MET", False: "not met", None: "n/a"}[met]


def render(result: dict) -> str:
    lines = ["FORWARD ACCEPTANCE PROGRESS (reports only, does not gate)",
             f"  generated: {result['generated_at']}",
             f"  scope: {result['scope']['exit_events']} exit events -> "
             f"{result['scope']['excluded_execution_test']} TEST excluded, "
             f"{result['scope']['excluded_not_claude_approved']} not Claude-approved "
             f"excluded, {result['scope'].get('excluded_pre_window', 0)} pre-window, "
             f"{result['scope'].get('excluded_pending_enrichment', 0)} pending-enrichment, "
             f"{result['scope'].get('excluded_no_market_data', 0)} no-market-data "
             f"-> {result['scope']['qualifying']} qualifying "
             f"[scoring: {result['scope'].get('scoring', 'booked')}]"]
    p = result["progress"]
    if not result["evaluable"]:
        lines.append(f"  trades: 0/{p['trades']['required']} — "
                     "criteria not evaluable yet")
        return "\n".join(lines)
    t = p["trades"]
    s = p["trading_day_span"]
    lines += [
        f"  total P&L (per-contract $): {result['total_pnl']:+.2f}",
        f"  trades:            {t['n']}/{t['required']}  [{_fmt_met(t['met'])}]",
        f"  trading-day span:  {s['trading_days']}/{s['required']} "
        f"({s['first_trade']} .. {s['last_trade']})  [{_fmt_met(s['met'])}]",
    ]
    sh = p["sharpe_ann"]
    lines.append(f"  Sharpe (ann.):     "
                 f"{sh['value'] if sh['value'] is not None else 'n/a'} "
                 f"(need >= {sh['required']})  [{_fmt_met(sh['met'])}]"
                 + (f"  NOTE: {sh['note']}" if sh.get("note") else ""))
    pf = p["profit_factor"]
    lines.append(f"  profit factor:     "
                 f"{pf['value'] if pf['value'] is not None else 'n/a'} "
                 f"(need >= {pf['required']})  [{_fmt_met(pf['met'])}]")
    dd = p["max_drawdown"]
    lines.append(f"  max drawdown:      ${dd['dollars']:.2f} = "
                 f"{dd['pct_of_risk_capital']:.2f}% of ${dd['risk_capital']:.0f} "
                 f"risk capital (max {dd['required_max_pct']}%)  "
                 f"[{_fmt_met(dd['met'])}]")
    sl = p["per_sleeve"]
    lines.append(f"  per-sleeve totals: {sl['totals']}  [{_fmt_met(sl['met'])}]")
    c = p["concentration"]
    lines.append(f"  concentration:     P&L after top-2 winners removed = "
                 f"{c['pnl_after_top2_winners_removed']:+.2f} (need > 0)  "
                 f"[{_fmt_met(c['met'])}]")
    a = p["adverse_regime"]
    lines.append(f"  adverse regime:    adverse days={a['adverse_days']}, "
                 f"adverse weeks={a['adverse_weeks']} (need >= 1 week)  "
                 f"[{_fmt_met(a['met'])}]"
                 + (f"  NOTE: {a['note']}" if a.get("note") else ""))
    o = result["overall"]
    lines.append(f"  OVERALL: {o['criteria_met']}/{o['criteria_total']} met "
                 f"({o['criteria_unknown']} unknown) — {o['summary']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="emit the machine-readable dict instead of text")
    args = parser.parse_args()
    try:
        result = evaluate(ROOT)
    except Exception as exc:  # report, never crash the caller
        result = {"check_error": str(exc)}
        print(json.dumps(result) if args.json else f"evaluator error: {exc}")
        return 0
    print(json.dumps(result, indent=1, default=str) if args.json
          else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
