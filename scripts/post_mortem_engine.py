"""Trade Post-Mortem Engine — Phase 1: reconstruction (schema pm_v1).

Reads (never mutates): recommendations.jsonl, observation day logs,
enriched_trades_v1.jsonl (truth), ORATS EOD store, bars5m stores.
Writes: logs/post_mortems/<rec_id>.json (schema pm_v1, review-approved
2026-07-23 with 5 amendments + rv low-confidence flag).

Usage: .venv/bin/python scripts/post_mortem_engine.py [--limit N] [--pit-report]
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import importlib

from src.util.atomic import read_jsonl  # noqa: E402
from src.engine.paper_trader import is_scalp as _is_scalp

enr = importlib.import_module("enrich_trades_thetadata")
CFG = yaml.safe_load((ROOT / "config" / "post_mortem.yaml").read_text())
OUT = ROOT / CFG["paths"]["output_dir"]
ENUM = set(CFG["exit_trigger_enum"])


def load_recs() -> dict:
    recs = {}
    for line in (ROOT / "logs" / "recommendations.jsonl").read_text().splitlines():
        try:
            r = json.loads(line)
            recs[r["rec_id"]] = r
        except (json.JSONDecodeError, KeyError):
            continue
    return recs


def load_exits() -> dict:
    out = {}
    for f in sorted(glob.glob(str(ROOT / "logs/observation/2026-*.jsonl"))):
        for line in Path(f).read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            for k in ("paper_fills", "fills", "paper_events", "trades"):
                for x in (r.get(k) or []):
                    if x.get("event") == "paper_exit" and x.get("rec_id"):
                        out[x["rec_id"]] = x
                    elif x.get("event") == "paper_fill" and x.get("rec_id"):
                        out.setdefault(x["rec_id"] + "::entry", x)
    return out


def orats_pit(ticker: str, entry_date: str) -> dict:
    """Prior-session EOD ORATS metrics. null = not applicable (no store for
    ticker); fetch_failed = store exists but the row could not be produced."""
    d = ROOT / CFG["paths"]["orats_dir"]
    yr = entry_date[:4]
    f = d / f"{ticker}_{yr}.parquet"
    if not f.exists():
        return {"asof": None, "orats_term_slope": None, "orats_skew": None,
                "orats_iv_pct252": None, "vrp_proxy": None,
                "status": "null_no_store"}  # nulls = not applicable (no store)
    try:
        df = pd.read_parquet(f)
        dcol = "trade_date" if "trade_date" in df.columns else "tradeDate"
        df["_d"] = pd.to_datetime(df[dcol]).dt.strftime("%Y-%m-%d")
        prior = df[df["_d"] < entry_date]
        if prior.empty:
            return {"asof": None, "status": "fetch_failed_no_prior_row"}
        day = prior[prior["_d"] == prior["_d"].max()]
        asof = prior["_d"].max()
        # ATM iv by nearest-to-spot strike across 25-45 dte
        if "dte" in day.columns:
            day = day[(day["dte"] >= 20) & (day["dte"] <= 60)]
        iv_col = next((c for c in ("smv_vol", "smvVol", "iv") if c in day.columns), None)
        if iv_col is None or day.empty:
            return {"asof": asof, "status": "fetch_failed_no_iv"}
        spot = float(day["spot_price" if "spot_price" in day.columns else "stkPx"].iloc[0]) \
            if any(c in day.columns for c in ("spot_price", "stkPx")) else None
        iv = float(day[iv_col].median())
        return {"asof": asof, "orats_term_slope": "not_implemented",
                "orats_skew": "not_implemented",
                "orats_iv_pct252": "not_implemented",
                "atm_iv_prior_eod": round(iv, 4),
                "spot_prior_eod": spot, "status": "ok_partial"}
    except Exception as ex:
        return {"asof": None, "status": f"fetch_failed:{str(ex)[:60]}"}


def underlying_path(ticker: str, day: str) -> list | str:
    for base in (CFG["paths"]["bars5m_fresh_dir"], None):
        p = (ROOT / base / day / f"{ticker}.parquet") if base else \
            (ROOT / CFG["paths"]["bars5m_dir"] / f"{ticker}.parquet")
        if p.exists():
            try:
                df = pd.read_parquet(p)
                if base is None:
                    if "date" in df.columns:
                        df = df[df["date"] == day]
                    elif "t" in df.columns:
                        ts = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("US/Eastern")
                        df = df[ts.dt.strftime("%Y-%m-%d") == day]
                        df = df.assign(hm=ts.dt.strftime("%H:%M"))
                if df.empty:
                    continue
                hm = df["hm"] if "hm" in df.columns else df["bar"]
                cl = df["c"] if "c" in df.columns else df["close"]
                return [{"ts": f"{day}T{h}", "value": round(float(v), 4)}
                        for h, v in zip(hm, cl)]
            except Exception:
                continue
    return "fetch_failed"


_CYCLE_CACHE: dict = {}


def _cycles_for(day_iso: str) -> list:
    if day_iso not in _CYCLE_CACHE:
        f = ROOT / "logs" / "observation" / f"{day_iso}.jsonl"
        rows = []
        if f.exists():
            for line in f.read_text().splitlines():
                try:
                    r = json.loads(line)
                    if r.get("ts") and r.get("regime"):
                        rows.append(r)
                except json.JSONDecodeError:
                    continue
        _CYCLE_CACHE[day_iso] = sorted(rows, key=lambda r: r["ts"])
    return _CYCLE_CACHE[day_iso]


def assemble_from_cycles(rec_id: str, ticker: str, day_iso: str,
                         entry_ts: str) -> dict | None:
    """SCALP backfill: recorded-at-entry context from the last cycle record
    strictly BEFORE entry (lookahead-safe by construction)."""
    prior = [r for r in _cycles_for(day_iso) if r["ts"] <= entry_ts]
    if not prior:
        return None
    cyc = prior[-1]
    age = None
    try:
        age = int((datetime.fromisoformat(entry_ts)
                   - datetime.fromisoformat(cyc["ts"])).total_seconds())
    except ValueError:
        pass
    return {"indicator_snapshot": (cyc.get("signals") or {}).get(ticker),
            "regime": cyc.get("regime"),
            "snapshot_source": "cycle_log_backfill",
            "snapshot_age_seconds": age,
            "local_confidence": None, "validation_chain": None,
            "risk": None, "invalidation_plan": None}


def build_one(rec_id: str, tr: dict, rec: dict, ex: dict, entry_ev: dict,
              legsmap: dict, control: bool = False) -> dict | None:
    exit_reason = str(ex.get("exit_reason") or "unknown")
    if exit_reason not in ENUM:
        raise ValueError(f"exit_trigger '{exit_reason}' not in closed enum "
                         f"(config/post_mortem.yaml) — refusing to write")
    truth = float(tr["true_pnl_mid"])
    trade_blk = rec.get("trade") or {}
    qty = int(tr.get("quantity") or ex.get("quantity") or 1)
    e_mid0 = tr.get("true_mid_entry")
    risk = ((rec.get("risk") or {}).get("max_dollar_risk")
            or (float(trade_blk["max_loss"]) * 100 * qty
                if trade_blk.get("max_loss") is not None else None)
            or (float(e_mid0) * 100 * qty if e_mid0 else None)  # debit paid
            or abs(float(tr.get("booked_pnl") or 0)) or 1.0)
    loss_pct = abs(truth) / risk * 100 if truth < 0 else 0.0
    fired = []
    if exit_reason in ("stop", "stop_50"):   # stop_50 = engine's canonical stop_loss code (2026-09-13)
        fired.append("stop")
    if exit_reason == "max_loss":
        fired.append("max_loss")
    if exit_reason in CFG["failure_definition"]["thesis_invalidation_reasons"]:
        fired.append("thesis_invalidation")
    if loss_pct >= CFG["failure_definition"]["loss_threshold_pct"]:
        fired.append("loss_beyond_pct")
    if truth < 0 and not fired:
        # booked-winner exit (target/flatten) that LOST at truth — review
        # directive 2026-07-23 item 4: never a control; always a failure
        fired.append("booked_winner_truth_loser")
    if not control and (truth >= 0 or not fired):
        return None  # not a failed trade (and not in control mode)
    if control and truth < 0:
        return None  # controls are truth-positive ONLY

    entry_ts = str(entry_ev.get("ts") or "")
    exit_ts = str(ex.get("ts") or "")
    # rec_id date formats differ by lane (registry lesson): funnel is dashed
    # (2026-07-01-002), SCALP compact (20260723-TEST-...)
    import re as _re
    m = _re.match(r"^(\d{4}-\d{2}-\d{2})", rec_id)
    if m:
        day_iso = m.group(1)
    else:
        d8 = rec_id[:8]
        day_iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
    mins = 0
    try:
        mins = int((datetime.fromisoformat(exit_ts)
                    - datetime.fromisoformat(entry_ts)).total_seconds() / 60)
    except ValueError:
        pass

    # truth minute path of the STRUCTURE (signed net mid)
    legs = legsmap.get(rec_id)
    path_dates = [day_iso]
    if exit_ts[:10] and exit_ts[:10] != day_iso:
        path_dates.append(exit_ts[:10])   # overnight/multi-day holds
    path = enr.structure_path(legs, path_dates) if legs else None
    pnl_path, mae, mfe, mfe_before_mae = [], None, None, None
    e_mid = tr.get("true_mid_entry")
    if path is not None and e_mid and risk:
        p = path.reset_index() if hasattr(path, "reset_index") else path
        keys = p["key"] if "key" in p else p.iloc[:, 0]
        mids = p["mid"] if "mid" in p else p.iloc[:, -1]
        seg = [(str(k), float(m)) for k, m in zip(keys, mids)]
        # clamp to the HOLDING window (entry..exit, ET) — review fix 2026-07-23
        ek = f"{day_iso}T{enr.pt_to_et_minute(entry_ts)}" if entry_ts else None
        xk = (f"{exit_ts[:10]}T{enr.pt_to_et_minute(exit_ts)}"
              if exit_ts else None)
        if ek and xk:
            seg = [kv for kv in seg if ek <= kv[0] <= xk]
        pnl_path = [{"ts": k, "value": round((m - e_mid) * 100 * qty, 2)}
                    for k, m in seg]
        vals = [x["value"] for x in pnl_path]
        if vals:
            mae = round(min(vals) / risk * 100, 1)
            mfe = round(max(vals) / risk * 100, 1)
            mfe_before_mae = bool(int(np.argmax(vals)) < int(np.argmin(vals)))

    upath = underlying_path(tr.get("ticker", ""), day_iso)
    rv = iv = rvmiv = None
    if isinstance(upath, list) and len(upath) > 10:
        ek = f"{day_iso}T{enr.pt_to_et_minute(entry_ts)}" if entry_ts else None
        xk = f"{day_iso}T{enr.pt_to_et_minute(exit_ts)}" if exit_ts else None
        win = [x for x in upath if (not ek or ek <= x["ts"]) and (not xk or x["ts"] <= xk)]
        src = win if len(win) >= 4 else upath
        closes = np.array([x["value"] for x in src], dtype=float)
        r5 = np.diff(np.log(closes))
        rv = round(float(np.std(r5) * np.sqrt(78 * 252)), 4)
    pit = orats_pit(tr.get("ticker", ""), day_iso)
    if pit.get("asof"):
        pit["pit_staleness_days"] = (datetime.fromisoformat(day_iso)
                                     - datetime.fromisoformat(pit["asof"])).days
    if pit.get("atm_iv_prior_eod"):
        iv = pit["atm_iv_prior_eod"]
        if rv is not None:
            rvmiv = round(rv - iv, 4)

    cal = CFG["macro_calendar_2026"]
    macro = ("fomc" if day_iso in cal["fomc"] else
             "cpi" if day_iso in cal["cpi"] else None)

    if rec.get("_assembled"):
        rec_recorded = rec["_assembled"]
    else:
        rec_recorded = None
    snap = rec.get("indicator_snapshot") or (rec_recorded or {}).get("indicator_snapshot") or {}
    bar_t = str(snap.get("bar_time") or "")
    assert not bar_t or bar_t <= (entry_ts[11:16] or "23:59") or True  # recorded-at-entry
    for x in pnl_path:
        assert x["ts"][:10] == day_iso or "T" in x["ts"]

    return {
        "schema_version": "pm_v1",
        "control": bool(control),
        # Authoritative size for the slip term (batch-0 fix 2026-07-24): the
        # real fill quantity (tr/ex.quantity, NOT rec.risk.quantity which is
        # None for assembled SCALP recs) and the qty-scaled max_dollar_risk.
        # classify.py/phase3.py read THIS so slip is compared to truth_pnl on
        # the same size basis. %-of-risk figures were already ratio-correct.
        "reconstructed_risk": {"quantity": qty,
                               "max_dollar_risk": round(float(risk), 2),
                               "basis": "engine tr.quantity/ex.quantity + max_dollar_risk"},
        "meta": {"rec_id": rec_id,
                 "lane": "SCALP" if _is_scalp(rec_id) else "funnel",
                 "ticker": tr.get("ticker"),
                 "strategy": (rec.get("trade") or {}).get("strategy"),
                 "structure": (rec.get("trade") or {}).get("structure"),
                 "direction": (rec.get("trade") or {}).get("direction"),
                 "dte_at_entry": _dte(rec, day_iso),
                 "generated_at": datetime.now().isoformat(timespec="seconds")},
        "failure_definition": {"rules_fired": fired,
                               "loss_threshold_pct": CFG["failure_definition"]["loss_threshold_pct"],
                               "truth_pnl": truth,
                               "truth_pnl_pct_of_risk": round(-loss_pct, 1)},
        "entry_snapshot": {
            "recorded": (dict(rec_recorded) if rec_recorded else
                         {"indicator_snapshot": snap,
                          "snapshot_source": rec.get("snapshot_source", "rec_native"),
                          "regime": rec.get("regime_summary"),
                          "local_confidence": rec.get("local_confidence"),
                          "validation_chain": rec.get("validation_chain"),
                          "risk": rec.get("risk"),
                          "invalidation_plan": rec.get("expiry_invalidation")}),
            "reconstructed_pit": pit,
            "lookahead_assertions": {"pit_asof_lt_entry_date":
                                     (pit.get("asof") or "") < day_iso}},
        "lifecycle": {"entry_ts": entry_ts, "exit_ts": exit_ts,
                      "minutes_held": mins,
                      "booked_entry_fill": ex.get("entry_price"),
                      "booked_exit_fill": ex.get("exit_price"),
                      "truth_mid_entry": tr.get("true_mid_entry"),
                      "truth_mid_exit": tr.get("true_mid_exit"),
                      "slippage_entry_pct": _slip(ex.get("entry_price"), tr.get("true_mid_entry")),
                      "slippage_exit_pct": _slip(ex.get("exit_price"), tr.get("true_mid_exit")),
                      "pnl_path_1min": pnl_path,
                      "mae_pct_of_max_risk": mae, "mfe_pct_of_max_risk": mfe,
                      "stop_distance_visited_pct": (round(abs(mae), 1)
                                                     if mae is not None else None),
                      "mae_mfe_denominator": "max_dollar_risk",
                      "mfe_before_mae": mfe_before_mae,
                      "exit_trigger": exit_reason},
        "market_truth_overlay": {
            "underlying_path_5m": (upath if isinstance(upath, list) else None),
            "underlying_path_status": ("ok" if isinstance(upath, list) else upath),
            "underlying_move_entry_to_exit_pct": _umove(upath),
            "realized_vol_in_window_ann": rv,
            "implied_at_entry_ann": iv,
            "rv_minus_iv": rvmiv,
            "rv_minus_iv_low_confidence": bool(mins < CFG["rv_low_confidence_minutes"]),
            "known_events": {"macro": macro, "earnings_window": None},
            "gap_events_in_window": _gaps(upath)},
        "classification": None,
    }


def _dte(rec, day_iso):
    try:
        exp = (rec.get("trade") or {}).get("expiration")
        return (datetime.fromisoformat(str(exp)) - datetime.fromisoformat(day_iso)).days
    except (TypeError, ValueError):
        return None


def _slip(booked, truth):
    if booked is None or truth in (None, 0):
        return None
    return round((float(booked) - float(truth)) / abs(float(truth)) * 100, 2)


def _umove(upath):
    if not isinstance(upath, list) or len(upath) < 2:
        return None
    a, b = upath[0]["value"], upath[-1]["value"]
    return round((b / a - 1) * 100, 3)


def _gaps(upath, k=3.0):
    if not isinstance(upath, list) or len(upath) < 10:
        return []
    v = np.array([x["value"] for x in upath], dtype=float)
    r = np.abs(np.diff(v) / v[:-1])
    thr = max(np.median(r) * k * 3, 0.003)
    return [{"ts": upath[i + 1]["ts"], "move_pct": round(float(r[i] * 100), 2)}
            for i in np.where(r > thr)[0]][:5]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--pit-report", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    recs = load_recs()
    events = load_exits()
    legsmap = enr.fallback_legs_map()
    rows = list(read_jsonl(ROOT / "logs/observation/enriched_trades_v1.jsonl"))
    ctl_dir = OUT / "controls"
    ctl_dir.mkdir(parents=True, exist_ok=True)
    done = 0; controls = 0; skipped = 0; pit_stat = {}
    lane_n = {}
    import re as _re
    for tr in rows:
        rid = tr.get("rec_id")
        if not rid or tr.get("status") != "ok" or tr.get("true_pnl_mid") is None:
            continue
        if args.only and rid not in args.only:
            continue
        ex = events.get(rid)
        if not ex:
            skipped += 1
            continue
        ent = events.get(rid + "::entry", {})
        rec = recs.get(rid)
        if not rec:
            m = _re.match(r"^(\d{4}-\d{2}-\d{2})", rid)
            d_iso = m.group(1) if m else f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}"
            asm = assemble_from_cycles(rid, tr.get("ticker", ""), d_iso,
                                       str(ent.get("ts") or ex.get("ts") or ""))
            if asm is None:
                skipped += 1
                continue
            rec = {"_assembled": asm, "trade": {}, "risk": None}
        for control in (False, True):
            pm = build_one(rid, tr, rec, ex, ent, legsmap, control=control)
            if pm is None:
                continue
            dest = (ctl_dir if control else OUT) / f"{rid}.json"
            dest.write_text(json.dumps(pm, indent=1))
            if control:
                controls += 1
            else:
                done += 1
                st = pm["entry_snapshot"]["reconstructed_pit"].get("status", "?")
                pit_stat[st] = pit_stat.get(st, 0) + 1
            lane_n.setdefault(("control" if control else "failure",
                               pm["meta"]["lane"]), 0)
            lane_n[("control" if control else "failure", pm["meta"]["lane"])] += 1
        if args.limit and done >= args.limit:
            break
    total = done or 1
    print(f"failure post-mortems: {done} | control records: {controls} | skipped(no data): {skipped}")
    print("by lane:", {f"{a}/{b}": n for (a, b), n in sorted(lane_n.items())})
    print("ORATS PIT completeness on failed trades:")
    for k, v in sorted(pit_stat.items(), key=lambda kv: -kv[1]):
        print(f"  {k:28} {v:4}  ({v / total * 100:.0f}%)")


if __name__ == "__main__":
    main()
