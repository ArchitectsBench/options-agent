"""Cockpit section: discovery observation sleeves (2026-07-18).

Read-only over the two sleeve ledgers written by scripts/rvolrank_sleeve.py
and scripts/ldte_prem_sleeve.py. Everything here is UNVALIDATED — PAPER ONLY
evidence accumulating toward the frozen criteria in config/*.yaml; this
section only REPORTS, it never judges a gate early.

RVOL-RANK panel (rewritten 2026-09-13): reports what the frozen gate is
actually judged on — the GATE-PRIMARY 09:45 executable lanes
(selection_exec / control_exec, amendment A1) after ledger hygiene
(window_start + excluded_from_gates read from the criteria file at runtime,
dedupe-by-date keeping the latest `generated`). The idealized 09:35 lanes are
kept as measurement-only. Every derived flag is stamped interim.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RVOL = ROOT / "logs" / "observation" / "rvolrank_sleeve.jsonl"
LDTE = ROOT / "logs" / "observation" / "ldte_prem_sleeve.jsonl"
CRITERIA = ROOT / "config" / "rvolrank_forward_criteria.yaml"

STAMP = "UNVALIDATED — PAPER ONLY"
STAMP_INTERIM = "interim, NOT a gate judgment"

# NYSE full-day closures. Only 2026-09-07 onward can affect the missing-weekday
# scan (window opens 2026-07-20) or the projection; earlier dates are inert but
# listed for completeness. Extend when the projection crosses into a new year.
NYSE_HOLIDAYS = frozenset({
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})

_CRITERIA_DEFAULTS = {
    "window_start": "2026-07-20",
    "excluded_from_gates": [],
    "min_sim_sessions": 60,
    "selection_t_min_deflated": 2.9,
    "gap_min": 0.10,
    "worst_month_mean_R_min": -0.25,
    "hard_min_exec_selection_trades": 400,
    "gate_binding_tier": "stress_2x",
    "trial_count_N": 27,
}


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for l in path.read_text().splitlines():
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            continue
    return out


def _criteria() -> dict:
    """Read the frozen criteria file (original block + every amendment_* block) at runtime.

    Read-only. Falls back to the frozen constants if the file is unreadable so
    the cockpit degrades with a flag rather than a crash."""
    c = dict(_CRITERIA_DEFAULTS)
    c["loaded"] = False
    try:
        import yaml
        raw = yaml.safe_load(CRITERIA.read_text()) or {}
    except Exception:
        return c
    ws = raw.get("window_start")
    if ws:
        c["window_start"] = str(ws)[:10]
    acc = raw.get("acceptance") or {}
    c["min_sim_sessions"] = int(acc.get("min_sim_sessions", c["min_sim_sessions"]))
    c["gap_min"] = float(acc.get("selection_vs_control_gap_min", c["gap_min"]))
    c["worst_month_mean_R_min"] = float(
        acc.get("worst_month_mean_R_min", c["worst_month_mean_R_min"]))
    # The file is amend-only by dated block (amendment_a1_..., amendment_a2_...).
    # Walk EVERY block in sorted order: exclusions are unioned, scalars are
    # overridden by the later block. (Plain string sort orders a1..a9; an
    # a10 would need zero-padding — note it when the time comes.)
    blocks = [k for k in raw if isinstance(k, str) and k.startswith("amendment_")]
    excluded: set[str] = set()
    for k in sorted(blocks):
        blk = raw.get(k) or {}
        if not isinstance(blk, dict):
            continue
        excluded.update(str(d)[:10] for d in (blk.get("excluded_from_gates") or []))
        defl = blk.get("deflation") or {}
        if "selection_t_min_DEFLATED" in defl:
            c["selection_t_min_deflated"] = float(defl["selection_t_min_DEFLATED"])
        # A2a (2026-09-13) re-froze the bar UP under a different key shape
        # (registry.deflated_t_bar, N 27 -> 30). Without this the cockpit kept
        # showing A1's superseded 2.90 while the registry said 2.94.
        reg = blk.get("registry") or {}
        if isinstance(reg, dict) and "deflated_t_bar" in reg:
            c["selection_t_min_deflated"] = float(reg["deflated_t_bar"])
            if "trial_count_N" in reg:
                c["trial_count_N"] = int(reg["trial_count_N"])
        bc = blk.get("binding_constraint") or {}
        if "hard_min_exec_selection_trades" in bc:
            c["hard_min_exec_selection_trades"] = int(bc["hard_min_exec_selection_trades"])
        tiers = blk.get("cost_tiers") or {}
        if "gate_binding_tier" in tiers:
            c["gate_binding_tier"] = str(tiers["gate_binding_tier"])
    c["excluded_from_gates"] = sorted(excluded)
    c["amendment_blocks"] = sorted(blocks)
    c["loaded"] = True
    return c


# ---------------------------------------------------------------- calendar --
def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def _missing_weekdays(start: str, end: str, present: set[str]) -> list[str]:
    """Trading days in [start, end] with no ledger row (holidays skipped)."""
    try:
        d, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return []
    out = []
    while d <= last:
        if _is_trading_day(d) and d.isoformat() not in present:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _add_trading_days(start: str, n: int) -> str | None:
    """The trading day `n` sessions after `start` (holidays skipped)."""
    try:
        d = date.fromisoformat(start)
    except ValueError:
        return None
    left = n
    while left > 0:
        d += timedelta(days=1)
        if _is_trading_day(d):
            left -= 1
    return d.isoformat()


# ------------------------------------------------------------------- stats --
def _r(x) -> float | None:
    return None if x is None else round(x, 4)


def _t_stat(vals: list[float]) -> float | None:
    """One-sample t on per-trade R: mean / (sd / sqrt n); sample sd (ddof=1)."""
    n = len(vals)
    if n < 2:
        return None
    sd = statistics.stdev(vals)
    if sd == 0:
        return None
    return statistics.mean(vals) / (sd / math.sqrt(n))


def _lane_stats(trades: list[dict], key=None) -> dict:
    """n / pnl / mean_R / sd / t for a list of trades; `key` picks the R field."""
    key = key or (lambda t: t.get("r"))
    vals = [v for v in (key(t) for t in trades) if isinstance(v, (int, float))]
    n = len(vals)
    return {
        "n": n,
        "pnl": round(sum(t.get("pnl") or 0 for t in trades), 2),
        "mean_R": _r(statistics.mean(vals)) if n else None,
        "sd": _r(statistics.stdev(vals)) if n >= 2 else None,
        "t": _r(_t_stat(vals)),
    }


def _tier_key(tier: str):
    return lambda t: (t.get("r_tiers") or {}).get(tier)


def _gap(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round(a - b, 4)


def _worst_month(rows: list[dict], lane: str, key=None) -> tuple[dict | None, dict]:
    """Per-calendar-month mean R (month = session date, fills are intraday)."""
    key = key or (lambda t: t.get("r"))
    by_month: dict[str, list[float]] = {}
    for row in rows:
        m = row["date"][:7]
        for t in row.get(lane, {}).get("trades", []):
            v = key(t)
            if isinstance(v, (int, float)):
                by_month.setdefault(m, []).append(v)
    months = {m: {"n": len(v), "mean_R": _r(statistics.mean(v))}
              for m, v in sorted(by_month.items())}
    if not months:
        return None, months
    worst = min(months, key=lambda m: months[m]["mean_R"])
    return {"month": worst, **months[worst]}, months


# --------------------------------------------------------------- hygiene ----
def _hygiene(raw: list[dict], crit: dict) -> tuple[list[dict], dict]:
    """Apply window_start / excluded_from_gates, dedupe by date keeping the
    LAST row by `generated` (the --supersede repair pattern; ties fall back to
    file order), and scan for missing trading days."""
    window_start = crit["window_start"]
    excluded = set(crit["excluded_from_gates"])
    dated = [r for r in raw if isinstance(r.get("date"), str)]
    dropped, kept = [], []
    for r in dated:
        (dropped if r["date"] < window_start or r["date"] in excluded
         else kept).append(r)
    by_date: dict[str, tuple[str, int, dict]] = {}
    dupes: list[str] = []
    for idx, r in enumerate(kept):
        k = (str(r.get("generated") or ""), idx)
        cur = by_date.get(r["date"])
        if cur is None:
            by_date[r["date"]] = (*k, r)
            continue
        if r["date"] not in dupes:
            dupes.append(r["date"])
        if k > cur[:2]:
            by_date[r["date"]] = (*k, r)
    valid = sorted((v[2] for v in by_date.values()), key=lambda r: r["date"])
    last = valid[-1]["date"] if valid else None
    # An in-window EXCLUDED date has a ledger row: it is reported under
    # excluded_dates, not double-counted as a missing weekday.
    present = set(by_date) | {r["date"] for r in dropped if r["date"] >= window_start}
    missing = _missing_weekdays(window_start, last, present) if last else []
    hyg = {
        "rows_total": len(raw),
        "rows_no_date": len(raw) - len(dated),
        "rows_excluded_pre_window": len(dropped),
        "excluded_dates": sorted({r["date"] for r in dropped}),
        "duplicate_dates": sorted(dupes),
        "missing_weekdays": missing,
        "sessions_valid": len(valid),
        "exec_sessions": sum(1 for r in valid if "selection_exec" in r),
        "window_start": window_start,
        "last_ledger_date": last,
        "criteria_loaded": crit["loaded"],
        "criteria_amendment_blocks": crit.get("amendment_blocks", []),
    }
    return valid, hyg


# ------------------------------------------------------------------ panel ---
def _rvolrank() -> dict:
    crit = _criteria()
    valid, hyg = _hygiene(_rows(RVOL), crit)
    sessions_goal = crit["min_sim_sessions"]
    trades_goal = crit["hard_min_exec_selection_trades"]
    t_bar, gap_bar = crit["selection_t_min_deflated"], crit["gap_min"]
    wm_bar, bind_tier = crit["worst_month_mean_R_min"], crit["gate_binding_tier"]

    # ---- GATE-PRIMARY: 09:45 executable lanes (amendment A1) ----
    exec_rows = [r for r in valid if "selection_exec" in r]
    sel_x = [t for r in exec_rows for t in r.get("selection_exec", {}).get("trades", [])]
    ctl_x = [t for r in exec_rows for t in r.get("control_exec", {}).get("trades", [])]
    sel = _lane_stats(sel_x)
    ctl = _lane_stats(ctl_x)
    sel["skips"] = sum(len(r.get("selection_exec", {}).get("skips", [])) for r in exec_rows)
    ctl["skips"] = sum(len(r.get("control_exec", {}).get("skips", [])) for r in exec_rows)
    gap = _gap(sel["mean_R"], ctl["mean_R"])
    tiers = {}
    for tier in ("base", "stress_2x", "stress_3x"):
        s, c = _lane_stats(sel_x, _tier_key(tier)), _lane_stats(ctl_x, _tier_key(tier))
        tiers[tier] = {"selection_n": s["n"], "selection_mean_R": s["mean_R"],
                       "selection_t": s["t"], "control_mean_R": c["mean_R"],
                       "gap": _gap(s["mean_R"], c["mean_R"])}
    worst, months = _worst_month(exec_rows, "selection_exec")
    worst_bind, _ = _worst_month(exec_rows, "selection_exec", _tier_key(bind_tier))

    n_exec_sessions = hyg["exec_sessions"]
    rate_raw = sel["n"] / n_exec_sessions if n_exec_sessions else None
    run_rate = round(rate_raw, 2) if rate_raw is not None else None
    proj_sessions = proj_date = None
    if sel["n"] >= trades_goal:
        proj_sessions = 0
    elif rate_raw:
        proj_sessions = math.ceil((trades_goal - sel["n"]) / rate_raw)
        proj_date = _add_trading_days(hyg["last_ledger_date"], proj_sessions)
    bind = tiers[bind_tier] if bind_tier in tiers else tiers["stress_2x"]
    # The nightly's own A1-required run-rate/projection (over gate-valid
    # sessions, i.e. incl. the three 09:35-only rows) — passed through so the
    # cockpit can show WHY its exec-session-based figures differ.
    nightly_bc = next((r["binding_constraint"] for r in reversed(valid)
                       if isinstance(r.get("binding_constraint"), dict)), None)

    def _ge(v, bar):
        return None if v is None else bool(v >= bar)

    gate_primary = {
        "label": "GATE-PRIMARY 09:45 exec",
        "lanes": ["selection_exec", "control_exec"],
        "exec_sessions": n_exec_sessions,
        # Raw `r` is the nightly's continuity metric (net of base slippage,
        # GROSS of commission) — NOT a frozen cost tier. The gate is judged on
        # `gate_binding_tier` (r_tiers[...]), surfaced under `binding` below.
        "selection": sel, "control": ctl, "gap": gap,
        "raw_r_basis": ("r = net of base slippage, gross of commission "
                        "(continuity metric, not a cost tier)"),
        "tiers": tiers, "gate_binding_tier": bind_tier,
        "binding": {"tier": bind_tier, **bind, "worst_month": worst_bind},
        "worst_month": worst, "months": months,
        f"worst_month_{bind_tier}": worst_bind,
        "bars": {"t_min_deflated": t_bar, "gap_min": gap_bar,
                 "worst_month_mean_R_min": wm_bar,
                 "min_sim_sessions": sessions_goal,
                 "hard_min_exec_selection_trades": trades_goal,
                 "trial_count_N": crit.get("trial_count_N")},
        "progress": {
            "sessions_valid": hyg["sessions_valid"], "sessions_goal": sessions_goal,
            "exec_sessions": n_exec_sessions,
            "exec_trades": sel["n"], "exec_trades_goal": trades_goal,
            "run_rate": run_rate,
            "run_rate_basis": f"selection_exec trades / {n_exec_sessions} exec sessions",
            "projected_sessions_to_400": proj_sessions,
            "projected_date_400": proj_date,
            "nightly_binding_constraint": nightly_bc,
        },
        "flags": {
            "stamp": STAMP_INTERIM,
            f"{bind_tier}_t_vs_bar": _ge(bind["selection_t"], t_bar),
            f"{bind_tier}_gap_vs_bar": _ge(bind["gap"], gap_bar),
            f"{bind_tier}_worst_month_vs_bar": _ge(
                worst_bind["mean_R"] if worst_bind else None, wm_bar),
            "raw_r_t_vs_bar": _ge(sel["t"], t_bar),
            "raw_r_gap_vs_bar": _ge(gap, gap_bar),
            "raw_r_worst_month_vs_bar": _ge(worst["mean_R"] if worst else None, wm_bar),
        },
    }

    # ---- IDEALIZED 09:35 lanes: measurement-only (no real-time stock data) ----
    sel_i = [t for r in valid for t in r.get("selection", {}).get("trades", [])]
    ctl_i = [t for r in valid for t in r.get("control", {}).get("trades", [])]
    si, ci = _lane_stats(sel_i), _lane_stats(ctl_i)
    idealized = {
        "label": "measurement-only (09:35, no real-time data)",
        "status": "measurement_only",
        "sessions": len(valid),
        "selection": {"n": si["n"], "pnl": si["pnl"], "mean_R": si["mean_R"], "t": si["t"]},
        "control": {"n": ci["n"], "pnl": ci["pnl"], "mean_R": ci["mean_R"]},
        "gap": _gap(si["mean_R"], ci["mean_R"]),
    }

    # ---- recent days: exec lanes, idealized fallback for pre-07-23 rows ----
    recent = []
    for r in valid[-10:][::-1]:
        has_exec = "selection_exec" in r
        s = r.get("selection_exec" if has_exec else "selection", {})
        c = r.get("control_exec" if has_exec else "control", {})
        recent.append({
            "date": r["date"],
            "lane": "exec" if has_exec else "idealized (fallback, pre-exec row)",
            "sel_n": s.get("n"), "sel_pnl": s.get("pnl"), "sel_mean_R": s.get("mean_R"),
            "ctl_n": c.get("n"), "ctl_pnl": c.get("pnl"), "ctl_mean_R": c.get("mean_R"),
        })

    return {
        "name": "RVOL-RANK (virtual ORB, nightly)",
        "stamp": STAMP,
        "stamp_interim": STAMP_INTERIM,
        "criteria": "config/rvolrank_forward_criteria.yaml",
        "gate_primary": gate_primary,
        "idealized": idealized,
        "recent_days": recent,
        "hygiene": hyg,
    }


def _ldte() -> dict:
    rows = _rows(LDTE)
    entries = {r["rec_id"]: r for r in rows if r.get("event") == "entry"}
    settles = {r["rec_id"]: r for r in rows if r.get("event") == "settlement"}
    open_pos, closed = [], []
    today = date.today()
    for rid, e in entries.items():
        if rid in settles:
            s = settles[rid]
            closed.append({
                "rec_id": rid, "ticker": e["ticker"],
                "entered": e.get("generated", "")[:10],
                "expiration": e["expiration"],
                "spread": f"{e['short_k']:g}/{e['long_k']:g}P",
                "entry_credit": e["entry_credit"],
                "settle_spot": s.get("settle_spot"),
                "pnl": s.get("pnl"), "R": s.get("R")})
        else:
            try:
                dte_left = (date.fromisoformat(e["expiration"]) - today).days
            except ValueError:
                dte_left = None
            open_pos.append({
                "rec_id": rid, "ticker": e["ticker"],
                "entered": e.get("generated", "")[:10],
                "expiration": e["expiration"], "dte_left": dte_left,
                "spread": f"{e['short_k']:g}/{e['long_k']:g}P",
                "entry_credit": e["entry_credit"],
                "max_loss": round((e["short_k"] - e["long_k"]
                                   - e["entry_credit"]) * 100, 2),
                "term_slope_pctl": e.get("term_slope_pctl")})
    closed.sort(key=lambda r: r["expiration"], reverse=True)
    n = len(closed)
    decisions = [r for r in rows if r.get("event") == "daily_decision"]
    return {
        "name": "LDTE-PREM (60-90d put spreads, hold to expiry)",
        "stamp": STAMP,
        "criteria": "config/ldte_prem_forward_criteria.yaml",
        "completed_trips": n, "trips_goal": 25,
        "mean_R": round(sum(t["R"] or 0 for t in closed) / n, 4) if n else None,
        "total_pnl": round(sum(t["pnl"] or 0 for t in closed), 2) if n else 0.0,
        "open_positions": sorted(open_pos, key=lambda r: r["expiration"]),
        "recent_closed": closed[:10],
        "last_decision": (decisions[-1] if decisions else None),
    }


def build_sleeves() -> dict:
    # LDTE-PREM retired 2026-07-18 (holdout FAIL before window open) —
    # section removed from the cockpit; _ldte() kept for the historical
    # ledger should anyone query it ad hoc.
    return {"rvolrank": _rvolrank()}
