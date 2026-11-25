"""Gen-4b — ORB re-validation on 5 YEARS of multi-regime 5m (the real test).

gen-4a showed a pulse (OOS 2.23) but on ~62 sessions / one bull quarter —
flagged as possibly a regime artifact. This re-runs ORB on the clean 5-year
store (data/research5m, 2021-2026) to settle it: proper walk-forward across
regimes + the ACID TEST — does ORB make money in the 2022 BEAR? A long
breakout strategy can fake edge via bull-market beta; standalone alpha shows
up as positive returns in a down year.

Long-side only (gen-4a: shorts dead). Variants: fixed ATR target vs trailing
ATR stop. Session-flat, 13:00 ET entry cutoff, 1.5bp/side costs. Stage 2-5 +
Stage-6 gate (now satisfiable: 5yr + regime diversity) + per-year breakdown.

Usage: python src/research/run_research_gen4b_orb.py
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.research.families import orb
from src.research.optimizer import optimize
from src.research.rule_validator import validate_strategy
from src.research.walk_forward import (make_folds, pooled_run, sharpe_of,
                                       wf_acceptance_gate, wf_evaluate, wf_train_objective)

STORE = ROOT / "data" / "research5m"
CFG = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
TICKERS = CFG["watchlist"]["default"]
N_TRIALS = 120
MIN_TRAIN_TRADES = 150
BASE_COST, CUTOFF = 1.5, 13
ORB_SPACE = {"n_or": ("cat", [3, 6, 12]), "atr_mult": ("float", 0.5, 3.0),
             "volume_confirm": ("cat", [True, False])}
VALIDATION_PARAMS = {"n_or": 6, "atr_mult": 1.5, "volume_confirm": False}
LOG = ROOT / "logs" / "research" / f"gen4b_orb_{datetime.now():%Y%m%d_%H%M%S}.log"


def variant(exit_style):
    def make_sf(p):
        return orb(n_or=p["n_or"], atr_mult=p["atr_mult"],
                   use_target=(exit_style == "fixed"), volume_confirm=p["volume_confirm"],
                   side="long", cutoff_hour=CUTOFF)
    def make_bt(p):
        kw = {"flat_at_session_end": True, "cost_bps": BASE_COST}
        if exit_style == "trail":
            kw["trail_mult"] = p["atr_mult"]
        return kw
    return make_sf, make_bt


def per_year(make_sf, make_bt, params, dfs, log):
    """The acid test: pooled Sharpe + trades per calendar year. 2022 = bear."""
    sf, bt = make_sf(params), make_bt(params)
    years = sorted({d.year for df in dfs.values() for d in [df.index.min(), df.index.max()]})
    yr_range = range(min(years), max(years) + 1)
    log(f"  {'year':<6}{'pooled Sharpe':>15}{'trades':>9}   regime")
    regimes = {2021: "bull", 2022: "BEAR", 2023: "recovery", 2024: "bull",
               2025: "bull", 2026: "ytd"}
    for yr in yr_range:
        sub = {t: df[df.index.year == yr] for t, df in dfs.items()}
        sub = {t: d for t, d in sub.items() if len(d) > 200}
        if not sub:
            continue
        results, rets, ppy = pooled_run(sf, sub, bt)
        n = sum(r.n_trades for r in results.values())
        sh = sharpe_of(rets, ppy)
        flag = "  <-- ACID TEST" if yr == 2022 else ""
        log(f"  {yr:<6}{(sh if not pd.isna(sh) else 0):>15.2f}{n:>9}   {regimes.get(yr,'?')}{flag}")


def main():
    out = open(LOG, "w")
    def log(m=""):
        print(m, flush=True); out.write(m + "\n"); out.flush()
    t0 = time.time()

    dfs = {t: pd.read_parquet(STORE / f"{t}.parquet") for t in TICKERS
           if (STORE / f"{t}.parquet").exists() and len(pd.read_parquet(STORE / f"{t}.parquet")) > 1000}
    sample = next(iter(dfs.values()))
    sessions = pd.DatetimeIndex(sample.index).normalize().nunique()
    log(f"GEN-4b ORB — {len(dfs)} tickers, {sessions} sessions, "
        f"{sample.index.min().date()}..{sample.index.max().date()}, costs {BASE_COST}bp, long-only")
    folds = make_folds(dfs, n_folds=8, train_sessions=126, test_sessions=63)
    log(f"{len(folds)} walk-forward folds spanning the full 5yr (train 126 / test 63 sessions):")
    for f in folds:
        log(f"  F{f.number}: train {f.train_range[0]}..{f.train_range[1]} | test {f.test_range[0]}..{f.test_range[1]}")

    leaderboard = []
    for exit_style in ("fixed", "trail"):
        vname = f"orb_long_{exit_style}"
        make_sf, make_bt = variant(exit_style)
        log(f"\n{'='*66}\nVARIANT {vname}")
        rep = validate_strategy(vname, {"direction": "long", "timeframe": "5m",
                                        "entry": "OR break before cutoff", "exit": "stop/target/session_end",
                                        "execution": "next_bar_open", "session_flat": True},
                                make_sf(VALIDATION_PARAMS), sample)
        if not rep.passed:
            log(rep.summary()); continue
        log("  Stage 2: VALID")
        stats = {}
        cands = optimize(ORB_SPACE, wf_train_objective(make_sf, make_bt, folds),
                         n_trials=N_TRIALS, top_n=2, min_trades=MIN_TRAIN_TRADES, stats=stats)
        log(f"  Stage 4: {N_TRIALS} trials, {stats['scarcity_pruned']} scarcity-pruned. Top:")
        for c in cands:
            log(f"    {c.params}  mean-train Sharpe {c.is_sharpe:.2f}")
        if not cands:
            continue
        for c in cands:
            ev = wf_evaluate(make_sf, make_bt, c.params, dfs, folds)
            leaderboard.append((vname, ev, make_sf, make_bt))
            folds_str = ",".join(f"{s:.1f}" if not pd.isna(s) else "nan" for s in ev.fold_oos_sharpes)
            log(f"  Stage 5 {c.params}: full {ev.full_sharpe:.2f} OOS {ev.oos_sharpe:.2f} "
                f"folds[{folds_str}] MC5 {ev.mc_p05_sharpe:.2f} sens {ev.sensitivity_score:.2f} trades {ev.full_trades}")

    log(f"\n{'='*78}\nLEADERBOARD + GATE (Sharpe>=1.2, OOS>=0.8 & >=0.6*IS, maxDD<=20%, MC5>0.5, >=100 trades)")
    log(f"{'='*78}")
    best = None
    for vname, ev, msf, mbt in sorted(leaderboard, key=lambda x: -(x[1].mc_p05_sharpe if not pd.isna(x[1].mc_p05_sharpe) else -9)):
        gate = wf_acceptance_gate(ev)
        passed = all(ok for _, ok, _ in gate)
        fails = [n for n, ok, _ in gate if not ok]
        pstr = ",".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in ev.params.items())
        log(f"{vname:<16}{pstr:<40}full {ev.full_sharpe:>5.2f} OOS {ev.oos_sharpe:>5.2f} MC5 {ev.mc_p05_sharpe:>5.2f} "
            f"trades {ev.full_trades:>5}  {'PASS' if passed else 'fail: '+';'.join(fails[:2])}")
        if best is None:
            best = (vname, ev, msf, mbt)

    if best:
        vname, ev, msf, mbt = best
        log(f"\n{'='*40}\nACID TEST — per-year regime breakdown of best ({vname} {ev.params}):")
        log("(a long breakout can fake edge via bull beta; POSITIVE 2022 = real standalone alpha)")
        per_year(msf, mbt, ev.params, dfs, log)

    log(f"\nrun {time.time()-t0:.0f}s — log {LOG.name}")
    out.close()


if __name__ == "__main__":
    main()
