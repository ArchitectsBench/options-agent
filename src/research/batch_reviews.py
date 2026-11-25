"""Nightly batch mock-reviews (self-training layer #3).

The live agent gets at most 20 Claude verdicts/day — too sparse for
calibration or distillation. This job sends the day's UNREVIEWED candidates
(held, cooldown-blocked, selector-skipped, below-threshold) to Claude via
the Batches API (50% cost, no latency pressure) for mock senior-trader
reviews. Pure training data: no orders, no notifications, no effect on the
live budget guard.

Sampling: candidates dedup to per-(ticker,strategy,direction,status) groups,
evenly spaced across the day, capped per group and per night (config:
llm.claude.batch_reviews).

Output: logs/observation/batch_verdicts-YYYY-MM-DD.jsonl, every row stamped
mock: true. Pending batches persist in data/batch_reviews_pending.json and
are collected on the next run.

Usage: python src/research/batch_reviews.py [--date YYYY-MM-DD]
       [--wait MIN] [--collect-only] [--dry-run]
"""

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

OBS_DIR = ROOT / "logs" / "observation"
PENDING_FILE = ROOT / "data" / "batch_reviews_pending.json"
PROMPT_FILE = ROOT / "src" / "llm" / "prompts" / "claude_validation.md"

MOCK_ADDENDUM = """

MOCK REVIEW CONTEXT (nightly batch — not a live cycle)
This candidate did NOT reach live validation; `live_status` says why. Review
it exactly as if it were presented live at the recorded timestamp. You are
given no information about what the market did afterward — judge only the
snapshot. If `proposed_trade` is null (no structure was built), assess the
setup directionally and assume a standard liquid debit spread was available.
Your verdict is logged as training data only; nothing trades on it."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean"},
        "confidence": {"type": "number"},
        "veto_reason": {"type": ["string", "null"]},
        "adjustments": {"type": ["string", "null"]},
        "narrative": {"type": "string"},
    },
    "required": ["approve", "confidence", "veto_reason", "adjustments", "narrative"],
    "additionalProperties": False,
}


def _cfg() -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    claude = cfg.get("llm", {}).get("claude", {})
    out = {"model": claude.get("model", "claude-opus-5"),
           **{"enabled": True, "max_per_night": 24, "per_group_cap": 3,
              **claude.get("batch_reviews", {})}}
    # llm.claude.enabled is the MASTER switch (2026-09-29): off kills every
    # Anthropic API consumer, this job included, whatever batch_reviews says.
    if not claude.get("enabled", True):
        out["enabled"] = False
    return out


def collect_candidates(obs_path: Path) -> list[dict]:
    """Every candidate WITHOUT a live verdict, with its full cycle context."""
    out = []
    for line in obs_path.read_text().splitlines():
        r = json.loads(line)
        cycle, regime = r.get("cycle"), r.get("regime", {})
        decided = {}  # (ticker, strategy) -> live status
        for e in r.get("recommendations", []):
            key = (e.get("ticker"), e.get("strategy"))
            if "rec_id" in e:
                decided[key] = ("live_" + e.get("validation_chain", {}).get("claude", {})
                                .get("status", "unknown"), e)
            elif "vetoed" in e:
                decided[key] = ("live_vetoed", e)
            else:
                decided[key] = (f"skipped_{e.get('skipped')}", e)
        for cand in (r.get("local_llm", {}).get("synthesis") or {}).get("candidates", []):
            ticker, strategy = cand.get("ticker"), cand.get("strategy")
            if ticker not in r.get("signals", {}) or "error" in r["signals"].get(ticker, {}):
                continue
            status, entry = decided.get((ticker, strategy), ("below_threshold", {}))
            if status in ("live_approved", "live_vetoed"):
                continue  # real verdict exists — don't duplicate
            sig = r["signals"][ticker]
            direction = entry.get("direction") or (
                "long" if sig.get("ema_stack", {}).get("15m") == "bullish" else "short")
            out.append({
                "ts": r["ts"], "cycle": cycle, "ticker": ticker, "strategy": strategy,
                "direction": direction, "live_status": status,
                "local_confidence": cand.get("final_confidence"),
                "context": {
                    "regime": {k: v for k, v in regime.items() if k not in ("spy", "qqq")},
                    "ticker": ticker, "signals": sig,
                    "applicability": r.get("applicability", {}).get(ticker, {}),
                    "local_candidate": cand,
                    "proposed_trade": entry.get("trade"),
                    "live_status": status,
                    "mode": "observation — UNVALIDATED, PAPER ONLY (mock batch review)",
                },
            })
    return out


def sample(cands: list[dict], per_group: int, total: int) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for c in cands:
        groups.setdefault((c["ticker"], c["strategy"], c["direction"], c["live_status"]), []).append(c)
    # held/cooldown candidates are closest to live decisions — review them first
    priority = sorted(groups.items(), key=lambda kv: (
        0 if "held" in kv[0][3] or "veto_active" in kv[0][3] else
        1 if kv[0][3] == "below_threshold" else 2, kv[0]))
    picked = []
    for _, members in priority:
        members.sort(key=lambda c: c["ts"])
        step = max(1, len(members) // per_group)
        picked += members[::step][:per_group]
    return picked[:total]


def submit(obs_path: Path, cfg: dict, dry_run: bool = False) -> dict | None:
    day = obs_path.stem
    if (OBS_DIR / f"batch_verdicts-{day}.jsonl").exists():
        print(f"{day}: verdicts already collected")
        return None
    pending = json.loads(PENDING_FILE.read_text()) if PENDING_FILE.exists() else []
    if any(p["date"] == day for p in pending):
        print(f"{day}: batch already pending")
        return None
    cands = collect_candidates(obs_path)
    picked = sample(cands, cfg["per_group_cap"], cfg["max_per_night"])
    print(f"{day}: {len(cands)} unreviewed candidates -> sampled {len(picked)} "
          f"(caps: {cfg['per_group_cap']}/group, {cfg['max_per_night']}/night)")
    if not picked or dry_run:
        return None

    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    system = PROMPT_FILE.read_text() + MOCK_ADDENDUM
    requests = []
    meta = {}
    for i, c in enumerate(picked):
        cid = f"r{i}-c{c['cycle']}-{c['ticker']}"[:60]
        meta[cid] = {k: c[k] for k in ("ts", "cycle", "ticker", "strategy",
                                       "direction", "live_status", "local_confidence")}
        requests.append(Request(
            custom_id=cid,
            params=MessageCreateParamsNonStreaming(
                model=cfg["model"],
                max_tokens=8000,
                thinking={"type": "adaptive"},
                system=system,
                messages=[{"role": "user", "content": json.dumps(c["context"], default=str)}],
                output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            ),
        ))
    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=requests)
    pending.append({"batch_id": batch.id, "date": day, "n": len(picked),
                    "submitted_at": datetime.now().isoformat(timespec="seconds"),
                    "meta": meta})
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(pending, indent=2))
    print(f"{day}: batch {batch.id} submitted ({len(picked)} reviews)")
    return {"batch_id": batch.id, "n": len(picked)}


def collect(wait_min: float = 0) -> int:
    if not PENDING_FILE.exists():
        return 0
    import anthropic
    client = anthropic.Anthropic()
    pending = json.loads(PENDING_FILE.read_text())
    deadline = time.time() + wait_min * 60
    written, still = 0, []
    for p in pending:
        while True:
            batch = client.messages.batches.retrieve(p["batch_id"])
            if batch.processing_status == "ended" or time.time() >= deadline:
                break
            time.sleep(60)
        if batch.processing_status != "ended":
            still.append(p)
            print(f"{p['date']}: batch {p['batch_id']} still {batch.processing_status} — will retry next run")
            continue
        out_path = OBS_DIR / f"batch_verdicts-{p['date']}.jsonl"
        n_ok = 0
        with open(out_path, "a") as fh:
            for result in client.messages.batches.results(p["batch_id"]):
                m = p["meta"].get(result.custom_id, {})
                if result.result.type != "succeeded":
                    fh.write(json.dumps({**m, "mock": True, "error": result.result.type}) + "\n")
                    continue
                msg = result.result.message
                text = next((b.text for b in msg.content if b.type == "text"), "")
                try:
                    verdict = json.loads(text)
                except json.JSONDecodeError:
                    verdict = None
                fh.write(json.dumps({**m, "mock": True, "verdict": verdict,
                                     "model": msg.model,
                                     "stamp": "UNVALIDATED — PAPER ONLY"}) + "\n")
                n_ok += 1
        written += n_ok
        print(f"{p['date']}: collected {n_ok}/{p['n']} verdicts -> {out_path.name}")
    PENDING_FILE.write_text(json.dumps(still, indent=2))
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(date.today()))
    ap.add_argument("--wait", type=float, default=45, help="minutes to wait for results")
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = _cfg()
    if not cfg["enabled"]:
        print("batch reviews disabled in config")
        return
    collect(wait_min=0)  # pick up anything left from previous runs first
    if not args.collect_only:
        obs = OBS_DIR / f"{args.date}.jsonl"
        if obs.exists():
            submitted = submit(obs, cfg, dry_run=args.dry_run)
            if submitted and not args.dry_run:
                collect(wait_min=args.wait)
        else:
            print(f"no observation file for {args.date}")


if __name__ == "__main__":
    main()
