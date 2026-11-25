"""M5 — zone-assignment asymmetry query (pre-registered 2026-07-23 in
docs/watchlist_known_asymmetries.md; first runnable form 2026-09-13).

Sources: archive + hot wl_event_v1 rows (fixture excluded, the same loader
scripts/radar_outcomes.py uses) joined to logs/alerts/outcomes.jsonl.

THE PRE-REGISTERED POPULATION (what the guard and the rule read):
  COMPLETED instances — a `zone_entered` anchor (earliest by data_asof,
  detected_at) followed by a terminal event (`invalidated` | `removed`) —
  whose anchor zone_dir is long / short (the pre-registration's
  `ANY_VALUE(zone_dir) AS side`). fire_rate / invalidation_rate are the
  radar's own `fired` / `invalidated` events, exactly the SQL's AVG(fired) /
  AVG(invalidated). Instances opened in a fired/extended state with zone_dir
  null are counted and reported as excluded: M5 is about the ZONE rule, and
  they have no zone. Still-open sided instances are shown as context.

SUPPLEMENTARY (printed, clearly labelled, never the guard):
  sided instances with a +10 outcome row (scripts/radar_outcomes.py): win
  rate, mean signed move (% — pooling lanes in ATR units would compare 1h to
  1d ATRs, not long to short), frozen-level breach rate and MAE/MFE at
  +3 / +5 / +10, plus a per-(timeframe, side) breakdown because the lane mix
  differs by side. These rows post-date the pre-registration and are not its
  population; they inform, they do not decide.

GUARD (frozen with the pre-registration): >= 30 COMPLETED instances PER SIDE.
n comes from `src.api.watchlist_squeeze.m5_population_counts` — the same
function that feeds the cockpit's m5_n_long / m5_n_short — and this script
asserts it agrees with its own reconstruction (`consistent_with_cockpit`).
Below the guard this script prints the tables and the guard line and REFUSES
to print any decision-rule language. At or above it, the pre-registered rule
is evaluated on the pre-registered population and printed as a rule reading —
still stamped NO EDGE CLAIM, still an operator-CONFIRM item before any rule
change. This script changes nothing.

Run: .venv/bin/python scripts/radar_m5_query.py [--events-hot F] [--archive-dir D]
     [--outcomes F] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import radar_outcomes as ro                                            # noqa: E402
from src.api.watchlist_squeeze import (M5_HORIZON, M5_MIN_PER_SIDE,   # noqa: E402
                                       M5_TERMINAL, NO_CLAIM_STAMP,
                                       m5_population_counts, outcome_counts)

SIDES = ("long", "short")
HORIZONS = ro.HORIZONS
POPULATION = ("completed instances: zone_entered -> " + "|".join(M5_TERMINAL)
              + ", side = anchor zone_dir, rates from the event log (archive + hot, fixture excluded)")


def load_outcomes(path: Path | None = None) -> list[dict]:
    """wl_outcome_v1 rows; a bad / non-dict / horizon-less line is skipped."""
    p = Path(path) if path is not None else ro.OUTCOMES
    rows = []
    try:
        text = p.read_text()
    except Exception:
        return rows
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
            if (isinstance(r, dict) and r.get("schema") == "wl_outcome_v1"
                    and r.get("state_instance_id")):
                r["horizon"] = int(r["horizon"])
                rows.append(r)
        except Exception:
            continue
    return rows


def _rate(xs) -> float | None:
    xs = list(xs)
    return round(sum(1 for x in xs if x) / len(xs), 3) if xs else None


def _mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def m5_stats(events: list[dict], outcomes: list[dict], counts: dict) -> dict:
    """Everything the report prints, as data. `counts` = the API's
    m5_population_counts() (+ outcome_counts() if merged in)."""
    insts = ro.instances(events)
    sided = {iid: i for iid, i in insts.items() if i["anchor"].get("zone_dir") in SIDES}
    excluded = len(insts) - len(sided)
    # THE pre-registered population: zone_entered anchor + terminal event
    prereg = {iid: i for iid, i in sided.items()
              if i["completed"] and i["anchor"].get("transition") == "zone_entered"}
    still_open = len(sided) - len(prereg)
    by_inst: dict[str, dict[int, dict]] = {}
    for r in outcomes:
        try:
            by_inst.setdefault(str(r["state_instance_id"]), {})[int(r["horizon"])] = r
        except Exception:
            continue
    matured = {iid: i for iid, i in sided.items() if M5_HORIZON in by_inst.get(iid, {})}

    def side_block(pop: dict, with_outcomes: bool) -> dict:
        out = {}
        for side in SIDES:
            ids = [iid for iid, i in pop.items() if i["anchor"]["zone_dir"] == side]
            term = Counter(pop[iid]["terminal"] for iid in ids if pop[iid].get("terminal"))
            blk = {"n": len(ids),
                   "fire_rate": _rate(pop[iid]["fired"] for iid in ids),
                   "invalidation_rate": _rate(pop[iid]["invalidated_by_machine"] for iid in ids),
                   "mixed_bias_entries": sum(1 for iid in ids
                                             if pop[iid]["anchor"].get("bias_d") == "mixed"),
                   "terminal": {t: term.get(t, 0) for t in M5_TERMINAL}}
            if with_outcomes:
                for h in HORIZONS:
                    rows = [by_inst[iid][h] for iid in ids if h in by_inst.get(iid, {})]
                    blk[f"h{h}"] = {"n": len(rows),
                                    "win_rate": _rate(r.get("win") for r in rows),
                                    "mean_signed_move_pct": _mean(r.get("signed_move_pct") for r in rows),
                                    "mean_signed_move_atr": _mean(r.get("signed_move_atr") for r in rows),
                                    "breach_rate": _rate(r.get("breached_by_close") for r in rows),
                                    "mean_mae_atr": _mean(r.get("mae_atr") for r in rows),
                                    "mean_mfe_atr": _mean(r.get("mfe_atr") for r in rows)}
            out[side] = blk
        return out

    lane_side = {}
    for tf in ("1d", "1h"):
        pop = {iid: i for iid, i in matured.items() if i["timeframe"] == tf}
        lane_side[tf] = side_block(pop, with_outcomes=True)
    n_long, n_short = int(counts["m5_n_long"]), int(counts["m5_n_short"])
    guard = {"min_per_side": M5_MIN_PER_SIDE, "population": POPULATION,
             "n_long": n_long, "n_short": n_short,
             "met": n_long >= M5_MIN_PER_SIDE and n_short >= M5_MIN_PER_SIDE}
    # the guard's n MUST equal the pre-registered population reconstructed
    # here — assert it loudly rather than let the cockpit and the query drift
    mine = {s: sum(1 for i in prereg.values() if i["anchor"]["zone_dir"] == s) for s in SIDES}
    guard["consistent_with_cockpit"] = (mine["long"], mine["short"]) == (n_long, n_short)
    return {"stamp": ro.STAMP, "no_claim_stamp": NO_CLAIM_STAMP,
            "instances_total": len(insts), "excluded_null_zone_dir": excluded,
            "sided_total": len(sided), "still_open": still_open,
            "outcomes_recorded": counts.get("outcomes_recorded"),
            "prereg": side_block(prereg, with_outcomes=False),
            "events_only": side_block(sided, with_outcomes=False),
            "matured": side_block(matured, with_outcomes=True),
            "matured_by_lane": lane_side, "guard": guard}


def rule_reading(stats: dict) -> list[str]:
    """The pre-registered decision rule, evaluated on the PRE-REGISTERED
    population (`prereg`). ONLY called when the guard is met — the caller
    enforces that."""
    m = stats["prereg"]
    fl, fs = float(m["long"]["fire_rate"] or 0.0), float(m["short"]["fire_rate"] or 0.0)
    il, is_ = (float(m["long"]["invalidation_rate"] or 0.0),
               float(m["short"]["invalidation_rate"] or 0.0))
    a, b = fs >= fl, is_ <= il
    says = ("loosen the short gate to accept mixed bias" if (a and b)
            else "leave as-is (stricter short gate stands)")
    return [f"PRE-REGISTERED RULE EVALUATION (guard met: long n={stats['guard']['n_long']}, "
            f"short n={stats['guard']['n_short']} completed instances)",
            f"  short fire_rate >= long fire_rate:                 {a}  ({fs} vs {fl})",
            f"  short invalidation_rate <= long invalidation_rate: {b}  ({is_} vs {il})",
            f"  rule says: {says}",
            "  A rule change is an operator CONFIRM item — this script changes nothing. "
            "NO EDGE CLAIM."]


def render(stats: dict) -> str:
    g = stats["guard"]
    L = ["M5 — zone-assignment asymmetry (pre-registered: docs/watchlist_known_asymmetries.md)",
         f"stamp: {stats['stamp']}",
         f"instances: {stats['instances_total']} total, "
         f"{stats['excluded_null_zone_dir']} excluded (anchor zone_dir null — no zone, outside M5), "
         f"{stats['sided_total']} sided = {stats['sided_total'] - stats['still_open']} completed "
         f"(the pre-registered population) + {stats['still_open']} still open",
         f"GUARD (pre-registered): needs >= {g['min_per_side']} COMPLETED instances per side — "
         f"long n={g['n_long']}, short n={g['n_short']} — {'MET' if g['met'] else 'NOT MET'}"
         + ("" if g["consistent_with_cockpit"] else "  ⚠ cockpit count mismatch"),
         f"  population: {g['population']}",
         "", "pre-registered population (completed sided instances, rates from the event log):",
         f"  {'side':6} {'n':>4} {'fire_rate':>10} {'inval_rate':>11} {'mixed_bias':>11} "
         f"{'removed':>8} {'invalid.':>9}"]
    for s in SIDES:
        b = stats["prereg"][s]
        L.append(f"  {s:6} {b['n']:>4} {str(b['fire_rate']):>10} "
                 f"{str(b['invalidation_rate']):>11} {b['mixed_bias_entries']:>11} "
                 f"{b['terminal'].get('removed', 0):>8} {b['terminal'].get('invalidated', 0):>9}")
    L += ["", "all sided instances incl. still-open (context only — not the guard):",
          f"  {'side':6} {'n':>4} {'fire_rate':>10} {'inval_rate':>11} {'mixed_bias':>11}"]
    for s in SIDES:
        b = stats["events_only"][s]
        L.append(f"  {s:6} {b['n']:>4} {str(b['fire_rate']):>10} "
                 f"{str(b['invalidation_rate']):>11} {b['mixed_bias_entries']:>11}")
    L += ["", f"SUPPLEMENTARY — NOT the pre-registered population: sided instances with a "
          f"+{M5_HORIZON} outcome row (outcomes recorded: {stats['outcomes_recorded']}) — "
          "close-to-close, no costs:",
          f"  {'side':6} {'n':>4} {'fire':>6} {'inval':>6}  "
          + "  ".join(f"| +{h}: n win% move% breach%" for h in HORIZONS)]

    def line(label, b):
        cells = []
        for h in HORIZONS:
            x = b.get(f"h{h}", {})
            cells.append(f"| {x.get('n', 0):>3} {_pct(x.get('win_rate')):>5} "
                         f"{_num(x.get('mean_signed_move_pct')):>6} {_pct(x.get('breach_rate')):>6}")
        return (f"  {label:6} {b['n']:>4} {str(b['fire_rate']):>6} "
                f"{str(b['invalidation_rate']):>6}  " + "  ".join(cells))
    for s in SIDES:
        L.append(line(s, stats["matured"][s]))
    L.append("  by lane (ATR units are lane-scaled; compare within a lane):")
    for tf, blk in stats["matured_by_lane"].items():
        for s in SIDES:
            L.append(line(f"{tf}/{s[0]}", blk[s]))
    L.append("")
    if g["met"]:
        L += rule_reading(stats)
    else:
        L.append(f"GUARD NOT MET: long n={g['n_long']}, short n={g['n_short']} completed "
                 f"(needs >= {g['min_per_side']} per side) — the decision rule is NOT "
                 "evaluated and nothing above is a reading of either side. NO EDGE CLAIM.")
    L.append(stats["no_claim_stamp"])
    return "\n".join(L)


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _num(x) -> str:
    return "—" if x is None else f"{x:+.2f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--events-hot", type=Path, default=None)
    ap.add_argument("--archive-dir", type=Path, default=None)
    ap.add_argument("--outcomes", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    events, _ = ro.load_events(a.events_hot, a.archive_dir)
    counts = {**m5_population_counts(a.events_hot, a.archive_dir), **outcome_counts(a.outcomes)}
    stats = m5_stats(events, load_outcomes(a.outcomes), counts)
    print(json.dumps(stats, indent=1) if a.json else render(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
