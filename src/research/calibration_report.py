"""Confidence calibration tracker (self-training layer #2 — MEASUREMENT ONLY).

Reads all candidate labels (logs/observation/labels-*.jsonl) and reports how
the local model's stated confidence relates to realized outcomes:
  - calibration table: confidence buckets vs realized win rate (cf_eod > 0)
  - Brier score (lower is better; 0.25 = uninformative coin flip at p=0.5)
  - confidence distribution (a constant 0.8 means NOTHING to calibrate)
  - cohort outcomes by decision status (posted / vetoed / held / skipped)

NO remapping is fitted or applied until ALL readiness gates pass:
  n >= 150 labeled candidates, confidence std >= 0.05, >= 14 calendar days
  of labels, >= 2 distinct regime biases observed. Until then this tool
  prints MEASURING ONLY — the 0.75 invoke threshold stays untouched.

Appends one summary row per run to logs/research/calibration_history.jsonl.
Runs automatically after the nightly labeler.
"""

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OBS_DIR = ROOT / "logs" / "observation"
HISTORY = ROOT / "logs" / "research" / "calibration_history.jsonl"

GATES = {"min_candidates": 150, "min_conf_std": 0.05, "min_days": 14, "min_biases": 2}
BUCKETS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


def load_rows() -> tuple[list[dict], list[dict]]:
    snapshots, candidates = [], []
    for path in sorted(OBS_DIR.glob("labels-*.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            (snapshots if row["kind"] == "snapshot" else candidates).append(row)
    return snapshots, candidates


def run() -> dict:
    snapshots, candidates = load_rows()
    scored = [c for c in candidates
              if c.get("local_confidence") is not None and c.get("cf_eod") is not None]
    days = sorted({c["ts"][:10] for c in scored})
    biases = sorted({s.get("regime_bias") for s in snapshots if s.get("regime_bias")})

    print(f"── CALIBRATION REPORT {datetime.now():%Y-%m-%d %H:%M} ──")
    print(f"  labeled candidates with confidence+outcome: {len(scored)} "
          f"across {len(days)} day(s); regime biases seen: {biases}")

    summary = {"ts": datetime.now().isoformat(timespec="seconds"),
               "n": len(scored), "days": len(days), "biases": biases}
    if scored:
        confs = [c["local_confidence"] for c in scored]
        wins = [1.0 if c["cf_eod"] > 0 else 0.0 for c in scored]
        brier = sum((p - w) ** 2 for p, w in zip(confs, wins)) / len(scored)
        conf_std = statistics.pstdev(confs)
        summary |= {"brier": round(brier, 4), "conf_mean": round(statistics.mean(confs), 3),
                    "conf_std": round(conf_std, 4), "win_rate": round(statistics.mean(wins), 3)}
        print(f"  confidence: mean {summary['conf_mean']}, std {summary['conf_std']} "
              f"| realized win rate (cf_eod>0): {summary['win_rate']:.0%} "
              f"| Brier {summary['brier']} (coin flip @0.5 = 0.25)")
        if conf_std < GATES["min_conf_std"]:
            print(f"  ⚠ confidence is near-constant (std {conf_std:.3f}) — nothing to "
                  f"calibrate; the local model isn't differentiating setups. Consider a "
                  f"prompt change to elicit a spread, or benchmark qwen2.5:14b.")

        print(f"  {'bucket':<12}{'n':>5}{'mean conf':>11}{'win rate':>10}{'gap':>8}")
        for lo, hi in BUCKETS:
            rows = [(p, w) for p, w in zip(confs, wins) if lo <= p < hi]
            if not rows:
                continue
            mp = statistics.mean(r[0] for r in rows)
            wr = statistics.mean(r[1] for r in rows)
            print(f"  {f'{lo:.1f}-{hi:.1f}':<12}{len(rows):>5}{mp:>11.2f}{wr:>10.0%}{wr - mp:>+8.2f}")

        print("  cohorts by decision status (mean direction-signed cf_eod):")
        by_status: dict[str, list[float]] = {}
        for c in scored:
            by_status.setdefault(c["status"], []).append(c["cf_eod"])
        for status, vals in sorted(by_status.items()):
            print(f"    {status:<30} n={len(vals):<5} mean={statistics.mean(vals):+.3%}")

    # ── readiness gates (remap is NOT fitted until all pass) ──
    checks = [
        ("n >= 150 candidates", len(scored) >= GATES["min_candidates"], f"{len(scored)}"),
        ("conf std >= 0.05", bool(scored) and statistics.pstdev(
            [c["local_confidence"] for c in scored]) >= GATES["min_conf_std"],
         f"{summary.get('conf_std', 'n/a')}"),
        (">= 14 days of labels", len(days) >= GATES["min_days"], f"{len(days)}"),
        (">= 2 regime biases", len(biases) >= GATES["min_biases"], f"{len(biases)}"),
    ]
    ready = all(ok for _, ok, _ in checks)
    print("  readiness gates for fitting a remap:")
    for name, ok, actual in checks:
        print(f"    [{'PASS' if ok else 'WAIT'}] {name} (have: {actual})")
    print(f"  => {'READY TO FIT — propose remap for review (not auto-applied)' if ready else 'MEASURING ONLY — invoke threshold stays at config value'}")

    summary["ready_to_fit"] = ready
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "a") as fh:
        fh.write(json.dumps(summary) + "\n")
    return summary


if __name__ == "__main__":
    run()
