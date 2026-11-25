#!/usr/bin/env python3
"""Pre-warm the ThetaData terminal ahead of the 17:45 enrichment job.

Scheduled 17:35 PT weekdays (com.optionsagent.thetaprewarm) so the JVM cold
start (~90s+) is absorbed before enrich runs. If the terminal is already
serving this is an instant no-op. Exits 0 when the terminal serves, 1 otherwise
— enrich still calls ensure_terminal() itself as a fallback, so a prewarm miss
degrades to the old behavior rather than breaking the night.

Root cause this addresses (2026-07-16): enrich's ensure_terminal() started the
JVM and timed out at 90s ~3s before it became ready, skipping the night's
market-truth run.

Entitlement (2026-10-22): serving=True is NOT the same fact as "the paid
endpoint answers". The terminal liveness check hits a FREE-tier endpoint, so a
FREE key serves happily while every /option/history/quote 403s — which is what
leaves enrichment rows as `no_market_data`. The prewarm now also probes
entitlement and writes a dated receipt, so the nightly report can read the
subscription state instead of inferring it from prewarm success. Exit code is
unchanged (serving only) on purpose: this is reporting, not a new gate.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.data import thetadata as td  # noqa: E402

RECEIPT_DIR = Path(__file__).resolve().parents[1] / "logs" / "ops"


def main() -> int:
    t0 = datetime.now()
    already = td.terminal_alive()
    ok = td.ensure_terminal(timeout_s=240)
    dt = (datetime.now() - t0).total_seconds()
    state = "already-serving" if already else ("started" if ok else "FAILED")
    print(f"{t0.isoformat(timespec='seconds')} theta prewarm: {state} "
          f"({dt:.0f}s, serving={ok})", flush=True)
    ent, detail = td.entitlement_probe() if ok else ("unreachable", "not serving")
    print(f"theta entitlement: {ent} ({detail})", flush=True)
    receipt = {"probed_at": datetime.now().isoformat(timespec="seconds"),
               "serving": ok, "entitlement": ent, "detail": detail}
    try:
        RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
        (RECEIPT_DIR / f"theta_entitlement-{t0:%Y-%m-%d}.json").write_text(
            json.dumps(receipt) + "\n")
    except OSError as exc:                 # reporting must never break the chain
        print(f"theta entitlement receipt not written: {exc}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
