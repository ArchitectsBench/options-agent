"""`causal_first10` — the allocation the lane executes and the nightly
comparator must replicate (spec §3). A pure, config-free module shared by
the session simulator's agreement test and `analysis/rvol_exec/causal_first10_sim.py`.
UNVALIDATED — PAPER ONLY.

Rules pinned by §3: (a) candidates = the lane's rank list minus its skips;
(b) ordering = crossing bar ascending, tie -> rank; (c) a slot is occupied at
the first execution; (d) remaining candidates are cancelled at the 10th
crossing's bar; (e) exit at the 15:55 bar OPEN (the close-exit view is also
returned); (f) window EXCLUSIVE of the 10:25 bar; (g) `entry_start` per name
from `placed_at_et` (`rules.entry_start_hm`).

`replay_k18_trigger_only` keeps `scripts/rvolrank_sleeve.py::replay_k18`'s
trigger, guardrail and stop mechanics line-for-line — including the hard stop
at the BASE-tier slipped fill (`fill_b = raw_fill*(1 ± 5 bps)`, `hard = fill_b
∓ stop_dist`; replay_k18's stop path is tier-invariant, only `r_tiers`
re-costs the same raw exit). Only the window end and the 15:55 exit price
differ, exactly as D24 records. Pinned by
`tests/rvol_exec/test_lane_rules.py::test_trigger_only_reproduces_replay_k18_on_the_fixture`
(every name of the vendored 2026-09-11 day, `entry_end_exclusive="10:30"`).
"""

from __future__ import annotations

import pandas as pd

from src.rvol_exec.constants import DOJI_MIN_BODY, GUARD_FRAC, PART_CAP, RISK_USD, SLOTS, STOP_ATR_FRAC

# frozen cost tiers (config/rvolrank_forward_criteria.yaml A1 item 3), copied
# so this module reads no config at runtime
BASE_SLIP = 5 / 10_000                      # replay_k18's SLIP: the stop hangs off the base-slipped fill
COST_TIERS = {"base": {"slippage_bps": 5, "per_share_side": 0.005},
              "stress_2x": {"slippage_bps": 10, "per_share_side": 0.010},
              "stress_3x": {"slippage_bps": 15, "per_share_side": 0.015}}


def replay_k18_trigger_only(bars: pd.DataFrame, atr: float, entry_start: str = "09:35",
                            entry_end_exclusive: str = "10:25") -> dict | None:
    b = bars.set_index("hm")
    if "09:30" not in b.index:
        return None
    orb = b.loc["09:30"]
    if isinstance(orb, pd.DataFrame):
        orb = orb.iloc[0]
    rng = float(orb["h"] - orb["l"])
    body = abs(float(orb["c"] - orb["o"]))
    if rng <= 0 or body < DOJI_MIN_BODY * rng:
        return None
    long_side = bool(orb["c"] > orb["o"])
    level = float(orb["h"]) + 0.01 if long_side else float(orb["l"]) - 0.01
    stop_dist = STOP_ATR_FRAC * float(atr)
    if stop_dist <= 0:
        return None
    sig = b[(b.index >= entry_start) & (b.index < entry_end_exclusive)]
    raw_fill = fill_t = None
    for tt, row in sig.iterrows():
        crossed = row["h"] >= level if long_side else row["l"] <= level
        if not crossed:
            continue
        raw = max(level, float(row["o"])) if long_side else min(level, float(row["o"]))
        if abs(raw - level) > GUARD_FRAC * stop_dist:
            return None                                       # cancel guardrail
        raw_fill = raw
        fill_t = tt
        break
    if raw_fill is None:
        return None
    # the hard stop hangs off the BASE-tier slipped fill, as in replay_k18
    fill_b = raw_fill * (1 + BASE_SLIP) if long_side else raw_fill * (1 - BASE_SLIP)
    hard = fill_b - stop_dist if long_side else fill_b + stop_dist
    live = b[b.index > fill_t]
    exit_open = exit_close = None
    reason = "eod"
    exit_t = None
    for tt, row in live.iterrows():
        if tt >= "15:55":
            exit_open, exit_close, exit_t = float(row["o"]), float(row["c"]), tt
            break
        hit = row["l"] <= hard if long_side else row["h"] >= hard
        if hit:
            px = min(hard, float(row["o"])) if long_side else max(hard, float(row["o"]))
            exit_open = exit_close = px
            reason, exit_t = "stop", tt
            break
    if exit_open is None:
        last = float(live["c"].iloc[-1]) if len(live) else raw_fill
        exit_open = exit_close = last
    return {"fill_t": fill_t, "level": round(level, 4), "raw_fill": raw_fill, "fill_base": round(fill_b, 6),
            "hard": round(hard, 4),
            "stop_dist": stop_dist, "side": "L" if long_side else "S", "reason": reason,
            "exit_t": exit_t, "exit_open": exit_open, "exit_close": exit_close,
            "open_vol": float(orb["v"])}


def net_r(side_long: bool, raw_fill: float, raw_exit: float, stop_dist: float, tier: str) -> float:
    t = COST_TIERS[tier]
    slip, per_share = t["slippage_bps"] / 10000.0, t["per_share_side"]
    f = raw_fill * (1 + slip) if side_long else raw_fill * (1 - slip)
    x = raw_exit * (1 - slip) if side_long else raw_exit * (1 + slip)
    gross = (x - f) if side_long else (f - x)
    return round((gross - 2 * per_share) / stop_dist, 4)


def replay_causal_first10(rank_rows, bars_by_name: dict, atr_by_name: dict, skips,
                          slots: int = SLOTS, entry_start_of=None) -> tuple[list[dict], list[tuple]]:
    """`rank_rows`: dicts with `ticker`, `rank` and (optionally) `entry_start`
    (else `entry_start_of(row)` else "09:35"). `skips`: tickers excluded BEFORE
    allocation. Returns (taken, cancelled) — taken rows carry `r_tiers` for the
    open-exit view and `r_tiers_close` for the close-exit view."""
    skips = set(skips or [])
    cands = []
    for row in rank_rows:
        t = row["ticker"]
        if t in skips or t not in bars_by_name or atr_by_name.get(t) in (None, 0):
            continue
        es = row.get("entry_start") or (entry_start_of(row) if entry_start_of else "09:35")
        tr = replay_k18_trigger_only(bars_by_name[t], float(atr_by_name[t]), entry_start=es)
        if tr is None:
            continue
        cands.append((tr["fill_t"], int(row["rank"]), t, tr))
    cands.sort(key=lambda x: (x[0], x[1]))
    taken_raw = cands[:slots]
    cut_bar = taken_raw[-1][0] if len(taken_raw) == slots else None
    taken = []
    for fill_t, rank, t, tr in taken_raw:
        long_side = tr["side"] == "L"
        shares = min(RISK_USD / tr["stop_dist"], PART_CAP * tr["open_vol"])
        out = dict(tr, ticker=t, rank=rank, shares=round(shares, 1),
                   r_tiers={k: net_r(long_side, tr["raw_fill"], tr["exit_open"], tr["stop_dist"], k)
                            for k in COST_TIERS},
                   r_tiers_close={k: net_r(long_side, tr["raw_fill"], tr["exit_close"], tr["stop_dist"], k)
                                  for k in COST_TIERS})
        taken.append(out)
    cancelled = [(c[2], "cancelled_slots", cut_bar) for c in cands[slots:]]
    return taken, cancelled


TRADE_FIELDS = ("ticker", "rank", "side", "fill_t", "level", "raw_fill", "hard", "stop_dist", "reason",
                "exit_t", "exit_open", "exit_close", "shares", "r_tiers", "r_tiers_close")


def summarize(taken: list[dict], tier: str = "stress_2x") -> dict:
    n = len(taken)
    return {"n": n, "cut_bar": None,
            "mean_R_open_exit": round(sum(t["r_tiers"][tier] for t in taken) / n, 4) if n else None,
            "mean_R_close_exit": round(sum(t["r_tiers_close"][tier] for t in taken) / n, 4) if n else None,
            "names": [t["ticker"] for t in taken],
            # per-trade rows the §20 scorer aligns on (analysis/rvol_exec/exec_gap_scorer.py)
            "trades": [{k: t.get(k) for k in TRADE_FIELDS} for t in taken]}
