"""5m squeeze experiment with BETA CONTROL (Track A #17) — the clean answer.

Resolves the question the daily-bar A/B left confounded: does requiring an
intraday squeeze fire add ALPHA to a directional debit-spread entry, or just
change beta exposure? Runs on 5 years of regular-hours 5m (data/research5m,
2021-2026 — spans the 2022 BEAR, the regime diversity the daily test lacked).

Method — forward-return signal quality with beta decomposition:
  Entry trigger fires on a 5m bar; we measure the UNDERLYING forward return
  over a multi-day horizon (debit spreads hold 2-7 DTE — Level-1 per
  CLAUDE.md), and decompose it:
      raw return = BETA (SPY's move over the same window) + ALPHA (excess).
  If a variant's edge is all beta, dropping the squeeze just buys more
  market. If the squeeze adds ALPHA (excess > 0 where no-squeeze isn't),
  it earns the mandate.

Variants (same state: bullish 9/21 stack + leading RS vs SPY; differ only in
the entry TRIGGER):
  A  squeeze REQUIRED  — fire only on a 5m squeeze fire
  B  squeeze DROPPED   — fire when bullish+leading turns on
Plus a per-window BETA row (SPY's forward return) so alpha is explicit.
Broken down by calendar year so the 2022 bear is visible.

CAVEAT printed: overlapping multi-day windows inflate apparent significance;
the beta-decomposition + regime breakdown are the robustness checks. Gross
of option-level costs (Level-1 underlying test).

Usage: python src/research/run_squeeze_5m_experiment.py
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

STORE = ROOT / "data" / "research5m"
CFG = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
TICKERS = [t for t in CFG["watchlist"]["default"]]
BARS_PER_DAY = 78
HORIZONS = {"1d": BARS_PER_DAY, "3d": 3 * BARS_PER_DAY}
RS_WIN = BARS_PER_DAY                 # trailing-1-day relative return vs SPY
# squeeze params (live-consistent: stock_signals SQUEEZE)
BB_PERIOD, RANK_WIN, WIDTH_PCT, MOM_PERIOD = 36, 126, 0.31, 23
LOG = ROOT / "logs" / "research" / f"squeeze_5m_{datetime.now():%Y%m%d_%H%M%S}.log"


def _entries(df: pd.DataFrame, spy_close: pd.Series, require_squeeze: bool) -> pd.Series:
    close = df["Close"]
    e9, e21 = ema(close, 9), ema(close, 21)
    stack_bull = e9 > e21
    spy = spy_close.reindex(df.index).ffill()
    rs = (close / close.shift(RS_WIN) - 1) - (spy / spy.shift(RS_WIN) - 1)
    rs_lead = rs > 0
    state = (stack_bull & rs_lead).fillna(False)
    if require_squeeze:
        width = 4 * close.rolling(BB_PERIOD).std() / sma(close, BB_PERIOD)
        rank = rolling_percentile(width, RANK_WIN)
        squeezed = (rank <= WIDTH_PCT).fillna(False)
        mom = close > ema(close, MOM_PERIOD)
        fired = squeezed.shift(1, fill_value=False) & ~squeezed & (width > width.shift(1)) & mom
        return (state & fired).fillna(False)
    return (state & ~state.shift(1, fill_value=False)).fillna(False)  # rising edge


def collect(require_squeeze: bool, dfs, spy_close) -> pd.DataFrame:
    recs = []
    for tk, df in dfs.items():
        entry = _entries(df, spy_close, require_squeeze)
        if not entry.any():
            continue
        close = df["Close"]
        spy = spy_close.reindex(df.index).ffill()
        idx = np.where(entry.to_numpy())[0]
        c = close.to_numpy(); s = spy.to_numpy()
        for i in idx:
            row = {"ticker": tk, "year": df.index[i].year}
            ok = True
            for name, h in HORIZONS.items():
                if i + h >= len(c) or c[i] <= 0 or s[i] <= 0:
                    ok = False
                    break
                row[f"fwd_{name}"] = c[i + h] / c[i] - 1
                row[f"beta_{name}"] = s[i + h] / s[i] - 1
                row[f"alpha_{name}"] = row[f"fwd_{name}"] - row[f"beta_{name}"]
            if ok:
                recs.append(row)
    return pd.DataFrame(recs)


def summarize(name, df, log):
    n = len(df)
    parts = [f"{name:<18}n={n:<6}"]
    for h in HORIZONS:
        raw, beta, alpha = df[f"fwd_{h}"], df[f"beta_{h}"], df[f"alpha_{h}"]
        a_sharpe = alpha.mean() / alpha.std() if alpha.std() else float("nan")
        parts.append(f"[{h}] raw {raw.mean():+.3%} = beta {beta.mean():+.3%} + "
                     f"ALPHA {alpha.mean():+.3%} (win {(alpha > 0).mean():.0%}, "
                     f"a-Sharpe {a_sharpe:.2f})")
    log("  " + "  ".join(parts))


def main():
    out = open(LOG, "w")
    def log(m=""):
        print(m); out.write(m + "\n")

    spy = pd.read_parquet(STORE / "SPY.parquet")["Close"]
    dfs = {t: pd.read_parquet(STORE / f"{t}.parquet") for t in TICKERS
           if (STORE / f"{t}.parquet").exists()}
    s = next(iter(dfs.values()))
    log(f"5m SQUEEZE EXPERIMENT (beta-controlled) — {len(dfs)} tickers, "
        f"{s.index.min().date()}..{s.index.max().date()}")
    log("raw return = beta (SPY move, same window) + ALPHA (excess). The question: "
        "does requiring squeeze add ALPHA, or just beta?\n")

    A = collect(True, dfs, spy)
    B = collect(False, dfs, spy)

    log("POOLED (all 5 years):")
    summarize("A squeeze REQ ", A, log)
    summarize("B squeeze DROP", B, log)

    log("\nBY YEAR — ALPHA (excess over SPY), 3d horizon [2022 = bear regime]:")
    log(f"  {'year':<6}{'A n':>7}{'A alpha3d':>11}{'B n':>7}{'B alpha3d':>11}")
    for yr in sorted(set(A["year"]) | set(B["year"])):
        a, b = A[A.year == yr], B[B.year == yr]
        aa = f"{a['alpha_3d'].mean():+.3%}" if len(a) else "-"
        ba = f"{b['alpha_3d'].mean():+.3%}" if len(b) else "-"
        log(f"  {yr:<6}{len(a):>7}{aa:>11}{len(b):>7}{ba:>11}")

    log("\nINDICATIVE READ (not an auto-decision):")
    am, bm = A["alpha_3d"].mean(), B["alpha_3d"].mean()
    a_sh = A["alpha_3d"].mean() / A["alpha_3d"].std()
    b_sh = B["alpha_3d"].mean() / B["alpha_3d"].std()
    log(f"  3d ALPHA: squeeze-required {am:+.3%} (a-Sharpe {a_sh:.2f}) vs "
        f"dropped {bm:+.3%} (a-Sharpe {b_sh:.2f})")
    if am > bm and a_sh > b_sh:
        log("  => requiring squeeze adds ALPHA beyond beta — the mandate is earned.")
    elif bm >= am and b_sh >= b_sh * 0:  # B at least as good
        if abs(bm) < 0.001:
            log("  => neither shows real alpha — both are mostly beta; squeeze is a throttle, not an edge. (debit_spread directional thesis may lack underlying-signal edge.)")
        else:
            log("  => dropping squeeze does NOT lose alpha — squeeze mainly throttles; candidate to demote (validate further, esp. 2022 bear column).")
    log("\nCAVEATS: overlapping multi-day windows inflate significance (lean on the "
        "beta-decomposition + per-year columns); gross of option costs (Level-1). "
        "Neither variant is gate-validated; this is a relative A/B to inform the "
        "squeeze-mandatory decision (a separate xhigh call).")
    log(f"\nlog: {LOG.name}")
    out.close()


if __name__ == "__main__":
    main()
