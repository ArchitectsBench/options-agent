"""Per-name rules of the frozen K18 construction, as PURE functions
(spec §7, §2, §3(g)). Every function has a known-answer test in
`tests/rvol_exec/test_lane_rules.py`. UNVALIDATED — PAPER ONLY.

The sim's formulas (`scripts/rvolrank_sleeve.py::replay_k18`) are reproduced
line-for-line where the rule is IDENTICAL (doji, side, level, stop_dist) and
adapted only where the spec says so (shares FLOORED and >= 1; limit = the
sim's cancel guardrail; stale-trigger guard = the guardrail at placement).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from src.rvol_exec.constants import (DOJI_MIN_BODY, GUARD_FRAC, LEVEL_TICK, MAX_ENTRIES,
                                     NOTIONAL_CAP_USD, PART_CAP, RISK_USD, SLOTS,
                                     STOP_ATR_FRAC)


def r2(x: float) -> float:
    return round(float(x) + 0.0, 2)


# ── rule 2: doji filter ──────────────────────────────────────────────────────
def doji_ok(o: float, h: float, l: float, c: float) -> bool:
    """`rng <= 0 or body < 0.10*rng -> None` in replay_k18 (a doji is SKIPPED)."""
    rng = float(h) - float(l)
    body = abs(float(c) - float(o))
    return not (rng <= 0 or body < DOJI_MIN_BODY * rng)


# ── rule 3/4: side and level ─────────────────────────────────────────────────
def side_of(o: float, c: float) -> str:
    return "long" if float(c) > float(o) else "short"


def level_of(side: str, h: float, l: float) -> float:
    return r2(float(h) + LEVEL_TICK) if side == "long" else r2(float(l) - LEVEL_TICK)


# ── rule 5: stop distance ────────────────────────────────────────────────────
def stop_dist_of(atr14: float) -> float:
    return STOP_ATR_FRAC * float(atr14)


def limit_of(side: str, level: float, stop_dist: float) -> float:
    """The sim's cancel guardrail as the parent's limit: `level ± 0.10*sd`."""
    off = GUARD_FRAC * float(stop_dist)
    return r2(level + off) if side == "long" else r2(level - off)


def stop_provisional_of(side: str, level: float, stop_dist: float) -> float:
    return r2(level - stop_dist) if side == "long" else r2(level + stop_dist)


def stop_final_of(side: str, avg_fill: float, stop_dist: float) -> float:
    return r2(avg_fill - stop_dist) if side == "long" else r2(avg_fill + stop_dist)


# ── rule 11: shares ──────────────────────────────────────────────────────────
def shares_sim(stop_dist: float, v930: float) -> float:
    """The sim's FRACTIONAL size, `round(., 1)`."""
    return round(min(RISK_USD / float(stop_dist), PART_CAP * float(v930)), 1)


def shares_live(stop_dist: float, v930: float) -> int:
    """`floor(min(50/sd, 0.01*v930))`; `< 1` means the name is skipped."""
    if float(stop_dist) <= 0:
        return 0
    return int(math.floor(min(RISK_USD / float(stop_dist), PART_CAP * float(v930))))


def notional_ok(shares: int, level: float) -> bool:
    return float(shares) * float(level) <= NOTIONAL_CAP_USD


# ── §7.5 stale-trigger guard ─────────────────────────────────────────────────
def stale_trigger_ok(side: str, guard_ref: float | None, limit: float) -> bool:
    """False when the reference price is already THROUGH the limit — the
    sim's guardrail cancels this name at its first bar. Exactly at the limit
    places; one tick beyond skips. No reference -> place (the rule is inert)."""
    if guard_ref is None:
        return True
    g = float(guard_ref)
    return g <= float(limit) + 1e-9 if side == "long" else g >= float(limit) - 1e-9


# ── §8.4 post-trigger breach ─────────────────────────────────────────────────
def breached(side: str, last: float | None, limit: float) -> bool:
    if last is None:
        return False
    return float(last) > float(limit) + 1e-9 if side == "long" else float(last) < float(limit) - 1e-9


# ── the plan for one name ────────────────────────────────────────────────────
def plan_name(o: float, h: float, l: float, c: float, v930: float, atr14: float) -> dict:
    """Rules 2-5 + 11 for one rank row. Returns a dict with `status` "ok" or
    "skipped" (+ `reason` in {doji, size, cap}) and the order geometry."""
    out: dict = {"o": float(o), "h": float(h), "l": float(l), "c": float(c),
                 "v930_used": float(v930), "atr14": float(atr14)}
    if not doji_ok(o, h, l, c):
        out.update(status="skipped", reason="doji", doji=True)
        return out
    side = side_of(o, c)
    level = level_of(side, h, l)
    sd = stop_dist_of(atr14)
    out.update(doji=False, side=side, level=level, stop_dist=sd)
    if sd <= 0:
        out.update(status="skipped", reason="size")
        return out
    out.update(limit_px=limit_of(side, level, sd),
               stop_provisional=stop_provisional_of(side, level, sd),
               shares_sim=shares_sim(sd, v930), shares_live=shares_live(sd, v930))
    if out["shares_live"] < 1:
        out.update(status="skipped", reason="size")
        return out
    if not notional_ok(out["shares_live"], level):
        out.update(status="skipped", reason="cap")
        return out
    out["status"] = "ok"
    return out


# ── §11.11 caps at placement ─────────────────────────────────────────────────
def may_place(now_et: datetime, t_entry_end, t_no_entry_after, placed: int,
              slots_used: int, entries_closed: bool) -> tuple[bool, str]:
    t = now_et.timetz().replace(tzinfo=None)
    if t >= t_entry_end:
        return False, "window_closed"
    if t >= t_no_entry_after:
        return False, "no_entry_after_1540"
    if entries_closed:
        return False, "entries_closed"
    if placed >= MAX_ENTRIES:
        return False, "max_entries"
    if slots_used >= SLOTS:
        return False, "slots_full"
    return True, ""


# ── §3(g) comparator alignment ───────────────────────────────────────────────
def entry_start_hm(placed_at_et: datetime, tolerance_s: int = 30) -> str:
    """`floor5m(placed_at)` when placement is within `tolerance_s` of the bar
    open (its open is a fair proxy for the price at placement), else
    `ceil5m(placed_at)`."""
    m5 = placed_at_et.replace(second=0, microsecond=0)
    m5 = m5 - timedelta(minutes=m5.minute % 5)
    if (placed_at_et - m5).total_seconds() <= tolerance_s:
        return m5.strftime("%H:%M")
    return (m5 + timedelta(minutes=5)).strftime("%H:%M")


# ── diagnostics ──────────────────────────────────────────────────────────────
def slip_bps(side: str, fill: float, ref: float) -> float | None:
    """Adverse-signed slippage in bps of `ref` (positive = worse than ref)."""
    try:
        f, r = float(fill), float(ref)
    except (TypeError, ValueError):
        return None
    if r <= 0:
        return None
    raw = (f - r) / r * 1e4
    return round(raw if side == "long" else -raw, 2)


def r_booked(side: str, entry_avg: float, exit_avg: float, stop_dist: float) -> float | None:
    try:
        e, x, sd = float(entry_avg), float(exit_avg), float(stop_dist)
    except (TypeError, ValueError):
        return None
    if sd <= 0 or e <= 0 or x <= 0:
        return None
    gross = (x - e) if side == "long" else (e - x)
    return round(gross / sd, 4)


def oca_group_for(day_yyyymmdd: str, symbol: str) -> str:
    return f"RVX-{day_yyyymmdd}-{symbol}"
