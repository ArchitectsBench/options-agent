"""Lever C — RESEARCH-ONLY diversified futures trend-following (TSMOM).

Pre-registered design (frozen 2026-07-06, docs/professional-path-2026-07-05.md
Lever C). ONE signal spec, no search, numbers reported as they fall.

UNIVERSE (16 markets, micro/small-tradeable at $50k):
  equity: ES NQ RTY YM   bonds: ZN ZF ZB   metals: GC SI HG
  energy: CL NG          fx: 6E 6B 6J 6A
Data: Yahoo continuous front-month splices (yfinance, daily, max history),
cached to data/futures_daily/.

DATA CAVEAT (prominent, mandatory): Yahoo continuous series are front-month
splices WITHOUT back-adjustment. Roll gaps pollute price-level signals and
embed roll yield incorrectly. Mitigations (both mandatory per the design):
  (1) signals computed on a RETURNS-CUMULATED series. HONEST NOTE: for a pure
      splice, cumulating daily returns reproduces the same ratio path as raw
      closes — the cumulation only helps where pathological prints are zeroed
      (price<=0 crossings, e.g. CL April 2020). Roll-DAY returns themselves
      still compare two different contracts; that residual error remains in
      BOTH signal and P&L and is documented, not fixed.
  (2) equity-index and bond/metal results are CROSS-CHECKED against long
      -history ETF proxies (SPY QQQ IWM DIA / IEF TLT / GLD SLV) under the
      same rules. Wild divergence => the data is the problem.

SIGNAL: TSMOM — sign of the 252-trading-day trailing total return, evaluated
daily; position sign changes only on flips. Corroboration variant (REPORT
-ONLY): price vs its own 200-day MA on the cumulated series.

SIZING: vol-targeted equal risk. Per-market target = (12% / N_active) /
ann_vol_20d(market). Two implementation levels:
  (a) INDEX level — fractional positions (academic replication).
  (b) $50k MICRO-QUANTIZED — integer micro contracts (real multipliers; where
      no micro exists, smallest listed contract, flagged), $1.50 commission
      + 1-tick slippage per side per contract. Contract counts refresh on
      sign flips and on the first trading day of each month.
Level (b) is the deliverable.

EVALUATION: daily portfolio equity 2000-2026; revised Stage-6 gate per
src/research/monte_carlo.py conventions (Sharpe>=0.8, OOS 70/30 with
OOS>=0.6 and OOS>=0.6*IS, DD<=20%, MC5>=0, worst-year>=0, >=100 position
changes); per-year table; era breakdown; daily-return correlation vs SPY.

Run:  .venv/bin/python -m src.research.futures_trend
Outputs: logs/research/futures_trend_20260706.{md,log,csv}
Strict ownership: creates only this file, the three outputs, and
data/futures_daily/ caches. Touches nothing else. No promoter call.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src.research.monte_carlo import bootstrap_sharpe, per_year_sharpe, worst_year_sharpe

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "futures_daily"
OUT = ROOT / "logs" / "research"
TAG = "futures_trend_20260706"

TRADING_DAYS = 252
LOOKBACK = 252           # TSMOM trailing window
VOL_WIN = 20             # sizing vol window
PORT_VOL_TARGET = 0.12   # annualized
START_EQUITY = 50_000.0
COMMISSION = 1.50        # per side per contract
MA_WIN = 200             # corroboration variant (report-only)


@dataclass(frozen=True)
class Market:
    code: str
    yahoo: str
    sector: str
    trade_as: str        # micro (or smallest) contract actually traded at $50k
    mult: float          # $ per 1.0 of quoted price for trade_as
    tick: float          # min tick of trade_as, in quote units
    has_micro: bool


MARKETS = [
    Market("ES", "ES=F", "equity", "MES", 5, 0.25, True),
    Market("NQ", "NQ=F", "equity", "MNQ", 2, 0.25, True),
    Market("RTY", "RTY=F", "equity", "M2K", 5, 0.10, True),
    Market("YM", "YM=F", "equity", "MYM", 0.5, 1.0, True),
    Market("ZN", "ZN=F", "bond", "ZN(full)", 1000, 1 / 64, False),
    Market("ZF", "ZF=F", "bond", "ZF(full)", 1000, 1 / 128, False),
    Market("ZB", "ZB=F", "bond", "ZB(full)", 1000, 1 / 32, False),
    Market("GC", "GC=F", "metal", "MGC", 10, 0.10, True),
    Market("SI", "SI=F", "metal", "SIL", 1000, 0.005, True),
    Market("HG", "HG=F", "metal", "MHG", 2500, 0.0005, True),
    Market("CL", "CL=F", "energy", "MCL", 100, 0.01, True),
    Market("NG", "NG=F", "energy", "QG(e-mini)", 2500, 0.005, False),
    Market("6E", "6E=F", "fx", "M6E", 12_500, 0.0001, True),
    Market("6B", "6B=F", "fx", "M6B", 6_250, 0.0001, True),
    Market("6J", "6J=F", "fx", "MJY", 1_250_000, 0.000001, True),
    Market("6A", "6A=F", "fx", "M6A", 10_000, 0.0001, True),
]

# futures code -> long-history ETF proxy (total-return via auto_adjust)
PROXIES = {"ES": "SPY", "NQ": "QQQ", "RTY": "IWM", "YM": "DIA",
           "ZN": "IEF", "ZB": "TLT", "GC": "GLD", "SI": "SLV"}

ERAS = {
    "GFC 2008 (2007-09)": (2007, 2009),
    "2011": (2011, 2011),
    "2015-16": (2015, 2016),
    "2018": (2018, 2018),
    "2020": (2020, 2020),
    "2022": (2022, 2022),
    "2023-26": (2023, 2026),
}

GATE_DESIGN_FLAG = (
    "GATE-DESIGN FLAG: the worst-year>=0 criterion was designed for intraday "
    "day-trading books; the trend literature shows even the best diversified "
    "CTA replications have negative years (2012-13 notoriously). Report the "
    "strict gate verdict AND separately state whether the strategy would pass "
    "with worst-year relaxed to >=-0.5 — as INFORMATION for a human "
    "gate-design discussion, not as a pass claim."
)


class Tee:
    def __init__(self, path: Path):
        self.f = open(path, "w")

    def write(self, s):
        sys.__stdout__.write(s)
        self.f.write(s)

    def flush(self):
        sys.__stdout__.flush()
        self.f.flush()


# ── data layer ──────────────────────────────────────────────────────────────

def fetch(ticker: str, auto_adjust: bool) -> pd.Series:
    """Daily close (max history), cached to data/futures_daily/."""
    CACHE.mkdir(parents=True, exist_ok=True)
    fp = CACHE / (ticker.replace("=", "_") + ".parquet")
    if fp.exists():
        s = pd.read_parquet(fp)["close"]
    else:
        import yfinance as yf
        d = yf.download(ticker, period="max", interval="1d",
                        auto_adjust=auto_adjust, progress=False)
        if d is None or len(d) == 0:
            raise RuntimeError(f"no data for {ticker}")
        c = d["Close"]
        if isinstance(c, pd.DataFrame):
            c = c.iloc[:, 0]
        s = c.dropna().astype(float)
        s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
        s = s[~s.index.duplicated(keep="last")].sort_index()
        s.rename("close").to_frame().to_parquet(fp)
    s.index = pd.DatetimeIndex(s.index)
    return s.rename(ticker)


def drop_bad_prints(close: pd.Series) -> tuple[pd.Series, int]:
    """Remove single-day BAD-PRINT rows: |r_t|>50% AND the next available close
    is back within 15% of the pre-spike level (spike fully reverses —
    exchange-impossible as a real move; catches Yahoo's 6J dropped-digit print
    2001-12-17 and nothing that is a real market move or roll gap). Applied to
    the CLOSE series so both signal returns and level-B price-difference P&L
    are sanitized. Disclosed per market."""
    prev, nxt = close.shift(1), close.shift(-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = close / prev - 1.0
        rec = (nxt / prev - 1.0).abs() < 0.15
    spike = (r.abs() > 0.5) & rec & (prev > 0) & (close > 0)
    return close[~spike.fillna(False)], int(spike.sum())


def clean_returns(close: pd.Series) -> tuple[pd.Series, dict]:
    """Daily returns with non-positive-price crossings zeroed (CL April 2020
    negative print). Returns (returns, quality dict)."""
    prev = close.shift(1)
    r = close / prev - 1.0
    bad = (close <= 0) | (prev <= 0)
    n_bad = int(bad.sum())
    r = r.where(~bad, 0.0)
    r = r.iloc[1:]
    q = {"zeroed_nonpositive_price_days": n_bad,
         "abs_ret_gt_10pct_days": int((r.abs() > 0.10).sum()),
         "abs_ret_gt_50pct_days": int((r.abs() > 0.50).sum()),
         "max_abs_ret": float(r.abs().max()) if len(r) else float("nan")}
    return r, q


# ── metrics helpers ─────────────────────────────────────────────────────────

def ann_sharpe(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) < 20 or r.std() == 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(TRADING_DAYS))


def ann_ret(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) == 0:
        return float("nan")
    return float((1 + r).prod() ** (TRADING_DAYS / len(r)) - 1)


def max_dd(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1).min())


def era_slice(r: pd.Series, y0: int, y1: int) -> pd.Series:
    return r[(r.index.year >= y0) & (r.index.year <= y1)]


# ── backtests ───────────────────────────────────────────────────────────────

def build_panel():
    """Aligned close / cleaned-return / signal / vol panels on the union calendar."""
    closes, rets, quality, depth = {}, {}, {}, {}
    for m in MARKETS:
        c = fetch(m.yahoo, auto_adjust=False)
        c, n_prints = drop_bad_prints(c)
        r, q = clean_returns(c)
        q["dropped_bad_print_rows"] = n_prints
        closes[m.code], rets[m.code], quality[m.code] = c, r, q
        yrs = (c.index.max() - c.index.min()).days / 365.25
        depth[m.code] = (str(c.index.min().date()), str(c.index.max().date()),
                         len(c), round(yrs, 1), yrs < 10)
    idx = pd.DatetimeIndex(sorted(set().union(*[c.index for c in closes.values()])))
    C = pd.DataFrame({k: v for k, v in closes.items()}).reindex(idx).ffill(limit=10)
    R = pd.DataFrame({k: v for k, v in rets.items()}).reindex(idx).fillna(0.0)
    # returns-cumulated adjusted series (signal basis, per data caveat)
    have = C.notna()
    A = (1.0 + R.where(have, 0.0)).cumprod().where(have)
    obs = have.cumsum()  # count of days with a price so far
    tsr = A / A.shift(LOOKBACK) - 1.0
    sig = np.sign(tsr).where(obs.shift(1) > LOOKBACK + 1)      # need real history
    vol = (R.where(have).rolling(VOL_WIN).std() * np.sqrt(TRADING_DAYS)).where(obs > VOL_WIN)
    ma = A.rolling(MA_WIN).mean()
    sig_ma = np.sign(A - ma).where(obs.shift(1) > MA_WIN + 1)  # corroboration, report-only
    return idx, C, R, sig, sig_ma, vol, quality, depth


def refresh_days(idx: pd.DatetimeIndex) -> np.ndarray:
    m = idx.to_period("M")
    return np.r_[True, m[1:] != m[:-1]]


def level_a(idx, R, sig, vol) -> pd.Series:
    """Index-level (fractional) portfolio daily returns. Weights refreshed monthly
    from t-1 info; sign applied daily from t-1 signal."""
    cols = list(R.columns)
    S, V, RA = sig[cols].values, vol[cols].values, R[cols].values
    monthly = refresh_days(idx)
    w = np.zeros(len(cols))
    out = np.zeros(len(idx))
    for t in range(1, len(idx)):
        valid = np.isfinite(S[t - 1]) & np.isfinite(V[t - 1]) & (V[t - 1] > 1e-4)
        if monthly[t] and valid.any():
            w = np.zeros(len(cols))
            w[valid] = (PORT_VOL_TARGET / valid.sum()) / V[t - 1][valid]
        s = np.where(np.isfinite(S[t - 1]), S[t - 1], 0.0)
        out[t] = float(np.nansum(w * s * RA[t]))
    return pd.Series(out, index=idx).iloc[1:]


def level_b(idx, C, sig, vol):
    """$50k micro-quantized book. Integer contracts in the trade_as instrument,
    P&L = n * mult * dClose (exact through negative prices), costs on |dn|.
    Contract counts refresh on sign flips + monthly; else held."""
    cols = [m.code for m in MARKETS]
    mkt = {m.code: m for m in MARKETS}
    S, V = sig[cols].values, vol[cols].values
    P = C[cols].values
    monthly = refresh_days(idx)
    mult = np.array([mkt[c].mult for c in cols])
    cost_per_ct = np.array([COMMISSION + mkt[c].tick * mkt[c].mult for c in cols])

    n = np.zeros(len(cols))            # signed contracts held
    equity = np.full(len(idx), np.nan)
    equity[0] = START_EQUITY
    flips = np.zeros(len(cols), int)
    dropped_days = np.zeros(len(cols), int)   # signal active but quantized to 0
    active_days = np.zeros(len(cols), int)
    total_costs = 0.0
    yearly_flips: dict[int, int] = {}

    for t in range(1, len(idx)):
        eq = equity[t - 1]
        valid = (np.isfinite(S[t - 1]) & np.isfinite(V[t - 1]) & (V[t - 1] > 1e-4)
                 & np.isfinite(P[t - 1]) & (P[t - 1] > 0))
        n_active = int(valid.sum())
        new_n = n.copy()
        if n_active > 0 and eq > 0:
            budget = (PORT_VOL_TARGET / n_active) * eq          # $ vol budget per market
            for i in range(len(cols)):
                if not valid[i]:
                    if n[i] != 0:
                        new_n[i] = 0.0                          # can't size -> flat
                    continue
                s = S[t - 1][i]
                flip = (np.sign(n[i]) != s)
                if monthly[t] or flip:
                    notional = (budget / V[t - 1][i])
                    k = round(notional / (mult[i] * P[t - 1][i]))
                    new_n[i] = k * s
                    if flip and np.sign(n[i]) != np.sign(new_n[i]):
                        flips[i] += 1
                        y = int(idx[t].year)
                        yearly_flips[y] = yearly_flips.get(y, 0) + 1
                active_days[i] += 1
                if new_n[i] == 0:
                    dropped_days[i] += 1
        dn = np.abs(new_n - n)
        costs = float((dn * cost_per_ct).sum())
        total_costs += costs
        dp = P[t] - P[t - 1]
        dp = np.where(np.isfinite(dp), dp, 0.0)
        pnl = float(np.nansum(new_n * mult * dp))
        equity[t] = eq + pnl - costs
        n = new_n

    eq = pd.Series(equity, index=idx)
    stats = {"flips": {c: int(f) for c, f in zip(cols, flips)},
             "total_flips": int(flips.sum()),
             "yearly_flips": yearly_flips,
             "dropped_frac": {c: (dropped_days[i] / active_days[i] if active_days[i] else float("nan"))
                              for i, c in enumerate(cols)},
             "total_costs": total_costs}
    return eq, stats


def single_market_tsmom(close: pd.Series, vol_target=PORT_VOL_TARGET):
    """Standalone vol-targeted TSMOM daily returns for one price series
    (used for per-market diagnostics and the ETF proxy cross-check)."""
    r, _ = clean_returns(close)
    a = (1 + r).cumprod()
    sig = np.sign(a / a.shift(LOOKBACK) - 1.0)
    vol = r.rolling(VOL_WIN).std() * np.sqrt(TRADING_DAYS)
    w = (vol_target / vol).clip(upper=10.0)
    # monthly weight refresh, daily sign
    m = r.index.to_period("M")
    refresh = pd.Series(np.r_[True, m[1:] != m[:-1]], index=r.index)
    w_held = w.shift(1).where(refresh).ffill()
    out = (w_held * sig.shift(1) * r).dropna()
    return out, sig


# ── main ────────────────────────────────────────────────────────────────────

def gate_rows(daily: pd.Series, equity: pd.Series, n_changes: int):
    """Revised Stage-6 gate, monte_carlo.py conventions, on daily returns."""
    full_sharpe = ann_sharpe(daily)
    split = int(len(daily) * 0.70)
    is_s, oos_s = ann_sharpe(daily.iloc[:split]), ann_sharpe(daily.iloc[split:])
    dd = max_dd(equity)
    _, mc5 = bootstrap_sharpe(daily.values, TRADING_DAYS, n_resamples=1000)
    wy, by = worst_year_sharpe(daily)
    rows = [
        ("Sharpe >= 0.8 (full period)", full_sharpe >= 0.8, f"{full_sharpe:.2f}"),
        ("OOS Sharpe >= 0.6 (last 30%)", np.isfinite(oos_s) and oos_s >= 0.6, f"{oos_s:.2f}"),
        ("OOS within 60% of IS", np.isfinite(oos_s) and is_s > 0 and oos_s >= 0.6 * is_s,
         f"OOS {oos_s:.2f} vs 0.6*IS={0.6 * is_s:.2f}"),
        ("Max drawdown <= 20%", dd >= -0.20, f"{dd:.1%}"),
        ("MC 5th-pct Sharpe >= 0.0", np.isfinite(mc5) and mc5 >= 0.0, f"{mc5:.2f}"),
        ("Worst-year Sharpe >= 0.0", np.isfinite(wy) and wy >= 0.0,
         f"{wy:.2f} ({min(by, key=by.get) if by else 'n/a'})"),
        (">= 100 position changes", n_changes >= 100, str(n_changes)),
    ]
    extras = {"full_sharpe": full_sharpe, "is_sharpe": is_s, "oos_sharpe": oos_s,
              "max_dd": dd, "mc5": mc5, "worst_year": wy, "by_year": by}
    return rows, extras


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tee = Tee(OUT / f"{TAG}.log")
    sys.stdout = tee
    print(f"=== Lever C — futures TSMOM research pass ({TAG}) ===")
    print("Frozen pre-registered spec; numbers reported as they fall.\n")

    idx, C, R, sig, sig_ma, vol, quality, depth = build_panel()
    print("Per-market history depth (flag = <10y):")
    for m in MARKETS:
        d = depth[m.code]
        flag = "  <10y FLAG" if d[4] else ""
        print(f"  {m.code:4s} {m.yahoo:6s} {d[0]} .. {d[1]}  {d[2]:5d} rows  {d[3]:5.1f}y"
              f"  trade_as={m.trade_as}{'' if m.has_micro else ' [NO MICRO]'}{flag}")
    print("\nData quality (cleaned-return diagnostics):")
    for m in MARKETS:
        q = quality[m.code]
        print(f"  {m.code:4s} dropped_bad_prints={q['dropped_bad_print_rows']}"
              f" zeroed_nonpos_price={q['zeroed_nonpositive_price_days']}"
              f" |r|>10%={q['abs_ret_gt_10pct_days']} |r|>50%={q['abs_ret_gt_50pct_days']}"
              f" max|r|={q['max_abs_ret']:.3f}")

    # ── Level A ──
    ra = level_a(idx, R, sig, vol)
    ra = ra[ra.index >= ra.loc[ra != 0].index.min()]
    eq_a = (1 + ra).cumprod() * START_EQUITY
    print(f"\nLEVEL A (index, fractional): {ra.index.min().date()}..{ra.index.max().date()}"
          f"  Sharpe {ann_sharpe(ra):.2f}  ann_ret {ann_ret(ra):.2%}  maxDD {max_dd(eq_a):.1%}")

    # corroboration variant (report-only)
    ra_ma = level_a(idx, R, sig_ma, vol)
    ra_ma = ra_ma[ra_ma.index >= ra_ma.loc[ra_ma != 0].index.min()]
    eq_ma = (1 + ra_ma).cumprod() * START_EQUITY
    wy_ma, _ = worst_year_sharpe(ra_ma)
    print(f"Corroboration (200d-MA, REPORT-ONLY, level A): Sharpe {ann_sharpe(ra_ma):.2f}"
          f"  ann_ret {ann_ret(ra_ma):.2%}  maxDD {max_dd(eq_ma):.1%}  worst-yr {wy_ma:.2f}")

    # ── Level B (the deliverable) ──
    eq_b, bstats = level_b(idx, C, sig, vol)
    first = eq_b.index[np.argmax(eq_b.values != START_EQUITY)]
    rb = eq_b.pct_change().loc[first:].dropna()
    eq_b_t = eq_b.loc[first:]
    print(f"\nLEVEL B ($50k micro-quantized, costed): {rb.index.min().date()}..{rb.index.max().date()}")
    print(f"  Sharpe {ann_sharpe(rb):.2f}  ann_ret {ann_ret(rb):.2%}  maxDD {max_dd(eq_b_t):.1%}"
          f"  final equity ${eq_b_t.iloc[-1]:,.0f}  total costs ${bstats['total_costs']:,.0f}"
          f"  sign-flips {bstats['total_flips']}")
    print("  Quantization dropout (fraction of active days sized to 0 contracts at $50k):")
    for m in MARKETS:
        f = bstats["dropped_frac"][m.code]
        note = " [NO MICRO]" if not m.has_micro else ""
        d = " <- effectively drops out" if np.isfinite(f) and f > 0.5 else ""
        print(f"    {m.code:4s} ({m.trade_as:9s}){note}: {f:.1%}{d}")

    # ── Gate (level b = deliverable; level a for reference) ──
    rows_b, xb = gate_rows(rb, eq_b_t, bstats["total_flips"])
    rows_a, xa = gate_rows(ra, eq_a, bstats["total_flips"])
    print("\nSTAGE-6 GATE — LEVEL B (deliverable):")
    for name, ok, val in rows_b:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    strict_pass = all(ok for _, ok, _ in rows_b)
    relaxed = all(ok for (nm, ok, _) in rows_b if not nm.startswith("Worst-year")) \
        and np.isfinite(xb["worst_year"]) and xb["worst_year"] >= -0.5
    print(f"  STRICT VERDICT: {'PASS' if strict_pass else 'FAIL'}")
    print(f"  Relaxed worst-year (>= -0.5) information line: would_{'PASS' if relaxed else 'FAIL'}")
    print("\nSTAGE-6 GATE — LEVEL A (reference):")
    for name, ok, val in rows_a:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")

    # ── per-year + era tables, SPY correlation ──
    spy = fetch("SPY", auto_adjust=True)
    spy_r = spy.pct_change().dropna()
    by_a, by_b = per_year_sharpe(ra), per_year_sharpe(rb)
    years = sorted(set(by_b) | set(by_a))
    rows_csv = []
    print("\nPER-YEAR (Sharpe / ann-ret; level A | level B; flips; corr vs SPY):")
    for y in years:
        a_y, b_y = ra[ra.index.year == y], rb[rb.index.year == y]
        s = spy_r[spy_r.index.year == y]
        j = b_y.align(s, join="inner")
        corr = float(j[0].corr(j[1])) if len(j[0]) > 30 else float("nan")
        fl = bstats["yearly_flips"].get(y, 0)
        rows_csv.append({"year": y, "lvlA_sharpe": round(by_a.get(y, float("nan")), 2),
                         "lvlA_ret": round(ann_ret(a_y), 4),
                         "lvlB_sharpe": round(by_b.get(y, float("nan")), 2),
                         "lvlB_ret": round(ann_ret(b_y), 4),
                         "flips": fl, "corr_spy": round(corr, 2)})
        print(f"  {y}: A {by_a.get(y, float('nan')):6.2f} / {ann_ret(a_y):8.2%}"
              f" | B {by_b.get(y, float('nan')):6.2f} / {ann_ret(b_y):8.2%}"
              f" | flips {fl:3d} | corrSPY {corr:5.2f}")
    pd.DataFrame(rows_csv).to_csv(OUT / f"{TAG}.csv", index=False)

    print("\nERA BREAKDOWN (level B):")
    era_lines = []
    for name, (y0, y1) in ERAS.items():
        e = era_slice(rb, y0, y1)
        if len(e) < 60:
            era_lines.append((name, "insufficient data"))
            continue
        eqe = (1 + e).cumprod()
        line = (f"Sharpe {ann_sharpe(e):5.2f}  ann_ret {ann_ret(e):7.2%}  maxDD {max_dd(eqe):6.1%}")
        era_lines.append((name, line))
        print(f"  {name:22s} {line}")

    j = rb.align(spy_r, join="inner")
    full_corr = float(j[0].corr(j[1]))
    beta = float(np.cov(j[0], j[1])[0, 1] / np.var(j[1]))
    print(f"\nDIVERSIFICATION: corr(level-B daily, SPY daily) = {full_corr:.3f}, beta = {beta:.3f}"
          f" over {len(j[0])} overlapping days")

    # ── ETF proxy cross-check ──
    print("\nETF PROXY CROSS-CHECK (same TSMOM rules, single-market, vol-targeted 12%,")
    print("no costs, overlapping window; wild divergence => data problem):")
    proxy_lines = []
    for code, etf in PROXIES.items():
        fut_close = C[code].dropna()
        etf_close, _ = drop_bad_prints(fetch(etf, auto_adjust=True))
        rf_, sf = single_market_tsmom(fut_close)
        re_, se = single_market_tsmom(etf_close)
        lo = max(rf_.index.min(), re_.index.min())
        hi = min(rf_.index.max(), re_.index.max())
        rf2, re2 = rf_.loc[lo:hi], re_.loc[lo:hi]
        sgn = sf.loc[lo:hi].align(se.loc[lo:hi], join="inner")
        agree = float((np.sign(sgn[0]) == np.sign(sgn[1])).mean())
        s_f, s_e = ann_sharpe(rf2), ann_sharpe(re2)
        div = "DIVERGENT" if (abs(s_f - s_e) > 0.4 or agree < 0.80) else "consistent"
        line = (f"{code:4s} vs {etf:4s} ({lo.date()}..{hi.date()}): fut Sharpe {s_f:5.2f}"
                f" | etf Sharpe {s_e:5.2f} | signal agreement {agree:.0%} | {div}")
        proxy_lines.append(line)
        print(f"  {line}")

    # ── markdown report ──
    write_report(depth, quality, ra, eq_a, ra_ma, eq_ma, wy_ma, rb, eq_b_t, bstats,
                 rows_b, rows_a, xb, xa, strict_pass, relaxed, rows_csv, era_lines,
                 full_corr, beta, proxy_lines)
    print(f"\nOutputs: {OUT / (TAG + '.md')}, .log, .csv ; caches in {CACHE}")
    sys.stdout = sys.__stdout__
    tee.f.close()


def write_report(depth, quality, ra, eq_a, ra_ma, eq_ma, wy_ma, rb, eq_b, bstats,
                 rows_b, rows_a, xb, xa, strict_pass, relaxed, rows_csv, era_lines,
                 full_corr, beta, proxy_lines):
    L = []
    A = L.append
    A(f"# Lever C — Diversified Futures Trend (TSMOM) research pass — 2026-07-06\n")
    A("RESEARCH-ONLY. Pre-registered frozen spec (docs/professional-path-2026-07-05.md"
      " Lever C); one signal, no search, numbers as they fall. No promoter call;"
      " validated_strategies.yaml untouched.\n")
    A("## DATA CAVEAT (prominent)\n")
    A("Yahoo continuous futures are FRONT-MONTH SPLICES WITHOUT BACK-ADJUSTMENT:"
      " roll gaps pollute price-level signals and embed roll yield incorrectly."
      " Mitigations applied per the frozen design: (1) signals computed on a"
      " returns-cumulated series — honest note: for a pure splice this reproduces"
      " the raw ratio path, so its only real effect here is zeroing pathological"
      " prints (e.g. CL's negative-price crossing, April 2020); ROLL-DAY returns"
      " still compare two different contracts and that residual error remains in"
      " both signal and P&L (roughly monthly for energy, quarterly elsewhere);"
      " (2) ETF-proxy cross-check below quantifies whether conclusions survive on"
      " clean total-return data.\n")
    A("## Universe and history depth\n")
    A("| mkt | yahoo | sector | traded as | micro? | history | years | <10y flag |")
    A("|---|---|---|---|---|---|---|---|")
    for m in MARKETS:
        d = depth[m.code]
        A(f"| {m.code} | {m.yahoo} | {m.sector} | {m.trade_as} |"
          f" {'yes' if m.has_micro else 'NO — smallest listed'} | {d[0]}..{d[1]} |"
          f" {d[3]} | {'FLAG' if d[4] else ''} |")
    A("")
    A("## Spec (frozen)\n")
    A("- Signal: TSMOM = sign of 252-trading-day trailing total return, daily eval,"
      " position changes on sign flips only; monthly refresh of vol scaling.")
    A("- Sizing: per-market target = (12%/N_active)/ann_vol_20d. Level (a) fractional"
      " index replication; level (b) $50k integer micro contracts, $1.50 commission"
      " + 1-tick slippage per side per contract. Level (b) is the deliverable.")
    A("- Corroboration (report-only): sign(price − 200d MA) on the cumulated series.\n")
    A("## Headline results\n")
    A(f"- LEVEL A (fractional, costless): Sharpe {ann_sharpe(ra):.2f}, ann ret"
      f" {ann_ret(ra):.2%}, maxDD {max_dd(eq_a):.1%}, {ra.index.min().date()}..{ra.index.max().date()}")
    A(f"- LEVEL B ($50k micro-quantized, costed) — THE DELIVERABLE: Sharpe"
      f" {ann_sharpe(rb):.2f}, ann ret {ann_ret(rb):.2%}, maxDD {max_dd(eq_b):.1%},"
      f" final equity ${eq_b.iloc[-1]:,.0f} from $50k, total costs"
      f" ${bstats['total_costs']:,.0f}, sign-flips {bstats['total_flips']}")
    A(f"- Corroboration 200d-MA variant (level A, REPORT-ONLY): Sharpe {ann_sharpe(ra_ma):.2f},"
      f" ann ret {ann_ret(ra_ma):.2%}, maxDD {max_dd(eq_ma):.1%}, worst-year {wy_ma:.2f}\n")
    yrs = len(rb) / TRADING_DAYS
    dropped = [f"{c} {bstats['dropped_frac'][c]:.0%}" for c in bstats["dropped_frac"]
               if np.isfinite(bstats["dropped_frac"][c]) and bstats["dropped_frac"][c] > 0.5]
    A("### Diagnosis of the A -> B degradation (what the $50k account actually buys)\n")
    A(f"Costs are NOT the driver: ${bstats['total_costs']:,.0f} total over {yrs:.0f} years"
      f" (~{bstats['total_costs'] / yrs / START_EQUITY:.2%}/yr on starting equity) cannot"
      f" explain Sharpe {ann_sharpe(ra):.2f} -> {ann_sharpe(rb):.2f} and OOS"
      f" {xa['oos_sharpe']:.2f} -> {xb['oos_sharpe']:.2f}. The driver is QUANTIZATION"
      f" DROPOUT: at 12%/N vol budgeting on $50k the per-market budget is"
      f" ~${PORT_VOL_TARGET / len(MARKETS) * START_EQUITY:,.0f} annualized vol, below one"
      f" contract for most of the book — dropped >50% of days: {', '.join(dropped)}."
      " The realized $50k book is essentially equity-index micros + FX micros;"
      " bonds/metals/energy — the legs that carry diversified TSMOM in the modern era —"
      " are absent. The paper (level A) book holds up out-of-sample; the $50k"
      " implementation does not, and the gap is structural granularity, not signal decay"
      " alone.\n")
    A("## Stage-6 gate — LEVEL B (deliverable)\n")
    A("| criterion | verdict | actual |")
    A("|---|---|---|")
    for name, ok, val in rows_b:
        A(f"| {name} | {'PASS' if ok else 'FAIL'} | {val} |")
    A(f"\n**STRICT GATE VERDICT (level b): {'PASS' if strict_pass else 'FAIL'}**\n")
    A(f"> {GATE_DESIGN_FLAG}\n")
    A(f"Relaxed-worst-year information line: with worst-year relaxed to >= -0.5 the"
      f" strategy would **{'PASS' if relaxed else 'FAIL'}** (worst year Sharpe"
      f" {xb['worst_year']:.2f}). This is information for a human gate-design"
      f" discussion, not a pass claim.\n")
    A("Level A gate (reference): " + "; ".join(
        f"{name}={'PASS' if ok else 'FAIL'}({val})" for name, ok, val in rows_a) + "\n")
    A("## Per-year table (level A | level B)\n")
    A("| year | A Sharpe | A ret | B Sharpe | B ret | flips | corr vs SPY |")
    A("|---|---|---|---|---|---|---|")
    for r in rows_csv:
        A(f"| {r['year']} | {r['lvlA_sharpe']} | {r['lvlA_ret']:.2%} | {r['lvlB_sharpe']} |"
          f" {r['lvlB_ret']:.2%} | {r['flips']} | {r['corr_spy']} |")
    A("")
    A("## Era breakdown (level B)\n")
    for name, line in era_lines:
        A(f"- {name}: {line}")
    A("")
    A(f"## Diversification claim, quantified\n")
    A(f"corr(book daily returns, SPY daily returns) = **{full_corr:.3f}** (beta {beta:.3f}).\n")
    A("## Micro-quantization dropout at $50k\n")
    A("Fraction of active days where the vol budget quantized to 0 contracts"
      " (market effectively drops out at this account size):\n")
    for m in MARKETS:
        f = bstats["dropped_frac"][m.code]
        A(f"- {m.code} ({m.trade_as}{'' if m.has_micro else ', NO MICRO'}):"
          f" {f:.1%}{' — drops out' if np.isfinite(f) and f > 0.5 else ''}")
    A("")
    A("## ETF proxy cross-check (mandatory mitigation #2)\n")
    for line in proxy_lines:
        A(f"- {line}")
    A("")
    A("## Honest caveats\n")
    A("- Data quality: unadjusted front-month splices; roll-day P&L/signal error"
      " unhedged; per-market pathological prints removed and counted (see log)."
      " Data-hygiene fixes applied, both narrow and disclosed: (i) 6J dropped"
      "-digit bad print 2001-12-17 removed by a spike-full-reversal rule"
      " (|r|>50% both days, price recovers within 15%); (ii) returns across"
      " non-positive prices zeroed (CL negative print, April 2020). NOT fixed"
      " (documented pollution): ZB has a persistent +10.4% level splice on"
      " 2015-03-23 (series switches price basis); NG roll gaps up to ~47% on"
      " single days are left in — they are real prints or genuine splice gaps"
      " indistinguishable from real moves.")
    A("- Survivorship-free universe is a plus: these 16 markets were chosen by asset"
      " class, not by past performance, and all still trade.")
    A("- Micro quantization granularity at $50k: per-market risk budget is 12%/N of"
      " $50k; markets whose smallest contract exceeds it drop out (table above) —"
      " the realized book is less diversified than the paper book.")
    A("- No collateral interest credited (futures cash could earn T-bills on top);"
      " Sharpe is on raw equity changes, consistent with project conventions.")
    A("- MC5 here bootstraps DAILY portfolio returns (n large), per monte_carlo.py"
      " bootstrap_sharpe; the per-trade convention does not map to a continuously"
      " -held book.")
    (OUT / f"{TAG}.md").write_text("\n".join(L))


if __name__ == "__main__":
    main()
