"""Counterfactual labeling of observation logs (self-training layer #1).

Converts logs/observation/YYYY-MM-DD.jsonl into a supervised dataset:

  snapshot labels — one row per (cycle, ticker): forward returns at +30m,
    +60m, and end-of-day from the NEXT 5m bar's open (same next-bar
    execution discipline as Track A — no same-bar fills, no lookahead),
    plus session-remainder MFE/MAE.

  candidate labels — one row per pipeline decision (posted / vetoed / held
    / skipped): the COUNTERFACTUAL direction-signed underlying return the
    candidate would have captured. This is what measures the gates: did
    Claude's vetoes avoid losses? Did held candidates outperform?

Honesty notes (also stamped into every file):
  - Counterfactuals for spreads use the UNDERLYING directional move
    (Track A "Level 1" semantics), not option P&L — option bars aren't
    archived. Real fills live in fills.jsonl round_trip records.
  - Labels use only bars strictly after the snapshot timestamp.

Output: logs/observation/labels-YYYY-MM-DD.jsonl (idempotent; --force to
rewrite). Usage:
  python src/research/label_observations.py [--date YYYY-MM-DD] [--force]
  (default: all observation files without a labels file)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.indicators.stock_signals import batch_intraday
from src.util.atomic import read_jsonl

OBS_DIR = ROOT / "logs" / "observation"
ET = ZoneInfo("America/New_York")
BARS_30M, BARS_60M = 6, 12


def _to_et(ts: str) -> pd.Timestamp:
    """Observation timestamps are tz-naive local time — attach the machine
    tz, convert to ET (bar index tz)."""
    return pd.Timestamp(datetime.fromisoformat(ts).astimezone(ET))


def forward_labels(df: pd.DataFrame, ts_et: pd.Timestamp) -> dict | None:
    """Next-bar-open reference; forward closes at +30m/+60m; EOD close;
    MFE/MAE over the session remainder."""
    idx = df.index.searchsorted(ts_et, side="left")
    if idx >= len(df):
        return None
    session = df.index[idx].normalize()
    ref = float(df["Open"].iloc[idx])
    if ref <= 0:
        return None

    def close_at(i):
        if i >= len(df) or df.index[i].normalize() != session:
            return None
        return round(float(df["Close"].iloc[i]) / ref - 1, 5)

    eod_mask = df.index.normalize() == session
    eod_slice = df[eod_mask & (df.index >= df.index[idx])]
    return {
        "ref_price": round(ref, 4),
        "ref_bar": str(df.index[idx]),
        "fwd_30m": close_at(idx + BARS_30M - 1),
        "fwd_60m": close_at(idx + BARS_60M - 1),
        "fwd_eod": round(float(eod_slice["Close"].iloc[-1]) / ref - 1, 5) if len(eod_slice) else None,
        "mfe_eod": round(float(eod_slice["High"].max()) / ref - 1, 5) if len(eod_slice) else None,
        "mae_eod": round(float(eod_slice["Low"].min()) / ref - 1, 5) if len(eod_slice) else None,
    }


def _candidate_status(entry: dict) -> str:
    if "rec_id" in entry:
        claude = entry.get("validation_chain", {}).get("claude", {})
        return f"posted_{claude.get('status', 'unknown')}"
    if "vetoed" in entry:
        return "vetoed"
    return f"skipped_{entry.get('skipped', 'unknown')}"


def _candidate_direction(entry: dict, record: dict, ticker: str) -> str | None:
    if entry.get("direction"):
        return entry["direction"]
    if "trade" in entry and entry["trade"]:
        return entry["trade"].get("direction")
    # legacy records: re-derive exactly as the pipeline did at the time
    stack = record.get("signals", {}).get(ticker, {}).get("ema_stack", {})
    if stack.get("15m") in ("bullish", "bearish"):
        return "long" if stack["15m"] == "bullish" else "short"
    return None


def _local_confidence(entry: dict, record: dict, ticker: str) -> float | None:
    if entry.get("local_confidence") is not None:
        return entry["local_confidence"]
    for c in (record.get("local_llm", {}).get("synthesis") or {}).get("candidates", []):
        if c.get("ticker") == ticker:
            return c.get("final_confidence")
    return None


def label_file(obs_path: Path, force: bool = False) -> dict | None:
    out_path = obs_path.parent / f"labels-{obs_path.stem}.jsonl"
    if out_path.exists() and not force:
        return None
    records = list(read_jsonl(obs_path))
    tickers = sorted({t for r in records for t in r.get("signals", {})
                      if "error" not in r["signals"][t]})
    if not tickers:
        return None
    bars = batch_intraday(tickers, "5m", "15d")

    snapshot_rows, candidate_rows = [], []
    for r in records:
        ts_et = _to_et(r["ts"])
        # hygiene: skip out-of-window snapshots (closed-market test cycles
        # produce overnight "forward returns" that pollute the dataset)
        if ts_et.weekday() >= 5 or not (
                (9, 30) <= (ts_et.hour, ts_et.minute) < (16, 0)):
            continue
        per_ticker = {}
        for t in tickers:
            if t not in bars or t not in r.get("signals", {}):
                continue
            labels = forward_labels(bars[t], ts_et)
            if labels is None:
                continue
            per_ticker[t] = labels
            snapshot_rows.append({
                "kind": "snapshot", "ts": r["ts"], "cycle": r.get("cycle"),
                "ticker": t, **labels,
                "regime_bias": r.get("regime", {}).get("bias"),
                "stamp": "UNVALIDATED — PAPER ONLY",
            })
        # candidates: recommendations[] entries PLUS synthesis candidates that
        # never reached the pipeline (below_threshold — the low-confidence
        # cohort calibration needs; data-gap fix #3)
        entries = list(r.get("recommendations", []))
        # L15: one label row per (cycle, ticker) decision. Posted recs carry
        # strategy only inside trade{}, so the old (ticker, strategy) key was
        # (ticker, None) for them and every posted rec ALSO got a phantom
        # 'skipped_below_threshold' twin. A ticker with ANY real entry this
        # cycle precludes a synthetic below-threshold row.
        decided_tickers = {e.get("ticker") for e in entries}
        for cand in (r.get("local_llm", {}).get("synthesis") or {}).get("candidates", []):
            if cand.get("ticker") not in decided_tickers and cand.get("ticker") in per_ticker:
                entries.append({"ticker": cand.get("ticker"), "strategy": cand.get("strategy"),
                                "skipped": "below_threshold",
                                "local_confidence": cand.get("final_confidence")})
        for entry in entries:
            ticker = entry.get("ticker")
            if ticker not in per_ticker:
                continue
            direction = _candidate_direction(entry, r, ticker)
            sign = {"long": 1, "short": -1}.get(direction, 0)
            labels = per_ticker[ticker]
            candidate_rows.append({
                "kind": "candidate", "ts": r["ts"], "cycle": r.get("cycle"),
                "ticker": ticker, "strategy": entry.get("strategy"),
                "status": _candidate_status(entry),
                "direction": direction,
                "local_confidence": _local_confidence(entry, r, ticker),
                "rec_id": entry.get("rec_id"),
                # counterfactual: direction-signed UNDERLYING move (Level 1 proxy)
                "cf_30m": None if not sign or labels["fwd_30m"] is None else round(sign * labels["fwd_30m"], 5),
                "cf_60m": None if not sign or labels["fwd_60m"] is None else round(sign * labels["fwd_60m"], 5),
                "cf_eod": None if not sign or labels["fwd_eod"] is None else round(sign * labels["fwd_eod"], 5),
                "counterfactual_basis": "underlying_directional_proxy",
                "stamp": "UNVALIDATED — PAPER ONLY",
            })

    with open(out_path, "w") as fh:
        for row in snapshot_rows + candidate_rows:
            fh.write(json.dumps(row) + "\n")
    return {"file": out_path.name, "snapshots": len(snapshot_rows), "candidates": len(candidate_rows)}


def summarize(out_path: Path):
    rows = list(read_jsonl(out_path))
    cands = [r for r in rows if r["kind"] == "candidate"]
    if not cands:
        return
    print(f"  candidate counterfactuals (direction-signed underlying, EOD):")
    by_status: dict[str, list[float]] = {}
    for c in cands:
        if c["cf_eod"] is not None:
            by_status.setdefault(c["status"], []).append(c["cf_eod"])
    for status, vals in sorted(by_status.items()):
        mean = sum(vals) / len(vals)
        print(f"    {status:<28} n={len(vals):<4} mean cf_eod={mean:+.3%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: all unlabeled)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.date:
        targets = [OBS_DIR / f"{args.date}.jsonl"]
    else:
        targets = sorted(p for p in OBS_DIR.glob("2*.jsonl") if not p.stem.startswith("labels"))
    any_done = False
    for path in targets:
        if not path.exists():
            print(f"{path.name}: no observation file")
            continue
        result = label_file(path, force=args.force)
        if result is None:
            print(f"{path.name}: already labeled (use --force to rewrite)")
            continue
        any_done = True
        print(f"{result['file']}: {result['snapshots']} snapshot labels, "
              f"{result['candidates']} candidate labels")
        summarize(path.parent / f"labels-{path.stem}.jsonl")
    if not any_done:
        print("nothing to do")

    # nightly chain: calibration tracker runs on the full labeled history
    from src.research.calibration_report import run as calibration_run
    print()
    calibration_run()

    # IV snapshot fallback: fill any ticker the 12:55 PT job missed today
    try:
        from src.data.iv_snapshot import snapshot as iv_snapshot
        print()
        iv_snapshot(fallback=True)
    except Exception as exc:
        print(f"IV fallback failed (non-fatal): {exc}")


if __name__ == "__main__":
    main()
