#!/usr/bin/env python3
"""IBKR real-time market-data ENTITLEMENT check — the standing "is it live yet?"
test for the options-book migration. READ-ONLY (reqMktData only, never an order).

Background (2026-07-27): after logging in as naraismha007 (Paper mode) with active
OPRA + US Securities Snapshot subscriptions shared to paper account DUQ000000, the
API still returns Error 10089 (delayed-only) — real-time entitlement is not being
delivered to the paper account. IBKR sharing changes take effect the NEXT business
day, so this reruns automatically each weekday morning until it passes.

Verdict keys on ERROR CODES (hours-independent), not on live prices (which only
stream when the market is open):
  - 10089 / 10091 / 10167  -> NOT entitled (delayed only)         -> FAIL
  - 10197                  -> competing live session (blocked)    -> FAIL
  - none of the above + live prices        -> real-time WORKS     -> PASS
  - none of the above + market closed      -> entitled, no quotes -> PASS (tentative)

Run: .venv/bin/python scripts/ibkr_entitlement_check.py [--notify] [--port 4002]
                                                        [--timeout 120]
Exit 0 = PASS, 1 = FAIL/blocked, 2 = gateway down / inconclusive.

The whole probe runs under a wall-clock bound (`--timeout`, default 120 s): a
gateway that ACCEPTS and then stalls post-connect (2026-10-05) used to hang it
indefinitely with no verdict. Expiry is reported as 2, deliberately never 0 —
see ENTITLEMENT_TIMEOUT_S below.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BAD = {10089, 10091, 10167, 10168, 354}   # entitlement-missing family
COMPETING = {10197}
INFRA = {2104, 2106, 2107, 2158, 2119, 2103, 2105, 2100, 2150, 300, 2119, 2158}


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


STATE_FILE = ROOT / "logs" / "ops" / "ibkr_entitlement_state.json"


def _notify(title: str, body: str, priority: str = "default",
            tags: str = "satellite") -> None:
    """Push to the phone channel. PRIORITY MATTERS (fixed 2026-08-04): the
    original sent every outcome at ntfy DEFAULT priority with an identical tag,
    so a week of routine FAILs and the one GREEN that starts the cutover clock
    would have looked identical on the phone — alert fatigue by construction."""
    # Every ntfy target (ntfy.sh + the second server, 2026-09-13) via the
    # engine's notifier. Its per-target failure warnings are silenced HERE:
    # callers capture this script's output with 2>&1 and read the verdict line,
    # so a stray stderr line must never follow it (parity with the old silent path).
    try:
        import logging
        logging.getLogger("trader").addHandler(logging.NullHandler())
        sys.path.insert(0, str(ROOT))
        from src.engine.notify import _ntfy_notify
        _ntfy_notify(title, body + "\nUNVALIDATED — PAPER ONLY", priority, tags)
    except Exception:
        pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, indent=1))
    except Exception:
        pass


def _error_code(msg: str) -> str:
    """Pull the entitlement error code out of the verdict for the TITLE, so the
    code is visible on the lock screen without opening the notification."""
    m = re.search(r"\[(\d+)\]", msg) or re.search(r"\b(101\d\d|10089)\b", msg)
    return m.group(1) if m else ""


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def _market_open_et(now=None) -> bool:
    """Regular US session (weekday 09:30-16:00 ET). Liveness/frozen assertions
    are only meaningful while the market is open."""
    from zoneinfo import ZoneInfo
    now = now or _dt.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 570 <= mins < 960


def _today_et() -> str:
    """Today's exchange date as YYYYMMDD — the same ET ruler as
    `_market_open_et`, because an option expiry is an exchange date."""
    from zoneinfo import ZoneInfo
    return _dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")


def _nearest_expiry(expirations, today: str) -> str | None:
    """The nearest expiry that has NOT already passed, or None.

    `reqSecDefOptParams` keeps just-expired dates in its returned list for a
    while after they expire, so a bare `sorted(expirations)[0]` can hand back
    YESTERDAY. An expired option legitimately quotes bid/ask/last = None, which
    is indistinguishable from a missing OPRA entitlement — that is how check
    (a) failed on 2026-10-02 (picked `20261001`) and again on 2026-10-09
    (picked `20261008` while the gateway was healthy and the data line free).

    Dates are fixed-width YYYYMMDD, so string ordering is chronological and no
    parsing is needed. `>=` is deliberate: an expiry dated today is still live
    through the close, which is why 2026-10-08 picking `20261008` was correct.
    """
    future = sorted(e for e in expirations if e >= today)
    return future[0] if future else None


def _quote(t) -> dict:
    """bid/ask/last as real numbers. IBKR uses -1 (and NaN) for 'no data' —
    the 2026-08-04 false-pass came from counting a static `close` as live, so
    `close` is captured but NEVER treated as evidence of a live feed."""
    q = {k: _num(getattr(t, k, None)) for k in ("bid", "ask", "last", "close")}
    return {k: (None if (v is None or v < 0) else v) for k, v in q.items()}


def _is_live(q: dict) -> bool:
    """A live feed means a two-sided quote or a trade print — never `close`."""
    return (q.get("bid") is not None and q.get("ask") is not None) or q.get("last") is not None


def check(port: int) -> tuple[int, str]:
    """Verdict on BOTH feeds the options book needs (2026-08-04):
      1. STOCK  (Network B / ARCA — SPY) real-time
      2. OPRA   (an ATM, near-dated SPY option) real-time, with spread sanity
    A stock-only PASS is NOT sufficient: equities and OPRA are separate
    subscriptions, so a green on SPY alone could start the cutover clock while
    the options data the book actually trades on is still delayed.

    Exit codes: 0 = PASS (both live) · 1 = FAIL (any entitlement/competing/
    frozen/missing-feed condition) · 2 = gateway down / inconclusive.
    """
    if not _port_open(port):
        return 2, f"GATEWAY DOWN — port {port} closed (IB Gateway not logged in)"
    from ib_async import IB, Option, Stock
    errs: list[int] = []
    ib = IB()
    ib.errorEvent += lambda rid, c, m, con=None: errs.append(c)
    try:
        ib.connect("127.0.0.1", port, clientId=120, timeout=10)
    except Exception as exc:
        return 2, f"CONNECT FAILED on {port}: {str(exc)[:80]}"

    open_now = _market_open_et()
    acct = "?"
    detail = {}
    try:
        accts = ib.managedAccounts()
        acct = accts[0] if accts else "?"
        ib.reqMarketDataType(1)

        # ---- 1. STOCK, polled twice for frozen-quote detection ----
        spy = Stock("SPY", "SMART", "USD")
        ib.qualifyContracts(spy)
        st = ib.reqMktData(spy, "", False, False)
        ib.sleep(5)
        s1 = _quote(st)
        ib.sleep(4)
        s2 = _quote(st)
        detail["stock"] = s2
        detail["stock_poll1"] = s1
        spot = s2.get("last") or s2.get("bid") or s2.get("close")

        # ---- 2. OPRA: ATM, nearest expiry that has NOT already expired ----
        opt_q = {}
        chosen = None
        today_et = _today_et()
        try:
            params = [c for c in ib.reqSecDefOptParams("SPY", "", "STK", spy.conId)
                      if c.exchange == "SMART"]
            pick = None
            for c in params:
                near = [s for s in c.strikes if spot and abs(s - spot) <= 5]
                exp = _nearest_expiry(c.expirations, today_et)
                if near and exp:
                    pick = (exp, min(near, key=lambda s: abs(s - spot)))
                    break
            if pick is None and spot:
                # No param set had an ATM strike; fall back to the nearest
                # strike of the first set that still has an unexpired expiry.
                for c in params:
                    exp = _nearest_expiry(c.expirations, today_et)
                    if exp and c.strikes:
                        pick = (exp, min(c.strikes, key=lambda s: abs(s - spot)))
                        break
            if pick is None:
                detail["opra_error"] = f"no expiry >= {today_et} in secdef params"
            else:
                exp, strike = pick
                chosen = f"SPY {exp} C{strike}"
                opt = Option("SPY", exp, strike, "C", "SMART", currency="USD")
                ib.qualifyContracts(opt)
                ot = ib.reqMktData(opt, "", False, False)
                ib.sleep(6)
                opt_q = _quote(ot)
        except Exception as exc:  # noqa: BLE001
            detail["opra_error"] = str(exc)[:80]
        detail["option"] = opt_q
        detail["contract"] = chosen
    finally:
        ib.disconnect()

    codes = sorted({c for c in errs if c not in INFRA})
    if COMPETING & set(codes):
        return 1, (f"FAIL (competing live session, 10197) — acct {acct}; another "
                   "session holds the market-data line (log out live sessions on "
                   "ALL devices, then restart the gateway)")
    if BAD & set(codes):
        return 1, (f"FAIL (delayed-only, {sorted(BAD & set(codes))}) — acct {acct}; "
                   "real-time NOT delivered to paper")

    s, o = detail.get("stock", {}), detail.get("option", {})
    if not open_now:
        return 0, (f"PASS (tentative, market CLOSED) — acct {acct}: no entitlement "
                   f"errors; liveness unverifiable outside the session. stock={s} "
                   f"option[{detail.get('contract')}]={o}")

    # --- market is OPEN: both feeds must be genuinely live ---
    if not _is_live(s):
        return 1, (f"FAIL (no live STOCK quote while market open) — acct {acct}: "
                   f"{s} (a `close`-only quote is delayed/static, not real-time)")
    # FROZEN-QUOTE detection: SPY is the most liquid US equity there is — if
    # bid/ask/last are byte-identical across two polls ~4s apart during the
    # regular session, the feed is not ticking (stale/frozen snapshot served as
    # if live), which is a FAIL even though no entitlement error was raised.
    s1 = detail.get("stock_poll1", {})
    if s1 and all(s1.get(k) == s.get(k) for k in ("bid", "ask", "last")):
        return 1, (f"FAIL (FROZEN stock quote) — acct {acct}: bid/ask/last "
                   f"unchanged across two polls while market open: {s}. Feed is "
                   "not ticking — treat as not-real-time.")
    if not detail.get("contract"):
        return 1, (f"FAIL (could not resolve an ATM SPY option to test OPRA) — "
                   f"acct {acct}: {detail.get('opra_error', 'no chain params')}")
    if not _is_live(o):
        return 1, (f"FAIL (no live OPRA quote while market open) — acct {acct}: "
                   f"option[{detail['contract']}]={o}. Equities may be entitled "
                   "while OPRA is NOT — the options book needs both")
    # spread sanity on the option: two-sided, correctly ordered, not absurd
    if o.get("bid") is not None and o.get("ask") is not None:
        if o["ask"] < o["bid"]:
            return 1, (f"FAIL (crossed OPRA quote) — acct {acct}: "
                       f"bid={o['bid']} > ask={o['ask']} on {detail['contract']}")
        mid = (o["bid"] + o["ask"]) / 2
        if mid > 0 and (o["ask"] - o["bid"]) / mid > 1.5:
            return 1, (f"FAIL (implausible OPRA spread) — acct {acct}: "
                       f"bid={o['bid']} ask={o['ask']} on {detail['contract']}")
    return 0, (f"PASS ✅ BOTH FEEDS REAL-TIME — acct {acct}: "
               f"SPY={s.get('last') or s.get('bid')} | "
               f"OPRA {detail['contract']} bid={o.get('bid')} ask={o.get('ask')} "
               "(no 10089/10197)")


# ── bounded run: a hung probe must still produce a verdict (fail closed) ─────
# 2026-11-11, lifted verbatim in design from rvol_exec_go_nogo.run_checks_bounded
# (mid_session 2026-11-10). Same family, same defect: on 2026-10-05 the gateway
# port ACCEPTED and then stalled, so the post-connect waits hung ~15 h 21 m. An
# ABSENT gateway (ECONNREFUSED, the current day-22 state) fails fast at
# _port_open and does NOT reproduce it — so the bound belongs around the whole
# check, not around connect.
#
# The expiry verdict is **2 (inconclusive), never 0**: code 0 latches
# `first_pass_at` and fires the one-shot urgent CUTOVER-CLOCK page, so a stall
# must never be able to start the migration clock.
ENTITLEMENT_TIMEOUT_S = 120.0


def check_bounded(port: int, timeout_s: float = ENTITLEMENT_TIMEOUT_S) -> tuple[int, str, bool]:
    """`check` under a wall-clock bound. Returns (code, msg, timed_out).

    The probe runs in a daemon thread on purpose: a stalled IB wait is not
    reliably interruptible from Python, so we abandon it rather than pretend we
    can cancel it. The CLI must therefore force-exit (os._exit) instead of
    returning normally, or the abandoned thread's live IB socket keeps the
    interpreter alive past the bound we just promised.
    """
    box: dict[str, tuple[int, str]] = {}

    def _run() -> None:
        try:
            box["res"] = check(port)
        except Exception as exc:  # noqa: BLE001
            box["res"] = (2, f"CHECK RAISED: {type(exc).__name__}: {str(exc)[:120]}")

    t = threading.Thread(target=_run, name="entitlement_check", daemon=True)
    t.start()
    t.join(timeout_s)
    if "res" in box:
        code, msg = box["res"]
        return code, msg, False
    return 2, (f"GATEWAY STALLED — probe did not return within {timeout_s:g}s "
               f"on port {port} (accepted-then-hung; inconclusive, NOT a pass)"), True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true", help="push result to ntfy")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--timeout", type=float, default=ENTITLEMENT_TIMEOUT_S,
                    help="wall-clock bound on the whole probe (seconds)")
    args = ap.parse_args()
    code, msg, timed_out = check_bounded(args.port, args.timeout)
    print(msg)
    if args.notify:
        st = _load_state()
        already_green = bool(st.get("first_pass_at"))
        if code == 0:
            if not already_green:
                # THE CUTOVER-CLOCK MESSAGE. This is the single notification the
                # whole migration waits on — it must be URGENT and unmistakable,
                # never one more line in a log read at 9pm. Latched: fires once.
                _notify("🟢 IBKR ENTITLEMENT GREEN — CUTOVER CLOCK STARTS",
                        f"{msg}\n\nReal-time data is now delivered to the paper "
                        "account. NEXT (pre-written, docs/parallel_run_plan.md): "
                        "parallel run -> review -> one-commit cutover.",
                        priority="urgent", tags="rotating_light,white_check_mark")
                st["first_pass_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            else:
                _notify("IBKR entitlement: still green", msg,
                        priority="low", tags="white_check_mark")
        elif code == 1:
            ec = _error_code(msg)
            _notify(f"IBKR entitlement FAIL{' — Error ' + ec if ec else ''}",
                    msg, priority="high", tags="warning")
        else:
            _notify("IBKR entitlement: GATEWAY DOWN / inconclusive", msg,
                    priority="high", tags="electric_plug")
        st["last_verdict"] = ("timeout" if timed_out
                              else {0: "pass", 1: "fail"}.get(code, "gateway"))
        st["last_checked_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        st["last_msg"] = msg
        _save_state(st)
    if timed_out:
        # The abandoned daemon thread may hold a live IB socket, so a normal
        # return can block on interpreter shutdown. Force-exit — but only AFTER
        # the notify branch and _save_state above have run, or the stall would
        # be silent as well as unbounded.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
