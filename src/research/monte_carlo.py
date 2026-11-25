"""Stage 5 — Monte Carlo Overfit Filtering.

Three tests per optimized candidate:

  a) Trade resampling (bootstrap, 1000 draws). Trade returns are resampled
     WITH replacement — a pure order shuffle leaves mean/std (and therefore
     Sharpe) unchanged, so the bootstrap is what yields a confidence
     interval on Sharpe. Reported: median and 5th-percentile Sharpe.
  b) Parameter sensitivity: each numeric parameter perturbed ±20% (others
     held). A robust strategy degrades gracefully; a Sharpe cliff means the
     optimizer found a lucky spike, i.e. overfit. Score = worst perturbed
     Sharpe / base Sharpe (closer to 1.0 is better). Categorical/boolean
     params are not perturbed.
  c) Out-of-sample check on the reserved 30%: OOS Sharpe should hold up
     vs in-sample (acceptance gate uses OOS >= 0.6 and OOS within 60% of IS;
     revised from 0.8 on 2026-07-01 with the headline-bar revision).

Candidates are ranked by MC 5th-percentile Sharpe — NOT raw in-sample Sharpe.
"""

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from src.research.backtester import TRADING_DAYS_PER_YEAR, backtest
from src.research.signals import as_signals


@dataclass
class MonteCarloReport:
    params: dict
    is_sharpe: float
    oos_sharpe: float
    mc_median_sharpe: float
    mc_p05_sharpe: float
    sensitivity_score: float  # worst perturbed Sharpe / base Sharpe
    sensitivity_detail: list[tuple[str, object, float]]  # (param, value, sharpe)
    full_sharpe: float
    full_max_dd: float
    full_trades: int
    worst_year_sharpe: float = float("nan")       # regime acid test
    regime_by_year: dict | None = None            # {year: sharpe} for the report


def years_of(df: pd.DataFrame) -> float:
    """Calendar length in trading years, counted in sessions (intraday-safe)."""
    return pd.DatetimeIndex(df.index).normalize().nunique() / TRADING_DAYS_PER_YEAR


# ── Regime robustness (the acid test) ──
# The bootstrap MC5 is BLIND to regime clustering: when most of the sample is one
# regime (e.g. 2024-26 bull), a resample of daily returns is dominated by good days
# and MC5 looks robust even when the strategy LOSES in the bear. ORB (2yr MC5 3.64 ->
# -0.59 in the 2022 bear) and base+EQGROW (MC5 0.60 -> -0.53 in 2022) both passed MC5
# while failing the bear. This gate requires the strategy to be flat-to-positive in
# every judgeable calendar year — directly implementing "positive 2022 = real alpha".
REGIME_MIN_SESSIONS = 100        # a year needs >= this many sessions to be judged
REGIME_MIN_WORST_YEAR_SHARPE = 0.0  # worst judged year must be >= this. Deliberately
                                     # strict (prefers false-negatives to false-positives,
                                     # per the project's overfit-rejection ethos). Tunable.


def per_year_sharpe(returns: pd.Series, periods_per_year: float = TRADING_DAYS_PER_YEAR,
                    min_sessions: int = REGIME_MIN_SESSIONS) -> dict[int, float]:
    """Annualized Sharpe within each calendar year of a dated return series. Years
    with < min_sessions bars are omitted (too short to judge a regime)."""
    r = returns.dropna()
    if not isinstance(r.index, pd.DatetimeIndex) or r.empty:
        return {}
    out = {}
    for year, d in r.groupby(r.index.year):
        if len(d) >= min_sessions and d.std() > 0:
            out[int(year)] = float(d.mean() / d.std() * np.sqrt(periods_per_year))
    return out


def worst_year_sharpe(returns: pd.Series, periods_per_year: float = TRADING_DAYS_PER_YEAR,
                      min_sessions: int = REGIME_MIN_SESSIONS) -> tuple[float, dict[int, float]]:
    """(worst per-calendar-year Sharpe, {year: sharpe}). NaN if no year qualifies —
    the gate then fails closed (can't prove regime robustness => not promoted)."""
    by = per_year_sharpe(returns, periods_per_year, min_sessions)
    if not by:
        return float("nan"), {}
    return min(by.values()), by


def bootstrap_sharpe(
    trade_returns: np.ndarray,
    trades_per_year: float,
    n_resamples: int = 1000,
    seed: int = 7,
) -> tuple[float, float]:
    """Median and 5th-percentile annualized Sharpe across bootstrap resamples."""
    if len(trade_returns) < 3:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    samples = rng.choice(trade_returns, size=(n_resamples, len(trade_returns)), replace=True)
    means = samples.mean(axis=1)
    stds = samples.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpes = means / stds * np.sqrt(trades_per_year)
    sharpes = sharpes[np.isfinite(sharpes)]
    if len(sharpes) == 0:
        return float("nan"), float("nan")
    return float(np.median(sharpes)), float(np.percentile(sharpes, 5))


def perturbed_param_sets(params: dict) -> list[tuple[str, object, dict]]:
    """±20% variants of each numeric parameter (ints rounded, bools untouched)."""
    out = []
    for name, value in params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        for factor in (0.8, 1.2):
            perturbed = dict(params)
            if isinstance(value, int):
                new = max(1, round(value * factor))
                if new == value:
                    continue
                perturbed[name] = new
            else:
                perturbed[name] = value * factor
            out.append((name, perturbed[name], perturbed))
    return out


def _default_constraint(params: dict) -> bool:
    if "fast_period" in params and "slow_period" in params:
        return params["fast_period"] < params["slow_period"]
    return True


def parameter_sensitivity(
    base_sharpe: float,
    params: dict,
    sharpe_fn: Callable[[dict], float],
    constraint: Callable[[dict], bool] | None = None,
) -> tuple[float, list[tuple[str, object, float]]]:
    """Perturb numeric params ±20%; return worst Sharpe retention + detail."""
    constraint = constraint or _default_constraint
    detail = []
    worst = np.inf
    for name, value, perturbed in perturbed_param_sets(params):
        if not constraint(perturbed):
            continue  # violates constraint; skip rather than fabricate
        sharpe = sharpe_fn(perturbed)
        sharpe = float("nan") if pd.isna(sharpe) else float(sharpe)
        detail.append((name, value, sharpe))
        if not np.isnan(sharpe):
            worst = min(worst, sharpe)
    if not np.isfinite(worst) or base_sharpe <= 0:
        return float("nan"), detail
    return worst / base_sharpe, detail


def evaluate_candidate(
    params: dict,
    is_sharpe: float,
    df_in_sample: pd.DataFrame,
    df_out_of_sample: pd.DataFrame,
    df_full: pd.DataFrame,
    signal_factory,
    n_resamples: int = 1000,
    backtest_kwargs: dict | None = None,
    constraint: Callable[[dict], bool] | None = None,
) -> MonteCarloReport:
    bt_kwargs = backtest_kwargs or {}

    def run(df: pd.DataFrame, p: dict):
        return backtest(df, as_signals(signal_factory(**p)(df)), **bt_kwargs)

    # a) bootstrap Sharpe CI from in-sample trades
    is_result = run(df_in_sample, params)
    trade_returns = np.array([t.pnl_pct for t in is_result.trades])
    years_is = years_of(df_in_sample)
    trades_per_year = len(trade_returns) / years_is if years_is > 0 else float("nan")
    mc_median, mc_p05 = bootstrap_sharpe(trade_returns, trades_per_year, n_resamples)

    # b) parameter sensitivity on in-sample
    sens_score, sens_detail = parameter_sensitivity(
        is_sharpe, params, lambda p: run(df_in_sample, p).sharpe, constraint
    )

    # c) out-of-sample check (first time OOS data is touched)
    oos_sharpe = run(df_out_of_sample, params).sharpe
    oos_sharpe = float("nan") if pd.isna(oos_sharpe) else float(oos_sharpe)

    # full-period stats for the acceptance gate
    full_result = run(df_full, params)

    # regime acid test: worst per-calendar-year Sharpe of the full equity curve
    wy_sharpe, wy_by = worst_year_sharpe(
        full_result.equity.pct_change(), full_result.periods_per_year)

    return MonteCarloReport(
        params=params,
        is_sharpe=is_sharpe,
        oos_sharpe=oos_sharpe,
        mc_median_sharpe=mc_median,
        mc_p05_sharpe=mc_p05,
        sensitivity_score=sens_score,
        sensitivity_detail=sens_detail,
        full_sharpe=float("nan") if pd.isna(full_result.sharpe) else float(full_result.sharpe),
        full_max_dd=full_result.max_drawdown,
        full_trades=full_result.n_trades,
        worst_year_sharpe=wy_sharpe,
        regime_by_year=wy_by,
    )


def acceptance_gate(report: MonteCarloReport) -> list[tuple[str, bool, str]]:
    """Stage 6 acceptance bar from CLAUDE.md. Returns (criterion, passed, actual).

    REVISED 2026-07-01 (user decision — docs/live-readiness-review-2026-07-01.md):
    headline Sharpe 1.2 -> 0.8, OOS 0.8 -> 0.6, MC5 >0.5 -> >=0.0. Six strategy
    generations showed 1.2 unreachable with retail-accessible data, and every false
    pass was caught by the robustness gates (worst-year, OOS ratio) rather than the
    headline — those gates are unchanged. A backtest pass now promotes to FORWARD
    VALIDATION only; live additionally requires the pre-committed criteria frozen
    in config/forward_acceptance.yaml.
    """
    def fmt(x):
        return "nan" if pd.isna(x) else f"{x:.2f}"

    return [
        ("Sharpe >= 0.8 (full period)", not pd.isna(report.full_sharpe) and report.full_sharpe >= 0.8, fmt(report.full_sharpe)),
        ("OOS Sharpe >= 0.6", not pd.isna(report.oos_sharpe) and report.oos_sharpe >= 0.6, fmt(report.oos_sharpe)),
        ("OOS within 60% of IS", not pd.isna(report.oos_sharpe) and report.is_sharpe > 0 and report.oos_sharpe >= 0.6 * report.is_sharpe, f"{fmt(report.oos_sharpe)} vs 0.6*IS={fmt(0.6 * report.is_sharpe)}"),
        ("Max drawdown <= 20%", report.full_max_dd >= -0.20, f"{report.full_max_dd:.1%}"),
        ("MC 5th-pct Sharpe >= 0.0", not pd.isna(report.mc_p05_sharpe) and report.mc_p05_sharpe >= 0.0, fmt(report.mc_p05_sharpe)),
        (f"Worst-year Sharpe >= {REGIME_MIN_WORST_YEAR_SHARPE:.1f} (regime)",
         not pd.isna(report.worst_year_sharpe) and report.worst_year_sharpe >= REGIME_MIN_WORST_YEAR_SHARPE,
         fmt(report.worst_year_sharpe) + (f" worst of {report.regime_by_year}" if report.regime_by_year else "")),
        (">= 100 trades", report.full_trades >= 100, str(report.full_trades)),
    ]
