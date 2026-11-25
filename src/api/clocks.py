"""Clocks endpoint — per-clock data for the cockpit's five-tab Trades page.

One tab per evidence stream, never blended (L12): FUNNEL (forward test),
SCALP (frozen candidate), TREND (Track C shadow book), SHADOW (option-b
counterfactual gate), L13 (give-back fix readiness). Each section is
fault-isolated (an error in one never blanks the others) and the whole payload
is cached ~60s — same pattern as feed.py.
"""
from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_cache: dict = {"ts": 0.0, "data": None}
TTL = 60.0


def _pnl(e: dict) -> float:
    v = e.get("pnl_total")
    return float(v) if v is not None else float(e.get("pnl_per_contract") or 0)


def _rec_meta() -> dict:
    """rec_id -> {structure, direction} from observation logs (funnel + test)."""
    meta = {}
    for f in sorted(glob.glob(str(ROOT / "logs/observation/2026-*.jsonl"))):
        for line in Path(f).read_text().splitlines():
            r = json.loads(line)
            for e in (r.get("recommendations") or []):
                if e.get("rec_id") and e.get("trade"):
                    meta[e["rec_id"]] = {"structure": e["trade"].get("structure"),
                                         "dir": e["trade"].get("direction")}
            for t in (r.get("paper_execution_tests") or []):
                tr = t.get("trade") or {}
                if t.get("rec_id"):
                    meta[t["rec_id"]] = {"structure": tr.get("structure"),
                                         "dir": tr.get("direction"),
                                         "mode": t.get("mode")}
    return meta


def _exits() -> list[dict]:
    p = ROOT / "logs/observation/fills.jsonl"
    if not p.exists():
        return []
    return [e for e in (json.loads(l) for l in p.read_text().splitlines())
            if e.get("event") == "paper_exit"]


def _trade_row(e: dict, meta: dict) -> dict:
    m = meta.get(e.get("rec_id", ""), {})
    ts = e.get("ts", "")
    return {"date": ts[:10], "time": ts[11:16], "ticker": e.get("ticker"),
            "structure": m.get("structure") or "—", "dir": (m.get("dir") or "—").upper(),
            "qty": int(e.get("quantity") or 1), "pnl": round(_pnl(e), 2),
            "exit": (e.get("exit_reason") or "—"),
            "path": bool((e.get("round_trip") or {}).get("mark_series"))}


def _funnel(exits, meta) -> dict:
    import forward_acceptance_check as fac
    res = fac.evaluate(ROOT)
    prog = res.get("progress", {})
    from src.engine.paper_trader import is_scalp as _is_scalp
    trades = [_trade_row(e, meta) for e in exits if not _is_scalp(e.get("rec_id", ""))]
    return {"progress": prog, "overall": res.get("overall"),
            "pnl_total": round(sum(t["pnl"] for t in trades), 2),
            "trades": list(reversed(trades))}


def _scalp(exits, meta) -> dict:
    import scalp_forward_check as sfc
    res = sfc.evaluate()
    qual_trades, _excluded = sfc.qualifying(sfc.load_criteria())  # (trades, exclusions) since the 07-11 artifact filter
    qual_ids = {t["rec_id"] for t in qual_trades}
    trades = [_trade_row(e, meta) for e in exits if e.get("rec_id") in qual_ids]
    return {"status": res.get("status"), "window_start": res.get("window_start"),
            "checks": res.get("checks"), "pnl_total": res.get("total_pnl"),
            "trades": list(reversed(trades))}


def _trend() -> dict:
    state = json.loads((ROOT / "data/trendbook/state.json").read_text())
    rows = [json.loads(l) for l in (ROOT / "logs/trendbook/ledger.jsonl").read_text().splitlines()]
    recent = [{"date": r.get("date"), "cycle": r.get("cycle"),
               "equity": round(r.get("equity", 0), 2),
               "trades": len(r.get("trades") or []),
               "pnl": round(r.get("pnl", 0), 2) if r.get("pnl") is not None else None}
              for r in rows[-30:]]
    return {"cycle": rows[-1].get("cycle") if rows else None,
            "equity": recent[-1]["equity"] if recent else None,
            "start_equity": 50000.0, "days": len(rows),
            "positions": len(state.get("positions") or state.get("legs") or []),
            "recent": list(reversed(recent))}


def _shadow() -> dict:
    import shadow_gate_report as sgr
    spec = sgr.load_spec()
    rows = sgr.cohorts(spec)
    picks = [{"date": r.get("ts", "")[:10], "time": r.get("ts", "")[11:16],
              "ticker": r.get("ticker"), "structure": r.get("strategy"),
              "dir": (r.get("direction") or "—").upper(),
              "conf": r.get("local_confidence"),
              "cf_eod_pct": None if r.get("cf_eod") is None else round(r["cf_eod"] * 100, 2)}
             for r in rows["pass"][-100:]]
    return {"threshold": spec["threshold_calibrated"],
            "window_start": spec["window_start_labels"],
            "pass": sgr.stats(rows["pass"]), "fail": sgr.stats(rows["fail"]),
            "posted": sgr.stats(rows["posted"]), "picks": list(reversed(picks))}


def _l13(exits, meta) -> dict:
    import giveback_analysis as ga
    rows = ga._rows()
    fr = ga.fix_readiness(rows)
    funnel_cap = ga.analyze([r for r in rows if not r["is_test"]]).get("vwap_invalidation", {})
    pathed = [_trade_row(e, meta) for e in exits
              if (e.get("round_trip") or {}).get("mark_series")]
    return {"readiness": fr,
            "funnel_invalidation_capture": funnel_cap.get("aggregate_capture_ratio"),
            "trades_with_paths": list(reversed(pathed))}


def build_clocks() -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < TTL:
        return _cache["data"]
    exits = _exits()
    meta = _rec_meta()
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for name, fn in (("funnel", lambda: _funnel(exits, meta)),
                     ("scalp", lambda: _scalp(exits, meta)),
                     ("trend", _trend),
                     ("shadow", _shadow),
                     ("l13", lambda: _l13(exits, meta))):
        try:
            out[name] = fn()
        except Exception as exc:  # fault isolation — one bad section never blanks the page
            out[name] = {"error": str(exc)}
    _cache.update(ts=now, data=out)
    return out
