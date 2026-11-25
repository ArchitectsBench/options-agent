"""Track A experiment: is the squeeze fire MANDATORY for debit_spread?

Offline only — touches nothing live. Answers the question with evidence
instead of intuition (per the 2026-06-15 discussion: debit_spread sat out a
momentum day because it requires a squeeze fire).

Models the debit_spread DIRECTIONAL thesis on daily bars (a Level-1
underlying-signal test per CLAUDE.md — would the directional move have been
profitable; not an exact replica of the live 5m logic). Two variants share
the SAME entry STATE (bullish EMA stack + leading relative strength vs SPY)
and the SAME exit (stack flips), differing only in the entry TRIGGER:

  A  squeeze REQUIRED  — enter only on a squeeze fire while bullish+leading
  B  squeeze DROPPED   — enter when bullish+leading turns on (no squeeze)

Compared pooled across 30 tickers: full-period Sharpe/win/PF/maxDD/trades,
walk-forward per-fold Sharpe (regime stability), and bootstrap Sharpe CI.
If requiring the squeeze improves ROBUST risk-adjusted return, it earns the
mandate; if it only cuts trade count without improving quality, it's a
throttle. Either way the decision is empirical.

Caveat printed in the report: daily-bar proxy, no parameter optimization
(fixed reasonable rules — an A/B, not a fit), 2yr history.

Usage: python src/research/run_squeeze_experiment.py
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.indicators.core import ema, rolling_percentile, sma
from src.research.backtester import backtest
from src.research.monte_carlo import bootstrap_sharpe
from src.research.signals import Signals
from src.research.walk_forward import make_folds, pooled_run, sharpe_of

CFG = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
TICKERS = CFG["watchlist"]["default"]
COST_BPS = 1.5
RS_LOOKBACK = 10           # trailing-N daily relative return vs SPY
SQ_PERIOD, SQ_RANK_WIN, SQ_PCT = 20, 126, 0.25
LOG = ROOT / "logs" / "research" / f"squeeze_experiment_{datetime.now():%Y%m%d_%H%M%S}.log"


def _load_daily(t):
    p = ROOT / "data" / "historical" / f"{t}.parquet"
    return pd.read_parquet(p) if p.exists() else None


def make_signal_func(require_squeeze: bool, spy_close: pd.Series):
    def signals(df: pd.DataFrame) -> Signals:
        close = df["Close"]
        e9, e21 = ema(close, 9), ema(close, 21)
        stack_bull = e9 > e21
        # relative strength vs SPY (trailing-N), aligned by date
        spy = spy_close.reindex(df.index).ffill()
        rs = (close / close.shift(RS_LOOKBACK) - 1) - (spy / spy.shift(RS_LOOKBACK) - 1)
        rs_lead = rs > 0
        # squeeze fire (BB-width bottom quartile -> expanding, momentum up)
        width = 4 * close.rolling(SQ_PERIOD).std() / sma(close, SQ_PERIOD)
        rank = rolling_percentile(width, SQ_RANK_WIN)
        squeezed = (rank <= SQ_PCT).fillna(False)
        fired = squeezed.shift(1, fill_value=False) & ~squeezed & (width > width.shift(1)) & (close > e9)

        state = (stack_bull & rs_lead).fillna(False)
        if require_squeeze:
            entry = (state & fired).fillna(False)            # trigger: squeeze fire
        else:
            entry = (state & ~state.shift(1, fill_value=False))  # trigger: state turns on
        exit_ = (~stack_bull).fillna(False)                   # exit: stack flips (same for both)
        return Signals(entry_long=entry, exit_long=exit_)

    return signals


def pooled_stats(signal_func, dfs):
    results, rets, ppy = pooled_run(signal_func, dfs, {"cost_bps": COST_BPS})
    trades = [t.pnl_pct for r in results.values() for t in r.trades]
    eq = (1 + rets.fillna(0)).cumprod()
    wins = sum(1 for r in results.values() for t in r.trades if t.pnl_pct > 0)
    n = len(trades)
    gp = sum(t for t in trades if t > 0); gl = -sum(t for t in trades if t < 0)
    years = len(next(iter(dfs.values()))) / 252
    mc_med, mc_p05 = bootstrap_sharpe(np.array(trades), n / years if years else np.nan)
    return {
        "sharpe": sharpe_of(rets, ppy), "trades": n,
        "win_rate": wins / n if n else float("nan"),
        "profit_factor": gp / gl if gl else float("inf"),
        "max_dd": float((eq / eq.cummax() - 1).min()),
        "mc_med": mc_med, "mc_p05": mc_p05,
    }


def main():
    out = open(LOG, "w")
    def log(m=""):
        print(m); out.write(m + "\n")

    spy_df = _load_daily("SPY")
    spy_close = spy_df["Close"]
    dfs = {t: _load_daily(t) for t in TICKERS if _load_daily(t) is not None and len(_load_daily(t)) > 200}
    sample = next(iter(dfs.values()))
    log(f"SQUEEZE EXPERIMENT — debit_spread directional thesis, daily bars")
    log(f"{len(dfs)} tickers, {sample.index.min().date()}..{sample.index.max().date()}, "
        f"costs {COST_BPS}bp, exit=stack-flip, RS vs SPY trailing {RS_LOOKBACK}d")
    log("CAVEAT: daily-bar Level-1 proxy of the live 5m logic; fixed rules (A/B, "
        "not optimized); informs a decision, does NOT change live strategy.\n")

    variants = {
        "A squeeze REQUIRED": make_signal_func(True, spy_close),
        "B squeeze DROPPED ": make_signal_func(False, spy_close),
    }
    folds = make_folds(dfs, n_folds=5, train_sessions=126, test_sessions=63)

    log(f"{'variant':<20}{'Sharpe':>8}{'trades':>8}{'win%':>7}{'PF':>6}{'maxDD':>8}{'MCmed':>7}{'MC5pct':>8}")
    log("-" * 72)
    stats = {}
    for name, sf in variants.items():
        s = pooled_stats(sf, dfs)
        stats[name] = s
        log(f"{name:<20}{s['sharpe']:>8.2f}{s['trades']:>8}{s['win_rate']:>7.0%}"
            f"{s['profit_factor']:>6.2f}{s['max_dd']:>8.1%}{s['mc_med']:>7.2f}{s['mc_p05']:>8.2f}")

    log("\nWalk-forward per-fold pooled Sharpe (regime stability):")
    log(f"{'variant':<20}{'F1':>7}{'F2':>7}{'F3':>7}{'F4':>7}{'F5':>7}{'OOS-agg':>9}")
    for name, sf in variants.items():
        test_rets = []
        for f in folds:
            _, tr, ppy = pooled_run(sf, f.test, {"cost_bps": COST_BPS})
            test_rets.append((sharpe_of(tr, ppy), tr))
        agg = sharpe_of(pd.concat([tr for _, tr in test_rets]), ppy)
        cells = "".join(f"{(s if not pd.isna(s) else 0):>7.2f}" for s, _ in test_rets)
        log(f"{name:<20}{cells}{agg:>9.2f}")

    # verdict heuristic (indicative — NOT an auto-decision)
    a, b = stats["A squeeze REQUIRED"], stats["B squeeze DROPPED "]
    log("\nINDICATIVE READ (not an auto-decision):")
    log(f"  requiring squeeze changes trade count {b['trades']}->{a['trades']} "
        f"({a['trades']/b['trades']:.0%}), full Sharpe {b['sharpe']:.2f}->{a['sharpe']:.2f}, "
        f"MC 5th-pct {b['mc_p05']:.2f}->{a['mc_p05']:.2f}")
    if a["sharpe"] > b["sharpe"] and a["mc_p05"] >= b["mc_p05"]:
        log("  => squeeze REQUIREMENT improves robust risk-adjusted return — keep it mandatory.")
    elif b["sharpe"] > a["sharpe"] and b["mc_p05"] > a["mc_p05"]:
        log("  => dropping squeeze improves BOTH Sharpe and robustness — candidate to demote to confirm (validate further before any live change).")
    else:
        log("  => mixed: squeeze mostly throttles trade count without clear robustness gain — inconclusive, needs the live-timeframe (5m) test before deciding.")
    log("\nNOTE: neither variant has passed the Stage-6 acceptance gate; this is a "
        "RELATIVE A/B, not a promotion. Any live change is a separate xhigh decision.")
    log(f"\nlog: {LOG.name}")
    out.close()


if __name__ == "__main__":
    main()
