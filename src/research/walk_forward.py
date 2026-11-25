"""Walk-forward validation (replaces the single 70/30 split).

Folds roll forward through the data: each has ~6 months (126 sessions) of
train and ~3 months (63 sessions) of test, with start offsets spread evenly
so the 5 test windows sample different regimes. A candidate's OOS metric is
the AGGREGATE of all test folds; per-fold Sharpe is reported so regime
dependence is visible (one bad fold vs uniformly dead).

Optimization caveat (logged with results): parameters are shared across
folds, so a fold's test window can overlap a later fold's train window.
Train windows never overlap their own fold's test window.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.research.backtester import BacktestResult, backtest
from src.research.monte_carlo import bootstrap_sharpe, parameter_sensitivity
from src.research.signals import as_signals

TRAIN_SESSIONS = 126  # ~6 months
TEST_SESSIONS = 63   # ~3 months
N_FOLDS = 5


@dataclass
class Fold:
    number: int
    train: dict[str, pd.DataFrame]
    test: dict[str, pd.DataFrame]
    train_range: tuple
    test_range: tuple


def make_folds(
    dfs: dict[str, pd.DataFrame],
    n_folds: int = N_FOLDS,
    train_sessions: int = TRAIN_SESSIONS,
    test_sessions: int = TEST_SESSIONS,
) -> list[Fold]:
    calendar = sorted(pd.DatetimeIndex(next(iter(dfs.values())).index).normalize().unique())
    span = train_sessions + test_sessions
    max_offset = len(calendar) - span
    if max_offset < 0:
        raise ValueError(f"not enough sessions ({len(calendar)}) for a {span}-session fold")
    offsets = [round(k * max_offset / (n_folds - 1)) for k in range(n_folds)]

    folds = []
    for k, off in enumerate(offsets, 1):
        train_dates = set(calendar[off : off + train_sessions])
        test_dates = set(calendar[off + train_sessions : off + span])
        train, test = {}, {}
        for ticker, df in dfs.items():
            sid = pd.DatetimeIndex(df.index).normalize()
            train[ticker] = df[sid.isin(train_dates)]
            test[ticker] = df[sid.isin(test_dates)]
        folds.append(
            Fold(
                number=k,
                train=train,
                test=test,
                train_range=(min(train_dates).date(), max(train_dates).date()),
                test_range=(min(test_dates).date(), max(test_dates).date()),
            )
        )
    return folds


def pooled_run(signal_func, dfs: dict[str, pd.DataFrame], bt_kwargs: dict):
    """Backtest each ticker; pool as an equal-weight portfolio.
    Returns (per-ticker results, pooled bar-returns Series, periods_per_year)."""
    results: dict[str, BacktestResult] = {}
    for ticker, df in dfs.items():
        results[ticker] = backtest(df, as_signals(signal_func(df)), **bt_kwargs)
    rets = pd.concat({t: r.equity.pct_change() for t, r in results.items()}, axis=1).fillna(0.0)
    pooled_ret = rets.mean(axis=1)
    ppy = float(np.mean([r.periods_per_year for r in results.values()]))
    return results, pooled_ret, ppy


def sharpe_of(returns: pd.Series, ppy: float) -> float:
    r = returns.dropna()
    if len(r) < 2 or r.std() == 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(ppy))


@dataclass
class WFEvaluation:
    params: dict
    is_sharpe: float            # mean Sharpe across train folds
    fold_train_sharpes: list[float] = field(default_factory=list)
    oos_sharpe: float = float("nan")   # aggregate across all test folds
    fold_oos_sharpes: list[float] = field(default_factory=list)
    mc_median_sharpe: float = float("nan")
    mc_p05_sharpe: float = float("nan")
    sensitivity_score: float = float("nan")
    full_sharpe: float = float("nan")
    full_max_dd: float = float("nan")
    full_trades: int = 0
    train_trades: int = 0


def wf_train_objective(make_signal_func, make_bt_kwargs, folds: list[Fold]):
    """Returns objective(params) -> (mean train Sharpe, total train trades)
    for Stage 4 with scarcity pruning."""

    def objective(params: dict):
        sharpes, n_trades = [], 0
        signal_func = make_signal_func(params)
        bt_kwargs = make_bt_kwargs(params)
        for fold in folds:
            results, rets, ppy = pooled_run(signal_func, fold.train, bt_kwargs)
            sharpes.append(sharpe_of(rets, ppy))
            n_trades += sum(r.n_trades for r in results.values())
        valid = [s for s in sharpes if not pd.isna(s)]
        return (float(np.mean(valid)) if valid else float("nan"), n_trades)

    return objective


def wf_evaluate(
    make_signal_func,
    make_bt_kwargs,
    params: dict,
    dfs_full: dict[str, pd.DataFrame],
    folds: list[Fold],
    constraint=None,
    n_resamples: int = 1000,
) -> WFEvaluation:
    """Full Stage 5 evaluation under walk-forward: per-fold train/test
    Sharpe, aggregate OOS, bootstrap CI on train trades, parameter
    sensitivity on the walk-forward objective, full-period stats."""
    signal_func = make_signal_func(params)
    bt_kwargs = make_bt_kwargs(params)

    train_sharpes, test_sharpes, test_rets = [], [], []
    train_trade_returns: list[float] = []
    train_sessions_total = 0
    test_ppy = float("nan")
    for fold in folds:
        results, rets, ppy = pooled_run(signal_func, fold.train, bt_kwargs)
        train_sharpes.append(sharpe_of(rets, ppy))
        train_trade_returns += [t.pnl_pct for r in results.values() for t in r.trades]
        train_sessions_total += pd.DatetimeIndex(next(iter(fold.train.values())).index).normalize().nunique()

        _, t_rets, test_ppy = pooled_run(signal_func, fold.test, bt_kwargs)
        test_sharpes.append(sharpe_of(t_rets, test_ppy))
        test_rets.append(t_rets)

    valid_train = [s for s in train_sharpes if not pd.isna(s)]
    is_sharpe = float(np.mean(valid_train)) if valid_train else float("nan")
    oos_sharpe = sharpe_of(pd.concat(test_rets), test_ppy)

    trade_arr = np.array(train_trade_returns)
    years_train = train_sessions_total / 252
    trades_per_year = len(trade_arr) / years_train if years_train > 0 else float("nan")
    mc_median, mc_p05 = bootstrap_sharpe(trade_arr, trades_per_year, n_resamples)

    def perturbed_sharpe(p: dict) -> float:
        return wf_train_objective(make_signal_func, make_bt_kwargs, folds)(p)[0]

    sens_score, _ = parameter_sensitivity(is_sharpe, params, perturbed_sharpe, constraint)

    full_results, full_rets, full_ppy = pooled_run(signal_func, dfs_full, bt_kwargs)
    full_eq = (1 + full_rets.fillna(0)).cumprod()

    return WFEvaluation(
        params=params,
        is_sharpe=is_sharpe,
        fold_train_sharpes=train_sharpes,
        oos_sharpe=oos_sharpe,
        fold_oos_sharpes=test_sharpes,
        mc_median_sharpe=mc_median,
        mc_p05_sharpe=mc_p05,
        sensitivity_score=sens_score,
        full_sharpe=sharpe_of(full_rets, full_ppy),
        full_max_dd=float((full_eq / full_eq.cummax() - 1).min()),
        full_trades=sum(r.n_trades for r in full_results.values()),
        train_trades=len(trade_arr),
    )


def wf_acceptance_gate(ev: WFEvaluation) -> list[tuple[str, bool, str]]:
    """Stage 6 acceptance bar, walk-forward edition (OOS = aggregate test folds)."""
    def fmt(x):
        return "nan" if pd.isna(x) else f"{x:.2f}"

    return [
        ("Sharpe >= 1.2 (full)", not pd.isna(ev.full_sharpe) and ev.full_sharpe >= 1.2, fmt(ev.full_sharpe)),
        ("OOS Sharpe >= 0.8", not pd.isna(ev.oos_sharpe) and ev.oos_sharpe >= 0.8, fmt(ev.oos_sharpe)),
        ("OOS within 60% of IS", not pd.isna(ev.oos_sharpe) and ev.is_sharpe > 0 and ev.oos_sharpe >= 0.6 * ev.is_sharpe, f"{fmt(ev.oos_sharpe)} vs 0.6*IS={fmt(0.6 * ev.is_sharpe)}"),
        ("Max drawdown <= 20%", ev.full_max_dd >= -0.20, f"{ev.full_max_dd:.1%}"),
        ("MC 5th-pct Sharpe > 0.5", not pd.isna(ev.mc_p05_sharpe) and ev.mc_p05_sharpe > 0.5, fmt(ev.mc_p05_sharpe)),
        (">= 100 trades", ev.full_trades >= 100, str(ev.full_trades)),
    ]
