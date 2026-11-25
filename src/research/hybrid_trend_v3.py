"""HYBRID TREND BOOK v3 — pre-registered COST-MODEL CORRECTION to v2.

NOT an iteration. v2's own report flagged that its flat 5%/yr financing
assumption overstates financing in the zero-rate eras (2009-15, 2020-21) and
may understate it recently. v3 replaces that admitted mismeasurement with
historical data. Frozen 2026-07-06 BEFORE results; one run, no variants.

THE ONLY CHANGE (frozen): financing charged daily at the HISTORICAL effective
federal funds rate + 1.50% spread, on the SAME base as v2:
max(0, gross ETF notional − equity). Rate source: FRED daily effective federal
funds rate (DFF), public CSV
(https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF), cached to
data/futures_daily/DFF.parquet; forward-filled onto the trading calendar
(DFF is published for all calendar days; ffill covers any gaps). Accrual
convention identical to v2: annualized rate / 252 per trading day.

EVERYTHING ELSE IDENTICAL TO V2: same 15 legs, TSMOM-252d sign, 12% vol
target with equal-risk (12%/N_active)/ann_vol_20d budgeting, same costs incl.
0.3%/yr short borrow, 1.5x gross-ETF-notional/equity cap with pro-rata
scale-down (integer floor), same window, same gates.

VERDICT (frozen before results):
  (a) IMPLEMENTABILITY LINE — thresholds UNCHANGED from v2's pre-registration:
      Sharpe >= 0.5 AND OOS Sharpe >= 0.4 AND maxDD <= 20%, evaluated on V3;
  (b) strict Stage-6 gate PASS/FAIL + standing GATE-DESIGN FLAG + relaxed
      worst-year info line.
No promoter call; validated_strategies.yaml untouched.

Run:  .venv/bin/python -m src.research.hybrid_trend_v3
Outputs: logs/research/hybrid_trend_v3_20260706.{md,log,csv}
Strict ownership: creates only this file, the three outputs, and the DFF rate
cache under data/futures_daily/. Reuses hybrid_trend_v2.py / hybrid_trend.py /
futures_trend.py machinery by import; modifies nothing existing.
"""

from __future__ import annotations

import sys
import urllib.request

import numpy as np
import pandas as pd

from src.research.futures_trend import (
    CACHE, ERAS, GATE_DESIGN_FLAG, OUT, PORT_VOL_TARGET, START_EQUITY,
    TRADING_DAYS, Tee, ann_ret, ann_sharpe, build_panel, era_slice, fetch,
    gate_rows, level_a, level_b, max_dd, refresh_days,
)
from src.research.hybrid_trend import (
    BORROW_RATE, LEGS, MIN_ETF_YEARS, build_hybrid_panel, level_b_hybrid,
)
from src.research.hybrid_trend_v2 import (
    CAP_MULT, FIN_RATE, FUT_MARGIN, level_b_hybrid_v2,
)
from src.research.monte_carlo import per_year_sharpe

TAG = "hybrid_trend_v3_20260706"

FIN_SPREAD = 0.015    # +1.50%/yr broker spread over effective fed funds (frozen)
DFF_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF"
DFF_CACHE = CACHE / "DFF.parquet"

# ZIRP eras named by v2's own caveat as the mismeasured periods
ZIRP_ERAS = {"ZIRP 2009-2015": (2009, 2015), "ZIRP 2020-2021": (2020, 2021)}


def fetch_dff() -> pd.Series:
    """FRED daily effective federal funds rate (DFF), % per annum, cached to
    data/futures_daily/DFF.parquet. DFF is published for every calendar day
    (weekends carry the Friday effective rate); any residual gaps are
    forward-filled at use time."""
    CACHE.mkdir(parents=True, exist_ok=True)
    if DFF_CACHE.exists():
        s = pd.read_parquet(DFF_CACHE)["dff"]
    else:
        with urllib.request.urlopen(DFF_URL, timeout=60) as resp:
            raw = pd.read_csv(resp)
        raw.columns = [c.strip().lower() for c in raw.columns]
        date_col = "observation_date" if "observation_date" in raw.columns else "date"
        s = pd.Series(pd.to_numeric(raw["dff"], errors="coerce").values,
                      index=pd.DatetimeIndex(pd.to_datetime(raw[date_col])),
                      name="dff").dropna().sort_index()
        s = s[~s.index.duplicated(keep="last")]
        s.to_frame().to_parquet(DFF_CACHE)
    s.index = pd.DatetimeIndex(s.index)
    return s.rename("dff")


def fin_rate_on(idx: pd.DatetimeIndex, dff: pd.Series) -> np.ndarray:
    """Annualized financing rate (decimal) per trading day: DFF/100 + spread,
    forward-filled onto the trading calendar (covers non-business days and any
    trailing gap at the end of the FRED series)."""
    r = dff.reindex(dff.index.union(idx)).ffill().reindex(idx)
    if r.isna().any():
        first = r.first_valid_index()
        raise RuntimeError(f"DFF does not cover window start; first valid {first}")
    return (r.values / 100.0) + FIN_SPREAD


# ── v3 book: identical to level_b_hybrid_v2 except the financing rate ───────

def level_b_hybrid_v3(idx, C, sig, vol, legs, fin_rate):
    """Byte-for-byte the v2 loop (1.5x cap, pro-rata scale-down, integer
    floor, financing on max(0, gross ETF notional − equity)) with ONE change:
    the flat 5%/yr financing rate is replaced by fin_rate[t] = historical
    DFF + 1.50% spread, annualized, accrued at rate/252 per trading day (same
    convention as v2). Also tracks per-year financing under the floating rate
    AND the flat-5% counterfactual accrued on the SAME base/path, so the
    rate-level effect is isolated from path differences vs the v2 run."""
    cols = [lg.code for lg in legs]
    S, V, P = sig[cols].values, vol[cols].values, C[cols].values
    monthly = refresh_days(idx)
    mult = np.array([lg.mult for lg in legs])
    cost_side = np.array([lg.cost_side for lg in legs])
    is_etf = np.array([lg.kind == "etf" for lg in legs])
    fut_margin = np.array([FUT_MARGIN.get(lg.trade_as, 0.0) for lg in legs])

    n = np.zeros(len(cols))
    equity = np.full(len(idx), np.nan)
    equity[0] = START_EQUITY
    flips = np.zeros(len(cols), int)
    dropped_days = np.zeros(len(cols), int)
    active_days = np.zeros(len(cols), int)
    first_active = [None] * len(cols)
    total_costs = fut_costs = etf_costs = total_borrow = total_financing = 0.0
    total_fin_flat5 = 0.0            # counterfactual: 5% flat on the SAME base
    fin_days = 0
    cap_checks = cap_binds = 0
    scale_sum = 0.0
    min_scale = 1.0
    yearly_capbind: dict[int, list[int]] = {}
    yearly_flips: dict[int, int] = {}
    yearly_fin: dict[int, float] = {}         # floating financing $ by year
    yearly_fin_flat5: dict[int, float] = {}   # flat-5% counterfactual $ by year
    yearly_rate_sum: dict[int, float] = {}    # for avg annualized rate by year
    yearly_rate_n: dict[int, int] = {}
    max_etf_gross = 0.0
    etf_gross_sum = 0.0
    etf_gross_n = 0
    max_etf_lev = 0.0
    max_etf_lev_date = None
    leg_notional_sum = np.zeros(len(cols))
    leg_notional_max = np.zeros(len(cols))
    margin_frac_sum = 0.0
    margin_frac_n = 0
    max_margin_frac = 0.0

    for t in range(1, len(idx)):
        eq = equity[t - 1]
        p_prev = P[t - 1]
        valid = (np.isfinite(S[t - 1]) & np.isfinite(V[t - 1]) & (V[t - 1] > 1e-4)
                 & np.isfinite(p_prev) & (p_prev > 0))
        n_active = int(valid.sum())
        new_n = n.copy()
        sized_today = np.zeros(len(cols), bool)
        if n_active > 0 and eq > 0:
            budget = (PORT_VOL_TARGET / n_active) * eq
            for i in range(len(cols)):
                if not valid[i]:
                    if n[i] != 0:
                        new_n[i] = 0.0
                    continue
                s = S[t - 1][i]
                flip = (np.sign(n[i]) != s)
                if monthly[t] or flip:
                    notional = budget / V[t - 1][i]
                    k = round(notional / (mult[i] * p_prev[i]))
                    new_n[i] = k * s
                    if flip and np.sign(n[i]) != np.sign(new_n[i]):
                        flips[i] += 1
                        y = int(idx[t].year)
                        yearly_flips[y] = yearly_flips.get(y, 0) + 1
                sized_today[i] = True

        px = np.where(np.isfinite(p_prev), p_prev, 0.0)

        # leverage cap at every rebalance (identical to v2)
        rebal = bool(monthly[t]) or bool(np.any(new_n != n))
        if rebal and eq > 0:
            cap_checks += 1
            y = int(idx[t].year)
            yc = yearly_capbind.setdefault(y, [0, 0])
            yc[1] += 1
            gross_target = float(np.where(is_etf, np.abs(new_n) * px, 0.0).sum())
            cap = CAP_MULT * eq
            if gross_target > cap:
                f = cap / gross_target
                cap_binds += 1
                yc[0] += 1
                scale_sum += f
                min_scale = min(min_scale, f)
                new_n = np.where(
                    is_etf, np.sign(new_n) * np.floor(np.abs(new_n) * f), new_n)

        for i in range(len(cols)):
            if sized_today[i]:
                active_days[i] += 1
                if first_active[i] is None:
                    first_active[i] = idx[t]
                if new_n[i] == 0:
                    dropped_days[i] += 1

        dn = np.abs(new_n - n)
        c_vec = dn * cost_side
        costs = float(c_vec.sum())
        fut_costs += float(c_vec[~is_etf].sum())
        etf_costs += float(c_vec[is_etf].sum())
        total_costs += costs
        short_notional = float(np.where(is_etf & (new_n < 0), -new_n * px, 0.0).sum())
        borrow = BORROW_RATE / TRADING_DAYS * short_notional
        total_borrow += borrow
        etf_abs = np.where(is_etf, np.abs(new_n) * px, 0.0)
        gross_etf = float(etf_abs.sum())
        if gross_etf > 0:
            max_etf_gross = max(max_etf_gross, gross_etf)
            etf_gross_sum += gross_etf
            etf_gross_n += 1
            leg_notional_sum += etf_abs
            leg_notional_max = np.maximum(leg_notional_max, etf_abs)
            if eq > 0 and gross_etf / eq > max_etf_lev:
                max_etf_lev = gross_etf / eq
                max_etf_lev_date = idx[t]
        marg = float(np.where(~is_etf, np.abs(new_n) * fut_margin, 0.0).sum())
        if eq > 0:
            mf = marg / eq
            margin_frac_sum += mf
            margin_frac_n += 1
            max_margin_frac = max(max_margin_frac, mf)

        # ── THE ONLY CHANGE vs v2: floating financing rate ──
        base = max(0.0, gross_etf - max(eq, 0.0))
        financing = fin_rate[t] / TRADING_DAYS * base
        fin_flat5 = FIN_RATE / TRADING_DAYS * base   # counterfactual, same base
        if financing > 0:
            fin_days += 1
        total_financing += financing
        total_fin_flat5 += fin_flat5
        y = int(idx[t].year)
        yearly_fin[y] = yearly_fin.get(y, 0.0) + financing
        yearly_fin_flat5[y] = yearly_fin_flat5.get(y, 0.0) + fin_flat5
        yearly_rate_sum[y] = yearly_rate_sum.get(y, 0.0) + float(fin_rate[t])
        yearly_rate_n[y] = yearly_rate_n.get(y, 0) + 1

        dp = P[t] - p_prev
        dp = np.where(np.isfinite(dp), dp, 0.0)
        pnl = float(np.nansum(new_n * mult * dp))
        equity[t] = eq + pnl - costs - borrow - financing
        n = new_n

    eq = pd.Series(equity, index=idx)
    stats = {
        "flips": {c: int(f) for c, f in zip(cols, flips)},
        "total_flips": int(flips.sum()),
        "yearly_flips": yearly_flips,
        "dropped_frac": {c: (dropped_days[i] / active_days[i] if active_days[i] else float("nan"))
                         for i, c in enumerate(cols)},
        "first_active": {c: (str(d.date()) if d is not None else "never")
                         for c, d in zip(cols, first_active)},
        "total_costs": total_costs, "fut_costs": fut_costs, "etf_costs": etf_costs,
        "total_borrow": total_borrow, "total_financing": total_financing,
        "total_fin_flat5": total_fin_flat5,
        "yearly_fin": yearly_fin, "yearly_fin_flat5": yearly_fin_flat5,
        "yearly_avg_rate": {y: yearly_rate_sum[y] / yearly_rate_n[y]
                            for y in yearly_rate_sum},
        "fin_days": fin_days,
        "cap_checks": cap_checks, "cap_binds": cap_binds,
        "avg_scale_when_binding": (scale_sum / cap_binds if cap_binds else float("nan")),
        "min_scale": (min_scale if cap_binds else float("nan")),
        "yearly_capbind": yearly_capbind,
        "max_etf_gross": max_etf_gross,
        "mean_etf_gross": (etf_gross_sum / etf_gross_n if etf_gross_n else 0.0),
        "max_etf_lev": max_etf_lev,
        "max_etf_lev_date": (str(max_etf_lev_date.date()) if max_etf_lev_date is not None else "n/a"),
        "leg_notional_share": {c: (leg_notional_sum[i] / leg_notional_sum.sum()
                                   if leg_notional_sum.sum() > 0 else float("nan"))
                               for i, c in enumerate(cols) if is_etf[i]},
        "leg_notional_max": {c: float(leg_notional_max[i])
                             for i, c in enumerate(cols) if is_etf[i]},
        "mean_margin_frac": (margin_frac_sum / margin_frac_n if margin_frac_n else 0.0),
        "max_margin_frac": max_margin_frac,
    }
    return eq, stats


# ── main ────────────────────────────────────────────────────────────────────

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tee = Tee(OUT / f"{TAG}.log")
    sys.stdout = tee
    print(f"=== HYBRID TREND BOOK v3 — cost-model correction to v2 ({TAG}) ===")
    print("Frozen design: identical to hybrid_trend_v2_20260706 (1.5x gross-ETF cap,")
    print("pro-rata scale-down, same legs/signal/sizing/costs/window) with the ONLY")
    print("change: financing = HISTORICAL FRED DFF + 1.50% spread instead of flat 5%.")
    print("This corrects a mismeasurement v2's own report flagged (ZIRP-era")
    print("overstatement). Numbers reported as they fall.\n")

    dff = fetch_dff()
    print(f"RATE SOURCE (prominent): FRED daily effective federal funds rate (DFF),"
          f"\n  {DFF_URL}"
          f"\n  cached at {DFF_CACHE}  ({dff.index.min().date()}..{dff.index.max().date()},"
          f" {len(dff)} obs); forward-filled onto the trading calendar."
          f"\n  Financing rate = DFF + {FIN_SPREAD:.2%}/yr spread, accrued at rate/252"
          f" per trading day on max(0, gross ETF notional - equity) — same base and"
          f" convention as v2; only the rate level changes."
          f"\n  Short borrow unchanged at {BORROW_RATE:.1%}/yr on short notional.")

    # ── reproduce prior books: A, B, hybrid uncapped, and V2 itself ──
    idx0, C0, R0, sig0, _sig_ma0, vol0, _q0, _d0 = build_panel()
    ra = level_a(idx0, R0, sig0, vol0)
    ra = ra[ra.index >= ra.loc[ra != 0].index.min()]
    eq_a = (1 + ra).cumprod() * START_EQUITY
    eq_b0, bstats0 = level_b(idx0, C0, sig0, vol0)
    fb = eq_b0.index[np.argmax(eq_b0.values != START_EQUITY)]
    rb = eq_b0.pct_change().loc[fb:].dropna()
    eq_b = eq_b0.loc[fb:]
    print(f"\nReproduced LEVEL A (fractional ideal):   Sharpe {ann_sharpe(ra):.2f}"
          f"  ann_ret {ann_ret(ra):.2%}  maxDD {max_dd(eq_a):.1%}")
    print(f"Reproduced LEVEL B (all-futures $50k):   Sharpe {ann_sharpe(rb):.2f}"
          f"  ann_ret {ann_ret(rb):.2%}  maxDD {max_dd(eq_b):.1%}"
          f"  final ${eq_b.iloc[-1]:,.0f}")

    legs = list(LEGS)
    cper = fetch("CPER", auto_adjust=True)
    cper_yrs = (cper.index.max() - cper.index.min()).days / 365.25
    if cper_yrs < MIN_ETF_YEARS:
        legs = [lg for lg in legs if lg.ticker != "CPER"]
        print(f"CPER history {cper_yrs:.1f}y < {MIN_ETF_YEARS:.0f}y — HG DROPPED.")
    else:
        print(f"CPER history {cper_yrs:.1f}y >= {MIN_ETF_YEARS:.0f}y — HG kept (as in v2).")
    print(f"Book: {len(legs)} legs ({sum(1 for lg in legs if lg.kind == 'fut')} futures,"
          f" {sum(1 for lg in legs if lg.kind == 'etf')} ETF). NG dropped (pre-registered, carried over).")

    idx, C, R, sig, vol, _quality, _depth = build_hybrid_panel(legs)

    eq_h0, hstats = level_b_hybrid(idx, C, sig, vol, legs)
    fh = eq_h0.index[np.argmax(eq_h0.values != START_EQUITY)]
    rh = eq_h0.pct_change().loc[fh:].dropna()
    eq_h = eq_h0.loc[fh:]
    print(f"Reproduced HYBRID uncapped $50k:         Sharpe {ann_sharpe(rh):.2f}"
          f"  ann_ret {ann_ret(rh):.2%}  maxDD {max_dd(eq_h):.1%}"
          f"  final ${eq_h.iloc[-1]:,.0f}")

    eq_v20, v2stats = level_b_hybrid_v2(idx, C, sig, vol, legs)
    fv2 = eq_v20.index[np.argmax(eq_v20.values != START_EQUITY)]
    rv2 = eq_v20.pct_change().loc[fv2:].dropna()
    eq_v2 = eq_v20.loc[fv2:]
    print(f"Reproduced HYBRID V2 (cap + flat 5%):    Sharpe {ann_sharpe(rv2):.2f}"
          f"  ann_ret {ann_ret(rv2):.2%}  maxDD {max_dd(eq_v2):.1%}"
          f"  final ${eq_v2.iloc[-1]:,.0f}  financing ${v2stats['total_financing']:,.0f}")

    # ── THE DELIVERABLE: v3 = v2 with floating DFF+1.5% financing ──
    fin_rate = fin_rate_on(idx, dff)
    eq_v0, vstats = level_b_hybrid_v3(idx, C, sig, vol, legs, fin_rate)
    fv = eq_v0.index[np.argmax(eq_v0.values != START_EQUITY)]
    rv = eq_v0.pct_change().loc[fv:].dropna()
    eq_v = eq_v0.loc[fv:]
    n_days = len(rv)
    w = fin_rate[np.searchsorted(idx, rv.index[0]):]
    print(f"\nHYBRID v3 (1.5x cap + DFF+{FIN_SPREAD:.2%} financing) — THE DELIVERABLE:"
          f" {rv.index.min().date()}..{rv.index.max().date()}")
    print(f"  Sharpe {ann_sharpe(rv):.2f}  ann_ret {ann_ret(rv):.2%}"
          f"  maxDD {max_dd(eq_v):.1%}  final equity ${eq_v.iloc[-1]:,.0f}")
    all_in = vstats["total_costs"] + vstats["total_borrow"] + vstats["total_financing"]
    print(f"  costs ${vstats['total_costs']:,.0f} (futures ${vstats['fut_costs']:,.0f}"
          f" + ETF half-spread ${vstats['etf_costs']:,.0f})"
          f"  short borrow ${vstats['total_borrow']:,.0f}"
          f"  FINANCING ${vstats['total_financing']:,.0f}"
          f" (flat-5% counterfactual on same base: ${vstats['total_fin_flat5']:,.0f})"
          f"  all-in ${all_in:,.0f}  sign-flips {vstats['total_flips']}")
    print(f"  financing rate over window: mean {np.mean(w):.2%}, min {np.min(w):.2%},"
          f" max {np.max(w):.2%} (DFF + {FIN_SPREAD:.2%})")

    # ── annual financing by era (the point of the study) ──
    print("\nFINANCING BY ERA — floating (DFF+1.5%) vs flat-5% counterfactual on the SAME base:")
    era_fin_rows = []
    all_eras = dict(ZIRP_ERAS)
    all_eras.update({k: v for k, v in ERAS.items()})
    for name, (y0, y1) in all_eras.items():
        yrs = [y for y in vstats["yearly_fin"] if y0 <= y <= y1]
        if not yrs:
            continue
        f3 = sum(vstats["yearly_fin"][y] for y in yrs)
        f5 = sum(vstats["yearly_fin_flat5"][y] for y in yrs)
        rt = np.mean([vstats["yearly_avg_rate"][y] for y in yrs])
        era_fin_rows.append((name, y0, y1, f3, f5, f3 - f5, rt))
        print(f"  {name:22s} floating ${f3:9,.0f} | flat-5% ${f5:9,.0f} |"
              f" delta ${f3 - f5:+9,.0f} | avg rate {rt:.2%}")
    zirp_delta = sum(r[5] for r in era_fin_rows if r[0].startswith("ZIRP"))
    print(f"  ZIRP eras combined (2009-2015 + 2020-2021): floating charges"
          f" ${zirp_delta:+,.0f} vs flat-5% — the mismeasurement v2 flagged, now measured.")

    # ── cap-binding diagnostics ──
    bind_frac_rebal = vstats["cap_binds"] / vstats["cap_checks"] if vstats["cap_checks"] else float("nan")
    bind_frac_all = vstats["cap_binds"] / n_days
    real_vol_v = float(rv.std() * np.sqrt(TRADING_DAYS))
    real_vol_v2 = float(rv2.std() * np.sqrt(TRADING_DAYS))
    print("\nCAP-BINDING DIAGNOSTICS (v3):")
    print(f"  rebalance days (cap checked): {vstats['cap_checks']}  of {n_days} trading days")
    print(f"  cap binds: {vstats['cap_binds']} days = {bind_frac_rebal:.1%} of rebalance days"
          f" ({bind_frac_all:.1%} of all days)")
    print(f"  avg scale-down when binding: {vstats['avg_scale_when_binding']:.2f}"
          f"  (min {vstats['min_scale']:.2f})")
    print(f"  gross ETF notional: max ${vstats['max_etf_gross']:,.0f},"
          f" mean ${vstats['mean_etf_gross']:,.0f};"
          f" max gross/equity = {vstats['max_etf_lev']:.2f}x on {vstats['max_etf_lev_date']}")
    print(f"  financing accrued on {vstats['fin_days']} days"
          f" ({vstats['fin_days'] / n_days:.1%} of days), total ${vstats['total_financing']:,.0f}")
    print(f"  realized portfolio vol: v3 {real_vol_v:.2%} vs v2 {real_vol_v2:.2%}"
          f" vs {PORT_VOL_TARGET:.0%} target")
    print(f"  futures margin usage (approx static micro margins): mean"
          f" {vstats['mean_margin_frac']:.1%} of equity, max {vstats['max_margin_frac']:.1%}")

    print("\nPer-leg dropout (fraction of active days sized to 0 units, post-cap):")
    for lg in legs:
        f = vstats["dropped_frac"][lg.code]
        d = " <- effectively drops out" if np.isfinite(f) and f > 0.5 else ""
        print(f"  {lg.code:4s} ({lg.trade_as:10s}) [{lg.kind}]"
              f" first_active={vstats['first_active'][lg.code]}: {f:.1%}{d}")

    # ── gates (all five books) ──
    rows_v, xv = gate_rows(rv, eq_v, vstats["total_flips"])
    rows_v2, xv2 = gate_rows(rv2, eq_v2, v2stats["total_flips"])
    rows_h, xh = gate_rows(rh, eq_h, hstats["total_flips"])
    rows_b, xb = gate_rows(rb, eq_b, bstats0["total_flips"])
    rows_a, xa = gate_rows(ra, eq_a, bstats0["total_flips"])
    print("\nSTAGE-6 GATE — HYBRID v3 (deliverable):")
    for name, ok, val in rows_v:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    strict_pass = all(ok for _, ok, _ in rows_v)
    relaxed = all(ok for (nm, ok, _) in rows_v if not nm.startswith("Worst-year")) \
        and np.isfinite(xv["worst_year"]) and xv["worst_year"] >= -0.5
    print(f"  STRICT VERDICT: {'PASS' if strict_pass else 'FAIL'}")
    print(f"  Relaxed worst-year (>= -0.5) information line: would_{'PASS' if relaxed else 'FAIL'}")

    # ── IMPLEMENTABILITY LINE — thresholds UNCHANGED from v2 pre-registration ──
    impl_rows = [
        ("Sharpe >= 0.5", np.isfinite(xv["full_sharpe"]) and xv["full_sharpe"] >= 0.5,
         f"{xv['full_sharpe']:.2f}"),
        ("OOS Sharpe >= 0.4", np.isfinite(xv["oos_sharpe"]) and xv["oos_sharpe"] >= 0.4,
         f"{xv['oos_sharpe']:.2f}"),
        ("maxDD <= 20%", xv["max_dd"] >= -0.20, f"{xv['max_dd']:.1%}"),
    ]
    impl_pass = all(ok for _, ok, _ in impl_rows)
    print("\nIMPLEMENTABILITY LINE (thresholds UNCHANGED from v2's pre-registration,"
          " NOT the promotion gate):")
    for name, ok, val in impl_rows:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    print(f"  IMPLEMENTABILITY VERDICT: {'YES' if impl_pass else 'NO'} — the diversifier"
          f" {'IS' if impl_pass else 'is NOT'} implementable within real margin at $50k"
          f" under the 1.5x cap + historical DFF+{FIN_SPREAD:.2%} financing.")

    # ── five-way comparison ──
    comp = [
        ("Sharpe (full)", f"{xa['full_sharpe']:.2f}", f"{xb['full_sharpe']:.2f}",
         f"{xh['full_sharpe']:.2f}", f"{xv2['full_sharpe']:.2f}", f"{xv['full_sharpe']:.2f}"),
        ("Ann. return", f"{ann_ret(ra):.2%}", f"{ann_ret(rb):.2%}",
         f"{ann_ret(rh):.2%}", f"{ann_ret(rv2):.2%}", f"{ann_ret(rv):.2%}"),
        ("Max drawdown", f"{max_dd(eq_a):.1%}", f"{max_dd(eq_b):.1%}",
         f"{max_dd(eq_h):.1%}", f"{max_dd(eq_v2):.1%}", f"{max_dd(eq_v):.1%}"),
        ("IS Sharpe (first 70%)", f"{xa['is_sharpe']:.2f}", f"{xb['is_sharpe']:.2f}",
         f"{xh['is_sharpe']:.2f}", f"{xv2['is_sharpe']:.2f}", f"{xv['is_sharpe']:.2f}"),
        ("OOS Sharpe (last 30%)", f"{xa['oos_sharpe']:.2f}", f"{xb['oos_sharpe']:.2f}",
         f"{xh['oos_sharpe']:.2f}", f"{xv2['oos_sharpe']:.2f}", f"{xv['oos_sharpe']:.2f}"),
        ("MC 5th-pct Sharpe", f"{xa['mc5']:.2f}", f"{xb['mc5']:.2f}",
         f"{xh['mc5']:.2f}", f"{xv2['mc5']:.2f}", f"{xv['mc5']:.2f}"),
        ("Worst-year Sharpe", f"{xa['worst_year']:.2f}", f"{xb['worst_year']:.2f}",
         f"{xh['worst_year']:.2f}", f"{xv2['worst_year']:.2f}", f"{xv['worst_year']:.2f}"),
        ("Final equity ($50k start)", "n/a (fractional)", f"${eq_b.iloc[-1]:,.0f}",
         f"${eq_h.iloc[-1]:,.0f}", f"${eq_v2.iloc[-1]:,.0f}", f"${eq_v.iloc[-1]:,.0f}"),
        ("Financing modeled", "none", "none", "none",
         f"flat 5%/yr (${v2stats['total_financing']:,.0f})",
         f"DFF+1.5% (${vstats['total_financing']:,.0f})"),
        ("Total costs (all-in)", "$0 (costless)", f"${bstats0['total_costs']:,.0f}",
         f"${hstats['total_costs'] + hstats['total_borrow']:,.0f}",
         f"${v2stats['total_costs'] + v2stats['total_borrow'] + v2stats['total_financing']:,.0f}",
         f"${all_in:,.0f}"),
        ("Max gross ETF / equity", "n/a", "n/a", f"{hstats['max_etf_lev']:.1f}x",
         f"{v2stats['max_etf_lev']:.2f}x", f"{vstats['max_etf_lev']:.2f}x (cap {CAP_MULT}x)"),
        ("Window", f"{ra.index.min().date()}..{ra.index.max().date()}",
         f"{rb.index.min().date()}..{rb.index.max().date()}",
         f"{rh.index.min().date()}..{rh.index.max().date()}",
         f"{rv2.index.min().date()}..{rv2.index.max().date()}",
         f"{rv.index.min().date()}..{rv.index.max().date()}"),
    ]
    print("\nFIVE-WAY COMPARISON — A (ideal) | B (all-fut $50k) | H (uncapped) |"
          " V2 (flat-5%) | V3 (floating):")
    for row in comp:
        print(f"  {row[0]:26s} A {row[1]:>16s} | B {row[2]:>14s} | H {row[3]:>14s}"
              f" | V2 {row[4]:>22s} | V3 {row[5]:>26s}")

    # ── per-year + era + SPY correlation ──
    spy = fetch("SPY", auto_adjust=True)
    spy_r = spy.pct_change().dropna()
    by_a, by_b = per_year_sharpe(ra), per_year_sharpe(rb)
    by_h, by_v2, by_v = per_year_sharpe(rh), per_year_sharpe(rv2), per_year_sharpe(rv)
    years = sorted(set(by_a) | set(by_b) | set(by_h) | set(by_v2) | set(by_v))
    rows_csv = []
    print("\nPER-YEAR (Sharpe/ann-ret: A | B | H | V2 | V3; v3 flips; cap-bind;"
          " corr SPY; financing floating vs flat-5; avg rate):")
    for y in years:
        a_y, b_y = ra[ra.index.year == y], rb[rb.index.year == y]
        h_y, v2_y, v_y = rh[rh.index.year == y], rv2[rv2.index.year == y], rv[rv.index.year == y]
        s = spy_r[spy_r.index.year == y]
        j = v_y.align(s, join="inner")
        corr = float(j[0].corr(j[1])) if len(j[0]) > 30 else float("nan")
        fl = vstats["yearly_flips"].get(y, 0)
        cb = vstats["yearly_capbind"].get(y, [0, 0])
        cb_frac = cb[0] / cb[1] if cb[1] else float("nan")
        fin3 = vstats["yearly_fin"].get(y, 0.0)
        fin5 = vstats["yearly_fin_flat5"].get(y, 0.0)
        rt = vstats["yearly_avg_rate"].get(y, float("nan"))
        rows_csv.append({
            "year": y,
            "lvlA_sharpe": round(by_a.get(y, float("nan")), 2), "lvlA_ret": round(ann_ret(a_y), 4),
            "lvlB_sharpe": round(by_b.get(y, float("nan")), 2), "lvlB_ret": round(ann_ret(b_y), 4),
            "hybrid_sharpe": round(by_h.get(y, float("nan")), 2), "hybrid_ret": round(ann_ret(h_y), 4),
            "v2_sharpe": round(by_v2.get(y, float("nan")), 2), "v2_ret": round(ann_ret(v2_y), 4),
            "v3_sharpe": round(by_v.get(y, float("nan")), 2), "v3_ret": round(ann_ret(v_y), 4),
            "v3_flips": fl, "v3_capbind_frac": round(cb_frac, 3),
            "v3_corr_spy": round(corr, 2),
            "v3_financing_usd": round(fin3, 0), "flat5_financing_usd": round(fin5, 0),
            "avg_fin_rate": round(rt, 4)})
        print(f"  {y}: A {by_a.get(y, float('nan')):6.2f} | B {by_b.get(y, float('nan')):6.2f}"
              f" | H {by_h.get(y, float('nan')):6.2f} | V2 {by_v2.get(y, float('nan')):6.2f}"
              f" | V3 {by_v.get(y, float('nan')):6.2f}/{ann_ret(v_y):8.2%}"
              f" | flips {fl:3d} | bind {cb_frac:5.1%} | corrSPY {corr:5.2f}"
              f" | fin ${fin3:7,.0f} vs ${fin5:7,.0f} | rate {rt:6.2%}")
    pd.DataFrame(rows_csv).to_csv(OUT / f"{TAG}.csv", index=False)

    print("\nERA BREAKDOWN (v3):")
    era_lines = []
    for name, (y0, y1) in ERAS.items():
        e = era_slice(rv, y0, y1)
        if len(e) < 60:
            era_lines.append((name, "insufficient data"))
            continue
        eqe = (1 + e).cumprod()
        line = f"Sharpe {ann_sharpe(e):5.2f}  ann_ret {ann_ret(e):7.2%}  maxDD {max_dd(eqe):6.1%}"
        era_lines.append((name, line))
        print(f"  {name:22s} {line}")

    j = rv.align(spy_r, join="inner")
    full_corr = float(j[0].corr(j[1]))
    beta = float(np.cov(j[0], j[1])[0, 1] / np.var(j[1]))
    print(f"\nDIVERSIFICATION: corr(v3 daily, SPY daily) = {full_corr:.3f},"
          f" beta = {beta:.3f} over {len(j[0])} overlapping days")

    write_report(legs, dff, w, ra, eq_a, rb, eq_b, bstats0, rh, eq_h, hstats,
                 rv2, eq_v2, v2stats, rv, eq_v, vstats,
                 rows_v, rows_v2, rows_h, rows_b, rows_a, xv, strict_pass, relaxed,
                 impl_rows, impl_pass, comp, rows_csv, era_lines, era_fin_rows,
                 zirp_delta, full_corr, beta, bind_frac_rebal, bind_frac_all,
                 real_vol_v, real_vol_v2, all_in, n_days)
    print(f"\nOutputs: {OUT / (TAG + '.md')}, .log, .csv ; DFF cache {DFF_CACHE}")
    sys.stdout = sys.__stdout__
    tee.f.close()


def write_report(legs, dff, w, ra, eq_a, rb, eq_b, bstats0, rh, eq_h, hstats,
                 rv2, eq_v2, v2stats, rv, eq_v, vstats,
                 rows_v, rows_v2, rows_h, rows_b, rows_a, xv, strict_pass, relaxed,
                 impl_rows, impl_pass, comp, rows_csv, era_lines, era_fin_rows,
                 zirp_delta, full_corr, beta, bind_frac_rebal, bind_frac_all,
                 real_vol_v, real_vol_v2, all_in, n_days):
    L = []
    A = L.append
    A("# HYBRID TREND BOOK v3 — cost-model correction to v2 (historical financing rates) — 2026-07-06\n")
    A("RESEARCH-ONLY. Pre-registered COST-MODEL CORRECTION, not an iteration:"
      " v2's own report flagged that its flat 5%/yr financing assumption"
      " overstates financing in the zero-rate eras (2009-15, 2020-21) and may"
      " understate it recently. v3 is IDENTICAL to hybrid_trend_v2_20260706"
      " (same 15 legs, TSMOM-252d sign, 12% vol target with equal-risk"
      " budgeting, same costs incl. 0.3%/yr short borrow, 1.5x"
      " gross-ETF-notional/equity cap with pro-rata scale-down, same window)"
      " with THE ONLY CHANGE: financing charged daily at the HISTORICAL"
      " effective federal funds rate + 1.50% spread on the same base,"
      " max(0, gross ETF notional − equity). One run, no variants, numbers as"
      " they fall. No promoter call; validated_strategies.yaml untouched.\n")
    A("## Rate source and assumptions (prominent)\n")
    A(f"- **FINANCING RATE: FRED daily effective federal funds rate (DFF) +"
      f" 1.50%/yr spread**, charged daily (rate/252 per trading day — same"
      f" accrual convention as v2) on max(0, gross ETF notional − equity), the"
      f" same base as v2. Only the rate level changes vs v2's flat 5%.")
    A(f"- Source: {DFF_URL} — cached to `data/futures_daily/DFF.parquet`"
      f" ({dff.index.min().date()}..{dff.index.max().date()}, {len(dff)}"
      f" daily observations); forward-filled onto the trading calendar for"
      f" non-business days and any trailing gap.")
    A(f"- Financing rate over the backtest window: mean {np.mean(w):.2%},"
      f" min {np.min(w):.2%}, max {np.max(w):.2%}.")
    A(f"- ETF short borrow unchanged: {BORROW_RATE:.1%}/yr on short notional.")
    A("- Futures margin diagnostic table unchanged from v2 (static approximate"
      " micro initial margins; diagnostic only, not used for sizing).\n")
    A("## Headline results\n")
    A(f"- LEVEL A (fractional ideal, reproduced): Sharpe {ann_sharpe(ra):.2f},"
      f" ann ret {ann_ret(ra):.2%}, maxDD {max_dd(eq_a):.1%}")
    A(f"- LEVEL B (all-futures $50k, reproduced): Sharpe {ann_sharpe(rb):.2f},"
      f" ann ret {ann_ret(rb):.2%}, maxDD {max_dd(eq_b):.1%}, final ${eq_b.iloc[-1]:,.0f}")
    A(f"- HYBRID uncapped (reproduced): Sharpe {ann_sharpe(rh):.2f}, ann ret"
      f" {ann_ret(rh):.2%}, maxDD {max_dd(eq_h):.1%}, final ${eq_h.iloc[-1]:,.0f}")
    A(f"- HYBRID V2 (cap + flat 5%, reproduced): Sharpe {ann_sharpe(rv2):.2f},"
      f" ann ret {ann_ret(rv2):.2%}, maxDD {max_dd(eq_v2):.1%}, final"
      f" ${eq_v2.iloc[-1]:,.0f}, financing ${v2stats['total_financing']:,.0f}")
    A(f"- **HYBRID V3 (cap + historical DFF+1.5% — THE DELIVERABLE): Sharpe"
      f" {ann_sharpe(rv):.2f}, ann ret {ann_ret(rv):.2%}, maxDD {max_dd(eq_v):.1%},"
      f" final equity ${eq_v.iloc[-1]:,.0f} from $50k**, all-in costs ${all_in:,.0f}"
      f" (trading ${vstats['total_costs']:,.0f} + short borrow"
      f" ${vstats['total_borrow']:,.0f} + financing ${vstats['total_financing']:,.0f};"
      f" flat-5% counterfactual on the same base would be"
      f" ${vstats['total_fin_flat5']:,.0f}), sign-flips {vstats['total_flips']},"
      f" window {rv.index.min().date()}..{rv.index.max().date()}\n")
    A("## MANDATORY five-way comparison\n")
    A("| metric | A: fractional ideal | B: all-futures $50k | H: hybrid uncapped |"
      " V2: cap + flat 5% | V3: cap + DFF+1.5% |")
    A("|---|---|---|---|---|---|")
    for row in comp:
        A(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} |")
    A("")
    A("## Annual financing cost by era — the point of this correction\n")
    A("Floating (DFF+1.5%) vs the flat-5% counterfactual accrued on the SAME"
      " base/path inside the v3 run (isolates the rate-level effect from"
      " equity-path differences vs the separate v2 run):\n")
    A("| era | floating financing | flat-5% counterfactual | delta (floating − flat) | avg annualized rate |")
    A("|---|---|---|---|---|")
    for name, _y0, _y1, f3, f5, d, rt in era_fin_rows:
        A(f"| {name} | ${f3:,.0f} | ${f5:,.0f} | ${d:+,.0f} | {rt:.2%} |")
    A("")
    A(f"**ZIRP-era difference, explicit: over 2009-2015 + 2020-2021 combined,"
      f" the floating model charges ${zirp_delta:+,.0f} vs flat-5% — i.e."
      f" ${-zirp_delta:,.0f} of v2's financing drag in those eras was the"
      f" admitted rate mismeasurement, now corrected with historical data.**"
      f" Full-window: floating ${vstats['total_financing']:,.0f} vs flat-5%"
      f" ${vstats['total_fin_flat5']:,.0f} on the same base (v2's own run"
      f" charged ${v2stats['total_financing']:,.0f}).\n")
    A("## Cap-binding diagnostics (v3)\n")
    A(f"- Cap checked on {vstats['cap_checks']} rebalance days (of {n_days} trading"
      f" days); binds on {vstats['cap_binds']} days = {bind_frac_rebal:.1%} of"
      f" rebalance days ({bind_frac_all:.1%} of all days).")
    A(f"- Average scale-down factor when binding: {vstats['avg_scale_when_binding']:.2f}"
      f" (minimum {vstats['min_scale']:.2f}).")
    A(f"- Gross ETF notional: max ${vstats['max_etf_gross']:,.0f}, mean"
      f" ${vstats['mean_etf_gross']:,.0f}; max gross/equity ="
      f" {vstats['max_etf_lev']:.2f}x on {vstats['max_etf_lev_date']} (cap"
      f" {CAP_MULT}x at rebalance; excess is inter-rebalance drift — see caveat).")
    A(f"- Financing accrued on {vstats['fin_days']} days"
      f" ({vstats['fin_days'] / n_days:.1%} of all days), total"
      f" ${vstats['total_financing']:,.0f}.")
    A(f"- Realized portfolio vol: v3 {real_vol_v:.2%} vs v2 {real_vol_v2:.2%} vs"
      f" the {PORT_VOL_TARGET:.0%} target.")
    A(f"- Futures margin usage (approx static micro margins, diagnostic only):"
      f" mean {vstats['mean_margin_frac']:.1%} of equity, max"
      f" {vstats['max_margin_frac']:.1%}.\n")
    A("## Per-leg dropout (fraction of active days sized to 0 units, post-cap)\n")
    A("| leg | instrument | kind | first active | dropout |")
    A("|---|---|---|---|---|")
    for lg in legs:
        f = vstats["dropped_frac"][lg.code]
        A(f"| {lg.code} | {lg.trade_as} | {lg.kind} |"
          f" {vstats['first_active'][lg.code]} | {f:.1%}"
          f"{' — drops out' if np.isfinite(f) and f > 0.5 else ''} |")
    A("")
    A("## Stage-6 gate — HYBRID v3 (deliverable)\n")
    A("| criterion | verdict | actual |")
    A("|---|---|---|")
    for name, ok, val in rows_v:
        A(f"| {name} | {'PASS' if ok else 'FAIL'} | {val} |")
    A(f"\n**STRICT GATE VERDICT (hybrid v3): {'PASS' if strict_pass else 'FAIL'}**\n")
    A(f"> {GATE_DESIGN_FLAG}\n")
    A(f"Relaxed-worst-year information line: with worst-year relaxed to >= -0.5 the"
      f" strategy would **{'PASS' if relaxed else 'FAIL'}** (worst year Sharpe"
      f" {xv['worst_year']:.2f}). This is information for a human gate-design"
      f" discussion, not a pass claim.\n")
    A("Reference gates: HYBRID V2: " + "; ".join(
        f"{n}={'PASS' if ok else 'FAIL'}({v})" for n, ok, v in rows_v2))
    A("HYBRID uncapped: " + "; ".join(
        f"{n}={'PASS' if ok else 'FAIL'}({v})" for n, ok, v in rows_h))
    A("LEVEL B: " + "; ".join(
        f"{n}={'PASS' if ok else 'FAIL'}({v})" for n, ok, v in rows_b))
    A("LEVEL A: " + "; ".join(
        f"{n}={'PASS' if ok else 'FAIL'}({v})" for n, ok, v in rows_a) + "\n")
    A("## IMPLEMENTABILITY LINE (pre-registered, distinct from the promotion gate)\n")
    A("Pre-stated question (v2's, unchanged): *is the diversifier implementable"
      " within real margin at $50k* — does the book retain Sharpe >= 0.5 AND OOS"
      " Sharpe >= 0.4 AND maxDD <= 20% with the cap and financing applied?"
      " **The thresholds are UNCHANGED from v2's pre-registration**; only the"
      " financing-rate measurement is corrected. Evaluated on V3:\n")
    A("| criterion | verdict | actual |")
    A("|---|---|---|")
    for name, ok, val in impl_rows:
        A(f"| {name} | {'PASS' if ok else 'FAIL'} | {val} |")
    A(f"\n**IMPLEMENTABILITY VERDICT: {'YES' if impl_pass else 'NO'}** — under"
      f" historically-measured financing (DFF + 1.50%), the diversifier"
      f" {'RETAINS' if impl_pass else 'does NOT retain'} the pre-stated"
      f" performance floor within real margin at $50k. This is an"
      f" implementability finding, NOT a promotion-gate pass claim.\n")
    A("## Per-year table (A | B | H | V2 | V3)\n")
    A("| year | A Sharpe | B Sharpe | H Sharpe | V2 Sharpe | V3 Sharpe | V3 ret |"
      " V3 flips | V3 cap-bind | V3 corr SPY | V3 financing | flat-5% cf | avg rate |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows_csv:
        A(f"| {r['year']} | {r['lvlA_sharpe']} | {r['lvlB_sharpe']} |"
          f" {r['hybrid_sharpe']} | {r['v2_sharpe']} | {r['v3_sharpe']} |"
          f" {r['v3_ret']:.2%} | {r['v3_flips']} | {r['v3_capbind_frac']:.1%} |"
          f" {r['v3_corr_spy']} | ${r['v3_financing_usd']:,.0f} |"
          f" ${r['flat5_financing_usd']:,.0f} | {r['avg_fin_rate']:.2%} |")
    A("")
    A("## Era breakdown (v3)\n")
    for name, line in era_lines:
        A(f"- {name}: {line}")
    A("")
    A("## Diversification claim, quantified\n")
    A(f"corr(v3 daily returns, SPY daily returns) = **{full_corr:.3f}**"
      f" (beta {beta:.3f}).\n")
    A("## Honest caveats and assumptions (carried forward + new)\n")
    A("- FINANCING MODEL (new): Fed funds + a fixed 1.50% spread approximates"
      " broker margin schedules imperfectly — real retail schedules are tiered"
      " by loan size, benchmark differently (some off prime, ~3% over fed"
      " funds), and reprice with lags; IBKR's actual tiered rates are the"
      " empirical answer a paper forward test would measure. Also, financing"
      " here accrues on gross ETF notional above equity, whereas real Reg-T"
      " interest accrues on the net debit balance and futures margin also"
      " consumes cash — the base convention is carried unchanged from v2 so"
      " the rate correction is the only moving part.")
    A("- DFF accrual convention: annualized rate / 252 per trading day"
      " (matching v2), not actual/360 on calendar days; the annual totals are"
      " equivalent, day-level accruals differ slightly. DFF for the accrual"
      " day is used; the Fed publishes it next-day, but it is the contractual"
      " rate for that day and does not enter position decisions except through"
      " realized equity.")
    A("- CAP MECHANICS (carried): enforced at every rebalance using prior-day"
      " prices — no lookahead; inter-rebalance drift can push gross marginally"
      " above 1.5x on hold days. Pro-rata scale-down trades all ETF legs on"
      " binding days, incurring extra half-spread costs (included).")
    A("- Reg-T context (carried): the 1.5x gross cap sits inside the ~2x Reg-T"
      " overnight limit, leaving headroom for adverse marks and futures margin.")
    A("- Duration mismatch (carried): IEF (7-10y) / SHY (1-3y) / TLT (20y+) are"
      " cash-bond proxies for ZN / ZF / ZB futures — similar but not identical"
      " exposures (carry and roll-down differ).")
    A("- ETF total-return approximation for shorts (carried): dividend-adjusted"
      " series used symmetrically; the short pays the dividend and this"
      " approximates that drag.")
    A("- Futures-leg data (carried): unadjusted Yahoo front-month splices —"
      " roll-day error remains in both signal and P&L; ETF legs are clean"
      " total-return series.")
    A("- ETF legs enter as history allows (carried): each ETF needs 252 trading"
      " days of history before it can signal; the early-2000s book is less"
      " diversified than the paper book.")
    A("- No collateral interest credited on futures margin or free cash (carried)"
      " — note the asymmetry: the book now pays historically-accurate floating"
      " interest on debit balances but still earns nothing on idle cash, a"
      " conservative convention.")
    A("- MC5 bootstraps DAILY portfolio returns per monte_carlo.py"
      " bootstrap_sharpe; the per-trade convention does not map to a"
      " continuously-held book.")
    A("- Static margin table (carried): real futures initial margins vary"
      " substantially over 25 years; the diagnostic is indicative, not a"
      " day-accurate margin-call simulation.")
    (OUT / f"{TAG}.md").write_text("\n".join(L))


if __name__ == "__main__":
    main()
