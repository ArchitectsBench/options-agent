"""Generation 4a — PROVISIONAL: true 5m ORB across the 30-ticker universe.

First use of the accumulated 5m snapshot store (~62 sessions). The formal
Stage 6 acceptance gate CANNOT be satisfied here (requires 2 years + regime
diversity) — every result is provisional by construction. Purpose: decide
whether ORB earns an observation-mode seat while the dataset matures.

Setup mirrors gen 2 discipline: walk-forward (shortened folds), 1.5bp/side
costs, session-flat, scarcity floor, Monte Carlo, variants long/short x
fixed/trailing exits, 13:00 ET entry cutoff. n_or searched over 15/30/60-min
opening ranges (3/6/12 five-minute bars).

Usage: python src/research/run_research_gen4a_orb.py
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

from src.research.families import orb
from src.research.optimizer import optimize
from src.research.rule_validator import validate_strategy
from src.research.walk_forward import make_folds, wf_acceptance_gate, wf_evaluate, wf_train_objective

CFG = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
TICKERS = CFG["watchlist"]["default"]
N_TRIALS = 80
TOP_FOR_MC = 2
MIN_TRAIN_TRADES = 150  # per objective evaluation (all folds, 30 tickers)
BASE_COST = 1.5
CUTOFF_HOUR = 13

ORB_SPACE = {
    "n_or": ("cat", [3, 6, 12]),   # 15 / 30 / 60-minute opening range on 5m bars
    "atr_mult": ("float", 0.5, 3.0),
    "volume_confirm": ("cat", [True, False]),
}
VALIDATION_PARAMS = {"n_or": 6, "atr_mult": 1.5, "volume_confirm": False}

LOG_DIR = ROOT / "logs" / "research"
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")


class Logger:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOG_DIR / f"gen4a_orb_{RUN_ID}.log", "w")

    def __call__(self, msg=""):
        print(msg, flush=True)
        self.fh.write(msg + "\n")
        self.fh.flush()


log = Logger()


def fmt(x):
    return "  nan" if pd.isna(x) else f"{x:6.2f}"


def variant(side, exit_style):
    def make_sf(params):
        return orb(n_or=params["n_or"], atr_mult=params["atr_mult"],
                   use_target=(exit_style == "fixed"),
                   volume_confirm=params["volume_confirm"],
                   side=side, cutoff_hour=CUTOFF_HOUR)

    def make_bt(params):
        kw = {"flat_at_session_end": True, "cost_bps": BASE_COST}
        if exit_style == "trail":
            kw["trail_mult"] = params["atr_mult"]
        return kw

    return make_sf, make_bt


def main():
    t0 = time.time()
    dfs = {}
    for t in TICKERS:
        p = ROOT / "data" / "historical" / f"{t}_5m.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            if len(df) > 1000:
                dfs[t] = df
    sample = next(iter(dfs.values()))
    sessions = pd.DatetimeIndex(sample.index).normalize().nunique()
    log(f"GEN 4a (PROVISIONAL — {sessions} sessions of 5m data, gate requires ~2yr)")
    log(f"{len(dfs)} tickers, {sample.index.min().date()} to {sample.index.max().date()}, "
        f"costs {BASE_COST}bp/side, cutoff {CUTOFF_HOUR}:00 ET\n")

    folds = make_folds(dfs, n_folds=5, train_sessions=25, test_sessions=10)
    for f in folds:
        log(f"  F{f.number}: train {f.train_range[0]}..{f.train_range[1]} | "
            f"test {f.test_range[0]}..{f.test_range[1]}")

    leaderboard = []
    for side in ("long", "short"):
        for exit_style in ("fixed", "trail"):
            vname = f"orb5m_{side}_{exit_style}"
            make_sf, make_bt = variant(side, exit_style)
            log(f"\n{'=' * 64}\nVARIANT {vname}")

            rep = validate_strategy(vname,
                                    {"direction": side, "timeframe": "5m",
                                     "entry": "first close beyond OR before cutoff",
                                     "exit": "stop/target/session_end", "execution": "next_bar_open",
                                     "session_flat": True},
                                    make_sf(VALIDATION_PARAMS), sample)
            if not rep.passed:
                log(rep.summary())
                continue
            log("  Stage 2: VALID (incl. truncation lookahead test)")

            stats = {}
            objective = wf_train_objective(make_sf, make_bt, folds)
            cands = optimize(ORB_SPACE, objective, n_trials=N_TRIALS, top_n=TOP_FOR_MC,
                             min_trades=MIN_TRAIN_TRADES, stats=stats)
            log(f"  Stage 4: {N_TRIALS} trials, {stats['scarcity_pruned']} scarcity-pruned")
            for c in cands:
                log(f"    {c.params} mean-train Sharpe {c.is_sharpe:.2f}")
            if not cands:
                continue

            for c in cands:
                ev = wf_evaluate(make_sf, make_bt, c.params, dfs, folds)
                leaderboard.append((vname, ev))
                folds_str = ",".join(fmt(s).strip() for s in ev.fold_oos_sharpes)
                log(f"  Stage 5 {c.params}: OOS {fmt(ev.oos_sharpe)} folds[{folds_str}] "
                    f"MC5 {fmt(ev.mc_p05_sharpe)} sens {fmt(ev.sensitivity_score)} "
                    f"trades {ev.full_trades}")

    log(f"\n{'=' * 100}")
    log("PROVISIONAL LEADERBOARD — formal gate UNSATISFIABLE at this history depth; verdicts indicative only")
    log(f"{'=' * 100}")
    for vname, ev in sorted(leaderboard,
                            key=lambda x: -(x[1].mc_p05_sharpe if not pd.isna(x[1].mc_p05_sharpe) else -9)):
        gate = wf_acceptance_gate(ev)
        fails = [n for n, ok, _ in gate if not ok]
        pstr = ",".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                        for k, v in ev.params.items())
        log(f"{vname:<22}{pstr:<42} IS {fmt(ev.is_sharpe)} OOS {fmt(ev.oos_sharpe)} "
            f"MC5 {fmt(ev.mc_p05_sharpe)} trades {ev.full_trades:>5}  "
            f"{'would-pass' if not fails else 'fails: ' + '; '.join(fails[:3])}")
    log(f"\nRun time {time.time() - t0:.0f}s — full log logs/research/gen4a_orb_{RUN_ID}.log")


if __name__ == "__main__":
    main()
