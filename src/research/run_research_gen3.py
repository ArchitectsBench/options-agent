"""Generation 3 — regime-conditioned squeeze under TRUE walk-forward.

Design:
  - Squeeze signal params FROZEN at gen-1 values (bb=36, width_pct=0.31,
    mom=23). Only the regime filter's params (<= 2) are optimized.
  - TRUE walk-forward: filter params re-optimized per fold on that fold's
    train window only, evaluated on its test window. This removes gen-2's
    fold-overlap caveat. The deployable record is the STITCHED test curve:
    the acceptance gate's Sharpe/trade-count/maxDD apply to it, and the MC
    bootstrap resamples its trades.
  - Costs 1.5bp/side. Regime label for session N uses data through N-1.

Filters: no_filter (baseline), vix_calm (VIX<T), vix_vol (VIX>T),
adx_trend (ADX>T), rv_band (RV pctile in [lo, hi]).

Usage: python src/research/run_research_gen3.py
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

from src.indicators.regime import attach_regime, regime_lookahead_test
from src.research.families import squeeze_proxy
from src.research.monte_carlo import bootstrap_sharpe, parameter_sensitivity
from src.research.optimizer import optimize
from src.research.rule_validator import validate_strategy
from src.research.signals import Signals
from src.research.walk_forward import WFEvaluation, make_folds, pooled_run, sharpe_of, wf_acceptance_gate

TICKERS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]
GEN1_SQUEEZE = {"bb_period": 36, "width_pct": 0.30951535327650626, "mom_period": 23}
BT_KWARGS = {"flat_at_session_end": True, "cost_bps": 1.5}
N_TRIALS_PER_FOLD = 60
MIN_TRAIN_TRADES_PER_FOLD = 25

LOG_DIR = ROOT / "logs" / "research"
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

FILTERS = {
    "no_filter": {"space": {}, "constraint": None},
    "vix_calm": {"space": {"t": ("float", 12.0, 30.0)}, "constraint": None},
    "vix_vol": {"space": {"t": ("float", 12.0, 35.0)}, "constraint": None},
    "adx_trend": {"space": {"t": ("float", 10.0, 40.0)}, "constraint": None},
    "rv_band": {
        "space": {"lo": ("float", 0.0, 0.5), "hi": ("float", 0.5, 1.0)},
        "constraint": lambda p: p["lo"] < p["hi"],
    },
}
VALIDATION_PARAMS = {
    "no_filter": {},
    "vix_calm": {"t": 20.0},
    "vix_vol": {"t": 20.0},
    "adx_trend": {"t": 20.0},
    "rv_band": {"lo": 0.2, "hi": 0.8},
}


class Logger:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOG_DIR / f"gen3_{RUN_ID}.log", "w")

    def __call__(self, msg: str = ""):
        print(msg)
        self.fh.write(msg + "\n")
        self.fh.flush()


log = Logger()


def fmt(x, spec=".2f"):
    return "  nan" if pd.isna(x) else f"{x:{spec}}"


def gate_series(df: pd.DataFrame, filter_name: str, params: dict) -> pd.Series:
    """NaN features compare False — the gate is OFF when the regime is unknown."""
    if filter_name == "no_filter":
        return pd.Series(True, index=df.index)
    if filter_name == "vix_calm":
        return (df["vix"] < params["t"]).fillna(False)
    if filter_name == "vix_vol":
        return (df["vix"] > params["t"]).fillna(False)
    if filter_name == "adx_trend":
        return (df["adx"] > params["t"]).fillna(False)
    if filter_name == "rv_band":
        return ((df["rv_pctile"] >= params["lo"]) & (df["rv_pctile"] <= params["hi"])).fillna(False)
    raise ValueError(filter_name)


def gated_squeeze(filter_name: str, params: dict):
    base = squeeze_proxy(**GEN1_SQUEEZE)

    def signals(df: pd.DataFrame) -> Signals:
        s = base(df)
        gate = gate_series(df, filter_name, params)
        return Signals(
            entry_long=s.entry_long & gate,
            exit_long=s.exit_long,
            entry_short=s.entry_short & gate,
            exit_short=s.exit_short,
        )

    return signals


def main():
    t0 = time.time()
    specs = yaml.safe_load((ROOT / "config" / "strategy_rules.yaml").read_text())
    vix_close = pd.read_parquet(ROOT / "data" / "historical" / "VIX_1d.parquet")["Close"]
    dfs = {
        t: attach_regime(pd.read_parquet(ROOT / "data" / "historical" / f"{t}_1h.parquet"), vix_close)
        for t in TICKERS
    }
    folds = make_folds(dfs)

    sample = dfs["SPY"]
    log(f"Gen-3 research run {RUN_ID} — regime-conditioned squeeze, true walk-forward")
    log(f"{len(TICKERS)} tickers, {len(sample)} 1h bars/ticker "
        f"({sample.index.min().date()} to {sample.index.max().date()}), costs 1.5bp/side")
    log(f"Squeeze params frozen at gen-1: {GEN1_SQUEEZE}")
    log(f"Per fold: {N_TRIALS_PER_FOLD} Optuna trials on train only, scarcity floor "
        f"{MIN_TRAIN_TRADES_PER_FOLD} train trades. Gate applies to the STITCHED OOS curve.")
    rv_valid_from = sample["rv_pctile"].first_valid_index()
    log(f"Warmup: rv_pctile first valid {rv_valid_from.date() if rv_valid_from is not None else 'never'} "
        f"(needs 272 prior sessions) — rv_band is gate-OFF before that, F1/F2 effectively dead for it.\n")

    # ── Stage 2: lookahead test on the regime labels themselves ──
    log("Stage 2a — regime label truncation test (per ticker):")
    for t in TICKERS:
        raw = pd.read_parquet(ROOT / "data" / "historical" / f"{t}_1h.parquet")
        ok, detail = regime_lookahead_test(raw, vix_close)
        log(f"  [{t}] {'PASS' if ok else 'FAIL'} — {detail}")
        if not ok:
            log("  Regime layer rejected — aborting run.")
            sys.exit(1)

    # ── Stage 2b: validate each gated strategy ──
    log("\nStage 2b — gated strategy validation:")
    for fname in FILTERS:
        sf = gated_squeeze(fname, VALIDATION_PARAMS[fname])
        bad = []
        for t in TICKERS:
            rep = validate_strategy(f"squeeze+{fname}", specs["squeeze_regime"], sf, dfs[t])
            if not rep.passed:
                bad.append((t, [n for n, ok, _ in rep.checks if not ok]))
        log(f"  squeeze+{fname}: " + ("VALID on all tickers" if not bad else f"REJECTED {bad}"))
    log("")

    leaderboard = []

    for fname, fcfg in FILTERS.items():
        space, constraint = fcfg["space"], fcfg["constraint"]
        log(f"{'=' * 70}\nFILTER: {fname}\n{'=' * 70}")
        fold_params, fold_train_sharpes, fold_test_sharpes = [], [], []
        test_rets, oos_trades = [], []
        scarcity_total = 0
        trials_fh = open(LOG_DIR / f"gen3_{RUN_ID}_{fname}_trials.jsonl", "w")

        for fold in folds:
            if space:
                stats = {}

                def objective(params: dict):
                    results, rets, ppy = pooled_run(gated_squeeze(fname, params), fold.train, BT_KWARGS)
                    n_trades = sum(r.n_trades for r in results.values())
                    s = sharpe_of(rets, ppy)
                    trials_fh.write(json.dumps({"fold": fold.number, "params": params,
                                                "train_sharpe": None if pd.isna(s) else s,
                                                "train_trades": n_trades}) + "\n")
                    return (s, n_trades)

                cands = optimize(space, objective, n_trials=N_TRIALS_PER_FOLD, top_n=1,
                                 seed=fold.number, constraint=constraint,
                                 min_trades=MIN_TRAIN_TRADES_PER_FOLD, stats=stats)
                scarcity_total += stats["scarcity_pruned"]
                if not cands:
                    fold_params.append(None)
                    fold_train_sharpes.append(float("nan"))
                    fold_test_sharpes.append(float("nan"))
                    log(f"  F{fold.number}: no candidate cleared the scarcity floor "
                        f"({stats['scarcity_pruned']} pruned) — fold skipped, counted against the filter")
                    continue
                params, train_sharpe = cands[0].params, cands[0].is_sharpe
            else:
                params, train_sharpe = {}, None

            sf = gated_squeeze(fname, params)
            if train_sharpe is None:
                _, tr_rets, tr_ppy = pooled_run(sf, fold.train, BT_KWARGS)
                train_sharpe = sharpe_of(tr_rets, tr_ppy)
            results, t_rets, t_ppy = pooled_run(sf, fold.test, BT_KWARGS)
            n_test_trades = sum(r.n_trades for r in results.values())
            oos_trades += [t.pnl_pct for r in results.values() for t in r.trades]
            fold_params.append(params)
            fold_train_sharpes.append(train_sharpe)
            fold_test_sharpes.append(sharpe_of(t_rets, t_ppy))
            test_rets.append(t_rets)
            pstr = ",".join(f"{k}={v:.2f}" for k, v in params.items()) or "(none)"
            log(f"  F{fold.number}: params [{pstr}] train Shp {fmt(train_sharpe)} "
                f"-> test Shp {fmt(fold_test_sharpes[-1])}, test trades {n_test_trades}")
        trials_fh.close()
        if space:
            log(f"  Scarcity-pruned trials across folds: {scarcity_total}")

        if not test_rets:
            log(f"  {fname}: no usable folds — filter dead.\n")
            continue

        # Stitched OOS record (the deployable performance)
        stitched = pd.concat(test_rets)
        ppy = t_ppy
        oos_sharpe = sharpe_of(stitched, ppy)
        eq = (1 + stitched.fillna(0)).cumprod()
        max_dd = float((eq / eq.cummax() - 1).min())
        trade_arr = np.array(oos_trades)
        oos_sessions = sum(len(f.test[TICKERS[0]].index.normalize().unique()) for f in folds)
        mc_median, mc_p05 = bootstrap_sharpe(trade_arr, len(trade_arr) / (oos_sessions / 252))

        # Sensitivity: perturb every fold's chosen params by the same +/-20%
        def stitched_sharpe_with(perturb: dict | None, pname=None, factor=None) -> float:
            rets = []
            for fold, params in zip(folds, fold_params):
                if params is None:
                    continue
                p = dict(params)
                if pname is not None and pname in p:
                    p[pname] = p[pname] * factor
                _, r, ppy_ = pooled_run(gated_squeeze(fname, p), fold.test, BT_KWARGS)
                rets.append(r)
            return sharpe_of(pd.concat(rets), ppy_) if rets else float("nan")

        sens = float("nan")
        if space:
            worst = np.inf
            for pname in space:
                for factor in (0.8, 1.2):
                    s = stitched_sharpe_with(None, pname, factor)
                    if not pd.isna(s):
                        worst = min(worst, s)
            sens = worst / oos_sharpe if np.isfinite(worst) and oos_sharpe > 0 else float("nan")

        valid_train = [s for s in fold_train_sharpes if not pd.isna(s)]
        ev = WFEvaluation(
            params={"per_fold": fold_params},
            is_sharpe=float(np.mean(valid_train)) if valid_train else float("nan"),
            fold_train_sharpes=fold_train_sharpes,
            oos_sharpe=oos_sharpe,
            fold_oos_sharpes=fold_test_sharpes,
            mc_median_sharpe=mc_median,
            mc_p05_sharpe=mc_p05,
            sensitivity_score=sens,
            full_sharpe=oos_sharpe,   # true WF: the stitched OOS curve IS the record
            full_max_dd=max_dd,
            full_trades=len(trade_arr),
        )
        leaderboard.append((fname, ev))
        log(f"  Stitched OOS: Sharpe {fmt(oos_sharpe)}, maxDD {max_dd:.1%}, trades {len(trade_arr)}, "
            f"MC5pct {fmt(mc_p05)}, sens {fmt(sens)}\n")

    # ── Final leaderboard ──
    log(f"{'=' * 112}")
    log("FINAL LEADERBOARD — true walk-forward; gate applies to stitched OOS curve")
    log("(baseline no_filter per-fold profile for comparison: gen-2 was -1.07, +4.76, +2.10, +0.55, -2.13)")
    log(f"{'=' * 112}")
    header = (f"{'filter':<12}{'fold params (T or lo/hi)':<38}{'IS':>6}{'OOS':>7}"
              f"{'F1':>6}{'F2':>6}{'F3':>6}{'F4':>6}{'F5':>6}{'MC5':>7}{'trades':>7}  result")
    log(header)
    log("-" * len(header))
    any_passed = False
    for fname, ev in leaderboard:
        gate = wf_acceptance_gate(ev)
        passed = all(ok for _, ok, _ in gate)
        any_passed |= passed
        reasons = "; ".join(name for name, ok, _ in gate if not ok)
        pstr = " ".join(
            ("-" if p is None else "/".join(f"{v:.1f}" for v in p.values()) or "·")
            for p in ev.params["per_fold"]
        )
        folds_cols = "".join(f"{fmt(s):>6}" for s in ev.fold_oos_sharpes)
        log(f"{fname:<12}{pstr:<38}{fmt(ev.is_sharpe):>6}{fmt(ev.oos_sharpe):>7}"
            f"{folds_cols}{fmt(ev.mc_p05_sharpe):>7}{ev.full_trades:>7}  "
            + ("PASS" if passed else f"FAIL ({reasons})"))

    log(f"\n=> {'At least one filter PASSED the acceptance bar.' if any_passed else 'No filter passed the acceptance bar.'}")
    log(f"Run time: {time.time() - t0:.0f}s. Full log: logs/research/gen3_{RUN_ID}.log")


if __name__ == "__main__":
    main()
