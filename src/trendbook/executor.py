"""TRACK C — IBKR PAPER EXECUTOR (the "paper" leg the shadow manager reserves).

Morning job (launchd 06:35 PT, weekdays): computes the frozen v3 book's
targets for TODAY and aligns the IBKR paper account (DUQ*, port 4002) to
them with market orders, then appends a mode="paper" record to the SAME
ledger the shadow manager writes (logs/trendbook/ledger.jsonl).

Purpose per config/trend_forward_criteria.yaml (frozen 2026-07-06, BEFORE
the first order): measurement, not promotion — tracking error vs the
same-day simulation, measured all-in costs vs the v3 cost model, and zero
unexplained position diffs. OBSERVATION-ONLY.

Design rules:

- ZERO-TRANSCRIPTION TARGETS. Targets for day T are produced by calling the
  manager's own v3_step on the panel extended with a synthetic row for T
  (P[T] = P[T-1], so only the sizing/cap block matters — new_n depends only
  on T-1 info plus the monthly-refresh flag). The 14:30 shadow run computes
  the same step from the same frozen inputs, so its ledger "target" fields
  must equal ours bit-for-bit; ops cross-checks this daily (any mismatch is
  an unexplained diff and an alarm).

- CACHE HYGIENE. The morning refresh passes before=today so yfinance's
  partial in-session bar can never be frozen into the append-only history
  (the 14:30 manager run owns today's final close).

- FILL CONVENTION GAP IS THE MEASUREMENT. The model implies fills at T-1
  close; the paper book fills at T's open + real spread/commissions. The
  ledger records both (model_fill_ref = prev_close vs avg_fill_price); the
  gap series IS the forward test's product. ETF dividend timing (model uses
  adjusted closes, broker credits real dividends) is an accepted, explained
  divergence class.

- SAFETY. Refuses non-'D' (non-paper) accounts; touches ONLY its own 15
  instruments; per-order notional cap $250k and <= 40 orders/run; refuses
  stale shadow state (last_processed must be the previous trading day);
  idempotent per date (exec_state.json); weekend/holiday no-op; multiplier
  of every qualified futures contract must equal the model's leg.mult or
  the run aborts before any order.

Silent-failure pre-mortem (commissioning checklist item 2, 2026-07-16):
  - Gateway down / daily-restart logout at 06:35 -> connect fails ->
    exec_skipped ledger record + ntfy + ops trendexec.present_today alarm.
    (User-side mitigation: Gateway Configure > Lock and Exit > Auto restart.)
  - Yesterday's 14:30 manager run failed -> stale-state guard refuses to
    trade (never executes on old info) -> skip record + ntfy.
  - Partial in-session yfinance bar -> before=today truncation + the
    frontier==last_processed assertion (both must fail together to leak).
  - Order rejected/partial/unfilled -> intended_vs_executed_diffs + ntfy;
    ops trendexec fails on any diff (criteria: unexplained diffs = 0).
  - Transcription drift between executor targets and shadow targets ->
    impossible by construction (same v3_step), asserted daily anyway by
    ops trendexec.targets_match.
  - launchd TCC/env failure -> no record at all -> manifest entry
    trendexec_paper_daily alarms after 07:05.
  - Futures roll leaves stale-expiry position -> detected by symbol match /
    conId mismatch -> closed as explained "contract_roll_close" diff.

Usage:
  .venv/bin/python -m src.trendbook.executor              # daily paper run
  .venv/bin/python -m src.trendbook.executor --dry-run    # no orders placed
  Options: --no-refresh, --as-of YYYY-MM-DD, --port 4002

State:  data/trendbook/exec_state.json
Ledger: logs/trendbook/ledger.jsonl (shared with the shadow manager)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import src.research.futures_trend as ft
import src.research.hybrid_trend as ht
import src.research.hybrid_trend_v3 as ht3
import src.trendbook.manager as mgr
from src.research.futures_trend import refresh_days

ROOT = Path(__file__).resolve().parents[2]
EXEC_STATE_PATH = ROOT / "data" / "trendbook" / "exec_state.json"

CLIENT_ID = 23
DEFAULT_PORT = 4002
MAX_ORDERS = 40
MAX_ORDER_NOTIONAL = 250_000.0
FILL_TIMEOUT_S = 180
STAMP = mgr.STAMP

# model leg code -> IBKR micro contract (symbol, exchange). ETF legs use the
# leg's own ticker on SMART/USD.
FUT_CONTRACTS = {
    "ES": ("MES", "CME"), "NQ": ("MNQ", "CME"), "RTY": ("M2K", "CME"),
    "YM": ("MYM", "CBOT"), "CL": ("MCL", "NYMEX"),
    "6E": ("M6E", "CME"), "6B": ("M6B", "CME"), "6J": ("MJY", "CME"),
    "6A": ("M6A", "CME"),
}


def log(msg: str) -> None:
    print(f"[trendexec] {msg}")


def log_err(msg: str) -> None:
    print(f"[trendexec] ERROR: {msg}", file=sys.stderr)
    print(f"[trendexec] ERROR: {msg}")


def _notify(title: str, body: str) -> None:
    try:
        from src.engine.notify import send
        send(title, body, priority="high")
    except Exception as e:  # noqa: BLE001 — alerting must never crash the job
        log_err(f"notify failed: {e}")


def _holidays() -> set[str]:
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    return set(cfg["agent"].get("holidays_2026", []))


def _skip(day, reason, extra=None):
    rec = {"record": "exec_skipped", "mode": "paper", "stamp": STAMP,
           "book": "hybrid_trend_v3 (frozen 2026-07-06)",
           "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "date": str(day), "reason": reason}
    if extra:
        rec.update(extra)
    mgr.append_ledger(rec)
    log_err(f"SKIPPED {day}: {reason}")


# ── targets for day T via the manager's own frozen step ─────────────────────

def compute_targets(today: pd.Timestamp, refresh: bool = True):
    """Returns (targets dict, context dict) for trading day `today`, computed
    by v3_step itself on a synthetic-row extension of the panel. Raises on
    any state/panel inconsistency — never guesses."""
    refresh_errors = {}
    if refresh:
        for lg in ht.LEGS:
            try:
                n_new = mgr.refresh_price_cache(lg.ticker, lg.auto_adjust,
                                                before=today)
                if n_new:
                    log(f"refreshed {lg.ticker}: "
                        f"{'seeded' if n_new < 0 else f'+{n_new} row(s)'}")
            except Exception as e:  # noqa: BLE001
                refresh_errors[lg.ticker] = str(e)
                log_err(f"refresh failed for {lg.ticker}: {e}")
        try:
            mgr.refresh_dff()
        except Exception as e:  # noqa: BLE001
            refresh_errors["DFF"] = str(e)

    state = mgr.load_state()
    if state is None:
        raise RuntimeError("no shadow state — the manager must run first")
    legs = mgr.active_legs()
    cols = [lg.code for lg in legs]
    if state["legs"] != cols:
        raise RuntimeError(f"leg set changed: state {state['legs']} vs {cols}")

    idx, C, _R, sig, vol, _q, _d = ht.build_hybrid_panel(legs)
    lp = pd.Timestamp(state["last_processed"])
    if idx.max() != lp:
        raise RuntimeError(
            f"panel frontier {idx.max().date()} != shadow last_processed "
            f"{lp.date()} — did the 14:30 manager run fail, or did the "
            f"morning refresh leak a partial bar?")

    # previous trading day of `today` per our calendar must be lp
    hol = _holidays()
    prev = today - pd.Timedelta(days=1)
    while prev.weekday() >= 5 or str(prev.date()) in hol:
        prev -= pd.Timedelta(days=1)
    if lp != prev:
        raise RuntimeError(
            f"stale shadow state: last_processed {lp.date()}, expected "
            f"previous trading day {prev.date()} — refusing to trade")

    S, V, P = sig[cols].values, vol[cols].values, C[cols].values
    idx_ext = idx.append(pd.DatetimeIndex([today]))
    nan_row = np.full((1, len(cols)), np.nan)
    S_ext = np.vstack([S, nan_row])
    V_ext = np.vstack([V, nan_row])
    P_ext = np.vstack([P, P[-1:].copy()])       # dp = 0; pnl side inert
    monthly = refresh_days(idx_ext)
    mult = np.array([lg.mult for lg in legs])
    cost_side = np.array([lg.cost_side for lg in legs])
    is_etf = np.array([lg.kind == "etf" for lg in legs])
    n0 = np.array([state["positions"][c] for c in cols], float)
    eq0 = float(state["equity"])

    t = len(idx_ext) - 1
    new_n, _eq, diag = mgr.v3_step(t, S_ext, V_ext, P_ext, monthly, mult,
                                   cost_side, is_etf, n0, eq0, 0.0)
    prev_close = {c: (None if not np.isfinite(P[-1][i]) else float(P[-1][i]))
                  for i, c in enumerate(cols)}
    ctx = {"info_date": str(idx.max().date()), "equity_ref": eq0,
           "monthly_refresh": bool(monthly[t]), "n_active": diag["n_active"],
           "sign_flips": diag["flips"], "shadow_positions": state["positions"],
           "prev_close": prev_close, "refresh_errors": refresh_errors,
           "legs": legs, "cols": cols}
    return {c: float(v) for c, v in zip(cols, new_n)}, ctx


# ── IBKR side ────────────────────────────────────────────────────────────────

def _qualify_contracts(ib, legs):
    """code -> qualified tradeable contract. Futures resolve via ContFuture
    to the current front month; multiplier must match the model or we abort."""
    from ib_async import ContFuture, Contract, Stock
    out = {}
    for lg in legs:
        if lg.kind == "etf":
            c = Stock(lg.ticker, "SMART", "USD")
            ib.qualifyContracts(c)
            if not c.conId:
                raise RuntimeError(f"could not qualify ETF {lg.ticker}")
            out[lg.code] = c
        else:
            sym, exch = FUT_CONTRACTS[lg.code]
            cf = ContFuture(sym, exch, "USD")
            ib.qualifyContracts(cf)
            if not cf.conId:
                raise RuntimeError(f"could not qualify ContFuture {sym}@{exch}")
            fut = Contract(secType="FUT", conId=cf.conId, exchange=exch)
            ib.qualifyContracts(fut)
            m = float(fut.multiplier or 0)
            if abs(m - lg.mult) > 1e-9:
                raise RuntimeError(
                    f"multiplier mismatch {lg.code}/{sym}: IBKR {m} vs model "
                    f"{lg.mult} — sizing convention differs, ABORT")
            out[lg.code] = fut
    return out


def _held_positions(ib, contracts):
    """code -> held qty in OUR instruments only; plus roll list of positions
    in the same futures symbol but a non-front contract."""
    by_symbol = {}
    for lg_code, c in contracts.items():
        key = (c.secType, c.symbol)
        by_symbol[key] = lg_code
    held = {code: 0.0 for code in contracts}
    rolls = []
    for p in ib.positions():
        key = (p.contract.secType, p.contract.symbol)
        code = by_symbol.get(key)
        if code is None:
            continue                     # not ours — never touched
        if p.contract.conId == contracts[code].conId:
            held[code] += p.position
        else:                            # same symbol, stale expiry -> roll
            rolls.append((code, p.contract, p.position))
    return held, rolls


def _await_fills(ib, trades, timeout_s=FILL_TIMEOUT_S):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if all(t.isDone() for t in trades):
            break
        ib.sleep(2)
    return trades


def run(dry_run=False, as_of=None, refresh=True, port=DEFAULT_PORT):
    today = pd.Timestamp(as_of if as_of else dt.date.today()).normalize()
    day = str(today.date())
    if today.weekday() >= 5:
        log(f"no-op: {day} is a weekend")
        return 0
    if day in _holidays():
        log(f"no-op: {day} is a market holiday")
        return 0
    ex_state = (json.loads(EXEC_STATE_PATH.read_text())
                if EXEC_STATE_PATH.exists() else {})
    if not dry_run and ex_state.get("last_run") == day:
        log(f"no-op: already executed {day}")
        return 0

    try:
        targets, ctx = compute_targets(today, refresh=refresh)
    except Exception as e:  # noqa: BLE001
        _skip(day, f"target computation failed: {e}")
        _notify("trendexec SKIPPED", f"{day}: {e}")
        return 1

    legs, cols = ctx["legs"], ctx["cols"]
    kind = {lg.code: lg.kind for lg in legs}
    mult = {lg.code: lg.mult for lg in legs}

    from ib_async import IB, MarketOrder
    ib = IB()
    # self-heal a stuck clientId (a phantom Gateway session from a crashed
    # prior connect) by retrying on fresh ids — connection-only, no trade
    # logic change (2026-07-24: stuck clientId 23 silently killed 2 days of
    # 06:35 runs). Same clientId family so the account/orders are unaffected.
    last_err = None
    for cid in (CLIENT_ID, CLIENT_ID + 10, CLIENT_ID + 20, CLIENT_ID + 30):
        try:
            ib.connect("127.0.0.1", port, clientId=cid, timeout=20)
            if cid != CLIENT_ID:
                log(f"connected on fallback clientId {cid} (default {CLIENT_ID} was stuck)")
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            ib.disconnect()
    else:
        _skip(day, f"IBKR connect failed on all clientIds: {last_err} — is Gateway running?")
        _notify("trendexec SKIPPED", f"{day}: Gateway unreachable")
        return 1
    try:
        accts = ib.managedAccounts()
        if not accts or not accts[0].startswith("D"):
            _skip(day, f"SAFETY ABORT: account {accts} is not paper ('D')")
            return 1
        account = accts[0]

        contracts = _qualify_contracts(ib, legs)
        held, rolls = _held_positions(ib, contracts)

        plan = []                                     # (code, contract, delta, why)
        for code, c, qty in rolls:
            plan.append((code, c, -qty, "contract_roll_close"))
        for code in cols:
            delta = targets[code] - held[code]
            if delta != 0:
                plan.append((code, contracts[code], delta, "align_to_target"))

        # guards before ANY order
        if len(plan) > MAX_ORDERS:
            _skip(day, f"guard: {len(plan)} orders > {MAX_ORDERS}", {
                "targets": targets, "held": held})
            _notify("trendexec GUARD ABORT", f"{day}: {len(plan)} orders")
            return 1
        for code, _c, delta, _why in plan:
            ref = ctx["prev_close"].get(code) or 0.0
            notion = abs(delta) * ref * mult[code]
            if notion > MAX_ORDER_NOTIONAL:
                _skip(day, f"guard: {code} order notional ${notion:,.0f} > "
                           f"${MAX_ORDER_NOTIONAL:,.0f}",
                      {"targets": targets, "held": held})
                _notify("trendexec GUARD ABORT", f"{day}: {code} ${notion:,.0f}")
                return 1

        log(f"{day} targets (info {ctx['info_date']}, eq_ref "
            f"${ctx['equity_ref']:,.2f}, monthly={ctx['monthly_refresh']}):")
        for code in cols:
            mark = " *" if targets[code] != held[code] else ""
            log(f"  {code:4s} [{kind[code]}] target {targets[code]:+8,.0f}  "
                f"held {held[code]:+8,.0f}{mark}")
        if rolls:
            log(f"  rolls needed: {[(c, p.localSymbol, q) for c, p, q in rolls]}")

        if dry_run:
            log(f"DRY RUN: {len(plan)} order(s) would be placed — none sent")
            return 0

        orders_out, trades = [], []
        for code, c, delta, why in plan:
            side = "BUY" if delta > 0 else "SELL"
            tr = ib.placeOrder(c, MarketOrder(side, abs(delta)))
            trades.append(tr)
            orders_out.append({"code": code, "why": why, "side": side,
                               "qty": abs(delta),
                               "local_symbol": c.localSymbol or c.symbol,
                               "con_id": c.conId})
        if trades:
            _await_fills(ib, trades)

        diffs = []
        for o, tr in zip(orders_out, trades):
            st = tr.orderStatus
            o.update({"status": st.status, "filled": float(st.filled),
                      "avg_fill_price": float(st.avgFillPrice or 0.0),
                      "commission": round(sum(
                          f.commissionReport.commission for f in tr.fills
                          if f.commissionReport), 4)})
            if st.status != "Filled":
                diffs.append({"code": o["code"], "cause":
                              f"{st.status}: filled {st.filled}/{o['qty']}"})

        held_after, _ = _held_positions(ib, contracts)
        for code in cols:
            if held_after[code] != targets[code] and not any(
                    d["code"] == code for d in diffs):
                diffs.append({"code": code, "cause":
                              f"post-run position {held_after[code]} != "
                              f"target {targets[code]} (unexplained)"})

        net_liq = next((r.value for r in ib.accountSummary()
                        if r.tag == "NetLiquidation"), None)

        rec = {"record": "exec", "mode": "paper", "stamp": STAMP,
               "book": "hybrid_trend_v3 (frozen 2026-07-06)",
               "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
               "date": day, "info_date": ctx["info_date"],
               "account": account, "net_liquidation": net_liq,
               "equity_ref": ctx["equity_ref"],
               "monthly_refresh": ctx["monthly_refresh"],
               "targets": targets, "held_before": held,
               "held_after": held_after,
               "model_fill_ref": ctx["prev_close"],
               "orders": orders_out,
               "intended_vs_executed_diffs": diffs,
               "refresh_errors": ctx["refresh_errors"],
               "fill_convention": (
                   "paper: market orders at the open following info_date; "
                   "model reference is prev_close (v3 convention) — the gap "
                   "is the measured execution cost")}
        mgr.append_ledger(rec)

        EXEC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = EXEC_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "last_run": day, "targets": targets, "held_after": held_after,
            "orders": len(orders_out), "diffs": len(diffs),
            "updated_at": dt.datetime.now().isoformat(timespec="seconds")},
            indent=2, sort_keys=True))
        os.replace(tmp, EXEC_STATE_PATH)

        log(f"paper {day}: {len(orders_out)} order(s), {len(diffs)} diff(s) | "
            f"netliq {net_liq} | {STAMP}")
        if diffs:
            _notify("trendexec DIFFS", f"{day}: {diffs}")
        return 0
    finally:
        ib.disconnect()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="compute targets + read positions, place no orders")
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    return run(dry_run=args.dry_run, as_of=args.as_of,
               refresh=not args.no_refresh, port=args.port)


if __name__ == "__main__":
    sys.exit(main())
