"""Phase 1, Step 2 — prove the Stage 2/3 pipeline with EMA 9/21 crossover on SPY daily.

Usage: python src/research/run_validation.py
"""

import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.indicators.core import cross_above, cross_below, ema
from src.research.backtester import backtest
from src.research.rule_validator import validate_strategy

STRATEGY = "ema_9_21_crossover"
TICKER = "SPY"


def make_signal_func(spec):
    fast_p = spec["indicators"]["ema_fast"]["period"]
    slow_p = spec["indicators"]["ema_slow"]["period"]

    def signals(df: pd.DataFrame):
        fast = ema(df["Close"], fast_p)
        slow = ema(df["Close"], slow_p)
        return cross_above(fast, slow), cross_below(fast, slow)

    return signals


def main():
    spec = yaml.safe_load((ROOT / "config" / "strategy_rules.yaml").read_text())[STRATEGY]
    df = pd.read_parquet(ROOT / "data" / "historical" / f"{TICKER}.parquet")
    signal_func = make_signal_func(spec)

    # ── Stage 2: rule validation ──
    report = validate_strategy(STRATEGY, spec, signal_func, df)
    print(report.summary())
    if not report.passed:
        print("\nStrategy rejected at Stage 2 — not backtesting.")
        sys.exit(1)

    # ── Stage 3: backtest ──
    entry_sig, exit_sig = signal_func(df)
    result = backtest(df, entry_sig, exit_sig)

    print(f"\nStage 3 — Backtest: {STRATEGY} on {TICKER} daily "
          f"({df.index.min().date()} to {df.index.max().date()}, {len(df)} bars)")
    print(f"  Sharpe ratio:    {result.sharpe:.2f}")
    print(f"  Win rate:        {result.win_rate:.1%}")
    print(f"  Profit factor:   {result.profit_factor:.2f}")
    print(f"  Max drawdown:    {result.max_drawdown:.1%}")
    print(f"  Total trades:    {result.n_trades}")
    print(f"  Avg bars held:   {result.avg_bars_held:.1f}")
    print(f"  Total return:    {result.total_return:.1%}")
    buy_hold = df["Close"].iloc[-1] / df["Close"].iloc[0] - 1
    print(f"  (Buy & hold:     {buy_hold:.1%} over same period)")


if __name__ == "__main__":
    main()
