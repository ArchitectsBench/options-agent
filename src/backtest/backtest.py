"""L2 backtester — runner + Stage-6 gate (build phase 1f).

Rolls regime-gated, non-overlapping premium-selling trades through history for a
set of underlyings, pools them, and reports the honest verdict:
  - per-trade stats + per-REGIME P&L (the 2022-bear acid test is mandatory)
  - compounded equity curve (fixed % risk/trade) + max drawdown
  - Stage-6 acceptance gate: Sharpe>=1.2, OOS>=0.8, maxDD<=20%, MC5pct>0.5, >=100 trades
  - results across fill scenarios (a strategy that only passes at "mid" is a fail)

Methodology (stated so it can be argued with):
  * per-ticker NON-OVERLAPPING: after a trade exits, next entry is the first
    gate-open session on/after the exit -> clean sequential equity, no concurrent-
    margin modeling.
  * trade return r = pnl / capital-at-risk. Annualized Sharpe = mean(r)/std(r) ×
    sqrt(trades_per_year). Equity compounds risking `risk_frac` of equity/trade.
  * OOS = last 30% of trades by entry date. MC = bootstrap resample of r (B=2000),
    5th-percentile annualized Sharpe.

  python src/backtest/backtest.py            # runs the SPY+ETF iron condor experiment
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from functools import partial

import numpy as np
import pandas as pd

from src.backtest.chain import load
from src.backtest.lifecycle import run_trade
from src.backtest.regime import GateParams, RegimeGate
from src.backtest.structures import iron_condor
from src.research.vrp_screen import _regime   # reuse regime labelling

LIQUID_ETFS = ["SPY", "QQQ", "IWM", "TLT", "SLV", "XLE", "XLF"]


def run_backtest(tickers, structure_fn, *, gate: RegimeGate, scenario="bidask",
                 profit_target=0.50, stop_mult=2.0, min_dte=2, step=1):
    """Non-overlapping gated trades per ticker, pooled. Returns trades DataFrame."""
    rows = []
    for tk in tickers:
        try:
            store = load(tk)
        except FileNotFoundError:
            continue
        sessions = store.sessions
        i = 252                                   # need history for iv_rank
        while i < len(sessions):
            d = sessions[i]
            if not gate.ok(tk, d):
                i += step
                continue
            snap = store.snapshot(d)
            s = structure_fn(snap)
            if s is None or not s.tradable:
                i += step
                continue
            # asymmetric: enter on strict gate, hold until profit/stop/time OR a
            # genuine vol shock (hard_break) — NOT on daily entry-gate flicker
            t = run_trade(store, s, scenario=scenario, profit_target=profit_target,
                          stop_mult=stop_mult, min_dte=min_dte,
                          regime_ok=lambda dd, _tk=tk: not gate.hard_break(_tk, dd))
            if t is None:
                i += step
                continue
            rows.append(asdict(t))
            # resume at first session strictly after exit
            while i < len(sessions) and sessions[i] <= t.exit_date:
                i += 1
    df = pd.DataFrame(rows)
    if not df.empty:
        df["entry_date"] = pd.to_datetime(df["entry_date"])
        df["r"] = df["pnl"] / df["capital"]
        df = df.sort_values("entry_date").reset_index(drop=True)
    return df


def _annual_sharpe(r: np.ndarray, trades_per_year: float) -> float:
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=1) * np.sqrt(trades_per_year))


def _equity_maxdd(r: np.ndarray, risk_frac=0.02):
    eq = np.cumprod(1 + risk_frac * r)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    return eq, float(-dd.min())


def metrics(df: pd.DataFrame) -> dict:
    r = df["r"].to_numpy()
    span_yrs = max((df["entry_date"].iloc[-1] - df["entry_date"].iloc[0]).days / 365.25, 0.25)
    tpy = len(df) / span_yrs
    sharpe = _annual_sharpe(r, tpy)
    eq, maxdd = _equity_maxdd(r)
    # OOS = last 30% by entry
    k = int(len(df) * 0.7)
    oos = _annual_sharpe(df["r"].to_numpy()[k:], tpy) if len(df) - k >= 2 else 0.0
    # Monte Carlo bootstrap of annualized Sharpe
    rng = np.random.default_rng(0)
    mc = np.array([_annual_sharpe(rng.choice(r, len(r), replace=True), tpy) for _ in range(2000)])
    mc5 = float(np.percentile(mc, 5))
    return {"n": len(df), "win_rate": float((r > 0).mean()), "avg_r": float(r.mean()),
            "total_$": float(df["pnl"].sum()), "sharpe": sharpe, "oos_sharpe": oos,
            "maxdd": maxdd, "mc5_sharpe": mc5, "trades_per_year": tpy}


def stage6_verdict(m: dict) -> tuple[bool, dict]:
    checks = {
        "sharpe>=1.2": m["sharpe"] >= 1.2,
        "oos>=0.8": m["oos_sharpe"] >= 0.8,
        "maxdd<=20%": m["maxdd"] <= 0.20,
        "mc5>0.5": m["mc5_sharpe"] > 0.5,
        "trades>=100": m["n"] >= 100,
    }
    return all(checks.values()), checks


def per_regime(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["regime"] = df["entry_date"].map(_regime)
    g = df.groupby("regime")
    out = pd.DataFrame({
        "trades": g.size(), "win_rate": g["r"].apply(lambda x: (x > 0).mean()),
        "avg_r": g["r"].mean(), "total_$": g["pnl"].sum(),
    })
    order = ["covid_crash", "calm_2021", "bear_2022", "recovery_23_24", "recent_25_26"]
    return out.reindex([o for o in order if o in out.index]).round(4)


def report(tickers, structure_fn, label, gate, scenarios=("bidask", "midminus", "mid"),
           profit_target=0.50, stop_mult=2.0):
    print(f"\n{'='*72}\n{label}\n  underlyings: {tickers}\n{'='*72}")
    base = None
    for sc in scenarios:
        df = run_backtest(tickers, structure_fn, gate=gate, scenario=sc,
                          profit_target=profit_target, stop_mult=stop_mult)
        if df.empty:
            print(f"[{sc}] no trades"); continue
        m = metrics(df)
        passed, checks = stage6_verdict(m)
        print(f"\n--- fill scenario: {sc} ---")
        print(f"  trades {m['n']}  win {m['win_rate']:.1%}  avg_r {m['avg_r']:+.3f}  "
              f"total ${m['total_$']:+,.0f}  ({m['trades_per_year']:.0f}/yr)")
        print(f"  Sharpe {m['sharpe']:.2f}  OOS {m['oos_sharpe']:.2f}  maxDD {m['maxdd']:.1%}  "
              f"MC5 {m['mc5_sharpe']:.2f}")
        print(f"  STAGE-6: {'PASS ✅' if passed else 'FAIL ❌'}  "
              f"{ {k: ('✓' if v else '✗') for k,v in checks.items()} }")
        if sc == scenarios[0]:
            base = df
    if base is not None:
        print(f"\n  PER-REGIME P&L (conservative bidask fills) — 2022 bear is the acid test:")
        print(per_regime(base).to_string())
        exits = base["exit_reason"].value_counts().to_dict()
        print(f"\n  exit reasons: {exits}")
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=",".join(LIQUID_ETFS))
    ap.add_argument("--dte", type=int, default=35)
    ap.add_argument("--delta", type=float, default=0.16)
    ap.add_argument("--wing", type=float, default=5.0)
    a = ap.parse_args()
    tickers = a.tickers.split(",")
    gate = RegimeGate(GateParams())
    fn = partial(iron_condor, dte=a.dte, short_delta=a.delta, wing_width=a.wing)
    report(tickers, fn,
           f"FIRST EXPERIMENT — iron condor {a.dte}DTE {a.delta:.2f}Δ ${a.wing:.0f}-wing, regime-gated",
           gate)


if __name__ == "__main__":
    main()
