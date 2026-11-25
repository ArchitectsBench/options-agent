"""Step 4c — autonomous research loop over the intraday strategy families.

Per family:
  Stage 2: rule validation on every ticker (reject family if any fail)
  Stage 4: Optuna optimization of POOLED Sharpe (equal-weight across the 5
           tickers) on the first 70% — one parameter set per family, shared
           across tickers, which itself resists per-ticker curve fitting
  Stage 5: Monte Carlo on the top 3 candidates (pooled); best by MC 5th-pct
           gets a per-ticker breakdown
  Gate:    CLAUDE.md Stage 6 acceptance bar

Everything logged to logs/research/. Usage: python src/research/run_research.py
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

from src.research.backtester import TRADING_DAYS_PER_YEAR, backtest
from src.research.families import FAMILIES
from src.research.monte_carlo import (
    MonteCarloReport,
    acceptance_gate,
    bootstrap_sharpe,
    evaluate_candidate,
    parameter_sensitivity,
    years_of,
)
from src.research.optimizer import optimize
from src.research.promoter import promote
from src.research.rule_validator import validate_strategy
from src.research.signals import as_signals

TICKERS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]
IS_FRACTION = 0.70
N_TRIALS = 200
TOP_FOR_MC = 3
BT_KWARGS = {"flat_at_session_end": True}

LOG_DIR = ROOT / "logs" / "research"
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")


class Logger:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOG_DIR / f"run_{RUN_ID}.log", "w")

    def __call__(self, msg: str = ""):
        print(msg)
        self.fh.write(msg + "\n")
        self.fh.flush()


log = Logger()


def fmt(x, spec=".2f"):
    return "  nan" if pd.isna(x) else f"{x:{spec}}"


def pooled_run(factory, params: dict, dfs: dict[str, pd.DataFrame]):
    """Backtest each ticker; pool as an equal-weight portfolio."""
    results = {}
    for ticker, df in dfs.items():
        results[ticker] = backtest(df, as_signals(factory(**params)(df)), **BT_KWARGS)
    rets = pd.concat(
        {t: r.equity.pct_change() for t, r in results.items()}, axis=1
    ).fillna(0.0)
    pooled_ret = rets.mean(axis=1)
    pooled_equity = (1 + pooled_ret).cumprod()
    ppy = np.mean([r.periods_per_year for r in results.values()])
    return results, pooled_equity, ppy


def pooled_sharpe(pooled_equity: pd.Series, ppy: float) -> float:
    r = pooled_equity.pct_change().dropna()
    if len(r) == 0 or r.std() == 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(ppy))


def evaluate_pooled(factory, params, is_sharpe, dfs_is, dfs_oos, dfs_full, constraint) -> MonteCarloReport:
    """MonteCarloReport for the pooled portfolio (mirrors evaluate_candidate)."""
    is_results, _, _ = pooled_run(factory, params, dfs_is)
    trade_returns = np.array([t.pnl_pct for r in is_results.values() for t in r.trades])
    years_is = years_of(next(iter(dfs_is.values())))
    trades_per_year = len(trade_returns) / years_is if years_is > 0 else float("nan")
    mc_median, mc_p05 = bootstrap_sharpe(trade_returns, trades_per_year)

    def pooled_sharpe_for(p: dict) -> float:
        _, eq, ppy = pooled_run(factory, p, dfs_is)
        return pooled_sharpe(eq, ppy)

    sens_score, sens_detail = parameter_sensitivity(is_sharpe, params, pooled_sharpe_for, constraint)

    _, oos_eq, oos_ppy = pooled_run(factory, params, dfs_oos)
    full_results, full_eq, full_ppy = pooled_run(factory, params, dfs_full)
    peak = full_eq.cummax()

    return MonteCarloReport(
        params=params,
        is_sharpe=is_sharpe,
        oos_sharpe=pooled_sharpe(oos_eq, oos_ppy),
        mc_median_sharpe=mc_median,
        mc_p05_sharpe=mc_p05,
        sensitivity_score=sens_score,
        sensitivity_detail=sens_detail,
        full_sharpe=pooled_sharpe(full_eq, full_ppy),
        full_max_dd=float((full_eq / peak - 1).min()),
        full_trades=sum(r.n_trades for r in full_results.values()),
    )


def main():
    t0 = time.time()
    specs = yaml.safe_load((ROOT / "config" / "strategy_rules.yaml").read_text())
    dfs_full = {t: pd.read_parquet(ROOT / "data" / "historical" / f"{t}_1h.parquet") for t in TICKERS}
    dfs_is, dfs_oos = {}, {}
    for t, df in dfs_full.items():
        split = int(len(df) * IS_FRACTION)
        dfs_is[t], dfs_oos[t] = df.iloc[:split], df.iloc[split:]

    sample = dfs_full["SPY"]
    data_window = f"{sample.index.min().date()}..{sample.index.max().date()}"
    log(f"Research run {RUN_ID} — {len(TICKERS)} tickers, 1h bars, "
        f"{len(sample)} bars/ticker ({sample.index.min().date()} to {sample.index.max().date()})")
    log(f"IS/OOS split: {IS_FRACTION:.0%} ({dfs_is['SPY'].index.max().date()} boundary), "
        f"session-flat execution, {N_TRIALS} trials/family\n")

    leaderboard = []  # rows: (family, ticker, report)

    for fam_name, fam in FAMILIES.items():
        factory, space, constraint = fam["factory"], fam["space"], fam["constraint"]
        log(f"{'=' * 70}\nFAMILY: {fam_name}\n{'=' * 70}")

        # ── Stage 2: validate on every ticker ──
        all_valid = True
        for ticker, df in dfs_full.items():
            report = validate_strategy(fam_name, specs[fam_name], factory(**fam["validation_params"]), df)
            status = "VALID" if report.passed else "REJECTED"
            failed = [name for name, ok, _ in report.checks if not ok]
            log(f"  Stage 2 [{ticker}]: {status}" + (f" — failed: {failed}" if failed else ""))
            if not report.passed:
                log("\n" + report.summary())
                all_valid = False
        if not all_valid:
            log(f"  Family {fam_name} rejected at Stage 2 — skipping Stages 4-5.\n")
            continue

        # ── Stage 4: pooled optimization (in-sample only) ──
        trials_path = LOG_DIR / f"run_{RUN_ID}_{fam_name}_trials.jsonl"
        trials_fh = open(trials_path, "w")

        def objective(params: dict) -> float:
            _, eq, ppy = pooled_run(factory, params, dfs_is)
            return pooled_sharpe(eq, ppy)

        def log_trial(params: dict, value: float):
            trials_fh.write(json.dumps({"params": params, "pooled_is_sharpe": value}) + "\n")

        candidates = optimize(space, objective, n_trials=N_TRIALS, top_n=TOP_FOR_MC,
                              constraint=constraint, trial_callback=log_trial)
        trials_fh.close()
        log(f"  Stage 4: {N_TRIALS} trials done (logged to {trials_path.name}). Top {len(candidates)}:")
        for c in candidates:
            log(f"    {c.params}  pooled IS Sharpe {c.is_sharpe:.2f}")

        # ── Stage 5: Monte Carlo on top candidates (pooled) ──
        log("  Stage 5: Monte Carlo on top candidates (pooled portfolio):")
        mc_reports = []
        for c in candidates:
            r = evaluate_pooled(factory, c.params, c.is_sharpe, dfs_is, dfs_oos, dfs_full, constraint)
            mc_reports.append(r)
            log(f"    {c.params}: OOS {fmt(r.oos_sharpe)}, MC med {fmt(r.mc_median_sharpe)}, "
                f"MC 5pct {fmt(r.mc_p05_sharpe)}, sens {fmt(r.sensitivity_score)}, trades {r.full_trades}")
        mc_reports.sort(key=lambda r: -np.inf if pd.isna(r.mc_p05_sharpe) else r.mc_p05_sharpe, reverse=True)
        best = mc_reports[0]
        log(f"  Best by MC 5th-pct: {best.params}")

        # per-ticker breakdown of the best candidate
        for ticker in TICKERS:
            entry, exit_ = None, None  # per-ticker IS sharpe for the report
            is_res = backtest(dfs_is[ticker], as_signals(factory(**best.params)(dfs_is[ticker])), **BT_KWARGS)
            tr = evaluate_candidate(best.params, is_res.sharpe, dfs_is[ticker], dfs_oos[ticker],
                                    dfs_full[ticker], factory, backtest_kwargs=BT_KWARGS,
                                    constraint=constraint)
            leaderboard.append((fam_name, ticker, tr))
        leaderboard.append((fam_name, "POOLED", best))
        log("")

    # ── Final leaderboard ──
    log(f"\n{'=' * 100}\nFINAL LEADERBOARD (gate rev. 2026-07-01: Sharpe>=0.8, OOS>=0.6 & >=0.6*IS, maxDD<=20%, MC5pct>=0.0, worstYr>=0.0 regime, >=100 trades)\n{'=' * 100}")
    header = (f"{'family':<18}{'ticker':<8}{'params':<46}{'IS':>6}{'OOS':>7}"
              f"{'MC5pct':>8}{'worstYr':>8}{'trades':>7}  result")
    log(header)
    log("-" * len(header))
    any_passed = False
    for fam_name, ticker, r in leaderboard:
        gate = acceptance_gate(r)
        passed = all(ok for _, ok, _ in gate)
        any_passed |= passed and ticker == "POOLED"
        reasons = "; ".join(name for name, ok, _ in gate if not ok)
        pstr = ",".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in r.params.items())
        log(f"{fam_name:<18}{ticker:<8}{pstr:<46}{fmt(r.is_sharpe):>6}{fmt(r.oos_sharpe):>7}"
            f"{fmt(r.mc_p05_sharpe):>8}{fmt(r.worst_year_sharpe):>8}{r.full_trades:>7}  "
            + ("PASS" if passed else f"FAIL ({reasons})"))
        # Stage 6 — promote passers to config/validated_strategies.yaml (frozen).
        # promote() re-checks the gate and refuses anything that fails, so this is
        # safe even if `passed` were miscomputed above.
        if passed:
            try:
                entry = promote(fam_name, ticker, r, RUN_ID, data_window)
                log(f"  -> PROMOTED to config/validated_strategies.yaml as '{entry['id']}' (params frozen)")
            except ValueError as exc:
                log(f"  -> promotion REFUSED: {exc}")

    log(f"\n=> {'At least one candidate PASSED the acceptance bar.' if any_passed else 'No candidate passed the acceptance bar (pooled).'}")
    log(f"Run time: {time.time() - t0:.0f}s. Full log: logs/research/run_{RUN_ID}.log")


if __name__ == "__main__":
    main()
