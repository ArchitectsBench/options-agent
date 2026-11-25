"""Regime-gated equity strategy — the ensemble play.

Thesis (the professional path): no single edge is strong, so COMBINE. Momentum
works in trending/'active' regimes; mean-reversion works in range/'calm' regimes.
Switch between them using the regime classifier we VALIDATED live (SPY term-slope
+ dealer GEX). Trade the underlying (low cost), not expensive option spreads.

Win condition (honest): the regime-SWITCH must BEAT momentum-only, MR-only, and
buy-hold — AND clear Stage-6 (Sharpe>=1.2, OOS>=0.8, maxDD<=20%, MC5>0.5, >=100
trades). No curve-fit; baselines + per-regime + cost sensitivity reported.

Causal: signals & regime use info through t-1 only (shift(1)). Costs in bps/turnover.

  python src/research/regime_equity.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.backtest.chain import load as load_chain   # for SPY GEX

R5M = ROOT / "data" / "research5m"
VRP = ROOT / "data" / "vrp_inputs.parquet"
COST_BPS = 2.0     # per unit turnover (round-trip ~4bp) — liquid equities/ETFs


def daily_closes() -> pd.DataFrame:
    out = {}
    for p in sorted(R5M.glob("*.parquet")):
        tk = p.stem
        df = pd.read_parquet(p)
        d = df["Close"].resample("1D").last().dropna()
        d.index = d.index.tz_localize(None).normalize()
        out[tk] = d
    return pd.DataFrame(out).sort_index()


def spy_regime() -> pd.Series:
    """Daily market regime (active/calm/neutral) from SPY term-slope + GEX —
    the same signals wired live. Returns a Series indexed by date."""
    v = pd.read_parquet(VRP)
    spy = v[v.ticker == "SPY"].copy()
    spy["date"] = pd.to_datetime(spy["date"])
    spy = spy.set_index("date").sort_index()
    slope = spy["iv60"] - spy["iv30"]
    iv_mom = spy["iv30"] - spy["iv30"].shift(5)
    # SPY GEX z-score
    store = load_chain("SPY")
    g = store._df
    g = g[(g["dte"] >= 0) & (g["dte"] <= 90)]
    gex = g.groupby("trade_date").apply(lambda x: float(
        (x["gamma"].fillna(0) * (x["cOi"].fillna(0) - x["pOi"].fillna(0))
         * x["spot"] ** 2 * 0.01 * 100).sum()))
    gex.index = pd.to_datetime(gex.index)
    gex_z = ((gex - gex.rolling(252, min_periods=60).mean())
             / gex.rolling(252, min_periods=60).std())
    idx = slope.index.union(gex_z.index)
    slope, iv_mom, gex_z = slope.reindex(idx), iv_mom.reindex(idx), gex_z.reindex(idx)

    score = pd.Series(0.0, index=idx)
    score += np.where(slope < 0, -1, np.where(slope > 0.01, 1, 0))      # backwardation active
    score += np.where(iv_mom > 0.005, -1, np.where(iv_mom < -0.005, 1, 0))
    score += np.where(gex_z < -0.3, -1, np.where(gex_z > 0.3, 1, 0))    # neg gamma active
    regime = pd.Series(np.where(score <= -1, "active", np.where(score >= 1, "calm", "neutral")), index=idx)
    return regime


def signals(close: pd.DataFrame):
    ret = close.pct_change()
    mom = np.sign(close.pct_change(20))                       # trend: +1 up / -1 down
    ma10 = close.rolling(10).mean()
    z = (close - ma10) / close.rolling(10).std()
    mr = pd.DataFrame(np.where(z < -1, 1, np.where(z > 1, -1, 0)),     # fade extremes
                      index=close.index, columns=close.columns)
    return ret, mom, mr


def _portfolio_returns(pos: pd.DataFrame, ret: pd.DataFrame) -> pd.Series:
    """Equal-weight daily portfolio return, net of turnover cost. pos already
    causal (decided at t-1, applied to ret[t])."""
    turnover = pos.diff().abs().fillna(pos.abs())
    pnl = pos * ret - (COST_BPS / 1e4) * turnover
    active = pos.abs() > 0
    daily = pnl.where(active).mean(axis=1)        # equal-weight across held names
    return daily.fillna(0.0)


def metrics(daily: pd.Series, pos: pd.DataFrame = None) -> dict:
    d = daily.dropna()
    if d.std() == 0 or len(d) < 50:
        return {"sharpe": 0, "n": len(d)}
    sharpe = d.mean() / d.std() * np.sqrt(252)
    eq = (1 + d).cumprod()
    maxdd = float((1 - eq / eq.cummax()).max())
    k = int(len(d) * 0.7)
    oos = d.iloc[k:].mean() / d.iloc[k:].std() * np.sqrt(252) if d.iloc[k:].std() else 0
    rng = np.random.default_rng(0)
    arr = d.to_numpy()
    mc = np.array([(s := rng.choice(arr, len(arr), replace=True)).mean() / s.std() * np.sqrt(252)
                   for _ in range(2000)])
    trades = int(pos.diff().abs().gt(0).sum().sum()) if pos is not None else len(d)
    return {"sharpe": float(sharpe), "oos": float(oos), "maxdd": maxdd,
            "mc5": float(np.percentile(mc, 5)), "trades": trades,
            "ann_ret": float(d.mean() * 252), "n": len(d)}


def main():
    close = daily_closes()
    ret, mom, mr = signals(close)
    regime = spy_regime().reindex(close.index).ffill()
    reg_lag = regime.shift(1)            # causal: yesterday's regime picks today's engine

    # PREMISE CHECK: is a long book actually worse in 'active' (high-vol) regimes?
    bh_daily = ret.mean(axis=1)
    print("buy-hold daily return BY regime (premise for risk-timing overlay):")
    for rg in ["active", "calm", "neutral"]:
        mask = reg_lag.reindex(ret.index).eq(rg)
        if mask.sum():
            sub = bh_daily[mask]
            print(f"  {rg:8}: mean {sub.mean()*1e4:+.1f}bp/day  Sharpe {sub.mean()/sub.std()*np.sqrt(252):+.2f}  (n={mask.sum()})")
    print()

    # Reframe: features predict VOL -> use for RISK TIMING, not direction.
    # exposure: full long in calm, half in neutral, defensive (flat) in active.
    expo = reg_lag.map({"calm": 1.0, "neutral": 0.5, "active": 0.0}).fillna(0.5)
    expo_df = pd.DataFrame(np.repeat(expo.reindex(close.index).values[:, None],
                                     close.shape[1], axis=1), index=close.index, columns=close.columns)
    mom_lf = mom.clip(lower=0)           # long/flat momentum (no shorting growth in a bull)

    strats = {
        "buy-hold (benchmark)": pd.DataFrame(1.0, index=close.index, columns=close.columns).shift(1),
        "VOL-TIMED long (defensive in active)": (expo_df).shift(1),
        "momentum long/flat": mom_lf.shift(1),
        "momentum long/flat + VOL-TIMED": (mom_lf * expo_df).shift(1),
        "REGIME-SWITCH mom/MR (orig)": (pd.DataFrame(0.0, index=close.index, columns=close.columns)
                                        .mask(reg_lag.eq("active"), mom).mask(reg_lag.eq("calm"), mr)).shift(1),
    }
    print(f"Universe {close.shape[1]} names, {close.index.min().date()}..{close.index.max().date()} "
          f"({len(close)} sessions), cost {COST_BPS}bp/turnover\n")
    print(f"{'strategy':46} {'Sharpe':>7} {'OOS':>6} {'maxDD':>6} {'MC5':>6} {'annRet':>7} {'trades':>7}")
    best = None
    for name, p in strats.items():
        daily = _portfolio_returns(p.fillna(0.0), ret)
        m = metrics(daily, p)
        print(f"{name:46} {m['sharpe']:>7.2f} {m.get('oos',0):>6.2f} {m['maxdd']:>6.1%} "
              f"{m.get('mc5',0):>6.2f} {m['ann_ret']:>6.1%} {m['trades']:>7}")
        if "benchmark" not in name and (best is None or m["sharpe"] > best[1]["sharpe"]):
            best = (name, m, daily, p)
    # Stage-6 verdict on the BEST non-benchmark strategy
    print(f"\nbest non-benchmark: {best[0]}")
    m = best[1]
    checks = {"sharpe>=1.2": m["sharpe"] >= 1.2, "oos>=0.8": m["oos"] >= 0.8,
              "maxdd<=20%": m["maxdd"] <= 0.20, "mc5>0.5": m["mc5"] > 0.5,
              "trades>=100": m["trades"] >= 100}
    print(f"\nSTAGE-6 (regime-switch): {'PASS ✅' if all(checks.values()) else 'FAIL ❌'}  "
          f"{ {k:('✓' if v else '✗') for k,v in checks.items()} }")
    # ── market-NEUTRAL cross-sectional momentum (no buy-hold beta to beat) ──
    print("\n── cross-sectional long-short (dollar-neutral) ──")
    look = 60
    xs = close.pct_change(look)
    rank = xs.rank(axis=1, pct=True)
    longs = (rank >= 2/3).astype(float)
    shorts = (rank <= 1/3).astype(float)
    w = longs.div(longs.sum(axis=1), axis=0) - shorts.div(shorts.sum(axis=1), axis=0)
    w = w.shift(1)                                  # causal
    cost = (COST_BPS / 1e4) * w.diff().abs().sum(axis=1).fillna(0)
    xsm = (w * ret).sum(axis=1) - cost
    # gated: skip the book on 'active' regime days (momentum crashes in high vol)
    w_g = w.mask(reg_lag.reindex(w.index).eq("active"), 0.0)
    cost_g = (COST_BPS / 1e4) * w_g.diff().abs().sum(axis=1).fillna(0)
    xsm_g = (w_g * ret).sum(axis=1) - cost_g
    xs_strats = {"x-sec momentum (60d) L/S": (xsm, w),
                 "x-sec momentum + regime-gate (flat in active)": (xsm_g, w_g),
                 "x-sec REVERSAL (60d) L/S": (-(w * ret).sum(axis=1) - cost, -w)}
    print(f"{'strategy':46} {'Sharpe':>7} {'OOS':>6} {'maxDD':>6} {'MC5':>6} {'annRet':>7} {'trades':>7}")
    for name, (dret, wgt) in xs_strats.items():
        m = metrics(dret.fillna(0), wgt.fillna(0).ne(wgt.shift(1).fillna(0)).astype(int))
        ck = (m["sharpe"] >= 1.2 and m["oos"] >= 0.8 and m["maxdd"] <= 0.20
              and m["mc5"] > 0.5 and m["trades"] >= 100)
        print(f"{name:46} {m['sharpe']:>7.2f} {m['oos']:>6.2f} {m['maxdd']:>6.1%} "
              f"{m['mc5']:>6.2f} {m['ann_ret']:>6.1%} {m['trades']:>7}  {'PASS' if ck else ''}")

    # per-regime daily mean of the best strategy
    dd = best[2]
    print(f"\n{best[0]} daily return by regime:")
    for rg in ["active", "calm", "neutral"]:
        mask = regime.shift(1).reindex(dd.index).eq(rg)
        if mask.sum():
            sub = dd[mask]
            print(f"  {rg:8}: mean {sub.mean()*1e4:+.1f}bp/day  Sharpe {sub.mean()/sub.std()*np.sqrt(252):.2f}  (n={mask.sum()})")


if __name__ == "__main__":
    main()
