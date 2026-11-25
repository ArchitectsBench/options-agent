"""Stage 4 — Parameter Optimization (Optuna / Bayesian TPE).

Rules from CLAUDE.md:
  - Optimize on the FIRST 70% of data only (in-sample). The caller passes
    the in-sample slice / objective; this module never sees OOS data.
  - Cap the search space — more than ~5 free parameters is an overfitting
    machine. `optimize()` refuses larger spaces.

Generic interface: a param space spec + an objective(params) -> Sharpe.
Invalid combinations (per the constraint callable) are pruned so the
sampler learns the constraint.
"""

from dataclasses import dataclass
from typing import Callable

import optuna
import pandas as pd

from src.research.backtester import backtest

optuna.logging.set_verbosity(optuna.logging.WARNING)

MAX_FREE_PARAMS = 5

# space entries: name -> ("int", lo, hi) | ("float", lo, hi) | ("cat", [choices])
ParamSpace = dict[str, tuple]


@dataclass
class Candidate:
    params: dict
    is_sharpe: float


def optimize(
    space: ParamSpace,
    objective: Callable[[dict], float],
    n_trials: int = 200,
    top_n: int = 10,
    seed: int = 42,
    constraint: Callable[[dict], bool] | None = None,
    trial_callback: Callable[[dict, float], None] | None = None,
    min_trades: int | None = None,
    stats: dict | None = None,
) -> list[Candidate]:
    """objective may return Sharpe, or (Sharpe, n_trades). When min_trades is
    set, trials below it are PRUNED — the optimizer cannot score a config
    whose 'edge' is a handful of lucky trades (the trade-scarcity cheat).
    Pruned-for-scarcity count is written to stats['scarcity_pruned']."""
    if len(space) > MAX_FREE_PARAMS:
        raise ValueError(f"{len(space)} free parameters > {MAX_FREE_PARAMS} — overfitting machine, refused")
    if stats is not None:
        stats.setdefault("scarcity_pruned", 0)

    def opt_objective(trial: optuna.Trial) -> float:
        params = {}
        for pname, spec in space.items():
            kind = spec[0]
            if kind == "int":
                params[pname] = trial.suggest_int(pname, spec[1], spec[2])
            elif kind == "float":
                params[pname] = trial.suggest_float(pname, spec[1], spec[2])
            elif kind == "cat":
                params[pname] = trial.suggest_categorical(pname, spec[1])
            else:
                raise ValueError(f"unknown param kind {kind!r}")
        if constraint is not None and not constraint(params):
            raise optuna.TrialPruned()
        value = objective(params)
        n_trades = None
        if isinstance(value, tuple):
            value, n_trades = value
        if min_trades is not None and n_trades is not None and n_trades < min_trades:
            if stats is not None:
                stats["scarcity_pruned"] += 1
            raise optuna.TrialPruned()
        value = -5.0 if pd.isna(value) else float(value)
        if trial_callback is not None:
            trial_callback(params, value)
        return value

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed),
    )
    study.optimize(opt_objective, n_trials=n_trials)

    # Dedupe identical param combos, keep top N.
    seen: dict[tuple, tuple[dict, float]] = {}
    for t in study.trials:
        if t.state != optuna.trial.TrialState.COMPLETE:
            continue
        key = tuple(sorted(t.params.items()))
        if key not in seen or t.value > seen[key][1]:
            seen[key] = (t.params, t.value)
    ranked = sorted(seen.values(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [Candidate(params=p, is_sharpe=v) for p, v in ranked]


def optimize_ema_crossover(
    df_in_sample: pd.DataFrame,
    n_trials: int = 200,
    top_n: int = 10,
    seed: int = 42,
    backtest_kwargs: dict | None = None,
) -> list[Candidate]:
    """Back-compat wrapper for the original EMA crossover study."""
    from src.research.strategies import ema_crossover

    bt_kwargs = backtest_kwargs or {}

    def objective(params: dict) -> float:
        entry, exit_ = ema_crossover(params["fast_period"], params["slow_period"])(df_in_sample)
        return backtest(df_in_sample, entry, exit_, **bt_kwargs).sharpe

    return optimize(
        space={"fast_period": ("int", 5, 50), "slow_period": ("int", 10, 100)},
        objective=objective,
        n_trials=n_trials,
        top_n=top_n,
        seed=seed,
        constraint=lambda p: p["fast_period"] < p["slow_period"],
    )
