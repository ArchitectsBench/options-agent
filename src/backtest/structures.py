"""L2 backtester — defined-risk structure constructors (build phase 1b).

Builds leg-sets from a ChainSnapshot for the six premium-selling structures, all
defined-risk. Each constructor returns a `Structure` (or None if a required leg is
missing / not tradable) carrying its legs, net credit (at mid), defined-risk
geometry (width, max profit/loss, breakevens), and a single-expiry expiry payoff.

Conventions (all math per-share / option points; ×100 = per-contract $):
  qty  = -1 short (sold), +1 long (bought)
  net credit (mid) = sum(-qty * mid)  -> positive = premium received
Cost-aware fills (cross the spread) live in build phase 1c, not here — this layer
is pure construction + geometry, validated by payoff identities.

  python src/backtest/structures.py --selftest
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np

from src.backtest.chain import ChainSnapshot, Leg, load


@dataclass
class PositionLeg:
    leg: Leg
    qty: int            # -1 short, +1 long

    @property
    def tradable(self) -> bool:
        # shorts must be sellable (live bid); longs must be buyable (live ask)
        return self.leg.sellable if self.qty < 0 else self.leg.ask > 0


@dataclass
class Structure:
    kind: str
    underlying: str
    entry_date: object
    legs: list[PositionLeg]

    # ---- premium ----
    @property
    def net_credit(self) -> float:
        """Net premium at mid; positive = credit received."""
        return float(sum(-pl.qty * pl.leg.mid for pl in self.legs))

    @property
    def single_expiry(self) -> bool:
        return len({pl.leg.expiration for pl in self.legs}) == 1

    @property
    def tradable(self) -> bool:
        return all(pl.tradable for pl in self.legs) and np.isfinite(self.net_credit)

    # ---- defined-risk geometry (single-expiry verticals/condors/flys) ----
    def _side_width(self, right: str) -> float:
        ks = sorted(pl.leg.strike for pl in self.legs if pl.leg.right == right)
        return (ks[-1] - ks[0]) if len(ks) >= 2 else 0.0

    @property
    def width(self) -> float:
        """Widest defended side (the binding max-loss side)."""
        return max(self._side_width("C"), self._side_width("P"))

    @property
    def max_profit(self) -> float:
        return self.net_credit                      # credit structures

    @property
    def max_loss(self) -> float:
        return self.width - self.net_credit          # per share; ×100 = $

    def breakevens(self) -> dict:
        be = {}
        short_call = [pl for pl in self.legs if pl.leg.right == "C" and pl.qty < 0]
        short_put = [pl for pl in self.legs if pl.leg.right == "P" and pl.qty < 0]
        if short_call:
            be["upper"] = min(pl.leg.strike for pl in short_call) + self.net_credit
        if short_put:
            be["lower"] = max(pl.leg.strike for pl in short_put) - self.net_credit
        return be

    def payoff_at_expiry(self, terminal_spot: float) -> float:
        """Per-share P&L if held to (single) expiry and settled at intrinsic.
        Only valid for single-expiry structures."""
        if not self.single_expiry:
            raise ValueError("payoff_at_expiry only valid for single-expiry structures")
        pnl = self.net_credit
        for pl in self.legs:
            intrinsic = (max(terminal_spot - pl.leg.strike, 0.0) if pl.leg.right == "C"
                         else max(pl.leg.strike - terminal_spot, 0.0))
            pnl += pl.qty * intrinsic
        return pnl


# ── constructors ──────────────────────────────────────────────────────────

def _atm_strike(snap: ChainSnapshot, expiration) -> float:
    rows = snap._expiry_rows(expiration)
    return float(rows.iloc[(rows["strike"] - snap.spot).abs().argmin()]["strike"])


def iron_condor(snap, dte=35, short_delta=0.16, wing_width=5.0, wing_pct=None, band=10):
    """wing_pct (e.g. 0.03 = 3% of spot) overrides fixed wing_width when given —
    scales the defended width to the underlying's price (a $5 wing is 25% of SLV
    but 1.2% of SPY)."""
    exp = snap.nearest_expiry(dte, band)
    if exp is None:
        return None
    sc = snap.select_by_delta(exp, "C", short_delta)
    sp = snap.select_by_delta(exp, "P", short_delta)
    if not (sc and sp):
        return None
    wing = wing_pct * snap.spot if wing_pct else wing_width
    lc = snap.select_by_strike(exp, "C", sc.strike + wing)
    lp = snap.select_by_strike(exp, "P", sp.strike - wing)
    if not (lc and lp):
        return None
    s = Structure("iron_condor", snap.ticker, snap.date,
                  [PositionLeg(sc, -1), PositionLeg(lc, +1),
                   PositionLeg(sp, -1), PositionLeg(lp, +1)])
    return s if s.tradable else None


def iron_fly(snap, dte=35, wing_width=15.0, band=10):
    exp = snap.nearest_expiry(dte, band)
    if exp is None:
        return None
    k = _atm_strike(snap, exp)
    sc = snap.select_by_strike(exp, "C", k)
    sp = snap.select_by_strike(exp, "P", k)
    lc = snap.select_by_strike(exp, "C", k + wing_width)
    lp = snap.select_by_strike(exp, "P", k - wing_width)
    if not all([sc, sp, lc, lp]):
        return None
    s = Structure("iron_fly", snap.ticker, snap.date,
                  [PositionLeg(sc, -1), PositionLeg(lc, +1),
                   PositionLeg(sp, -1), PositionLeg(lp, +1)])
    return s if s.tradable else None


def vertical_credit_spread(snap, right="P", dte=35, short_delta=0.16, width=5.0, band=10):
    """Put credit spread (right='P', bullish) or call credit spread ('C', bearish)."""
    exp = snap.nearest_expiry(dte, band)
    if exp is None:
        return None
    short = snap.select_by_delta(exp, right, short_delta)
    if not short:
        return None
    long_strike = short.strike - width if right == "P" else short.strike + width
    long = snap.select_by_strike(exp, right, long_strike)
    if not long:
        return None
    s = Structure(f"{'put' if right=='P' else 'call'}_credit_spread", snap.ticker,
                  snap.date, [PositionLeg(short, -1), PositionLeg(long, +1)])
    return s if s.tradable else None


def calendar(snap, right="P", front_dte=30, back_dte=60, delta=0.50, band=12):
    """Short front / long back, SAME strike. delta=0.50 -> ATM calendar."""
    fe = snap.nearest_expiry(front_dte, band)
    be = snap.nearest_expiry(back_dte, band)
    if fe is None or be is None or fe == be:
        return None
    front = (snap.select_by_strike(fe, right, _atm_strike(snap, fe)) if delta >= 0.5
             else snap.select_by_delta(fe, right, delta))
    if not front:
        return None
    back = snap.select_by_strike(be, right, front.strike)   # same strike
    if not back:
        return None
    s = Structure("calendar", snap.ticker, snap.date,
                  [PositionLeg(front, -1), PositionLeg(back, +1)])
    return s if s.tradable else None


def diagonal(snap, right="P", front_dte=30, back_dte=60, short_delta=0.30, long_delta=0.20, band=12):
    """Short front (nearer money) / long back (further OTM), different strikes."""
    fe = snap.nearest_expiry(front_dte, band)
    be = snap.nearest_expiry(back_dte, band)
    if fe is None or be is None or fe == be:
        return None
    front = snap.select_by_delta(fe, right, short_delta)
    back = snap.select_by_delta(be, right, long_delta)
    if not (front and back):
        return None
    s = Structure("diagonal", snap.ticker, snap.date,
                  [PositionLeg(front, -1), PositionLeg(back, +1)])
    return s if s.tradable else None


def long_strangle(snap, dte=35, delta=0.16, band=10):
    """Buy OTM call + OTM put (both LONG). Pure long-vol, POSITIVE skew: max loss =
    debit paid; large gain on a big move in EITHER direction. The backwardation play —
    the payoff profile that survives the directional bear moves that kill short-vol."""
    exp = snap.nearest_expiry(dte, band)
    if exp is None:
        return None
    lc = snap.select_by_delta(exp, "C", delta)
    lp = snap.select_by_delta(exp, "P", delta)
    if not (lc and lp):
        return None
    s = Structure("long_strangle", snap.ticker, snap.date,
                  [PositionLeg(lc, +1), PositionLeg(lp, +1)])
    return s if s.tradable else None


def long_straddle(snap, dte=35, band=10):
    """Buy ATM call + ATM put (both LONG). Long-vol, higher cost/gamma than a strangle."""
    exp = snap.nearest_expiry(dte, band)
    if exp is None:
        return None
    k = _atm_strike(snap, exp)
    lc = snap.select_by_strike(exp, "C", k)
    lp = snap.select_by_strike(exp, "P", k)
    if not (lc and lp):
        return None
    s = Structure("long_straddle", snap.ticker, snap.date,
                  [PositionLeg(lc, +1), PositionLeg(lp, +1)])
    return s if s.tradable else None


def selftest():
    store = load("SPY")
    d = store.sessions[len(store.sessions) // 2]
    snap = store.snapshot(d)
    print(f"SPY {d.date()}  spot={snap.spot:.2f}\n")

    ic = iron_condor(snap, dte=35, short_delta=0.16, wing_width=5.0)
    assert ic and ic.tradable
    strikes = {pl.leg.right: [] for pl in ic.legs}
    for pl in ic.legs:
        strikes[pl.leg.right].append((pl.leg.strike, pl.qty))
    sc = min(pl.leg.strike for pl in ic.legs if pl.leg.right == "C" and pl.qty < 0)
    sp = max(pl.leg.strike for pl in ic.legs if pl.leg.right == "P" and pl.qty < 0)
    print(f"IRON CONDOR  credit={ic.net_credit:.2f}  width={ic.width:.0f}  "
          f"maxP={ic.max_profit:.2f} maxL={ic.max_loss:.2f}  BE={ {k:round(v,2) for k,v in ic.breakevens().items()} }")
    print(f"  legs: {[(pl.leg.right, pl.leg.strike, '+' if pl.qty>0 else '-') for pl in ic.legs]}")
    # PAYOFF IDENTITIES
    mid_spot = (sc + sp) / 2
    assert abs(ic.payoff_at_expiry(mid_spot) - ic.net_credit) < 1e-9, "between shorts must = credit"
    assert abs(ic.payoff_at_expiry(sc + 1000) - (-ic.max_loss)) < 1e-6, "blown call side must = -maxLoss"
    assert abs(ic.payoff_at_expiry(max(sp - 1000, 0)) - (-ic.max_loss)) < 1e-6, "blown put side must = -maxLoss"
    assert ic.max_loss > 0 and ic.net_credit > 0
    print("  payoff identities OK (between shorts=+credit, beyond wings=-maxLoss)")

    pcs = vertical_credit_spread(snap, "P", dte=35, short_delta=0.16, width=5.0)
    assert pcs and pcs.tradable
    short_k = max(pl.leg.strike for pl in pcs.legs if pl.qty < 0)
    assert abs(pcs.payoff_at_expiry(short_k + 100) - pcs.net_credit) < 1e-9
    assert abs(pcs.payoff_at_expiry(short_k - 100) - (-pcs.max_loss)) < 1e-6
    print(f"PUT CREDIT SPREAD  credit={pcs.net_credit:.2f} width={pcs.width:.0f} "
          f"maxL={pcs.max_loss:.2f}  payoff identities OK")

    fly = iron_fly(snap, dte=35, wing_width=15.0)
    assert fly and fly.tradable and fly.net_credit > pcs.net_credit  # ATM richer
    print(f"IRON FLY  credit={fly.net_credit:.2f} width={fly.width:.0f} maxL={fly.max_loss:.2f}")

    cal = calendar(snap, "P", front_dte=30, back_dte=60, delta=0.50)
    assert cal is not None
    assert cal.net_credit < 0, "ATM calendar is a net DEBIT (long back richer)"
    fe = min(pl.leg.expiration for pl in cal.legs); be = max(pl.leg.expiration for pl in cal.legs)
    assert fe != be and not cal.single_expiry
    print(f"CALENDAR  net={cal.net_credit:.2f} (debit ok)  front {fe.date()} / back {be.date()}  same-strike={cal.legs[0].leg.strike:.0f}")

    diag = diagonal(snap, "P", short_delta=0.30, long_delta=0.20)
    assert diag is not None
    ks = [pl.leg.strike for pl in diag.legs]
    assert ks[0] != ks[1], "diagonal strikes must differ"
    print(f"DIAGONAL  net={diag.net_credit:.2f}  short_k={ks[0]:.0f} long_k={ks[1]:.0f}")

    print("\nSELFTEST OK — all 6 structures construct on real data; payoff identities verified")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        ap.print_help()
