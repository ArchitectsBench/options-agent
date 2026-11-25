#!/usr/bin/env python3
"""B5 FORCED-TIMEOUT DRILL — approved 2026-08-05, runs 08-06 pre-open.

PROVES against the REAL gateway that a place which times out AFTER IBKR may
have accepted it classifies AMBIGUOUS, leaves the intent UNBOUND, and is then
correctly adjudicated — never a blind resubmit, never a definitive reject.

THE FOUR APPROVED AMENDMENTS
  1. BUY-ONLY, FAR LIMIT. A far-BELOW-market BUY on a deep-OTM, far-dated
     contract cannot fill; a SELL could open a short. 1 lot.
  2. 2-ATTEMPT CAP. At most two forced-timeout attempts, then stop regardless.
  3. SUCCESS = INTENT ADJUDICATED AND CLOSED. Not merely "classified
     ambiguous": the drill must end with NO unbound intent — either bound to a
     real order or dropped because none exists.
  4. REPORT ARTIFACT written to logs/ops/b5_drill-<date>.json.

ABORT (any one -> stop, clean up, report):
  * an order FILLS
  * more than 2 drill orders appear
  * get_orders() returns None twice consecutively (broker view unreadable)
  * any non-drill symbol is touched
  * the engine is running (the drill owns the account)

CLEANUP always runs, even on abort: cancel ONLY drill orders carrying our
orderRef. The trend book (GLD/SHY/IEF/TLT/CPER/SLV + M6*U6 futures) is NEVER
touched — it is a different live strategy on the same paper account.

Run: .venv/bin/python scripts/ops/b5_timeout_drill.py [--dry-run]
Exit: 0 = drill PASSED · 1 = drill FAILED/aborted · 2 = preconditions not met
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MAX_ATTEMPTS = 2                 # amendment 2
DRILL_UNDERLYING = "SPY"
FORCED_PLACE_TIMEOUT_S = 0.001   # forces the client-side timeout deterministically


def _report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["stamp"] = "UNVALIDATED — PAPER ONLY"
    path.write_text(json.dumps(payload, indent=1, default=str))
    print(f"\nreport artifact -> {path}")


def _engine_running() -> bool:
    out = subprocess.run(["/bin/ps", "-eo", "command"], capture_output=True, text=True).stdout
    return "src/scheduler.py" in out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="verify preconditions and contract wiring; place nothing")
    args = ap.parse_args()

    now = datetime.now()
    rpt = {"drill": "B5_forced_timeout", "started_at": now.isoformat(timespec="seconds"),
           "dry_run": args.dry_run, "attempts": [], "aborted": None, "passed": False}
    out_path = ROOT / "logs" / "ops" / f"b5_drill-{now:%Y-%m-%d}.json"

    # ── preconditions ───────────────────────────────────────────────────────
    if _engine_running():
        rpt["aborted"] = "engine is RUNNING — the drill must own the account"
        print("ABORT:", rpt["aborted"]); _report(out_path, rpt); return 2

    from src.data import order_client as oc
    from src.data.ibkr_orders import IbkrOrderClient, ORDER_TAG
    from src.data.ibkr_thread import ThreadConfinedIbkrClient

    probe = IbkrOrderClient(client_id=205)
    if not probe.available():
        rpt["aborted"] = "IBKR client unavailable (gateway down or non-paper account)"
        print("ABORT:", rpt["aborted"]); _report(out_path, rpt); return 2
    acct = getattr(probe, "_account", "?")
    if not str(acct).startswith("D"):
        rpt["aborted"] = f"account {acct!r} is not an IB paper account"
        print("ABORT:", rpt["aborted"]); _report(out_path, rpt); return 2
    rpt["account"] = acct
    print(f"preconditions OK — engine down, paper account {acct}")

    # baseline: what exists BEFORE the drill (so cleanup touches only ours)
    before = probe.get_orders()
    if before is None:
        rpt["aborted"] = "baseline get_orders() returned None (broker view unreadable)"
        print("ABORT:", rpt["aborted"]); _report(out_path, rpt); return 2
    before_ids = {o["id"] for o in before}
    baseline_positions = probe.get_positions() or []
    rpt["baseline_orders"] = sorted(before_ids)
    rpt["baseline_option_positions"] = len(baseline_positions)
    print(f"baseline: {len(before_ids)} our-orders, {len(baseline_positions)} option positions")

    # ── drill contract: deep-OTM, far-dated, BUY far below market ──────────
    #    (amendment 1 — a BUY this far below the market cannot fill)
    try:
        import datetime as _dt
        from ib_async import Option, Stock
        ib = probe._ib
        ib.reqMarketDataType(1)
        spy = Stock(DRILL_UNDERLYING, "SMART", "USD"); ib.qualifyContracts(spy)
        t = ib.reqMktData(spy, "", False, False); ib.sleep(3)
        spot = next((float(v) for v in (t.last, t.bid, t.ask, t.close)
                     if v and v == v and v > 0), None)
        if not spot:
            rpt["aborted"] = "no SPY quote — cannot size a safe far strike"
            print("ABORT:", rpt["aborted"]); _report(out_path, rpt); return 2
        params = [c for c in ib.reqSecDefOptParams(DRILL_UNDERLYING, "", "STK", spy.conId)
                  if c.exchange == "SMART"]
        # Aggregate across ALL SMART param sets: params[0] alone can carry a
        # narrow strike list (it returned only strikes far below spot on 08-05,
        # which is why the naive pick landed deep ITM).
        all_strikes = sorted({x for c in params for x in c.strikes})
        all_exps = sorted({e for c in params for e in c.expirations})
        far_exp = all_exps[-1]                                   # farthest listed
        # deep-OTM CALL: only strikes ABOVE spot qualify. (A prior version took
        # the nearest strike to spot*1.30 across ALL strikes and selected one
        # deep ITM when the chain's listed strikes did not reach that far — safe
        # by limit price, but not what the drill documents. Filter explicitly.)
        _otm = [x for x in all_strikes if x > spot]
        if not _otm:
            rpt["aborted"] = "no strike above spot — cannot pick an OTM call"
            print("ABORT:", rpt["aborted"]); _report(out_path, rpt); return 2
        strike = min(_otm, key=lambda x: abs(x - spot * 1.30))
        limit = 0.01                                              # far below any real ask
        rpt["contract"] = {"underlying": DRILL_UNDERLYING, "expiration": far_exp,
                           "strike": strike, "right": "C", "side": "buy_to_open",
                           "quantity": 1, "limit": limit, "spot": spot}
        print(f"drill contract: {DRILL_UNDERLYING} {far_exp} C{strike} BUY 1 @ {limit} "
              f"(spot {spot}) — deep OTM, far-dated, unfillable limit")
    finally:
        try:
            probe.disconnect()
        except Exception:
            pass

    if args.dry_run:
        rpt["passed"] = True
        rpt["note"] = "dry run: preconditions + contract selection only, nothing placed"
        print("\nDRY RUN complete — nothing placed.")
        _report(out_path, rpt); return 0

    # ── forced-timeout attempts ─────────────────────────────────────────────
    occ = f"{DRILL_UNDERLYING}{far_exp[2:]}C{int(round(strike*1000)):08d}"
    none_streak = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n--- attempt {attempt}/{MAX_ATTEMPTS}: forcing a place timeout ---")
        client = ThreadConfinedIbkrClient(
            timeouts={"place": FORCED_PLACE_TIMEOUT_S, "read": 15.0, "default": 20.0},
            client_id=206 + attempt)
        res = client.place_option_order(DRILL_UNDERLYING, occ, "buy_to_open", 1, limit)
        verdict = oc.classify_post_result(res)
        rec = {"attempt": attempt, "raw": res, "classification": verdict["status"]}
        print(f"    raw        : {res}")
        print(f"    classified : {verdict['status']}")
        if verdict["status"] != oc.AMBIGUOUS:
            rec["FAIL"] = f"expected AMBIGUOUS, got {verdict['status']}"
            rpt["attempts"].append(rec)
            rpt["aborted"] = rec["FAIL"]
            print("    FAIL:", rec["FAIL"]); break

        # did an order actually land? (broker truth, bounded read)
        after = client.get_orders()
        if after is None:
            none_streak += 1
            rec["broker_read"] = "None"
            if none_streak >= 2:
                rpt["attempts"].append(rec)
                rpt["aborted"] = "get_orders() None twice consecutively"
                print("    ABORT:", rpt["aborted"]); break
        else:
            none_streak = 0
            new = [o for o in after if o["id"] not in before_ids]
            rec["new_orders"] = [o["id"] for o in new]
            filled = [o for o in new if o.get("status") == "filled"
                      or float(o.get("filled_quantity") or 0) > 0]
            if filled:
                rpt["attempts"].append(rec)
                rpt["aborted"] = f"an order FILLED ({[o['id'] for o in filled]})"
                print("    ABORT:", rpt["aborted"]); break
            if len(new) > MAX_ATTEMPTS:
                rpt["attempts"].append(rec)
                rpt["aborted"] = f"more than {MAX_ATTEMPTS} drill orders appeared"
                print("    ABORT:", rpt["aborted"]); break
            print(f"    new orders at broker: {rec['new_orders'] or 'none (POST never landed)'}")
        rpt["attempts"].append(rec)

    # ── amendment 3: the intent must be ADJUDICATED AND CLOSED ─────────────
    from src.engine import paper_trader as pt
    unbound_before = [o for o in pt._load(pt.PENDING_FILE) if o.get("order_id") is None]
    rpt["unbound_intents_before_adjudication"] = len(unbound_before)
    if unbound_before:
        print(f"\nadjudicating {len(unbound_before)} unbound intent(s) via resolve_intents...")
        try:
            pt.resolve_intents(pt.get_order_client())
        except Exception as exc:  # noqa: BLE001
            rpt["aborted"] = rpt["aborted"] or f"resolve_intents raised: {exc}"
    unbound_after = [o for o in pt._load(pt.PENDING_FILE) if o.get("order_id") is None]
    rpt["unbound_intents_after_adjudication"] = len(unbound_after)

    # ── cleanup: cancel ONLY drill orders (ours), never the trend book ─────
    cleanup = {"cancelled": [], "left": [], "trend_book_touched": False}
    try:
        # CANCEL VIA THE ORIGINATING clientId, KEYED BY permId (fixed 2026-08-06).
        # Two bugs this replaces, both found live:
        #  * keying by orderId under-reported: BOTH drill orders carried orderId=4
        #    with different permIds, so the old code cancelled one and reported
        #    success. A cleanup that under-reports is worse than one that fails.
        #  * IBKR scopes orders PER clientId: a fresh session gets Error 10147
        #    ("not found") and cannot cancel another session's orders. Each drill
        #    attempt used its own clientId, so cleanup must revisit each of them.
        # reqGlobalCancel is deliberately NOT used — it would also cancel the
        # trend book's orders on this shared paper account.
        from ib_async import IB as _IB
        for _cid in [206 + a for a in range(1, MAX_ATTEMPTS + 1)]:
            _ib = _IB()
            try:
                _ib.connect("127.0.0.1", 4002, clientId=_cid, timeout=8)
            except Exception:  # noqa: BLE001
                continue
            try:
                for t in (_ib.openTrades() or []):
                    perm = getattr(t.order, "permId", None)
                    if str(getattr(t.order, "orderRef", "")) != ORDER_TAG:
                        cleanup["left"].append(perm)      # not ours: never cancel
                        continue
                    _ib.cancelOrder(t.order)
                    cleanup["cancelled"].append(perm)     # keyed by permId
                _ib.sleep(2)
            finally:
                _ib.disconnect()
        c2 = IbkrOrderClient(client_id=209)
        remaining = c2.get_orders()
        cleanup["still_working_after_cleanup"] = (
            None if remaining is None
            else [o["id"] for o in remaining if o["id"] not in before_ids])
        post = c2.get_positions()
        cleanup["option_positions_after"] = None if post is None else len(post)
        c2.disconnect()
    except Exception as exc:  # noqa: BLE001
        cleanup["error"] = str(exc)[:160]
    rpt["cleanup"] = cleanup
    print(f"\ncleanup: cancelled={cleanup['cancelled']} left_foreign={cleanup['left']} "
          f"option_positions_after={cleanup.get('option_positions_after')}")

    # ── verdict ─────────────────────────────────────────────────────────────
    # cleanup must be PROVEN complete, not assumed
    if cleanup.get("still_working_after_cleanup"):
        rpt["aborted"] = (rpt["aborted"] or
                          f"cleanup incomplete: {cleanup['still_working_after_cleanup']} "
                          "still working at the broker")
    ok = (rpt["aborted"] is None
          and rpt["attempts"]
          and all(a["classification"] == oc.AMBIGUOUS for a in rpt["attempts"])
          and rpt["unbound_intents_after_adjudication"] == 0
          and cleanup.get("option_positions_after") == 0)
    rpt["passed"] = bool(ok)
    print("\n=== B5 DRILL:", "PASSED" if ok else f"FAILED ({rpt['aborted'] or 'see report'})", "===")
    _report(out_path, rpt)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
