#!/usr/bin/env python3
"""IBKR READ-ONLY rehearsal — parallel-run step (B), 2026-08-05.

Polls the LIVE IBKR paper gateway on the engine's real cadence (5 min) and
records exactly how it behaves across a full session: disconnects (1100/1102),
timeouts, the R2 None-vs-[] resolution, and recovery. Feeds criterion 5 of
docs/parallel_run_plan.md (breaker/deadline behaviour under real gateway drops).

ZERO ORDERS. Two independent guarantees, belt AND suspenders:
  BELT       — this script never calls a place_*/cancel_* method. The client is
               wrapped in _ReadOnlyClient, which RAISES on any write method, so
               a submit is not merely un-called but structurally impossible.
  SUSPENDERS — the paper-only guard is re-verified at session start: the account
               must be 'D'-prefixed (IB paper) or the rehearsal REFUSES to run.

Touches NO live-engine state: it does not read or write the position book,
pending intents, or agent_state. It only reads from the broker and appends to
its own log. The live Tradier-backed engine is unaffected and unaware.

Run: .venv/bin/python scripts/ibkr_readonly_rehearsal.py [--minutes 390]
Output: logs/ops/ibkr_rehearsal-<date>.jsonl  (+ EOD summary to stdout)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PT = ZoneInfo("America/Los_Angeles")
_WRITE_METHODS = ("place_option_order", "place_spread_order", "cancel_order")


class ReadOnlyViolation(RuntimeError):
    """A write was attempted during a read-only rehearsal. Must never happen."""


class _ReadOnlyClient:
    """Structural read-only wrapper. Any write method raises instead of
    reaching the broker — the rehearsal cannot place an order even by mistake."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in _WRITE_METHODS:
            def _blocked(*a, **k):
                raise ReadOnlyViolation(
                    f"{name}() attempted during READ-ONLY rehearsal — blocked")
            return _blocked
        return getattr(self._inner, name)


def _now():
    return datetime.now(PT)


def poll_once(client) -> dict:
    """One read-only sweep, mirroring what the engine reads each cycle.
    Distinguishes None (read FAILED — the R2 case) from [] (broker says none)."""
    row = {"ts": _now().isoformat(timespec="seconds")}
    for name, fn in (("orders", client.get_orders),
                     ("positions", client.get_positions)):
        t0 = time.monotonic()
        try:
            val = fn()
            row[name] = {
                "outcome": ("error_none" if val is None
                            else "empty" if len(val) == 0 else "data"),
                "count": (None if val is None else len(val)),
                "latency_ms": round((time.monotonic() - t0) * 1000),
            }
        except Exception as exc:  # noqa: BLE001
            row[name] = {"outcome": "exception", "error": str(exc)[:160],
                         "latency_ms": round((time.monotonic() - t0) * 1000)}
    try:
        row["connected"] = bool(client.available())
    except Exception as exc:  # noqa: BLE001
        row["connected"] = False
        row["available_error"] = str(exc)[:120]
    return row


def summarize(rows: list[dict]) -> str:
    """EOD gateway-stability report, stated against criterion 5."""
    n = len(rows)
    if not n:
        return "no polls recorded"
    def cnt(key, outcome):
        return sum(1 for r in rows if (r.get(key) or {}).get("outcome") == outcome)
    disconnects = sum(1 for r in rows if not r.get("connected"))
    err_none = cnt("orders", "error_none") + cnt("positions", "error_none")
    exc = cnt("orders", "exception") + cnt("positions", "exception")
    empty = cnt("orders", "empty") + cnt("positions", "empty")
    data = cnt("orders", "data") + cnt("positions", "data")
    lat = [x for r in rows for k in ("orders", "positions")
           for x in [(r.get(k) or {}).get("latency_ms")] if isinstance(x, int)]
    lat.sort()
    p50 = lat[len(lat) // 2] if lat else None
    p95 = lat[int(len(lat) * 0.95) - 1] if len(lat) >= 20 else (lat[-1] if lat else None)
    # recovery: a not-connected poll followed later by a connected one
    recovered = any(not rows[i].get("connected") and
                    any(r.get("connected") for r in rows[i + 1:])
                    for i in range(n - 1))
    return "\n".join([
        "── IBKR READ-ONLY REHEARSAL — EOD GATEWAY-STABILITY REPORT ──",
        f"polls: {n}   window: {rows[0]['ts']} .. {rows[-1]['ts']}",
        f"disconnected polls : {disconnects} ({100*disconnects/n:.0f}%)",
        f"read outcomes      : data={data} empty={empty} "
        f"error_none={err_none} exception={exc}",
        f"latency (ms)       : p50={p50} p95={p95}",
        f"recovery observed  : {'YES' if recovered else 'no disconnect to recover from'}",
        "",
        "CRITERION 5 (breaker/deadline under real gateway drops) — evidence:",
        f"  * {err_none} read(s) resolved as None (FAILED) vs {empty} as [] (genuinely"
        " empty). The R2 contract requires these stay distinguishable; any"
        " error_none proves the distinction fired against the real gateway.",
        f"  * {disconnects} poll(s) saw the client unavailable"
        + ("; recovery WAS observed." if recovered else "; no drop occurred."),
        "  * NOTE: a clean session does NOT satisfy criterion 5 — the forced-timeout"
        " drill is still required (frozen plan, criterion 4/5).",
        "",
        "STANDING ANNOTATION (pending B1): get_orders() is PROCESS-LOCAL — it",
        "  iterates this process's own placeOrder results, not broker truth, so the",
        "  'orders' column above reports only what THIS process placed (always 0 in a",
        "  read-only session). It is NOT evidence about the broker's order book and",
        "  must not be read as such. POSITIONS and EXECUTIONS are broker-sourced and",
        "  are the real criterion-5 evidence in this report. See B1 in",
        "  docs/ibkr_verification_matrix.md.",
        "",
        "ORDERS PLACED: 0 (structurally blocked — read-only wrapper).",
        "UNVALIDATED — PAPER ONLY",
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=390, help="session length")
    ap.add_argument("--interval", type=int, default=300, help="engine cadence (s)")
    ap.add_argument("--once", action="store_true", help="single poll (smoke test)")
    args = ap.parse_args()

    from src.data.ibkr_orders import IbkrOrderClient
    raw = IbkrOrderClient()
    client = _ReadOnlyClient(raw)

    # SUSPENDERS: paper-only guard re-verified before a single poll runs.
    if not client.available():
        print("REFUSING: IBKR client unavailable (gateway down, or NOT a paper "
              "account — the adapter refuses non-'D' accounts by design).")
        return 2
    acct = getattr(raw, "_account", "?")
    if not str(acct).startswith("D"):
        print(f"REFUSING: account {acct!r} is not an IB paper ('D') account.")
        return 2

    # BELT: prove the write guard actually bites before trusting the session.
    try:
        client.place_option_order("SPY", "SPY260807C00770000", "buy_to_open", 1, 0.5)
        print("REFUSING: read-only guard did NOT block a place call.")
        return 2
    except ReadOnlyViolation:
        pass

    out = ROOT / "logs" / "ops" / f"ibkr_rehearsal-{_now():%Y-%m-%d}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"READ-ONLY rehearsal armed — acct {acct}, write-guard verified, "
          f"cadence {args.interval}s -> {out.name}")

    rows: list[dict] = []
    deadline = time.monotonic() + args.minutes * 60
    try:
        while True:
            row = poll_once(client)
            rows.append(row)
            with out.open("a") as f:
                f.write(json.dumps(row) + "\n")
            print(f"[{row['ts'][11:19]}] connected={row['connected']} "
                  f"orders={row['orders'].get('outcome')} "
                  f"positions={row['positions'].get('outcome')}", flush=True)
            if args.once or time.monotonic() >= deadline:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ninterrupted — writing summary")
    finally:
        try:
            raw.disconnect()
        except Exception:
            pass

    report = summarize(rows)
    print("\n" + report)
    (ROOT / "logs" / "ops" / f"ibkr_rehearsal-{_now():%Y-%m-%d}.report.txt").write_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
