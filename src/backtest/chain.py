"""L2 backtester — options chain loader (build phase 1a).

Loads the per-strike ORATS store (data/iv_history_full/{ticker}_{yr}.parquet,
full chain w/ actual bid/ask + greeks) and exposes a clean per-day snapshot with
the selection primitives every multi-leg structure needs:
  - list expiries with true DTE; pick expiry nearest a target DTE
  - select a strike by target delta (call or put) or by strike/moneyness
  - quote any leg: bid / ask / mid / iv / delta / OI / DTE, with a `sellable`
    flag (zero/locked bid -> you cannot actually sell it).

Data quirks handled here so nothing downstream re-discovers them:
  * trade_date / expiration are M/D/YYYY text   -> parsed to dates, DTE computed.
  * 'delta' is the CALL delta (0..1)            -> put delta = delta - 1.
  * expiry-day / deep-ITM rows have iv=0 delta=1 zero-bid -> flagged not sellable;
    delta selection restricts to rows with valid iv (>0) and a live two-sided market.

  python src/backtest/chain.py --selftest
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
IV_FULL = ROOT / "data" / "iv_history_full"

# columns we pull from the parquet (lean — pricing + selection + liquidity)
_COLS = ["trade_date", "expiration", "strike", "spot",
         "cBidPx", "cAskPx", "pBidPx", "pAskPx", "cValue", "pValue",
         "call_iv", "put_iv", "delta", "gamma", "theta", "vega", "cOi", "pOi"]


def _parse_dates(s: pd.Series) -> pd.Series:
    """Fast M/D/YYYY -> datetime via unique-value map (cols have <2k uniques)."""
    u = pd.Series(s.unique())
    m = dict(zip(u, pd.to_datetime(u, format="mixed")))
    return s.map(m)


@dataclass
class Leg:
    """A single option leg quote at a point in time."""
    date: pd.Timestamp
    expiration: pd.Timestamp
    dte: int
    strike: float
    right: str            # 'C' or 'P'
    bid: float
    ask: float
    mid: float
    iv: float
    delta: float          # signed: calls + , puts -
    oi: float
    spot: float

    @property
    def sellable(self) -> bool:
        # a real two-sided market: positive bid, ask>=bid, finite spread
        return self.bid > 0 and self.ask >= self.bid


class ChainSnapshot:
    """All strikes/expiries for one ticker on one trade date."""

    def __init__(self, ticker: str, date: pd.Timestamp, df: pd.DataFrame):
        self.ticker = ticker
        self.date = date
        self.spot = float(df["spot"].iloc[0])
        self._df = df

    def expiries(self) -> pd.DataFrame:
        """Available expirations with DTE, sorted near->far."""
        e = (self._df[["expiration", "dte"]].drop_duplicates()
             .sort_values("dte").reset_index(drop=True))
        return e[e["dte"] >= 0]

    def nearest_expiry(self, target_dte: int, band: int = 10) -> pd.Timestamp | None:
        e = self.expiries()
        e = e[(e["dte"] - target_dte).abs() <= band]
        if e.empty:
            return None
        return e.iloc[(e["dte"] - target_dte).abs().argmin()]["expiration"]

    def _leg(self, row: pd.Series, right: str) -> Leg:
        if right == "C":
            bid, ask, iv, dl = row["cBidPx"], row["cAskPx"], row["call_iv"], row["delta"]
        else:
            bid, ask, iv, dl = row["pBidPx"], row["pAskPx"], row["put_iv"], row["delta"] - 1.0
        bid = float(bid or 0.0); ask = float(ask or 0.0)
        return Leg(self.date, row["expiration"], int(row["dte"]), float(row["strike"]),
                   right, bid, ask, (bid + ask) / 2 if bid > 0 and ask > 0 else float("nan"),
                   float(iv), float(dl), float((row["cOi"] if right == "C" else row["pOi"]) or 0),
                   self.spot)

    def _expiry_rows(self, expiration: pd.Timestamp) -> pd.DataFrame:
        return self._df[self._df["expiration"] == expiration]

    def select_by_delta(self, expiration, right: str, target_delta: float) -> Leg | None:
        """Strike whose |delta| is nearest target_delta (e.g. 0.16), among OTM
        options with a valid IV and a live market. right in {'C','P'}."""
        rows = self._expiry_rows(expiration)
        if rows.empty:
            return None
        if right == "C":
            cand = rows[(rows["strike"] > self.spot) & (rows["call_iv"] > 0)
                        & (rows["cBidPx"] > 0)].copy()
            cand["d"] = cand["delta"]
        else:
            cand = rows[(rows["strike"] < self.spot) & (rows["put_iv"] > 0)
                        & (rows["pBidPx"] > 0)].copy()
            cand["d"] = (cand["delta"] - 1.0).abs()
        cand = cand[cand["d"].between(0, 1)]
        if cand.empty:
            return None
        row = cand.iloc[(cand["d"].abs() - abs(target_delta)).abs().argmin()]
        return self._leg(row, right)

    def select_by_strike(self, expiration, right: str, strike: float) -> Leg | None:
        rows = self._expiry_rows(expiration)
        if rows.empty:
            return None
        row = rows.iloc[(rows["strike"] - strike).abs().argmin()]
        return self._leg(row, right)

    def quote(self, expiration, strike: float, right: str) -> Leg | None:
        """Exact-strike quote (nearest available). Used for daily mark-to-market
        and exit pricing of an already-open leg."""
        return self.select_by_strike(expiration, right, strike)


class ChainStore:
    """Per-ticker chain access over the full-store parquets, date-indexed."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        paths = sorted(IV_FULL.glob(f"{ticker}_*.parquet"))
        if not paths:
            raise FileNotFoundError(f"no per-strike store for {ticker} in {IV_FULL}")
        df = pd.concat([pd.read_parquet(p, columns=_COLS) for p in paths], ignore_index=True)
        df["trade_date"] = _parse_dates(df["trade_date"])
        df["expiration"] = _parse_dates(df["expiration"])
        df["dte"] = (df["expiration"] - df["trade_date"]).dt.days
        df = df[df["dte"] >= 0].sort_values(["trade_date", "expiration", "strike"])
        self._df = df
        self._by_date = {d: g for d, g in df.groupby("trade_date")}
        self.sessions = sorted(self._by_date)

    def snapshot(self, date) -> ChainSnapshot | None:
        date = pd.Timestamp(date)
        g = self._by_date.get(date)
        return ChainSnapshot(self.ticker, date, g) if g is not None else None

    def session_on_or_after(self, date) -> pd.Timestamp | None:
        date = pd.Timestamp(date)
        for d in self.sessions:
            if d >= date:
                return d
        return None

    def next_session(self, date) -> pd.Timestamp | None:
        return self.session_on_or_after(pd.Timestamp(date) + pd.Timedelta(days=1))


@lru_cache(maxsize=8)
def load(ticker: str) -> ChainStore:
    return ChainStore(ticker)


def selftest():
    store = load("SPY")
    print(f"SPY: {len(store.sessions)} sessions, {store.sessions[0].date()}..{store.sessions[-1].date()}")
    # a mid-history date
    d = store.sessions[len(store.sessions) // 2]
    snap = store.snapshot(d)
    exps = snap.expiries()
    print(f"\n{d.date()} spot={snap.spot:.2f}, {len(exps)} expiries, "
          f"DTE range {exps['dte'].min()}..{exps['dte'].max()}")
    exp = snap.nearest_expiry(35, band=12)
    dte = int(snap.expiries().set_index("expiration").loc[exp, "dte"])
    print(f"nearest expiry to 35 DTE: {pd.Timestamp(exp).date()} ({dte} DTE)")

    for right, tgt in [("C", 0.16), ("P", 0.16)]:
        leg = snap.select_by_delta(exp, right, tgt)
        assert leg is not None, f"no {right} leg found"
        assert leg.sellable, f"{right} leg not sellable"
        otm = (right == "C" and leg.strike > snap.spot) or (right == "P" and leg.strike < snap.spot)
        assert otm, f"{right} {tgt}d not OTM (strike {leg.strike} vs spot {snap.spot})"
        assert abs(abs(leg.delta) - tgt) < 0.06, f"{right} delta {leg.delta} far from {tgt}"
        assert leg.bid < leg.mid < leg.ask, "mid not between bid/ask"
        print(f"  {right} {tgt:.2f}Δ -> strike {leg.strike:.0f}  Δ={leg.delta:+.3f}  "
              f"bid/ask {leg.bid:.2f}/{leg.ask:.2f} mid {leg.mid:.2f}  iv {leg.iv:.3f}  oi {leg.oi:.0f}")

    # re-quote the same strike (mark-to-market path)
    leg = snap.select_by_delta(exp, "P", 0.16)
    q = snap.quote(exp, leg.strike, "P")
    assert abs(q.mid - leg.mid) < 1e-9
    nxt = store.next_session(d)
    print(f"\nnext session after {d.date()}: {pd.Timestamp(nxt).date()}")
    print("SELFTEST OK — chain loader: dates parsed, DTE/delta selection + sellable flag verified")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        ap.print_help()
