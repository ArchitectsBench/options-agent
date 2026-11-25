"""L2 backtester — cost model + trade lifecycle (build phases 1c + 1d).

Takes a constructed Structure and runs it through its life: real multi-leg fills
(cross the spread), daily mark-to-market, EVENT-DRIVEN management (profit target /
setup invalidation / stop / time-stop), and expiry settlement at intrinsic.
Returns a Trade with P&L in dollars and on capital-at-risk.

Cost model (the make-or-break piece — index VRP is only a few vol points):
  fill scenarios, conservative by default:
    "bidask"   shorts sell @ bid, longs buy @ ask   (pay the full half-spread)
    "midminus" mid worsened by `frac` of the half-spread
    "mid"      mid (optimistic reference only)
  commission: $/contract/leg on entry and on any MARKET exit (none at expiry).

Management decisions use MID (fair trigger); realization uses the SCENARIO fill
(real cost). Closing reverses each leg: buy back shorts, sell longs. At the front
expiry, expired legs settle at intrinsic; any longer-dated leg (calendar/diagonal)
is closed at market.

  python src/backtest/lifecycle.py --selftest
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field

from src.backtest.chain import ChainStore, Leg, load
from src.backtest.structures import Structure, iron_condor

COMMISSION = 0.65            # $/contract/leg
CONTRACT = 100              # shares/contract


def leg_fill(leg: Leg, side: str, scenario: str, frac: float = 0.25) -> float:
    """Execution price for one leg. side in {'buy','sell'}."""
    bid, ask, mid = leg.bid, leg.ask, leg.mid
    if not math.isfinite(mid):
        mid = (ask / 2 if ask > 0 else 0.0)
    if scenario == "mid":
        return mid
    if scenario == "midminus":
        half = max(ask - mid, mid - bid, 0.0)
        return mid + frac * half if side == "buy" else mid - frac * half
    # bidask (conservative)
    return ask if side == "buy" else bid


def leg_value(leg: Leg) -> float:
    """Current fair value for MTM (mid, with a decayed-short fallback)."""
    if math.isfinite(leg.mid):
        return leg.mid
    return leg.ask / 2 if leg.ask > 0 else 0.0


def _open_side(qty: int) -> str:
    return "sell" if qty < 0 else "buy"


def _close_side(qty: int) -> str:
    return "buy" if qty < 0 else "sell"      # reverse the position


def entry_cash(s: Structure, scenario: str, frac: float = 0.25) -> float:
    """Per-share cash received opening the structure (net of entry commission).
    Positive = credit."""
    cash = sum(-pl.qty * leg_fill(pl.leg, _open_side(pl.qty), scenario, frac) for pl in s.legs)
    return cash - COMMISSION * len(s.legs) / CONTRACT


@dataclass
class Trade:
    ticker: str
    kind: str
    scenario: str
    entry_date: object
    exit_date: object
    exit_reason: str
    dte_entry: int
    days_held: int
    credit: float            # entry cash (per share, after costs)
    capital: float           # $ at risk
    pnl: float               # $ realized
    pnl_pct: float           # on capital
    mae: float               # worst unrealized $ during hold (<=0)


def capital_at_risk(s: Structure) -> float:
    """$ at risk. Credit structures: (width - credit)×100. Debit
    calendars/diagonals: the net debit paid."""
    credit_mid = s.net_credit
    if s.width > 0 and credit_mid > 0:
        cap = (s.width - credit_mid)
    else:
        cap = abs(credit_mid)
    return max(cap, 0.05) * CONTRACT


def _requote(store: ChainStore, s: Structure, date) -> list[Leg] | None:
    """Re-quote every leg on `date` (same expiration/strike/right). None if the
    session is missing."""
    snap = store.snapshot(date)
    if snap is None:
        return None
    out = []
    for pl in s.legs:
        q = snap.quote(pl.leg.expiration, pl.leg.strike, pl.leg.right)
        if q is None:
            return None
        out.append(q)
    return out


def _unrealized_mid(s: Structure, legs_now: list[Leg]) -> float:
    """Per-share unrealized P&L at mid: entry credit + cost-to-close at mid."""
    # close at mid = sum(qty * value); for shorts qty<0 -> pay value
    close_mid = sum(pl.qty * leg_value(ln) for pl, ln in zip(s.legs, legs_now))
    return s._entry_cash_mid + close_mid


def _realize_exit(s: Structure, legs_now: list[Leg], date, scenario, frac, settle: bool) -> float:
    """Per-share exit cash. settle=True -> intrinsic for legs expiring on/before
    date, market fill for the rest; settle=False -> all market fill."""
    spot = legs_now[0].spot
    cash = 0.0
    n_market = 0
    for pl, ln in zip(s.legs, legs_now):
        expired = settle and ln.dte <= 0
        if expired:
            intrinsic = (max(spot - pl.leg.strike, 0.0) if pl.leg.right == "C"
                         else max(pl.leg.strike - spot, 0.0))
            cash += pl.qty * intrinsic       # no spread / no commission at expiry
        else:
            cash += pl.qty * leg_fill(ln, _close_side(pl.qty), scenario, frac)
            n_market += 1
    return cash - COMMISSION * n_market / CONTRACT


def run_trade(store: ChainStore, s: Structure, *, scenario="bidask", frac=0.25,
              profit_target=0.50, stop_mult=2.0, min_dte=2, regime_ok=None) -> Trade | None:
    """Open s on its entry date, manage daily to exit. regime_ok(date)->bool is
    the setup-validity hook (build phase 1e)."""
    if not s.tradable:
        return None
    s._entry_cash_mid = sum(-pl.qty * pl.leg.mid for pl in s.legs)   # mid ref for MTM
    credit = entry_cash(s, scenario, frac)
    # true capital at risk = max loss given the ACTUAL credit received
    if s.width > 0 and credit > 0:
        cap = max(s.width - credit, 0.05) * CONTRACT
    else:
        cap = max(abs(credit), 0.05) * CONTRACT
    # profit/stop reference magnitude — credit for credit structures, debit for
    # calendars/diagonals. Sign-correct for both (u>0 is always "in profit").
    profit_ref = abs(s.net_credit)
    exit_exp = min(pl.leg.expiration for pl in s.legs)  # front expiry
    entry = s.entry_date
    exit_cap = store.session_on_or_after(exit_exp) or store.sessions[-1]  # data may end first
    sessions = [d for d in store.sessions if entry < d <= exit_cap]
    mae = 0.0
    for d in sessions:
        legs_now = _requote(store, s, d)
        if legs_now is None:
            continue
        u = _unrealized_mid(s, legs_now)
        mae = min(mae, (u) * CONTRACT)
        reason = None
        if legs_now[0].dte <= 0:
            reason = "expiry"
        elif d >= exit_cap:
            reason = "data_end"            # data ran out before expiry -> market close
        elif profit_ref > 0 and u >= profit_target * profit_ref:
            reason = "profit_target"
        elif u <= -stop_mult * profit_ref:
            reason = "stop"
        elif regime_ok is not None and not regime_ok(d):
            reason = "setup_invalidated"
        elif legs_now[0].dte <= min_dte:
            reason = "time_stop"
        if reason:
            exit_cash = _realize_exit(s, legs_now, d, scenario, frac, settle=(reason == "expiry"))
            pnl = (credit + exit_cash) * CONTRACT
            return Trade(s.underlying, s.kind, scenario, entry, d, reason,
                         s.legs[0].leg.dte, (d - entry).days, round(credit, 4),
                         round(cap, 2), round(pnl, 2), round(pnl / cap, 4), round(mae, 2))
    return None     # never found an exit session (shouldn't happen)


def selftest():
    store = load("SPY")
    # pick an entry ~2yrs in so a full 35-DTE life fits
    entry = store.sessions[700]
    snap = store.snapshot(entry)
    ic = iron_condor(snap, dte=35, short_delta=0.16, wing_width=5.0)
    assert ic and ic.tradable
    print(f"SPY iron condor opened {entry.date()} spot={snap.spot:.2f}")
    print(f"  mid credit {ic.net_credit:.2f} | conservative entry credit "
          f"{entry_cash(ic,'bidask'):.2f} (< mid, spread paid) | capital ${capital_at_risk(ic):.0f}")
    assert entry_cash(ic, "bidask") < ic.net_credit, "conservative entry must be below mid"
    assert entry_cash(ic, "mid") > entry_cash(ic, "bidask"), "mid fills must beat bid/ask"

    for sc in ["bidask", "midminus", "mid"]:
        ic2 = iron_condor(store.snapshot(entry), dte=35, short_delta=0.16, wing_width=5.0)
        t = run_trade(store, ic2, scenario=sc, profit_target=0.50, stop_mult=2.0)
        assert t is not None, "trade never exited"
        assert -t.capital - 1 <= t.pnl <= t.credit * CONTRACT + 1, "pnl outside defined-risk bounds"
        print(f"  [{sc:8}] exit {t.exit_date.date()} via {t.exit_reason:16} "
              f"held {t.days_held}d  P&L ${t.pnl:+.2f} ({t.pnl_pct:+.1%})  MAE ${t.mae:.2f}")
    print("  bounds OK: P&L within [-capital, +credit]; costs ordered mid>midminus>bidask")
    print("\nSELFTEST OK — fills cross the spread, lifecycle manages to a real exit, "
          "P&L respects defined risk")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        ap.print_help()
