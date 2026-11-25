"""Winner Give-Back Study — G1 reconstruction (schema gb_v1, 4 marks).

Reads control post-mortems (truth-positive winners) + re-fetches leg
quotes for net-mid path AND per-minute half-spreads. Truth-only. Writes
logs/give_back/<rec_id>.json. Reusable on MFE>=10% losers for the G2
rescue table (--losers, mark 3). Never mutates sources.

Marks: (1) friction = max(0, friction_at_peak - friction_at_exit) — both
P&L figures are mid-based so only the spread DIFFERENCE is a real haircut;
per-side config fallback when a leg quote is missing; friction_source
records the mix. (2) sustained_mfe = peak of rolling-3min-median; below
floor OR short-hold -> dollars-only, never substitute raw MFE. (3)
minutes_entry_to_peak + minutes_peak_to_exit. (4) trigger_level_usd from
booked_entry_fill; trigger_level_basis="booked_entry".
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import importlib

enr = importlib.import_module("enrich_trades_thetadata")
CFG = yaml.safe_load((ROOT / "config" / "give_back.yaml").read_text())["give_back"]
OUT = ROOT / "logs" / "give_back"
WIN = CFG["sustained_window_min"]
FLOOR = CFG["efficiency_floor_pct"]
FB = CFG["friction_fallback_pct_of_mid"]

PRICE_DEP = {"target_50", "stop", "max_loss"}
SIGNAL_DEP = {"vwap_invalidation"}
TIME_DEP = {"flatten_1545"}


def leg_series(legs, dates):
    """Per-minute: net_mid, half_sum, n_quoted, n_legs, per key."""
    rows = {}
    n_legs = len(legs)
    for leg in legs:
        occ = enr.parse_occ(leg["symbol"])
        if occ is None:
            return None
        sign = 1 if str(leg.get("side", "")).startswith("buy") else -1
        frames = []
        for d in dates:
            q = enr.td.option_quotes(occ["symbol"], occ["expiration"],
                                     occ["strike"], occ["right"], d)
            if q is None or q.empty:
                continue
            q = q[(q["bid"] > 0) & (q["ask"] >= q["bid"])]
            if q.empty:
                continue
            q = q.assign(key=d + "T" + q["timestamp"].str[11:16],
                         mid=(q["bid"] + q["ask"]) / 2,
                         half=(q["ask"] - q["bid"]) / 2)
            frames.append(q[["key", "mid", "half"]])
        if not frames:
            continue
        lg = pd.concat(frames).groupby("key").last()
        med_mid = float(lg["mid"].median())
        for key, r in lg.iterrows():
            slot = rows.setdefault(key, {"net_mid": 0.0, "half": 0.0,
                                         "nq": 0, "fb": 0.0})
            slot["net_mid"] += sign * r["mid"]
            slot["half"] += r["half"]
            slot["nq"] += 1
        rows.setdefault("__legmed__", {})[leg["symbol"]] = med_mid
    return rows, n_legs


def friction_at(rows, key, legs, n_legs, qty):
    """Half-spread cost to close the structure at `key`, per-side fallback."""
    slot = rows.get(key)
    quoted = slot and slot.get("nq", 0) == n_legs
    if quoted:
        return round(slot["half"] * 100 * qty, 2), "quoted"
    # fallback per missing side
    legmed = rows.get("__legmed__", {})
    half = (slot["half"] if slot else 0.0)
    missing = n_legs - (slot["nq"] if slot else 0)
    for _ in range(missing):
        avg_med = np.mean(list(legmed.values())) if legmed else 1.0
        half += FB * avg_med
    src = "config_fallback" if not slot or slot.get("nq", 0) == 0 else "mixed"
    return round(half * 100 * qty, 2), src


def build(pm: dict, is_winner: bool, legsmap: dict) -> dict | None:
    rid = pm["meta"]["rec_id"]
    legs = legsmap.get(rid)
    lc = pm["lifecycle"]
    risk = ((pm["entry_snapshot"]["recorded"].get("risk") or {}).get("max_dollar_risk")
            or None)
    if risk is None:
        tb = pm["meta"]
        e_mid = lc.get("truth_mid_entry")
        risk = float(e_mid) * 100 * (pm.get("_qty") or 1) if e_mid else None
    qty = (pm["entry_snapshot"]["recorded"].get("risk") or {}).get("quantity") or 1
    if not legs or not risk:
        return None
    entry_ts, exit_ts = lc.get("entry_ts", ""), lc.get("exit_ts", "")
    dates = [entry_ts[:10]]
    if exit_ts[:10] and exit_ts[:10] != entry_ts[:10]:
        dates.append(exit_ts[:10])
    res = leg_series(legs, dates)
    if res is None:
        return None
    rows, n_legs = res
    e_mid = lc.get("truth_mid_entry")
    ek = f"{entry_ts[:10]}T{enr.pt_to_et_minute(entry_ts)}"
    xk = f"{exit_ts[:10]}T{enr.pt_to_et_minute(exit_ts)}"
    path = [(k, v["net_mid"]) for k, v in sorted(rows.items())
            if k != "__legmed__" and ek <= k <= xk]
    if len(path) < 2:
        return None
    keys = [k for k, _ in path]
    mids = np.array([m for _, m in path])
    pnl = (mids - e_mid) * 100 * qty
    truth_exit = float(pnl[-1])
    raw_mfe = float(pnl.max())
    raw_mfe_i = int(pnl.argmax())

    # sustained (rolling-3min median), mark 2
    sustained_ev = len(pnl) >= WIN
    if sustained_ev:
        med = pd.Series(pnl).rolling(WIN, center=True, min_periods=WIN).median()
        s_i = int(med.idxmax()) if med.notna().any() else raw_mfe_i
        sustained = float(med.max()) if med.notna().any() else raw_mfe
        peak_key = keys[s_i]
    else:
        sustained, peak_key = None, keys[raw_mfe_i]

    denom = sustained if sustained_ev else None
    s_pct = round(denom / risk * 100, 1) if denom is not None else None
    computable = bool(denom is not None and s_pct is not None and s_pct >= FLOOR)

    fr_peak, src_p = friction_at(rows, peak_key, legs, n_legs, qty)
    fr_exit, src_x = friction_at(rows, xk, legs, n_legs, qty)
    haircut = round(max(0.0, fr_peak - fr_exit), 2)     # mark 1
    gross = round((denom if denom is not None else raw_mfe) - truth_exit, 2)
    net_cap = round(max(0.0, gross - haircut), 2)
    src = "quoted" if src_p == "quoted" == src_x else (
        "config_fallback" if "config_fallback" in (src_p, src_x) and "quoted" not in (src_p, src_x)
        else "mixed")

    def mins(a, b):
        try:
            from datetime import datetime as _dt
            return int((_dt.fromisoformat(b.replace("T", " ").replace("-", "-", 2))
                        - _dt.fromisoformat(a.replace("T", " "))).total_seconds() / 60)
        except Exception:
            return None
    e2p = keys.index(peak_key)
    p2x = len(keys) - 1 - keys.index(peak_key)

    trig = lc.get("exit_trigger", "unknown")
    dep = ("price_dependent" if trig in PRICE_DEP else
           "signal_dependent" if trig in SIGNAL_DEP else
           "time_dependent" if trig in TIME_DEP else "unknown")
    booked_entry = lc.get("booked_entry_fill")
    trig_level = trig_reached = None
    if dep == "price_dependent" and booked_entry:
        if trig == "target_50":
            trig_level = round(booked_entry * 1.50, 4)
        peak_struct_mid = float(mids.max())
        trig_reached = bool(trig_level is not None and peak_struct_mid >= trig_level)

    return {
        "schema_version": "gb_v1",
        "meta": {"rec_id": rid, "lane": pm["meta"]["lane"],
                 "ticker": pm["meta"]["ticker"], "strategy": pm["meta"].get("strategy"),
                 "dte_at_entry": pm["meta"].get("dte_at_entry"),
                 "is_winner": is_winner, "control": pm.get("control", False)},
        "config_used": {"sustained_window_min": WIN, "efficiency_floor_pct": FLOOR,
                        "friction_fallback_pct_of_mid": FB, "thresholds_version": "gb_v1"},
        "risk": {"max_dollar_risk": round(float(risk), 2)},
        "path": {"truth_pnl_at_exit": round(truth_exit, 2),
                 "raw_mfe_usd": round(raw_mfe, 2),
                 "raw_mfe_pct_of_risk": round(raw_mfe / risk * 100, 1),
                 "sustained_mfe_usd": (round(sustained, 2) if sustained is not None else None),
                 "sustained_mfe_pct_of_risk": s_pct,
                 "sustained_evaluable": sustained_ev,
                 "sustained_mfe_ts": peak_key,
                 "minutes_entry_to_peak": e2p, "minutes_peak_to_exit": p2x},
        "give_back": {"gross_usd": gross,
                      "gross_pct_of_risk": round(gross / risk * 100, 1),
                      "friction_at_peak_usd": fr_peak, "friction_at_exit_usd": fr_exit,
                      "friction_haircut_usd": haircut, "friction_source": src,
                      "net_capturable_usd": net_cap,
                      "net_capturable_pct_of_risk": round(net_cap / risk * 100, 1)},
        "efficiency": {"computable": computable, "floor_pct": FLOOR,
                       "value": (round(truth_exit / denom, 3)
                                 if computable and denom else None),
                       "note": (None if computable else
                                ("short_hold_below_window" if not sustained_ev
                                 else "dollars_only_below_floor"))},
        "exit": {"trigger": trig, "dependency": dep,
                 "trigger_level_usd": trig_level, "trigger_level_basis": "booked_entry",
                 "truth_reached_trigger": trig_reached},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--losers", action="store_true", help="G2 rescue table: MFE>=10% losers")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    legsmap = enr.fallback_legs_map()
    if args.losers:
        src = glob.glob(str(ROOT / "logs/post_mortems/*.json"))
        is_win = False
        subdir = OUT / "losers"
    else:
        src = glob.glob(str(ROOT / "logs/post_mortems/controls/*.json"))
        is_win = True
        subdir = OUT
    subdir.mkdir(parents=True, exist_ok=True)
    n = 0; summ = {"evaluable_eff": 0, "below_floor": 0, "short_hold": 0,
                   "friction": {}, "dep": {}, "trig_reached": {"true": 0, "false": 0, "na": 0}}
    for f in src:
        if "phase3_results" in f or "give_back" in f:
            continue
        pm = json.load(open(f))
        if pm.get("meta", {}).get("lane") is None:
            continue
        if args.losers:
            mfe = pm["lifecycle"].get("mfe_pct_of_max_risk")
            if mfe is None or mfe < FLOOR:
                continue
        gb = build(pm, is_win, legsmap)
        if gb is None:
            continue
        (subdir / f"{gb['meta']['rec_id']}.json").write_text(json.dumps(gb, indent=1))
        n += 1
        eff = gb["efficiency"]
        if eff["computable"]:
            summ["evaluable_eff"] += 1
        elif eff["note"] == "short_hold_below_window":
            summ["short_hold"] += 1
        else:
            summ["below_floor"] += 1
        summ["friction"][gb["give_back"]["friction_source"]] = \
            summ["friction"].get(gb["give_back"]["friction_source"], 0) + 1
        summ["dep"][gb["exit"]["dependency"]] = summ["dep"].get(gb["exit"]["dependency"], 0) + 1
        tr = gb["exit"]["truth_reached_trigger"]
        summ["trig_reached"]["na" if tr is None else ("true" if tr else "false")] += 1
        if args.limit and n >= args.limit:
            break
    print(f"give-back records: {n} ({'losers' if args.losers else 'winners'})")
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
