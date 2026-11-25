"""Adaptive vol-regime strategy — the payoff of the vol-regime arc.

Single pre-specified signal (the term structure) switches the WHOLE structure:
  contango      (iv60 - iv30 >  band)  -> SHORT premium (iron condor)  : collect VRP
  backwardation (iv60 - iv30 < -band)  -> LONG  premium (long strangle): catch the move
  neutral band                          -> no trade

Hypothesis: short-vol and long-vol fail in OPPOSITE regimes; switching on the term
structure makes the book long vol exactly when short-vol would have bled (the 2022
bear), so the combination may be regime-robust where neither leg is. One signal, no
tuned parameter -> a defensible test of regime balance, not a curve-fit.

Each leg keeps its own management: short = take 50% / stop 2x / bail on vol shock;
long = let winners run / no stop (max loss = debit) / hold through shocks.

  python src/backtest/adaptive.py
"""
from __future__ import annotations

from dataclasses import asdict
from functools import partial
from pathlib import Path

import pandas as pd

from src.backtest.backtest import metrics, per_regime, stage6_verdict
from src.backtest.chain import load
from src.backtest.lifecycle import run_trade
from src.backtest.regime import RegimeGate, GateParams
from src.backtest.structures import iron_condor, long_strangle

ROOT = Path(__file__).resolve().parents[2]
INP = ROOT / "data" / "vrp_inputs.parquet"

SHORT_MGMT = dict(profit_target=0.50, stop_mult=2.0)   # credit: take 50%, stop 2x
LONG_MGMT = dict(profit_target=1.5, stop_mult=99.0)    # debit: let run, no stop


def _term_lookup(inputs_path: Path = INP) -> dict[str, dict[str, float]]:
    df = pd.read_parquet(inputs_path)
    out = {}
    for tk, g in df.groupby("ticker"):
        g = g.sort_values("date")
        out[tk] = dict(zip(g["date"], (g["iv60"] - g["iv30"]).astype(float)))
    return out


def run_adaptive(tickers, *, scenario="bidask", band=0.0,
                 short_fn=None, long_fn=None, hard_gate: RegimeGate | None = None) -> pd.DataFrame:
    """Per session, pick short/long by the term structure; non-overlapping, pooled."""
    short_fn = short_fn or partial(iron_condor, dte=35, short_delta=0.16, wing_width=5.0, wing_pct=0.03)
    long_fn = long_fn or partial(long_strangle, dte=35, delta=0.16)
    hard_gate = hard_gate or RegimeGate(GateParams())
    term = _term_lookup()
    rows = []
    for tk in tickers:
        try:
            store = load(tk)
        except FileNotFoundError:
            continue
        tmap = term.get(tk, {})
        sessions = store.sessions
        i = 252
        while i < len(sessions):
            d = sessions[i]
            t = tmap.get(d.strftime("%Y-%m-%d"))
            if t is None:
                i += 1; continue
            if t > band:                       # contango -> short premium
                fn, mgmt = short_fn, SHORT_MGMT
                regime_ok = lambda dd, _tk=tk: not hard_gate.hard_break(_tk, dd)
                side = "short"
            elif t < -band:                    # backwardation -> long premium
                fn, mgmt = long_fn, LONG_MGMT
                regime_ok = None               # long vol: hold through shocks
                side = "long"
            else:
                i += 1; continue               # neutral band -> stand aside
            snap = store.snapshot(d)
            s = fn(snap)
            if s is None or not s.tradable:
                i += 1; continue
            tr = run_trade(store, s, scenario=scenario, **mgmt, regime_ok=regime_ok)
            if tr is None:
                i += 1; continue
            row = asdict(tr); row["side"] = side
            rows.append(row)
            while i < len(sessions) and sessions[i] <= tr.exit_date:
                i += 1
    df = pd.DataFrame(rows)
    if not df.empty:
        df["entry_date"] = pd.to_datetime(df["entry_date"])
        df["r"] = df["pnl"] / df["capital"]
        df = df.sort_values("entry_date").reset_index(drop=True)
    return df


def report(tickers, label, band=0.0, scenarios=("bidask", "mid")):
    print(f"\n{'='*72}\n{label}\n  underlyings: {tickers}\n{'='*72}")
    base = None
    for sc in scenarios:
        df = run_adaptive(tickers, scenario=sc, band=band)
        if df.empty:
            print(f"[{sc}] no trades"); continue
        m = metrics(df)
        passed, checks = stage6_verdict(m)
        sh = df.groupby("side")["pnl"].agg(["count", "sum"]).to_dict("index")
        print(f"\n--- fill scenario: {sc} ---")
        print(f"  trades {m['n']}  win {m['win_rate']:.1%}  avg_r {m['avg_r']:+.3f}  "
              f"total ${m['total_$']:+,.0f}  ({m['trades_per_year']:.0f}/yr)")
        print(f"  Sharpe {m['sharpe']:.2f}  OOS {m['oos_sharpe']:.2f}  maxDD {m['maxdd']:.1%}  MC5 {m['mc5_sharpe']:.2f}")
        print(f"  STAGE-6: {'PASS ✅' if passed else 'FAIL ❌'}  { {k:('✓' if v else '✗') for k,v in checks.items()} }")
        print(f"  by side: { {k: (v['count'], round(v['sum'])) for k,v in sh.items()} }")
        if sc == scenarios[0]:
            base = df
    if base is not None:
        print(f"\n  PER-REGIME P&L (bidask) — 2022 bear is the acid test:")
        print(per_regime(base).to_string())
    return base


if __name__ == "__main__":
    report(["SPY", "QQQ", "IWM"], "ADAPTIVE vol-regime — INDICES")
    import glob, os
    wl = sorted({os.path.basename(f).rsplit("_", 1)[0] for f in glob.glob(str(ROOT / "data/iv_history_full/*_2022.parquet"))})
    report(wl, "ADAPTIVE vol-regime — BROAD WATCHLIST (30)")
