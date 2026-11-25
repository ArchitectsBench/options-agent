"""One-shot backfill of 6/11 round trips (post-session logging fix).

The session's exit records carried sign-corrupted prices (Tradier reports
credit multileg closes with negative parent avg_fill_price). This script
re-derives every round trip from broker LEG-level data and appends corrected
records to fills.jsonl, marked backfilled: true. Idempotent: skips rec_ids
already backfilled.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.tradier_orders import PaperOrderClient
from src.engine.paper_trader import FILLS_LOG, exit_reason_code, order_fill_detail


def main():
    lines = FILLS_LOG.read_text().splitlines() if FILLS_LOG.exists() else []
    events = [json.loads(l) for l in lines]
    already = {e.get("rec_id") for e in events if e.get("backfilled")}
    entries = {e["rec_id"]: e for e in events if e.get("event") == "paper_fill"
               and e.get("ts", "").startswith("2026-06-11")}
    exits = {e["rec_id"]: e for e in events if e.get("event") == "paper_exit"
             and e.get("ts", "").startswith("2026-06-11")}

    client = PaperOrderClient()
    if not client.available():
        print("sandbox unavailable — cannot backfill")
        return 1

    n = 0
    for rec_id, entry in sorted(entries.items()):
        if rec_id in already or rec_id not in exits:
            continue
        exit_ev = exits[rec_id]
        entry_order = client.get_order(entry["order_id"])
        exit_order = client.get_order(exit_ev["order_id"])
        if not entry_order or not exit_order:
            print(f"{rec_id}: broker order(s) missing — skipped")
            continue
        ed, xd = order_fill_detail(entry_order), order_fill_detail(exit_order)
        entry_debit = ed["net_debit"]
        exit_credit = round(-xd["net_debit"], 4)
        pnl = round((exit_credit - entry_debit) * 100, 2)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": "round_trip",
            "backfilled": True,
            "rec_id": rec_id,
            "ticker": entry["ticker"],
            "exit_reason": exit_reason_code(exit_ev.get("close_reason")),
            "close_reason": exit_ev.get("close_reason"),
            "entry_order_id": entry["order_id"],
            "exit_order_id": exit_ev["order_id"],
            "entry_legs": ed["legs"],
            "exit_legs": xd["legs"],
            "entry_filled_at": ed["broker_closed"],
            "exit_filled_at": xd["broker_closed"],
            "entry_net_debit": entry_debit,
            "exit_net_credit": exit_credit,
            "pnl_per_contract": pnl,
            "corrects": {"logged_exit_price": exit_ev.get("exit_price"),
                         "logged_pnl": exit_ev.get("pnl_per_contract")},
            "stamp": "UNVALIDATED — PAPER ONLY",
        }
        with open(FILLS_LOG, "a") as fh:
            fh.write(json.dumps(record) + "\n")
        n += 1
        print(f"{rec_id} {entry['ticker']}: entry {entry_debit} -> exit {exit_credit} "
              f"P&L ${pnl:+.2f} [{record['exit_reason']}] (was logged as {exit_ev.get('pnl_per_contract')})")
    print(f"\nbackfilled {n} round trips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
