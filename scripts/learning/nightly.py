"""CLL Tier 1 — Nightly (automatic, no judgment).

Ingest new closed trades -> post-mortems (failure + winner control) -> classify
with the frozen ruleset on the corrected metric -> append to classified_ledger.
Then run the metric-integrity check. Emits COUNTS ONLY. MAY NOT propose, tune,
or flag hypotheses (structurally enforced by guard.py — proposing ledgers are
not in nightly's write allow-list).

On integrity ALARM (or a pre-existing hold): candidate rows are tagged
integrity_suspect and quarantined from all three ledgers; the alarm halts data
flow, not just logs. Clear with clear_hold.py (human, reason required).

Spec: docs/continuous_learning_loop_spec.md §5, §6. Run nightly.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "scripts" / "learning"))
from src.util.atomic import read_jsonl, load_jsonl_list  # noqa: E402
eng = importlib.import_module("post_mortem_engine")
clf = importlib.import_module("post_mortem_classify")
guard = importlib.import_module("guard")
integrity = importlib.import_module("integrity")
pregate = importlib.import_module("pregate")

LEARN = ROOT / "logs" / "learning"
CFG = yaml.safe_load((ROOT / "config" / "learning" / "loop.yaml").read_text())


def _date(rid):
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", rid)
    return m.group(1) if m else f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}"


def _load_jsonl(name):
    return load_jsonl_list(LEARN / name)  # torn-line tolerant (skips+counts corrupt)


def _load_ledger():
    return _load_jsonl("classified_ledger.jsonl")


def _quarantine_depth():
    """Outstanding = quarantined (rec_id,cohort) neither committed to the ledger
    nor explicitly discarded. Reported every run so a growing file can't go
    unread (drain requires human-clear/discard-with-reason — never silent)."""
    committed = {(r["rec_id"], r["cohort"]) for r in _load_ledger()}
    discarded = {(r["rec_id"], r["cohort"]) for r in _load_jsonl("quarantine_resolved.jsonl")
                 if r.get("resolution") in ("discarded", "data_suspect")}
    q = {(r["rec_id"], r["cohort"]) for r in _load_jsonl("classified_quarantine.jsonl")}
    return sorted(q - committed - discarded)


def _strategy(pm):
    rid = pm["meta"]["rec_id"]
    s = pm["meta"].get("strategy")
    # LANE MARKER (2026-08-10): guarded on the literal "-TEST-", so after the
    # 2026-08-08 forward-only rename this fallback stopped firing for SCALP ids
    # and strategy fell through as None. meta.strategy is absent on ~83% of
    # post-mortems, so the fallback is the normal path, not an edge case, and
    # nightly APPENDS to classified_ledger — mislabelled rows would accumulate.
    # Field positions are identical in both eras; only the marker changed.
    from src.engine.paper_trader import is_scalp
    if not s and is_scalp(rid):
        parts = rid.split("-")
        s = "-".join(parts[4:]) if len(parts) > 4 else None
    return s


def _signal_age_min(pm):
    """decision wall-clock (entry_ts, PT) minus the signal bar timestamp (ET)."""
    from zoneinfo import ZoneInfo
    snap = (pm["entry_snapshot"]["recorded"].get("indicator_snapshot") or {})
    bt, ets = snap.get("bar_time"), pm["lifecycle"].get("entry_ts")
    if not bt or not ets:
        return None
    try:
        bar = datetime.fromisoformat(bt); ent = datetime.fromisoformat(ets)
        if ent.tzinfo is None:
            ent = ent.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        return round((ent - bar).total_seconds() / 60.0, 2)
    except Exception:
        return None


def _row(pm, cl, cohort, pin):
    lc = pm["lifecycle"]; rr = pm.get("reconstructed_risk") or {}
    return {"date": _date(pm["meta"]["rec_id"]), "rec_id": pm["meta"]["rec_id"],
            "lane": pm["meta"]["lane"], "stream": "scalp" if pm["meta"]["lane"] == "SCALP" else "funnel",
            "strategy": _strategy(pm), "signal_data_age_min": _signal_age_min(pm),
            "underlying_drift_over_lag": pregate.drift_over_lag_atr(pm),
            "missing_bars_over_lag": pregate.bar_gap(pm)[0], "cross_session": pregate.bar_gap(pm)[1],
            "cohort": cohort, "primary": (cl["primary"] if cl else None),
            "primary_rule": (cl.get("primary_rule") if cl else None),
            "truth_pnl": pm["failure_definition"]["truth_pnl"],
            "truth_pnl_pct_of_risk": pm["failure_definition"]["truth_pnl_pct_of_risk"],
            "reconstructed_qty": rr.get("quantity"), "max_dollar_risk": rr.get("max_dollar_risk"),
            "ruleset": "p2_v1", "baseline_pin": pin,
            "classified_at": None}  # stamped by caller (no wall-clock in libs)


def _phone(title, body):
    """Item-0 wiring (2026-08-03): integrity alarms must reach the human.
    Never raises — alerting failure cannot break the nightly."""
    try:
        from src.engine.notify import send
        send(title, body + "\nUNVALIDATED — PAPER ONLY", priority="urgent", tags="rotating_light")
    except Exception:
        pass


def main():
    now = datetime.now().isoformat(timespec="seconds")
    active_pin = (LEARN / "ACTIVE").read_text().strip() if (LEARN / "ACTIVE").exists() else CFG["baseline"]["active_pin"]
    baseline = json.loads((LEARN / "baselines" / f"baseline-{active_pin}.json").read_text())
    p95 = baseline["frozen_p95"]
    legs = eng.enr.fallback_legs_map()
    recs, events = eng.load_recs(), eng.load_exits()
    enriched = {}
    for r in read_jsonl(ROOT / "logs/observation/enriched_trades_v1.jsonl"):
        if r.get("status") == "ok":
            enriched[r["rec_id"]] = r

    discarded = {(r["rec_id"], r["cohort"]) for r in _load_jsonl("quarantine_resolved.jsonl")
                 if r.get("resolution") in ("discarded", "data_suspect")}
    # discarded rows leave BOTH the candidate set and the integrity view — a
    # data_suspect entry is a contaminated input, not evidence, so no invariant
    # (incl. INV8) should keep firing on it once it's been drained with a reason.
    ledger = [r for r in _load_ledger() if (r["rec_id"], r["cohort"]) not in discarded]
    have = {(r["rec_id"], r["cohort"]) for r in ledger} | discarded  # skip committed + discarded

    # build + classify candidates for trades not yet in the ledger
    cand_rows, cand_pms = [], []
    for rid, e in enriched.items():
        if e.get("true_pnl_mid") is None:
            continue
        ex = events.get(rid)
        if not ex:
            continue
        ent = events.get(rid + "::entry", {}); rec = recs.get(rid)
        if not rec:
            m = re.match(r"^(\d{4})(\d{2})(\d{2})", rid)
            d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
            asm = eng.assemble_from_cycles(rid, e.get("ticker", ""), d, str(ent.get("ts") or ex.get("ts") or ""))
            if asm is None:
                continue
            rec = {"_assembled": asm, "trade": {}, "risk": None}
        for control in (False, True):
            if (rid, "control" if control else "failure") in have:
                continue
            pm = eng.build_one(rid, e, rec, ex, ent, legs, control=control)
            if pm is None:
                continue
            lane = pm["meta"]["lane"]; cohort = "control" if control else "failure"
            cl = clf.classify(pm, p95.get(lane, 100.0), legs) if not control else None
            row = _row(pm, cl, cohort, active_pin)
            row["classified_at"] = now
            # data_suspect PRE-GATE (symmetric): a stale-input entry never enters
            # the taxonomy — routed to quarantine_resolved, not the ledger.
            ds, reason = pregate.verdict(lane, row.get("underlying_drift_over_lag"),
                                         row.get("signal_data_age_min"), row.get("missing_bars_over_lag"),
                                         row.get("cross_session"))
            if ds:
                guard.append_jsonl("nightly", "quarantine_resolved.jsonl",
                                   {"date": row["date"], "rec_id": row["rec_id"], "cohort": cohort,
                                    "lane": lane, "resolution": "data_suspect", "pregate_reason": reason,
                                    "signal_data_age_min": row.get("signal_data_age_min"),
                                    "underlying_drift_over_lag": row.get("underlying_drift_over_lag"),
                                    "missing_bars_over_lag": row.get("missing_bars_over_lag"),
                                    "cross_session": row.get("cross_session")})
                continue
            cand_rows.append(row); cand_pms.append(pm)

    hold = (LEARN / "integrity_hold.flag").exists()

    # prior-hold gate: quarantine, do not commit, re-emit standing alarm
    if hold:
        for r in cand_rows:
            r["integrity_suspect"] = True
            guard.append_jsonl("nightly", "classified_quarantine.jsonl", r)
        guard.append_jsonl("nightly", "integrity_alarms.jsonl",
                           {"date": now[:10], "run": "nightly", "status": "HELD",
                            "detail": {"quarantined": len(cand_rows),
                                       "note": "integrity_hold set; awaiting human clear_hold with reason"}})
        print(f"[cll-nightly] HELD — integrity_hold active; quarantined {len(cand_rows)} rows. "
              f"counts-only, no commit. quarantine_depth={len(_quarantine_depth())} "
              f"(clear/discard via clear_hold.py, reason required).")
        _phone("CLL INTEGRITY HOLD standing",
               f"nightly ran under a standing hold; {len(cand_rows)} rows quarantined; "
               f"depth {len(_quarantine_depth())}. Human clear required (clear_hold.py).")
        return

    # integrity over (existing ledger + candidates)
    check = integrity.run_all(ledger=ledger + cand_rows, batch_pms=cand_pms, enriched=enriched,
                              baseline=baseline, classify_fn=clf.classify, legs=legs, enr_mod=eng.enr, cfg=CFG)
    alarms = [r for r in check["results"] if r["status"] == "ALARM"]

    if check["overall"] == "ALARM":
        for a in check["results"]:
            guard.append_jsonl("nightly", "integrity_alarms.jsonl",
                               {"date": now[:10], "run": "nightly", **a,
                                "action": "integrity_hold set; tiers 2/3 blocked; batch quarantined"})
        for r in cand_rows:
            r["integrity_suspect"] = True
            guard.append_jsonl("nightly", "classified_quarantine.jsonl", r)
        guard.guarded_path("nightly", "integrity_hold.flag").write_text(
            json.dumps({"raised_at": now, "invariants": [a["invariant"] for a in alarms]}, indent=1))
        print(f"[cll-nightly] 🚨 INTEGRITY ALARM: {[a['invariant'] for a in alarms]} — "
              f"hold set, {len(cand_rows)} rows quarantined, data flow halted. "
              f"quarantine_depth={len(_quarantine_depth())} (clear/discard via clear_hold.py, reason required).")
        _phone("CLL INTEGRITY ALARM — data flow halted",
               f"invariants: {[a['invariant'] for a in alarms]}; {len(cand_rows)} rows "
               f"quarantined; tiers 2/3 blocked until human clear_hold.")
        return

    # OK — commit candidates + OK heartbeat
    for r in cand_rows:
        guard.append_jsonl("nightly", "classified_ledger.jsonl", r)
    guard.append_jsonl("nightly", "integrity_alarms.jsonl",
                       {"date": now[:10], "run": "nightly", "status": "OK",
                        "detail": {v["invariant"]: v["status"] for v in check["results"]}})
    by_lane = Counter(r["lane"] for r in cand_rows)
    by_class = Counter(r["primary"] for r in cand_rows if r["cohort"] == "failure")
    qdepth = len(_quarantine_depth())
    qnote = f" | ⚠ quarantine_depth={qdepth} outstanding (needs human drain)" if qdepth else ""
    print(f"[cll-nightly] OK | new classified: {len(cand_rows)} "
          f"(by_lane {dict(by_lane)}, failures_by_class {dict(by_class)}) | integrity: all OK{qnote}")


if __name__ == "__main__":
    main()
