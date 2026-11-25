"""Risk kernel — Phase L step 1 (2026-07-02, roadmap-to-live).

Deterministic, config-driven monetary risk controls, wired into the PAPER
path for burn-in months before any live mode exists:

  size_position    — contracts from max_risk_per_trade_pct x account_size
                     against the trade's max_loss (0 = unaffordable, refuse)
  gate_new_entry   — single decision point for every new entry:
                     daily-loss circuit breaker (funnel AND execution-test),
                     max_open_recommendations / max_recommendations_per_day
                     (funnel only; execution-tests keep their own caps),
                     then sizing
  circuit_breaker  — halts NEW entries when realized day P&L breaches
                     max_daily_loss_pct; open positions keep being managed
                     (exits always run). Alerts once per day via ntfy.

All dollar math: trade prices are per-share; x100 per contract.
State: data/risk_state.json (breaker alert dedup). Reads fills.jsonl for
realized P&L (pnl_total when present, pnl_per_contract fallback for
pre-sizing records).
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from src.engine.paper_trader import is_scalp as _is_scalp

ROOT = Path(__file__).resolve().parents[2]
FILLS_LOG = ROOT / "logs" / "observation" / "fills.jsonl"
RISK_STATE = ROOT / "data" / "risk_state.json"

logger = logging.getLogger("trader")

DEFAULTS = {
    "account_size": 50000,
    "max_risk_per_trade_pct": 2.0,
    "max_open_recommendations": 3,
    "max_recommendations_per_day": 6,
    "max_daily_loss_pct": 3.0,   # breaker: halt new entries at -3% of account/day
    "max_contracts": 10,         # absolute size ceiling (liquidity sanity)
}


def risk_config(cfg: dict | None) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("risk") or {})}


def per_trade_budget(rcfg: dict) -> float:
    return rcfg["account_size"] * rcfg["max_risk_per_trade_pct"] / 100.0


def size_position(trade: dict, rcfg: dict) -> dict:
    """Contracts = floor(risk budget / max loss per contract), capped at
    max_contracts. quantity 0 means the trade is unaffordable at the
    configured risk — refuse, never round up."""
    max_loss = trade.get("max_loss")
    if max_loss is None or float(max_loss) <= 0:
        return {"quantity": 0,
                "reason": f"max_loss undefined/non-positive ({max_loss}) — refusing to size"}
    budget = per_trade_budget(rcfg)
    per_contract = round(float(max_loss) * 100.0, 2)
    qty = min(int(budget // per_contract), int(rcfg["max_contracts"]))
    if qty < 1:
        return {"quantity": 0, "risk_budget": budget, "max_loss_per_contract": per_contract,
                "reason": f"max_loss ${per_contract:.0f}/contract exceeds ${budget:.0f} risk budget"}
    return {"quantity": qty, "risk_budget": budget, "max_loss_per_contract": per_contract,
            "max_dollar_risk": round(qty * per_contract, 2),
            "reason": f"{qty} contract(s): ${per_contract:.0f} max loss each vs ${budget:.0f} budget"}


def realized_pnl_today() -> float:
    """Realized paper P&L today from exit events. pnl_total (quantity-aware,
    2026-07-02+) preferred; pnl_per_contract fallback for older records."""
    if not FILLS_LOG.exists():
        return 0.0
    today = f"{datetime.now():%Y-%m-%d}"
    total = 0.0
    for line in FILLS_LOG.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") == "paper_exit" and str(e.get("ts", "")).startswith(today):
            total += float(e.get("pnl_total") if e.get("pnl_total") is not None
                           else e.get("pnl_per_contract") or 0)
    return round(total, 2)


def _load_state() -> dict:
    if RISK_STATE.exists():
        try:
            return json.loads(RISK_STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def realized_pnl_today_by_lane(lane: str) -> float:
    """Realized paper P&L today for ONE lane (L-b, 2026-08-06). Lane is derived
    from the rec_id convention via the canonical is_scalp() predicate, which
    recognises both marker eras (docstring corrected 2026-08-10: it named only
    the legacy marker, which is how callers came to hardcode it)."""
    if not FILLS_LOG.exists():
        return 0.0
    today = f"{datetime.now():%Y-%m-%d}"
    total = 0.0
    for line in FILLS_LOG.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") != "paper_exit" or not str(e.get("ts", "")).startswith(today):
            continue
        rid = str(e.get("rec_id", ""))
        if ("scalp" if _is_scalp(rid) else "funnel") != lane:
            continue
        total += float(e.get("pnl_total") if e.get("pnl_total") is not None
                       else e.get("pnl_per_contract") or 0)
    return round(total, 2)


def circuit_breaker(rcfg: dict, lane: str | None = None) -> dict:
    """TWO-TIER daily-loss breaker (L-b, 2026-08-06).

    Tier 1 — PER LANE: a lane that breaches its own limit stops entering. The
             other lane keeps trading. Execution state is lane-isolated.
    Tier 2 — ACCOUNT: the account-level cap halts BOTH lanes. Account risk
             intentionally aggregates across lanes (restated O-13).

    Exits and position management are never blocked by either tier.
    `lane=None` evaluates the account tier only (back-compatible).
    """
    limit = round(rcfg["account_size"] * rcfg["max_daily_loss_pct"] / 100.0, 2)
    realized = realized_pnl_today()
    tripped = realized <= -limit
    scope = "account"
    if not tripped and lane:
        per = (rcfg.get("max_daily_loss_pct_per_lane") or {}).get(lane)
        if per is not None:
            lane_limit = round(rcfg["account_size"] * float(per) / 100.0, 2)
            lane_realized = realized_pnl_today_by_lane(lane)
            if lane_realized <= -lane_limit:
                tripped, scope = True, "lane"
                logger.warning("LANE BREAKER TRIPPED (%s): realized %+.2f vs -%.2f "
                               "— %s entries halted; the other lane continues",
                               lane, lane_realized, lane_limit, lane)
                return {"tripped": True, "scope": "lane", "lane": lane,
                        "realized_today": lane_realized, "limit": lane_limit}
    if tripped:
        today = f"{datetime.now():%Y-%m-%d}"
        state = _load_state()
        if state.get("breaker_alerted") != today:
            state["breaker_alerted"] = today
            RISK_STATE.parent.mkdir(parents=True, exist_ok=True)
            RISK_STATE.write_text(json.dumps(state))
            logger.warning("DAILY-LOSS BREAKER TRIPPED: realized %+.2f vs -%.2f limit — "
                           "new entries halted for the day", realized, limit)
            try:
                from src.engine.notify import send
                send("DAILY-LOSS BREAKER TRIPPED",
                     f"realized {realized:+.2f} vs -{limit:.2f} limit — new entries "
                     "halted; exits still managed", priority="high", tags="octagonal_sign")
            except Exception:
                pass
    return {"tripped": tripped, "scope": scope, "lane": lane,
            "realized_today": realized, "limit": limit}


def _funnel_exposure() -> tuple[int, int]:
    """(open funnel positions, today's posted funnel recs) — the FUNNEL lane's
    own exposure, used to gate funnel entries.

    C-1 (2026-08-10, operator-approved). Two defects, both counting SCALP
    activity against the funnel's limits:

      * lane test was `"-TEST-" not in rec_id`, so every id minted after the
        2026-08-08 forward-only rename read as funnel;
      * recs_today counted EVERY recommendation logged today with no lane
        filter at all — a bug independent of the rename.

    Measured before the fix on 2026-08-10: open=4 (cap 3) and recs_today=19
    (cap 10) while ACTUAL funnel positions and recs were both ZERO — the
    Claude-gated lane was shut out on both counters by SCALP activity.
    config.yaml:121 ("funnel: 3 — inherited from max_open_recommendations")
    shows lane isolation was always the intent; this restores it.
    """
    from src.engine.paper_trader import load_positions, _load, PENDING_FILE, is_scalp
    is_funnel = lambda rid: not is_scalp(rid)  # noqa: E731
    open_n = sum(1 for p in load_positions() if is_funnel(p.get("rec_id")))
    open_n += sum(1 for o in _load(PENDING_FILE)
                  if o.get("intent") == "open" and is_funnel(o.get("rec_id")))
    recs_log = ROOT / "logs" / "recommendations.jsonl"
    today = f"{datetime.now():%Y-%m-%d}"
    recs_today = 0
    if recs_log.exists():
        for line in recs_log.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(r.get("generated", "")).startswith(today) \
                    and is_funnel(r.get("rec_id")):
                recs_today += 1
    return open_n, recs_today


def gate_new_entry(trade: dict, kind: str, cfg: dict | None) -> dict:
    """Single risk decision for a prospective NEW entry.
    kind: 'funnel' (real recommendation) | 'test' (execution-test).
    Returns {allowed, quantity, reason, breaker, sizing[, exposure]}."""
    rcfg = risk_config(cfg)
    breaker = circuit_breaker(rcfg)
    if breaker["tripped"]:
        return {"allowed": False, "quantity": 0, "breaker": breaker,
                "reason": f"daily-loss breaker tripped ({breaker['realized_today']:+.2f} "
                          f"vs -{breaker['limit']:.2f})"}
    out = {"breaker": breaker}
    if kind == "funnel":
        open_n, recs_today = _funnel_exposure()
        out["exposure"] = {"open": open_n, "recs_today": recs_today}
        if open_n >= rcfg["max_open_recommendations"]:
            return {**out, "allowed": False, "quantity": 0,
                    "reason": f"max_open_recommendations reached ({open_n}/"
                              f"{rcfg['max_open_recommendations']})"}
        if recs_today >= rcfg["max_recommendations_per_day"]:
            return {**out, "allowed": False, "quantity": 0,
                    "reason": f"max_recommendations_per_day reached ({recs_today}/"
                              f"{rcfg['max_recommendations_per_day']})"}
    sizing = size_position(trade, rcfg)
    out["sizing"] = sizing
    if sizing["quantity"] < 1:
        return {**out, "allowed": False, "quantity": 0, "reason": sizing["reason"]}
    return {**out, "allowed": True, "quantity": sizing["quantity"], "reason": sizing["reason"]}
