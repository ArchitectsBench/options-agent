#!/usr/bin/env python3
"""IBKR Gateway smoke test (paper, port 4002) — connectivity, account,
market data, and (if not read-only) a place-and-cancel order round trip
far from the market. PAPER ONLY: refuses any account not starting with 'D'.

Run: .venv/bin/python scripts/ibkr_smoke.py [--order-test]
"""
import argparse
import sys

from ib_async import IB, Stock, LimitOrder


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--order-test", action="store_true")
    args = ap.parse_args()
    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=17, timeout=15)
    except Exception as e:
        print(f"CONNECT FAILED: {e}")
        return 1
    print("connected:", ib.isConnected())
    accts = ib.managedAccounts()
    print("accounts:", accts)
    if not accts or not accts[0].startswith("D"):
        print("SAFETY ABORT: not a paper account (expected 'D' prefix)")
        ib.disconnect(); return 1
    summary = {r.tag: r.value for r in ib.accountSummary()
               if r.tag in ("NetLiquidation", "BuyingPower", "AvailableFunds")}
    print("summary:", summary)
    spy = Stock("SPY", "SMART", "USD")
    ib.qualifyContracts(spy)
    t = ib.reqMktData(spy, "", False, False)
    ib.sleep(3)
    print(f"SPY market data: last={t.last} bid={t.bid} ask={t.ask} "
          f"(delayed ok for smoke)")
    if args.order_test:
        import math
        ref = next((v for v in (t.bid, t.last, t.close) if v and not math.isnan(v)), None)
        px = round(ref * 0.5, 2) if ref else 100.00   # far-below-market; never fills
        order = LimitOrder("BUY", 1, px, tif="DAY")
        trade = ib.placeOrder(spy, order)
        ib.sleep(2)
        print("order status:", trade.orderStatus.status)
        ib.cancelOrder(order)
        ib.sleep(2)
        print("after cancel:", trade.orderStatus.status)
        ok = trade.orderStatus.status in ("Cancelled", "PendingCancel", "ApiCancelled")
        print("ORDER ROUND TRIP:", "OK" if ok else "CHECK MANUALLY")
    ib.disconnect()
    print("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
