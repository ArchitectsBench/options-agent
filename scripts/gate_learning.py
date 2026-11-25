#!/usr/bin/env python3
"""Gate-learning loop (user 2026-07-15: "I want this self-learning to be a
looping effort") — the standing daily comparison of Claude's verdicts against
market-truth outcomes.

Every day, after enrichment:
  1. ARCHIVE today's live Claude verdicts (data/claude_budget.json resets
     daily — without this the record is lost) to
     logs/observation/gate_verdicts-YYYY-MM-DD.json.
  2. For each verdict, find its NATURAL-EXPERIMENT TWINS: same-day SCALP
     round trips on the same ticker+strategy (the gate-free lane trades what
     Claude refuses), scored at MARKET-TRUTH P&L (L21 enrichment).
  3. Append one row per verdict to logs/observation/gate_learning.jsonl and
     maintain the cumulative by-category scoreboard the daily ops sweep and
     weekly review read.

Veto categories (pre-declared keyword map; 'other' is a bucket, not a bug).
LEARNING DISCIPLINE: this loop ACCUMULATES evidence only. It never adjusts
prompts, thresholds, or rules — when a category reaches n>=30 with a
consistent verdict-vs-market signal, that is grounds for a pre-registered
prompt/rule proposal (self-training roadmap step 4), decided by the user.

Run: .venv/bin/python scripts/gate_learning.py  (launchd 18:00 weekdays)
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
from src.util.atomic import read_jsonl  # noqa: E402

OBS = ROOT / "logs" / "observation"
LEARN = OBS / "gate_learning.jsonl"

CATEGORIES = [
    ("poor_rr", ("risk/reward", "reward/risk", "r/r", "risking")),
    ("chasing_extended", ("chas", "extended", "stretch", "atr above", "atr below")),
    ("regime_conflict", ("conflict", "fights", "regime bias", "counter-trend")),
    ("premium_cost", ("theta", "iv rank", "peak premium", "decay", "lottery")),
    ("liquidity", ("spread", "slippage", "illiquid", "open interest")),
]


def categorize(reason: str | None) -> str:
    r = (reason or "").lower()
    for name, keys in CATEGORIES:
        if any(k in r for k in keys):
            return name
    return "other"


def market_truth_map() -> dict:
    out = {}
    for r in read_jsonl(OBS / "enriched_trades_v1.jsonl"):
        out[r["rec_id"]] = r            # last row per rec_id wins
    return out


def scalp_twins(day: str, ticker: str, strategy: str, enrich: dict) -> list[dict]:
    twins = []
    for e in read_jsonl(OBS / "fills.jsonl"):
        if e.get("event") != "paper_exit" or "-TEST-" not in e.get("rec_id", ""):
            continue
        if not e.get("ts", "").startswith(day) or e.get("ticker") != ticker:
            continue
        if not e["rec_id"].endswith(strategy):
            continue
        row = enrich.get(e["rec_id"]) or {}
        twins.append({"rec_id": e["rec_id"],
                      "true_pnl": row.get("true_pnl_mid"),
                      "booked_pnl": e.get("pnl_total")})
    return twins


def run_day(day: str) -> list[dict]:
    budget_file = ROOT / "data" / "claude_budget.json"
    archive = OBS / f"gate_verdicts-{day}.json"
    verdicts = json.loads(archive.read_text()) if archive.exists() else {}
    b = json.loads(budget_file.read_text())
    if b.get("date") == day:
        # MERGE, don't freeze: an intraday run must not lock the archive at
        # its early state (2026-07-15 bug: a 9:30 manual run archived 6
        # verdicts; the 18:00 automated run then missed the afternoon's 11)
        verdicts.update(b.get("verdicts") or {})
        archive.write_text(json.dumps(verdicts, indent=1))
    if not verdicts:
        return []
    enrich = market_truth_map()
    seen = set()
    for r in read_jsonl(LEARN):
        seen.add((r["date"], r["key"]))
    rows = []
    for key, v in verdicts.items():
        if (day, key) in seen:
            continue
        ticker, strategy = key.split(":", 1)
        twins = scalp_twins(day, ticker, strategy, enrich)
        scored = [t for t in twins if t["true_pnl"] is not None]
        rows.append({
            "date": day, "key": key, "ticker": ticker, "strategy": strategy,
            "status": v.get("status"),
            "category": categorize((v.get("verdict") or {}).get("veto_reason"))
                        if v.get("status") == "vetoed" else "approved",
            "n_twins": len(twins),
            "twins_true_pnl": round(sum(t["true_pnl"] for t in scored), 2) if scored else None,
            "twin_rec_ids": [t["rec_id"] for t in twins],
        })
    if rows:
        with LEARN.open("a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return rows


def scoreboard() -> dict:
    """Cumulative: per veto category, did the gate-free twins make or lose
    money at market truth? (veto 'saved' when twins lost)."""
    if not LEARN.exists():
        return {}
    cats: dict[str, dict] = {}
    for line in LEARN.read_text().splitlines():
        r = json.loads(line)
        c = cats.setdefault(r["category"], {"n_verdicts": 0, "n_with_twins": 0,
                                            "twins_true_pnl_sum": 0.0,
                                            "twins_lost_money": 0})
        c["n_verdicts"] += 1
        if r["twins_true_pnl"] is not None:
            c["n_with_twins"] += 1
            c["twins_true_pnl_sum"] += r["twins_true_pnl"]
            c["twins_lost_money"] += r["twins_true_pnl"] < 0
    for c in cats.values():
        c["twins_true_pnl_sum"] = round(c["twins_true_pnl_sum"], 2)
    return cats


# ── pre-registered readiness bar (2026-07-15, BEFORE any category matured) ──
# A category becomes PROPOSAL-WARRANTED when ALL hold:
READY_MIN_TWINNED = 30      # twinned verdicts (market-scored controls)
READY_MIN_WEEKS = 4         # spread across >= 4 distinct ISO weeks
READY_CONSISTENCY = 0.70    # >= 70% of twinned verdicts agree in direction
# Direction per verdict: twins_true_pnl > 0 means the veto REFUSED profit;
# < 0 means the veto SAVED money. Warranted either way — the proposal then
# strengthens or corrects that reasoning line via replay-gated prompt
# evolution (self-training step 4). NOTHING changes without user sign-off.


def readiness() -> dict:
    if not LEARN.exists():
        return {}
    from collections import defaultdict
    cats = defaultdict(lambda: {"twinned": 0, "weeks": set(),
                                "refused_profit": 0, "saved_money": 0})
    for line in LEARN.read_text().splitlines():
        r = json.loads(line)
        if r.get("twins_true_pnl") is None or r["category"] == "approved":
            continue
        c = cats[r["category"]]
        c["twinned"] += 1
        y, w, _ = date.fromisoformat(r["date"]).isocalendar()
        c["weeks"].add(f"{y}-{w}")
        c["refused_profit" if r["twins_true_pnl"] > 0 else "saved_money"] += 1
    out = {}
    for k, c in cats.items():
        n = c["twinned"]
        consistency = max(c["refused_profit"], c["saved_money"]) / n if n else 0
        ready = (n >= READY_MIN_TWINNED and len(c["weeks"]) >= READY_MIN_WEEKS
                 and consistency >= READY_CONSISTENCY)
        out[k] = {"twinned": n, "weeks": len(c["weeks"]),
                  "consistency": round(consistency, 2),
                  "lean": ("vetoes_refuse_profit" if c["refused_profit"] >= c["saved_money"]
                           else "vetoes_save_money"),
                  "proposal_warranted": ready}
    return out


def _alert_ready(ready: dict):
    """One ntfy per newly-warranted category (state-file throttled)."""
    state = OBS / "gate_learning_alerted.json"
    seen = set(json.loads(state.read_text())) if state.exists() else set()
    new = [k for k, v in ready.items() if v["proposal_warranted"] and k not in seen]
    if not new:
        return
    state.write_text(json.dumps(sorted(seen | set(new))))
    try:
        sys.path.insert(0, str(ROOT))
        from src.engine.notify import send
        send("GATE-LEARNING: proposal warranted",
             f"categories ready for pre-registered prompt proposal: {', '.join(new)}",
             priority="high", tags="brain")
    except Exception:
        pass


def main():
    today = str(date.today())
    rows = run_day(today)
    print(f"{today}: {len(rows)} verdict rows appended")
    for r in rows:
        twin = (f"twins {r['n_twins']} -> true {r['twins_true_pnl']:+.0f}$"
                if r["twins_true_pnl"] is not None else
                ("no scored twins" if r["n_twins"] else "no twins"))
        print(f"  {r['status']:9s} {r['key']:26s} [{r['category']:17s}] {twin}")
    ready = readiness()
    _alert_ready(ready)
    warranted = [k for k, v in ready.items() if v["proposal_warranted"]]
    if warranted:
        print(f"\n*** PROPOSAL WARRANTED: {warranted} — draft pre-registered prompt "
              "amendment for user sign-off (replay-gated) ***")
    print("\nCUMULATIVE SCOREBOARD (veto categories vs gate-free twins @ market truth):")
    for cat, c in sorted(scoreboard().items()):
        print(f"  {cat:18s} verdicts {c['n_verdicts']:3d} | twinned {c['n_with_twins']:3d} "
              f"| twins P&L {c['twins_true_pnl_sum']:+8.0f}$ "
              f"| twins lost: {c['twins_lost_money']}/{c['n_with_twins']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
