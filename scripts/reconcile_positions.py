#!/usr/bin/env python3
"""Administrative close of broker-lost paper positions (lesson L20).

When the sandbox deletes a position our book still holds (2026-07-14: SMCI +
IWM vanished overnight; every close rejected "cannot sell unless closing a
long position"), no real exit fill can ever exist. This tool:

  1. Computes the FULL engine-vs-broker leg drift via the pure
     `reconcile_drift()` (src/engine/reconcile_drift.py) and reports every
     drift class — broker_lost, orphan_at_broker, quantity_mismatch, in_sync;
  2. VERIFIES every leg of a target position is absent at the broker before
     closing it (a position is administratively closed ONLY when EVERY leg is
     classified `broker_lost`; if the broker still holds any leg it is skipped,
     "broker still holds — not lost");
  3. appends a `position_reconcile_close` event to fills.jsonl — full
     context, last verifiable mark, informational P&L, and an explicit
     NOT-GATE-EVIDENCE stamp. It is deliberately NOT a `paper_exit`, so no
     gate tracker, P&L aggregate, or UI stream ever counts it as a round
     trip: the trade simply never completes in evidence terms;
  4. removes the position from paper_positions.json (UI reflects) and drops
     its pending close orders.

Manual-position safety: only positions in OUR book are ever closed. Broker-only
legs (the user's manual HON/GOOGL/DELL/PLTR trades) surface as
`orphan_at_broker`; with `adopt_foreign=false` (config.yaml default) they are
reported and left untouched — never flagged, never closed.

Dry-run by default. Run: .venv/bin/python scripts/reconcile_positions.py
    [--rec-id ID ...] [--apply]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "logs" / "observation" / "fills.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec-id", action="append", default=[])
    ap.add_argument("--apply", action="store_true")
    # EXPIRY (2026-08-10..15): "the broker lost the position" and "the position
    # EXPIRED" are different economic events. This tool was written for the
    # first and prices it at the last verifiable mark. For an expired position
    # that mark is stale and materially wrong (WMT 08-14: stale mark implied
    # -$4.50; the position actually settled +$678.00 with both legs ITM).
    # Rather than guess a settlement, REFUSE and require the operator to supply
    # the verified value — fail closed, same discipline as the truth ruler.
    ap.add_argument("--settlement-value", type=float, default=None,
                    help="verified net settlement per contract for an EXPIRED "
                         "position (blended across any partial closes)")
    ap.add_argument("--settlement-note", default=None,
                    help="how --settlement-value was derived (recorded verbatim)")
    args = ap.parse_args()

    from src.data.order_client import get_order_client
    from src.engine.reconcile_drift import reconcile_drift

    # BROKER RESOLUTION (2026-08-15): was hardcoded to the Tradier sandbox
    # while the configured broker is ibkr_paper — the same defect fixed in
    # src/api/reconcile.py. Comparing a live book against an abandoned broker
    # would report every real leg as "lost" and make them all closable.
    client = get_order_client()
    # Safety: without a verifiable broker view we cannot prove a position is
    # absent — an empty/disconnected read must NOT masquerade as "everything
    # lost". Refuse rather than close on unverifiable state.
    if hasattr(client, "available") and not client.available():
        print("broker client unavailable — cannot verify positions; aborting "
              "(nothing closed).")
        return 1

    raw = client.get_positions()
    # R2: a FAILED READ IS NOT AN EMPTY BROKER. None here would otherwise mean
    # "every leg is lost" and make the whole book administratively closable.
    if raw is None:
        print("broker position read FAILED (not an empty account) — aborting "
              "(nothing closed).")
        return 1
    broker_positions = list(raw)
    broker = {p["symbol"]: float(p["quantity"]) for p in broker_positions}

    # LANE REPOINT (2026-08-15): read/write the LANE books; the shared
    # paper_positions.json/paper_orders.json were retired by L-e and are empty,
    # so this tool silently operated on nothing.
    from src.engine.paper_trader import (load_positions, load_positions_for_lane,
                                         lane_of, lane_book_path, _load, _save)
    book = load_positions()

    # adopt_foreign governs how orphan (untracked-at-broker) legs are handled.
    # Default False protects the user's manual positions in the shared sandbox.
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
        adopt_foreign = bool(cfg.get("agent", {}).get("adopt_foreign", False))
    except Exception:
        adopt_foreign = False

    # Full leg-level diff (pure, side-effect-free). We LOG its one-liners here.
    diff = reconcile_drift(book, broker_positions, adopt_foreign=adopt_foreign)
    for line in diff["actions"]:
        print(line)
    if diff["matched"]:
        print(f"in_sync: {len(diff['matched'])} leg(s) reconciled")

    # A position is administratively closable only when EVERY one of its legs is
    # broker_lost. Any leg still held at the broker (in_sync / quantity_mismatch)
    # keeps the position off-limits — preserving the "broker still holds" safety.
    lost_syms = {d["symbol"] for d in diff["drift"] if d["class"] == "broker_lost"}

    targets = [p for p in book if not args.rec_id or p["rec_id"] in args.rec_id]
    closed_ids = []
    for p in targets:
        leg_syms = [leg["symbol"] for leg in p["legs"]]
        if not leg_syms or not all(sym in lost_syms for sym in leg_syms):
            held = {sym: broker.get(sym, 0.0) for sym in leg_syms}
            print(f"SKIP {p['rec_id']} {p['ticker']}: broker still holds {held} — not lost")
            continue
        # EXPIRY FAIL-CLOSED: an expired position's last mark is stale by
        # definition; pricing a settlement off it produces a confidently wrong
        # number in the ledger. Refuse unless the operator supplies the
        # verified settlement.
        expired = bool(p.get("expiration")) and \
            str(p["expiration"]) < datetime.now().strftime("%Y-%m-%d")
        if expired and args.settlement_value is None:
            print(f"REFUSE {p['rec_id']} {p['ticker']}: EXPIRED {p['expiration']} — "
                  f"last mark {p.get('last_value')} is stale and would misprice the "
                  f"settlement. Re-run with --settlement-value (and --settlement-note).")
            continue
        if expired:
            last_mark = args.settlement_value
            info_pnl = round((float(last_mark) - float(p["entry_price"])) * 100
                             * int(p.get("quantity", 1)), 2)
        else:
            last_mark = p.get("last_value")
            info_pnl = (round((float(last_mark) - float(p["entry_price"])) * 100
                              * int(p.get("quantity", 1)), 2)
                        if last_mark is not None else None)
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": "position_reconcile_close",
            "rec_id": p["rec_id"], "ticker": p["ticker"],
            "quantity": p.get("quantity", 1),
            "entry_price": p.get("entry_price"),
            "entry_order_id": p.get("entry_order_id"),
            "last_verifiable_mark": last_mark,
            "informational_pnl_total": info_pnl,
            "close_reject_count": p.get("close_reject_count"),
            "mark_high": p.get("mark_high"), "mark_low": p.get("mark_low"),
            "mark_series": p.get("mark_series"),
            "exit_reason": "expired_settled" if expired else "broker_position_lost",
            "evidence": ("EXCLUDED — administrative close, no verifiable exit fill "
                         "exists (broker deleted the position). NOT a round trip; "
                         "never counted by any gate tracker (L20)."),
            "stamp": "UNVALIDATED — PAPER ONLY",
        }
        if expired:
            event["expiration"] = p["expiration"]
            event["settlement_value"] = args.settlement_value
            event["settlement_note"] = args.settlement_note
            event["evidence"] = (
                "EXCLUDED — administrative close. The position reached EXPIRY only "
                "because the broker connection failed 08-12..08-14, so the outcome "
                "is an INFRASTRUCTURE ARTIFACT, not a strategy result: the exit was "
                "correctly submitted at time_flat on 08-11 and would have completed. "
                "Settlement is verified (see settlement_note) but this is NOT a "
                "round trip and is never counted by any gate tracker (L20).")
        print(f"{'CLOSE' if args.apply else 'DRY-RUN'} {p['rec_id']} {p['ticker']}: "
              f"entry {p.get('entry_price')} x{p.get('quantity')}, "
              f"{'settlement' if expired else 'last mark'} {last_mark}, "
              f"informational P&L {info_pnl}")
        if not args.apply:
            # show the EXACT row that would be appended — reviewing a summary
            # line is not reviewing the write
            shown = {k: v for k, v in event.items() if k != "mark_series"}
            shown["mark_series"] = f"<{len(event.get('mark_series') or [])} marks>"
            print(json.dumps(shown, indent=1))
        if args.apply:
            with LEDGER.open("a") as f:
                f.write(json.dumps(event) + "\n")
            closed_ids.append(p["rec_id"])
    if args.apply and closed_ids:
        from src.engine.paper_trader import POSITIONS_FILE, PENDING_FILE
        book = [p for p in book if p["rec_id"] not in closed_ids]
        pending = [o for o in _load(PENDING_FILE) if o.get("rec_id") not in closed_ids]
        # SENTINEL writes PARTITION by lane, so a row can never land in the
        # other lane's book (L-e). Do not write lane files by hand here.
        _save(POSITIONS_FILE, book)
        _save(PENDING_FILE, pending)
        print(f"applied: {len(closed_ids)} positions reconciled, book now {len(book)} open")
    return 0


if __name__ == "__main__":
    sys.exit(main())
