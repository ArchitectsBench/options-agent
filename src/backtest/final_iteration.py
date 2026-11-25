"""FINAL pre-committed premium-selling iteration (2026-07-01) — bear-exclusion gate.

Per the decision boundary logged in TRACK_A_STATUS.md ("a stricter bear-exclusion
gate is the ONLY principled iteration left, else premium-selling is done"), this
runs EXACTLY ONE pre-specified configuration — no tuning, no variant shopping:

  Gate      = existing RegimeGate (contango + iv_rank + vol-not-spiking)
              AND TrendGate: prior-day close > 200d SMA (prior-day data only).
  Structure = the SAME wing-scaled iron condors from experiments.py
              (35DTE, 0.16Δ shorts, 3%-of-spot wings, 50% profit target;
              the two pre-existing management variants: 2x-credit stop / no stop).
  Universe  = the same 7 liquid ETFs.  Window = full store (2021-01..2026-06),
              with the 2021-2023 slice reported for comparability.
  Fills     = bidask AND mid (midminus too) — only-passes-at-mid is a FAIL.

Evaluated against the REVISED Stage-6 gate: Sharpe>=0.8, OOS>=0.6, maxDD<=20%,
MC5>=0.0, worst-year Sharpe>=0.0, >=100 trades.

  .venv/bin/python -m src.backtest.final_iteration
"""
from __future__ import annotations

from functools import partial

import pandas as pd

from src.backtest.backtest import (LIQUID_ETFS, _annual_sharpe, metrics,
                                   per_regime, run_backtest)
from src.backtest.regime import AndGate, GateParams, RegimeGate, TrendGate
from src.backtest.structures import iron_condor

SCENARIOS = ("bidask", "midminus", "mid")
YEARS = [str(y) for y in range(2021, 2027)]


def stage6_revised(m: dict, worst_year_sharpe: float) -> tuple[bool, dict]:
    """Revised Stage-6 acceptance gate (adopted 2026)."""
    checks = {
        "sharpe>=0.8": m["sharpe"] >= 0.8,
        "oos>=0.6": m["oos_sharpe"] >= 0.6,
        "maxdd<=20%": m["maxdd"] <= 0.20,
        "mc5>=0.0": m["mc5_sharpe"] >= 0.0,
        "worst_yr_sharpe>=0.0": worst_year_sharpe >= 0.0,
        "trades>=100": m["n"] >= 100,
    }
    return all(checks.values()), checks


def per_year(df: pd.DataFrame) -> pd.DataFrame:
    """Per-entry-year stats incl. annualized Sharpe (span-based trades/yr)."""
    out = {}
    for y, g in df.groupby(df["entry_date"].dt.year):
        r = g["r"].to_numpy()
        span = max((g["entry_date"].iloc[-1] - g["entry_date"].iloc[0]).days / 365.25, 0.25)
        out[y] = {"trades": len(g), "win_rate": round(float((r > 0).mean()), 3),
                  "avg_r": round(float(r.mean()), 4), "total_$": round(float(g["pnl"].sum()), 0),
                  "sharpe": round(_annual_sharpe(r, len(g) / span), 2)}
    return pd.DataFrame(out).T


def gate_open_fractions(gate, tickers) -> pd.DataFrame:
    """Fraction of sessions with entry gate OPEN, per ticker per year (uses the
    RegimeGate's own signal calendar; NaN-warmup days count as closed, matching
    backtest behavior where no-signal -> no trade)."""
    rows = {}
    base = gate.gates[0] if isinstance(gate, AndGate) else gate
    for tk in tickers:
        g = base.sig.get(tk)
        if g is None:
            continue
        counts = {y: [0, 0] for y in YEARS}
        for ds in g.index:
            y = ds[:4]
            if y not in counts:
                continue
            counts[y][1] += 1
            if gate.ok(tk, ds):
                counts[y][0] += 1
        rows[tk] = {y: (o / t if t else float("nan")) for y, (o, t) in counts.items()}
    df = pd.DataFrame(rows).T
    df.loc["POOLED"] = df.mean()
    return df.round(3)


def block(df: pd.DataFrame, label: str) -> None:
    if df.empty:
        print(f"  [{label}] NO TRADES")
        return
    m = metrics(df)
    py = per_year(df)
    worst = float(py["sharpe"].min())
    passed, checks = stage6_revised(m, worst)
    print(f"  [{label}] trades {m['n']}  win {m['win_rate']:.1%}  avg_r {m['avg_r']:+.4f}  "
          f"total ${m['total_$']:+,.0f}  ({m['trades_per_year']:.0f}/yr)")
    print(f"           Sharpe {m['sharpe']:.2f}  OOS {m['oos_sharpe']:.2f}  "
          f"maxDD {m['maxdd']:.1%}  MC5 {m['mc5_sharpe']:.2f}  worst-yr {worst:.2f}")
    print(f"           STAGE-6(revised): {'PASS' if passed else 'FAIL'}  "
          f"{ {k: ('OK' if v else 'X') for k, v in checks.items()} }")


def run_config(label: str, stop_mult: float, gate) -> None:
    fn = partial(iron_condor, dte=35, short_delta=0.16, wing_pct=0.03)
    print(f"\n{'='*76}\n{label}\n{'='*76}")
    for sc in SCENARIOS:
        df = run_backtest(LIQUID_ETFS, fn, gate=gate, scenario=sc,
                          profit_target=0.50, stop_mult=stop_mult)
        print(f"\n--- fill scenario: {sc} ---")
        if df.empty:
            print("  NO TRADES")
            continue
        block(df, "FULL 2021-2026")
        sub = df[df["entry_date"] <= "2023-12-31"].reset_index(drop=True)
        block(sub, "SUB  2021-2023")
        if sc == "bidask":
            print("\n  PER-YEAR (bidask):")
            print(per_year(df).to_string())
            print("\n  PER-REGIME (bidask) — 2022 bear is the acid test:")
            print(per_regime(df).to_string())
            print(f"\n  exit reasons: {df['exit_reason'].value_counts().to_dict()}")


def main():
    base = RegimeGate(GateParams())
    combined = AndGate(base, TrendGate(200))

    print("GATE VERIFICATION — fraction of sessions OPEN per year")
    print("\nOLD gate (RegimeGate alone):")
    print(gate_open_fractions(base, LIQUID_ETFS).to_string())
    print("\nCOMBINED gate (RegimeGate AND prior-day close > 200d SMA):")
    print(gate_open_fractions(combined, LIQUID_ETFS).to_string())

    run_config("A) WING-SCALED iron condor 35DTE 0.16Δ 3%-wing — WITH stop (2x credit)"
               " — BEAR-EXCLUSION gate", 2.0, combined)
    run_config("B) WING-SCALED iron condor 35DTE 0.16Δ 3%-wing — NO stop (defined-risk only)"
               " — BEAR-EXCLUSION gate", 999, combined)


if __name__ == "__main__":
    main()
