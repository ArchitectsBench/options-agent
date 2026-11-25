#!/usr/bin/env python3
"""Pre-session GO/NO-GO checks that need Python/ib_async (b, d, e, f, g).

Emits one line per check: "<id>|<PASS|FAIL|NA>|<detail>".
The shell wrapper (ibkr_go_nogo.sh) owns orchestration, alerting, and the
disarm action. This module NEVER writes the abort flag itself — it only reports.

Checks implemented here:
  b  gateway connected + D-account (paper) confirmed
  d  shadow-book file isolation: shadow rec_id prefix must not appear in ANY
     live file (position book, pending, fills.jsonl, agent_state)
  e  kill-switch state readable + reported; once a shadow client exists,
     additionally assert it REFUSES to submit while KILL is engaged (runtime
     check, not code review)
  f  yesterday's shadow reconcile clean (no orphans carried overnight)
  g  B1 regression sentinel: get_orders() must return broker truth or None —
     never a silent [] while the gateway itself reports open orders
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SHADOW_PREFIX = "SHDW-"          # reserved shadow rec_id prefix (pre-mortem V10)
# LANE REPOINT (2026-08-10): the shared books are retired and now empty, so
# scanning them for the shadow prefix passed vacuously. Glob the LANE books so
# a new lane directory is covered automatically rather than needing this list
# edited (the omission is exactly what made the check silently useless).
LIVE_FILES = sorted((ROOT / "data" / "lane").glob("*/*.json")) + [
    ROOT / "logs" / "observation" / "fills.jsonl",
    ROOT / "data" / "agent_state.json",
]


def emit(cid: str, status: str, detail: str) -> None:
    print(f"{cid}|{status}|{detail}")


def check_b_gateway():
    try:
        from src.data.ibkr_orders import IbkrOrderClient
        c = IbkrOrderClient()
        if not c.available():
            return emit("b", "FAIL", "IBKR client unavailable (gateway down, or "
                                     "non-paper account refused by the adapter)")
        acct = str(getattr(c, "_account", "?"))
        try:
            c.disconnect()
        except Exception:
            pass
        if not acct.startswith("D"):
            return emit("b", "FAIL", f"account {acct!r} is NOT an IB paper 'D' account")
        emit("b", "PASS", f"gateway connected, paper account {acct}")
    except Exception as exc:  # noqa: BLE001
        emit("b", "FAIL", f"gateway check errored: {str(exc)[:120]}")


def check_d_isolation():
    """Any shadow rec_id inside a LIVE file means the namespaces have merged —
    pre-mortem V1/V2/V3/V10. Cheap grep, run every session."""
    hits = []
    for p in LIVE_FILES:
        try:
            if p.exists() and SHADOW_PREFIX in p.read_text():
                hits.append(p.name)
        except Exception as exc:  # noqa: BLE001
            hits.append(f"{p.name}(unreadable:{exc})")
    if hits:
        return emit("d", "FAIL", f"shadow prefix {SHADOW_PREFIX!r} found in LIVE "
                                 f"file(s): {', '.join(hits)} — isolation BREACHED")
    emit("d", "PASS", f"no {SHADOW_PREFIX!r} in any live file "
                      f"({len(LIVE_FILES)} checked)")


def check_e_kill():
    try:
        from src.engine.live_gate import kill_switch_engaged
        ks = kill_switch_engaged()
    except Exception as exc:  # noqa: BLE001
        return emit("e", "FAIL", f"kill-switch state UNREADABLE: {str(exc)[:100]}")
    state = f"ENGAGED ({ks})" if ks else "clear"

    # Runtime assertion that the shadow path honours KILL — not code review.
    try:
        from src.engine.shadow_control import guard_shadow_submit, SHADOW_DISABLE_FILE
    except Exception:
        return emit("e", "PASS", f"kill switch {state}; shadow guard not present yet")
    if ks:
        blocked = guard_shadow_submit("go/no-go probe")
        if not blocked:
            return emit("e", "FAIL", f"kill switch {state} but shadow guard ALLOWED "
                                     "a submit — shadow does not honour KILL")
        return emit("e", "PASS", f"kill switch {state}; shadow guard correctly refuses")
    emit("e", "PASS", f"kill switch {state}; shadow guard present"
                      + (" (SHADOW_DISABLE set)" if SHADOW_DISABLE_FILE.exists() else ""))


def check_f_reconcile():
    """Yesterday's shadow reconcile must have carried no orphans overnight."""
    import glob
    import json
    files = sorted(glob.glob(str(ROOT / "logs" / "ops" / "shadow_reconcile-*.json")))
    if not files:
        return emit("f", "NA", "no prior shadow reconcile (shadow not yet run)")
    last = files[-1]
    try:
        d = json.loads(Path(last).read_text())
        orphans = d.get("orphans")
        n = len(orphans) if isinstance(orphans, list) else (orphans or 0)
        if n:
            return emit("f", "FAIL", f"{Path(last).name}: {n} orphan(s) carried overnight")
        emit("f", "PASS", f"{Path(last).name}: 0 orphans")
    except Exception as exc:  # noqa: BLE001
        emit("f", "FAIL", f"could not read {Path(last).name}: {str(exc)[:100]}")


def check_h_book_vs_broker():
    """ITEM 4 preflight surface (2026-08-06): a book/broker divergence must be
    visible BEFORE the engine starts. On 08-06 a stale book (7 phantom contracts
    the engine did not own) reached a live start; its exit manager would have
    tried to sell contracts we do not hold.

    2026-10-12: this check called the RAW audit_book_vs_broker(), which returns
    [] both when the book is clean and when the broker could not be read — so
    on 10-12, with port 4002 closed all day, (h) printed "[PASS] book matches
    broker" while the same night's checks JSON reported position_reconcile
    FAILED. audit_book_vs_broker_checked() exists for exactly this and states
    the rule a gate must follow: could_not_check is a FAILURE, because not
    knowing whether the book is stale is not knowing that it is fine."""
    try:
        from src.engine.paper_trader import audit_book_vs_broker_checked
        r = audit_book_vs_broker_checked()
        if r.get("status") == "could_not_check":
            return emit("h", "FAIL", "CANNOT VERIFY book vs broker — "
                                     f"{r.get('detail', 'broker unreadable')}")
        alarms = r.get("alarms") or []
        if alarms:
            kinds = ", ".join(f"{a['kind']}:{a['symbol']}({a['book_quantity']}"
                              f"vs{a['broker_quantity']})" for a in alarms)
            return emit("h", "FAIL", f"BOOK/BROKER DIVERGENCE — {kinds}")
        emit("h", "PASS", "book matches broker (no divergence)")
    except Exception as exc:  # noqa: BLE001
        emit("h", "FAIL", f"book/broker audit errored: {str(exc)[:120]}")


def check_g_b1_sentinel():
    """B1 REGRESSION SENTINEL. get_orders() must reflect BROKER truth (or None
    on read failure) — never a silent [] while the gateway reports open orders.

    The B1 defect: get_orders() iterates an in-process dict populated only by
    this process's own placeOrder calls, so a fresh process (e.g. after a
    restart) reports [] regardless of what IBKR actually holds. Detect it by
    comparing against a DIRECT broker query in the same fresh process.
    """
    try:
        from ib_async import IB
        from src.data.ibkr_orders import IbkrOrderClient
        c = IbkrOrderClient()
        if not c.available():
            return emit("g", "FAIL", "cannot run B1 sentinel — client unavailable")
        adapter = c.get_orders()
        ib = IB()
        ib.connect("127.0.0.1", 4002, clientId=137, timeout=10)
        try:
            broker_open = list(ib.reqAllOpenOrders() or [])
        finally:
            ib.disconnect()
        try:
            c.disconnect()
        except Exception:
            pass
        n_ad = None if adapter is None else len(adapter)
        n_br = len(broker_open)
        if adapter is None:
            return emit("g", "PASS", f"adapter signalled None (read failure); "
                                     f"broker reported {n_br} open")
        if n_ad == 0 and n_br > 0:
            return emit("g", "FAIL", f"B1 PRESENT: adapter get_orders()=[] while the "
                                     f"gateway reports {n_br} open order(s) — silent "
                                     "empty, unbound intents would be dropped")
        if n_br == 0:
            # NOT a pass: with nothing at the broker, adapter==broker==0 is
            # vacuously true and cannot distinguish a correct adapter from the
            # B1 defect. A sentinel that "passes" when it cannot see is exactly
            # the false assurance this matrix exists to eliminate.
            return emit("g", "NA", "UNINFORMATIVE — 0 open orders at the broker, so "
                                   "adapter=0 vs broker=0 cannot detect B1. Sentinel "
                                   "is only conclusive with >=1 live order (it becomes "
                                   "conclusive during the shadow session).")
        emit("g", "PASS", f"adapter={n_ad} vs broker={n_br} — consistent "
                          f"(conclusive: broker had {n_br} open)")
    except Exception as exc:  # noqa: BLE001
        emit("g", "FAIL", f"B1 sentinel errored: {str(exc)[:120]}")


if __name__ == "__main__":
    # DAILY PREFLIGHT (2026-08-04, post-migration): the shadow path was
    # superseded by the full flip to ibkr_paper, so the shadow-specific checks
    # (d isolation, f shadow reconcile) no longer have a subject. What remains
    # is the standing daily preflight: gateway up, paper account confirmed,
    # kill state readable, and the B1 sentinel (now meaningful every day,
    # because the live engine places real IBKR orders).
    check_b_gateway()
    check_e_kill()
    check_g_b1_sentinel()
    check_h_book_vs_broker()
