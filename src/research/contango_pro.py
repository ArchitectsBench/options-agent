"""CONTANGO RE-TEST under professional-path assumptions — study B5.

Ref: docs/professional-path-2026-07-05.md (Lever B, item 5) and the closed
vol-regime arc in logs/research/TRACK_A_STATUS.md. The arc was closed with
"do not reopen without a FUNDAMENTALLY DIFFERENT monetization" — this re-test
qualifies because the ASSUMPTIONS change (professional cost structure), not
the structure-tuning.

ONE pre-specified configuration, run ONCE, no variants, no tuning:

  Underlyings : SPY, QQQ, IWM  (SPX proxy run — SPX extraction in parallel)
  Signal      : enter only when iv60 > iv30 (contango), exactly as previously
                validated (data/vrp_inputs.parquet; missing signal -> no trade)
  Structure   : iron condor 45 DTE, 0.16-delta shorts, wings at 5% of spot
  Management  : professional-static — exit at 50% max profit OR 21 DTE
                remaining, whichever first; NO stop. One position per
                underlying at a time (sequential, non-overlapping).
  Fills       : (a) mid          — patient-execution assumption
                (b) midminus 25% — mid +/- 25% of half-spread (realistic patient)
                (c) bidask       — full spread crossing (stress)
  Window      : entries 2021-01-01 .. 2026-06-30 (full available store)
  Gate        : REVISED Stage 6 via src.research.monte_carlo.acceptance_gate
                (Sharpe>=0.8, OOS>=0.6, OOS within 60% of IS, DD<=20%,
                 MC5>=0.0, worst-year>=0.0, >=100 trades), PRIMARY at (b).

Pre-committed verdict rule: PASS/FAIL per gate at (b). Failing at (b) but
passing at (a) = "edge exists at perfect mid only" = FAIL with a note.
< 100 trades = FAIL on its own. No iteration beyond this configuration.

  nice .venv/bin/python -m src.research.contango_pro
"""
from __future__ import annotations

from dataclasses import asdict
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.backtest import _annual_sharpe, _equity_maxdd, metrics, per_regime
from src.backtest.chain import load
from src.backtest.lifecycle import run_trade
from src.backtest.structures import iron_condor
from src.research.monte_carlo import MonteCarloReport, acceptance_gate

ROOT = Path(__file__).resolve().parents[2]
INP = ROOT / "data" / "vrp_inputs.parquet"

TICKERS = ["SPY", "QQQ", "IWM"]
SCENARIOS = ["mid", "midminus", "bidask"]        # (a), (b), (c)
SCEN_LABEL = {"mid": "(a) MID", "midminus": "(b) MID+/-25% half-spread",
              "bidask": "(c) FULL BID/ASK"}
ENTRY_START = pd.Timestamp("2021-01-01")
ENTRY_END = pd.Timestamp("2026-06-30")

# structure / management — THE pre-specified configuration
DTE = 45
SHORT_DELTA = 0.16
WING_PCT = 0.05           # wings at 5% of spot
PROFIT_TARGET = 0.50      # 50% of max profit (mid-credit reference)
MIN_DTE = 21              # time exit at 21 DTE remaining
STOP_MULT = 999.0         # NO stop (prior work showed stops worsened it)

# tax assumptions (stated, approximate)
ST_RATE = 0.35            # assumed marginal short-term rate
LT_RATE = 0.20            # assumed long-term capital gains rate
BLEND_1256 = 0.60 * LT_RATE + 0.40 * ST_RATE    # section 1256 60/40 blend


def contango_signal() -> dict[str, dict[str, float]]:
    """Per-ticker {date_str: iv60 - iv30}. Entry permitted iff term > 0.
    Missing (ticker, date) -> no signal -> no trade (fail closed)."""
    df = pd.read_parquet(INP)
    out = {}
    for tk, g in df.groupby("ticker"):
        g = g.sort_values("date")
        out[tk] = dict(zip(g["date"], (g["iv60"] - g["iv30"]).astype(float)))
    return out


def run_scenario(scenario: str, term: dict) -> pd.DataFrame:
    """Sequential non-overlapping contango-gated condors per ticker, pooled."""
    fn = partial(iron_condor, dte=DTE, short_delta=SHORT_DELTA, wing_pct=WING_PCT)
    rows = []
    for tk in TICKERS:
        store = load(tk)
        tmap = term.get(tk, {})
        sessions = [d for d in store.sessions if ENTRY_START <= d <= ENTRY_END]
        i = 0
        while i < len(sessions):
            d = sessions[i]
            t = tmap.get(d.strftime("%Y-%m-%d"))
            if t is None or t <= 0:                     # signal: contango only
                i += 1
                continue
            snap = store.snapshot(d)
            s = fn(snap)
            if s is None or not s.tradable:
                i += 1
                continue
            tr = run_trade(store, s, scenario=scenario, frac=0.25,
                           profit_target=PROFIT_TARGET, stop_mult=STOP_MULT,
                           min_dte=MIN_DTE, regime_ok=None)   # static mgmt, no gate exit
            if tr is None:
                i += 1
                continue
            rows.append(asdict(tr))
            while i < len(sessions) and sessions[i] <= tr.exit_date:
                i += 1
    df = pd.DataFrame(rows)
    if not df.empty:
        df["entry_date"] = pd.to_datetime(df["entry_date"])
        df["exit_date"] = pd.to_datetime(df["exit_date"])
        df["r"] = df["pnl"] / df["capital"]
        df = df.sort_values("entry_date").reset_index(drop=True)
    return df


def per_year(df: pd.DataFrame) -> pd.DataFrame:
    out = {}
    for y, g in df.groupby(df["entry_date"].dt.year):
        r = g["r"].to_numpy()
        span = max((g["entry_date"].iloc[-1] - g["entry_date"].iloc[0]).days / 365.25, 0.25)
        out[y] = {"trades": len(g), "win_rate": round(float((r > 0).mean()), 3),
                  "avg_r": round(float(r.mean()), 4),
                  "total_$": round(float(g["pnl"].sum()), 0),
                  "sharpe": round(_annual_sharpe(r, len(g) / span), 2)}
    return pd.DataFrame(out).T


def margin_and_tax(df: pd.DataFrame) -> dict:
    """Portfolio-margin APPROXIMATION: margin per position ~= max loss of the
    defined-risk structure (the capital column). This understates true PM
    house requirements slightly and ignores cross-position offsets — stated
    as approximate. Annualized return-on-margin = total P&L / time-weighted
    deployed margin (dollar-years)."""
    dollar_days = float((df["capital"] * df["days_held"].clip(lower=1)).sum())
    avg_margin_deployed = dollar_days / max((ENTRY_END - ENTRY_START).days, 1)
    years = (ENTRY_END - ENTRY_START).days / 365.25
    total = float(df["pnl"].sum())
    rom_ann = total / years / avg_margin_deployed if avg_margin_deployed > 0 else float("nan")
    # section 1256 after-tax adjustment (SPY-as-SPX proxy): identical pre-tax
    # P&L taxed at ST_RATE (equity-option treatment, all short-term holds)
    # vs the 60/40 blend. Losses treated symmetrically (both regimes allow
    # offset against like gains) — an approximation, stated as such.
    after_st = total * (1 - ST_RATE) if total > 0 else total
    after_1256 = total * (1 - BLEND_1256) if total > 0 else total
    return {"avg_margin_deployed": avg_margin_deployed, "years": years,
            "total_pretax": total, "rom_annualized": rom_ann,
            "after_tax_st": after_st, "after_tax_1256": after_1256,
            "tax_pickup": after_1256 - after_st}


def gate_eval(df: pd.DataFrame, label: str) -> tuple[bool, list]:
    """REVISED Stage-6 via src.research.monte_carlo.acceptance_gate."""
    r = df["r"].to_numpy()
    m = metrics(df)
    k = int(len(df) * 0.7)
    span = max((df["entry_date"].iloc[-1] - df["entry_date"].iloc[0]).days / 365.25, 0.25)
    tpy = len(df) / span
    is_sharpe = _annual_sharpe(r[:k], tpy)
    py = per_year(df)
    worst = float(py["sharpe"].min())
    regime_by_year = {int(y): float(v) for y, v in py["sharpe"].items()}
    rng = np.random.default_rng(0)
    mc = np.array([_annual_sharpe(rng.choice(r, len(r), replace=True), tpy)
                   for _ in range(2000)])
    rep = MonteCarloReport(
        params={"config": label}, is_sharpe=is_sharpe, oos_sharpe=m["oos_sharpe"],
        mc_median_sharpe=float(np.median(mc)), mc_p05_sharpe=float(np.percentile(mc, 5)),
        sensitivity_score=float("nan"), sensitivity_detail=[],
        full_sharpe=m["sharpe"], full_max_dd=-m["maxdd"], full_trades=len(df),
        worst_year_sharpe=worst, regime_by_year=regime_by_year)
    checks = acceptance_gate(rep)
    return all(ok for _, ok, _ in checks), checks


def contango_open_fractions(term: dict) -> pd.DataFrame:
    rows = {}
    for tk in TICKERS:
        counts: dict[str, list[int]] = {}
        for ds, t in term.get(tk, {}).items():
            y = ds[:4]
            if y < "2021" or ds > "2026-06-30":
                continue
            counts.setdefault(y, [0, 0])
            counts[y][1] += 1
            if t > 0:
                counts[y][0] += 1
        rows[tk] = {y: o / n for y, (o, n) in sorted(counts.items())}
    df = pd.DataFrame(rows).T
    df.loc["POOLED"] = df.mean()
    return df.round(3)


def block(df: pd.DataFrame, sc: str) -> None:
    m = metrics(df)
    print(f"\n{'-'*72}\nFILL SCENARIO {SCEN_LABEL[sc]}\n{'-'*72}")
    print(f"  trades {m['n']}  win {m['win_rate']:.1%}  avg_r {m['avg_r']:+.4f}  "
          f"total ${m['total_$']:+,.0f}  ({m['trades_per_year']:.1f}/yr pooled)")
    print(f"  Sharpe {m['sharpe']:.2f}  OOS {m['oos_sharpe']:.2f}  "
          f"maxDD {m['maxdd']:.1%}  MC5 {m['mc5_sharpe']:.2f}")
    print(f"  avg days held {df['days_held'].mean():.1f}  "
          f"avg credit {df['credit'].mean():.2f}  avg capital ${df['capital'].mean():,.0f}")
    print(f"  exit reasons: {df['exit_reason'].value_counts().to_dict()}")
    print(f"\n  PER-YEAR ({sc}):")
    print("  " + per_year(df).to_string().replace("\n", "\n  "))
    mt = margin_and_tax(df)
    print(f"\n  PM-approx margin (~= max loss, APPROXIMATE): avg deployed "
          f"${mt['avg_margin_deployed']:,.0f} over {mt['years']:.1f}y  "
          f"-> annualized return-on-margin {mt['rom_annualized']:+.1%}")
    print(f"  Sec-1256 after-tax (proxy, ST {ST_RATE:.0%} vs 60/40 blend {BLEND_1256:.1%}): "
          f"pre-tax ${mt['total_pretax']:+,.0f} -> ST ${mt['after_tax_st']:+,.0f} | "
          f"1256 ${mt['after_tax_1256']:+,.0f}  (pickup ${mt['tax_pickup']:+,.0f})")
    passed, checks = gate_eval(df, f"contango_pro_{sc}")
    print(f"\n  REVISED STAGE-6 (acceptance_gate): {'PASS' if passed else 'FAIL'}")
    for name, ok, actual in checks:
        print(f"    [{'OK' if ok else ' X'}] {name:38} actual {actual}")


def main():
    print("=" * 72)
    print("CONTANGO RE-TEST — PROFESSIONAL ASSUMPTIONS (study B5)  2026-07-05")
    print("SPY/QQQ/IWM proxy run (SPX 2007-2026 extraction in parallel)")
    print(f"iron condor {DTE}DTE {SHORT_DELTA:.2f}d shorts, wings {WING_PCT:.0%} of spot;"
          f" exit 50% profit OR {MIN_DTE} DTE; NO stop; 1 position/underlying")
    print(f"entries {ENTRY_START.date()} .. {ENTRY_END.date()};"
          " signal: iv60 > iv30 (contango) only")
    print("=" * 72)

    term = contango_signal()
    print("\nCONTANGO-OPEN fraction of sessions per year (signal availability):")
    print(contango_open_fractions(term).to_string())

    dfs = {}
    for sc in SCENARIOS:
        df = run_scenario(sc, term)
        dfs[sc] = df
        if df.empty:
            print(f"\n[{sc}] NO TRADES")
            continue
        block(df, sc)

    b = dfs.get("midminus")
    if b is not None and not b.empty:
        print(f"\n{'='*72}\nPER-REGIME P&L at PRIMARY scenario (b) — 2022 bear is the acid test:")
        print(per_regime(b).to_string())
        print("\nPER-TICKER at (b):")
        g = b.groupby("ticker").agg(trades=("r", "size"), win=("r", lambda x: (x > 0).mean()),
                                    avg_r=("r", "mean"), total=("pnl", "sum"))
        print(g.round(4).to_string())
        b.to_csv(ROOT / "logs" / "research" / "contango_pro_20260705_trades_midminus.csv",
                 index=False)
        print("\ntrade list (b) -> logs/research/contango_pro_20260705_trades_midminus.csv")


if __name__ == "__main__":
    main()
