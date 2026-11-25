"""Generation 2 research run.

Pipeline fixes vs gen 1:
  1. Stage 4 prunes trials with < 150 total in-sample (train-fold) trades —
     kills the trade-scarcity cheat. Pruned counts logged per study.
  2. Transaction costs: 1.5bp per side base case; ORB variants additionally
     reported at 1bp / 1.5bp / 3bp.
  3. Walk-forward (5 folds, ~126 train + ~63 test sessions) replaces 70/30.
     OOS = aggregate of test folds; per-fold Sharpe reported.

Candidates:
  - ORB variants: {long, short} x {fixed ATR target, trailing ATR stop},
    all with 13:00 ET entry cutoff.
  - squeeze_proxy gen-1 params re-tested under walk-forward (no re-opt).
  - vwap_trend, vwap_reversion, momentum_reentry RETIRED (reasons logged).

Usage: python src/research/run_research_gen2.py
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.research.families import orb, squeeze_proxy
from src.research.optimizer import optimize
from src.research.rule_validator import validate_strategy
from src.research.walk_forward import (
    make_folds,
    pooled_run,
    sharpe_of,
    wf_acceptance_gate,
    wf_evaluate,
    wf_train_objective,
)

TICKERS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]
N_TRIALS = 200
TOP_FOR_MC = 3
MIN_IS_TRADES = 150
BASE_COST_BPS = 1.5
ORB_COST_GRID = [1.0, 1.5, 3.0]
CUTOFF_HOUR = 13

LOG_DIR = ROOT / "logs" / "research"
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

RETIREMENTS = {
    "vwap_trend": "degenerate under optimization — best params produced 4 trades in 2.5yr "
                  "(triple-condition entry too restrictive on 1h bars; a 5m-timeframe idea)",
    "vwap_reversion": "overfit/regime artifact — IS 1.14 vs OOS -1.87 with sign-flipped edge",
    "momentum_reentry": "re-entry mechanics worked (2,771 trades) but no edge at any scale "
                        "(pooled IS 0.24, OOS -0.11)",
}

GEN1_SQUEEZE_PARAMS = {"bb_period": 36, "width_pct": 0.30951535327650626, "mom_period": 23}


class Logger:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOG_DIR / f"gen2_{RUN_ID}.log", "w")

    def __call__(self, msg: str = ""):
        print(msg)
        self.fh.write(msg + "\n")
        self.fh.flush()


log = Logger()


def fmt(x, spec=".2f"):
    return "  nan" if pd.isna(x) else f"{x:{spec}}"


# ── Variant definitions ──
# Each variant: signal params go to the orb() factory; for trail variants
# atr_mult instead drives the backtester's trailing stop.

def orb_variant(side: str, exit_style: str):
    def make_signal_func(params):
        return orb(
            n_or=params["n_or"],
            atr_mult=params["atr_mult"],
            use_target=(exit_style == "fixed"),
            volume_confirm=params["volume_confirm"],
            side=side,
            cutoff_hour=CUTOFF_HOUR,
        )

    def make_bt_kwargs(params, cost_bps=BASE_COST_BPS):
        kw = {"flat_at_session_end": True, "cost_bps": cost_bps}
        if exit_style == "trail":
            kw["trail_mult"] = params["atr_mult"]
        return kw

    return make_signal_func, make_bt_kwargs


VARIANTS = {}
for side in ("long", "short"):
    for exit_style in ("fixed", "trail"):
        VARIANTS[f"orb_{side}_{exit_style}"] = orb_variant(side, exit_style)

ORB_SPACE = {
    "n_or": ("cat", [1, 2]),
    "atr_mult": ("float", 0.5, 3.0),
    "volume_confirm": ("cat", [True, False]),
}
ORB_VALIDATION_PARAMS = {"n_or": 1, "atr_mult": 1.5, "volume_confirm": False}


def main():
    t0 = time.time()
    specs = yaml.safe_load((ROOT / "config" / "strategy_rules.yaml").read_text())
    dfs_full = {t: pd.read_parquet(ROOT / "data" / "historical" / f"{t}_1h.parquet") for t in TICKERS}
    folds = make_folds(dfs_full)

    sample = dfs_full["SPY"]
    log(f"Gen-2 research run {RUN_ID} — {len(TICKERS)} tickers, 1h bars, "
        f"{len(sample)} bars/ticker ({sample.index.min().date()} to {sample.index.max().date()})")
    log(f"Costs: {BASE_COST_BPS}bp/side base. Stage 4: {N_TRIALS} trials, scarcity floor {MIN_IS_TRADES} IS trades.")
    log("Walk-forward folds (train | test):")
    for f in folds:
        log(f"  F{f.number}: {f.train_range[0]} to {f.train_range[1]} | {f.test_range[0]} to {f.test_range[1]}")
    log("Caveat: params are shared across folds, so a fold's test window can overlap a "
        "LATER fold's train window; each fold's own train/test are disjoint.\n")

    log("RETIRED FAMILIES (gen-1 evidence):")
    for fam, reason in RETIREMENTS.items():
        log(f"  - {fam}: {reason}")
    log("")

    leaderboard = []  # (variant, label, WFEvaluation)

    # ── ORB variants ──
    for vname, (make_sf, make_bt) in VARIANTS.items():
        log(f"{'=' * 70}\nVARIANT: {vname}\n{'=' * 70}")

        # Stage 2 on every ticker
        all_valid = True
        for ticker, df in dfs_full.items():
            rep = validate_strategy(vname, specs["orb_v2"], make_sf(ORB_VALIDATION_PARAMS), df)
            failed = [n for n, ok, _ in rep.checks if not ok]
            log(f"  Stage 2 [{ticker}]: {'VALID' if rep.passed else 'REJECTED — ' + str(failed)}")
            all_valid &= rep.passed
        if not all_valid:
            log(f"  {vname} rejected at Stage 2 — skipping.\n")
            continue

        # Stage 4: walk-forward objective with scarcity pruning
        stats = {}
        trials_path = LOG_DIR / f"gen2_{RUN_ID}_{vname}_trials.jsonl"
        trials_fh = open(trials_path, "w")
        objective = wf_train_objective(make_sf, make_bt, folds)

        def log_trial(params, value):
            trials_fh.write(json.dumps({"params": params, "mean_train_sharpe": value}) + "\n")

        candidates = optimize(ORB_SPACE, objective, n_trials=N_TRIALS, top_n=TOP_FOR_MC,
                              min_trades=MIN_IS_TRADES, stats=stats, trial_callback=log_trial)
        trials_fh.close()
        log(f"  Stage 4: {N_TRIALS} trials, {stats['scarcity_pruned']} pruned for trade scarcity "
            f"(<{MIN_IS_TRADES} IS trades). Top {len(candidates)}:")
        for c in candidates:
            log(f"    {c.params}  mean train Sharpe {c.is_sharpe:.2f}")
        if not candidates:
            log(f"  No viable candidates for {vname}.\n")
            continue

        # Stage 5: walk-forward Monte Carlo on top candidates
        log("  Stage 5 (pooled, walk-forward):")
        evals = []
        for c in candidates:
            ev = wf_evaluate(make_sf, make_bt, c.params, dfs_full, folds)
            evals.append(ev)
            folds_str = ",".join(fmt(s).strip() for s in ev.fold_oos_sharpes)
            log(f"    {c.params}: OOS agg {fmt(ev.oos_sharpe)} folds [{folds_str}], "
                f"MC5pct {fmt(ev.mc_p05_sharpe)}, sens {fmt(ev.sensitivity_score)}, trades {ev.full_trades}")
        evals.sort(key=lambda e: -np.inf if pd.isna(e.mc_p05_sharpe) else e.mc_p05_sharpe, reverse=True)
        best = evals[0]
        log(f"  Best by MC 5th-pct: {best.params}")
        leaderboard.append((vname, "POOLED", best))

        # per-ticker breakdown of best candidate
        for ticker in TICKERS:
            ev_t = wf_evaluate(make_sf, make_bt, best.params, {ticker: dfs_full[ticker]},
                               [type(f)(f.number, {ticker: f.train[ticker]}, {ticker: f.test[ticker]},
                                        f.train_range, f.test_range) for f in folds])
            leaderboard.append((vname, ticker, ev_t))

        # Cost sensitivity (full-period + aggregate OOS at each cost level)
        log("  Cost sensitivity (full Sharpe / OOS agg Sharpe):")
        for cost in ORB_COST_GRID:
            bt = lambda p, c=cost: make_bt(p, cost_bps=c)
            sf = make_sf(best.params)
            _, full_rets, full_ppy = pooled_run(sf, dfs_full, bt(best.params))
            test_rets = []
            ppy = full_ppy
            for f in folds:
                _, tr, ppy = pooled_run(sf, f.test, bt(best.params))
                test_rets.append(tr)
            log(f"    {cost}bp: full {fmt(sharpe_of(full_rets, full_ppy))} / "
                f"OOS {fmt(sharpe_of(pd.concat(test_rets), ppy))}")
        log("")

    # ── squeeze_proxy gen-1 params under walk-forward (regime autopsy) ──
    log(f"{'=' * 70}\nRE-TEST: squeeze_proxy gen-1 params under walk-forward\n{'=' * 70}")
    sq_sf = lambda p: squeeze_proxy(**p)  # noqa: E731
    sq_bt = lambda p: {"flat_at_session_end": True, "cost_bps": BASE_COST_BPS}  # noqa: E731
    ev = wf_evaluate(sq_sf, sq_bt, GEN1_SQUEEZE_PARAMS, dfs_full, folds)
    folds_str = ", ".join(f"F{i + 1}={fmt(s).strip()}" for i, s in enumerate(ev.fold_oos_sharpes))
    log(f"  params {ev.params}")
    log(f"  per-fold OOS: {folds_str}")
    log(f"  OOS agg {fmt(ev.oos_sharpe)}, IS mean {fmt(ev.is_sharpe)}, MC5pct {fmt(ev.mc_p05_sharpe)}, trades {ev.full_trades}")
    positive_folds = sum(1 for s in ev.fold_oos_sharpes if not pd.isna(s) and s > 0)
    log(f"  Verdict: {positive_folds}/{len(ev.fold_oos_sharpes)} test folds positive — "
        + ("regime-dependent (not uniformly dead)" if 0 < positive_folds < len(ev.fold_oos_sharpes)
           else "uniformly dead" if positive_folds == 0 else "robust across folds"))
    leaderboard.append(("squeeze_gen1", "POOLED", ev))
    log("")

    # ── Final leaderboard ──
    log(f"{'=' * 118}")
    log("FINAL LEADERBOARD — gate: Sharpe>=1.2, OOS(agg)>=0.8 & >=0.6*IS, maxDD<=20%, MC5pct>0.5, >=100 trades")
    log(f"{'=' * 118}")
    header = (f"{'variant':<18}{'ticker':<8}{'params':<40}{'IS':>6}{'OOS':>7}"
              f"{'F1':>6}{'F2':>6}{'F3':>6}{'F4':>6}{'F5':>6}{'MC5':>7}{'trades':>7}  result")
    log(header)
    log("-" * len(header))
    any_passed = False
    for vname, label, ev in leaderboard:
        gate = wf_acceptance_gate(ev)
        passed = all(ok for _, ok, _ in gate)
        any_passed |= passed and label == "POOLED"
        reasons = "; ".join(name for name, ok, _ in gate if not ok)
        pstr = ",".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in ev.params.items())
        folds_cols = "".join(f"{fmt(s):>6}" for s in ev.fold_oos_sharpes)
        log(f"{vname:<18}{label:<8}{pstr:<40}{fmt(ev.is_sharpe):>6}{fmt(ev.oos_sharpe):>7}"
            f"{folds_cols}{fmt(ev.mc_p05_sharpe):>7}{ev.full_trades:>7}  "
            + ("PASS" if passed else f"FAIL ({reasons})"))

    log(f"\n=> {'At least one candidate PASSED the acceptance bar.' if any_passed else 'No candidate passed the acceptance bar.'}")
    log(f"Run time: {time.time() - t0:.0f}s. Full log: logs/research/gen2_{RUN_ID}.log")


if __name__ == "__main__":
    main()
