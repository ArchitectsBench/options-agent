"""Execution-quality report (2026-07-06) — the question that decides whether
the cost wall is climbable: can our paper orders systematically capture mid?

Reads logs/observation/fills.jsonl:
  - fill rate: entries filled vs expired unfilled (the cost of patience)
  - time-to-fill: broker_created -> broker_filled
  - slippage vs decision-time mid (rec_price) — the legacy metric
  - capture_vs_mid: fill vs the CONTEMPORANEOUS mid at fill time (recorded
    from 2026-07-06 onward) — the true capture metric; + means better than mid
  - z-overlay accounting once overlay-tagged trades appear

Usage: .venv/bin/python scripts/execution_quality.py [--json]
Read-only; exit 0 always.
"""

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _events():
    path = ROOT / "logs" / "observation" / "fills.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _minutes(a: str | None, b: str | None) -> float | None:
    try:
        ta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        tb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        return round((tb - ta).total_seconds() / 60, 1)
    except (TypeError, ValueError):
        return None


def evaluate(root: Path = ROOT) -> dict:
    events = _events()
    fills = [e for e in events if e.get("event") == "paper_fill"]
    expired = [e for e in events if e.get("event") == "entry_expired"]

    slippage = [e["slippage"] for e in fills if e.get("slippage") is not None]
    ttf = [m for e in fills
           if (m := _minutes(e.get("broker_created"), e.get("broker_filled"))) is not None]
    captures = [e["capture_vs_mid"] for e in fills if e.get("capture_vs_mid") is not None]
    spreads = [e["spread_at_fill"] for e in fills if e.get("spread_at_fill") is not None]

    def stats(xs, lower_is_better=False):
        """worst/best follow the metric's orientation (L16 family: labels were
        universally worst=min/best=max — inverted for time-to-fill/spread)."""
        if not xs:
            return None
        lo, hi = round(min(xs), 4), round(max(xs), 4)
        return {"n": len(xs), "mean": round(statistics.fmean(xs), 4),
                "median": round(statistics.median(xs), 4),
                "worst": hi if lower_is_better else lo,
                "best": lo if lower_is_better else hi}

    n_entries = len(fills) + len(expired)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fills": len(fills),
        "entries_expired_unfilled": len(expired),
        "fill_rate": round(len(fills) / n_entries, 3) if n_entries else None,
        "time_to_fill_min": stats(ttf, lower_is_better=True),
        "slippage_vs_decision_mid": stats(slippage),
        "capture_vs_mid_at_fill": stats(captures),
        "capture_positive_share": (round(sum(1 for c in captures if c >= 0) / len(captures), 3)
                                   if captures else None),
        "spread_at_fill": stats(spreads, lower_is_better=True),
        "note": ("capture_vs_mid recorded from 2026-07-06 — the wall-climbing metric. "
                 "+ = filled better than the mid at fill time. Sample grows daily."),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    out = evaluate()
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        for k, v in out.items():
            print(f"{k}: {v}")
    sys.exit(0)
