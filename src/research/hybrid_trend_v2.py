"""HYBRID TREND BOOK v2 — pre-registered leverage-capped implementability test.

Pre-registered design (frozen 2026-07-06 BEFORE results). ONE design, no
variants, numbers reported as they fall. Identical to the hybrid study
(src/research/hybrid_trend.py: same 15 legs, same TSMOM-252d signal, same 12%
vol target, same costs incl. 0.3%/yr short borrow, same window) with EXACTLY
TWO additions:

1. LEVERAGE CAP: total gross ETF notional (longs + |shorts|) <= 1.5x current
   equity at every rebalance. When the vol target demands more, ALL ETF legs
   are scaled down PRO-RATA (preserving relative risk weights; shares floored
   to integers so the cap is never exceeded). Futures legs are UNAFFECTED —
   they consume margin, not Reg-T buying power; futures margin usage is
   reported separately as a % of equity using an approximate static micro
   initial-margin table (stated in the report).

2. FINANCING COST: 5%/yr charged daily on max(0, gross ETF notional − equity)
   — the borrow-to-hold cost the uncapped study ignored. RATE ASSUMPTION:
   5%/yr flat (broker margin loan on the notional financed above equity; a
   simplification — real Reg-T interest accrues on the net debit balance and
   floats with rates; 5% is a round, stated assumption).

VERDICT (frozen before results):
  (a) strict Stage-6 gate PASS/FAIL (same revised gate as the prior studies,
      plus the standing GATE-DESIGN FLAG and relaxed worst-year info line);
  (b) IMPLEMENTABILITY LINE (pre-stated, distinct from the promotion gate):
      'is the diversifier implementable within real margin at $50k' = does v2
      retain Sharpe >= 0.5 AND OOS Sharpe >= 0.4 AND maxDD <= 20% with the
      cap and financing applied?
No promoter call; validated_strategies.yaml untouched.

Run:  .venv/bin/python -m src.research.hybrid_trend_v2
Outputs: logs/research/hybrid_trend_v2_20260706.{md,log,csv}
Strict ownership: creates only this file and the three outputs. Reuses
hybrid_trend.py / futures_trend.py machinery by import; modifies neither.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from src.research.futures_trend import (
    ERAS, GATE_DESIGN_FLAG, OUT, PORT_VOL_TARGET, START_EQUITY, TRADING_DAYS,
    Tee, ann_ret, ann_sharpe, build_panel, era_slice, fetch, gate_rows,
    level_a, level_b, max_dd, refresh_days,
)
from src.research.hybrid_trend import (
    BORROW_RATE, LEGS, MIN_ETF_YEARS, build_hybrid_panel, level_b_hybrid,
)
from src.research.monte_carlo import per_year_sharpe

TAG = "hybrid_trend_v2_20260706"

CAP_MULT = 1.5        # gross ETF notional <= 1.5x equity (frozen)
FIN_RATE = 0.05       # 5%/yr on max(0, gross ETF notional - equity) (frozen)

# Approximate CME micro initial margins (static, $/contract — stated
# assumption; real margins float with volatility). Used ONLY for the
# separately-reported futures margin usage diagnostic, NOT for sizing.
FUT_MARGIN = {
    "MES": 2_300.0, "MNQ": 3_500.0, "M2K": 850.0, "MYM": 1_600.0,
    "MCL": 1_200.0, "M6E": 500.0, "M6B": 450.0, "MJY": 350.0, "M6A": 400.0,
}


# ── v2 book: hybrid loop + leverage cap + financing ─────────────────────────

def level_b_hybrid_v2(idx, C, sig, vol, legs):
    """Identical to hybrid_trend.level_b_hybrid EXCEPT: (1) at every rebalance
    (monthly refresh or any leg change) the target ETF book is scaled down
    pro-rata so gross ETF notional <= CAP_MULT * equity (integer shares via
    floor, so the cap binds from below); (2) 5%/yr financing charged daily on
    max(0, gross ETF notional - equity). Futures legs untouched by the cap."""
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
    fin_days = 0
    cap_checks = cap_binds = 0
    scale_sum = 0.0
    min_scale = 1.0
    yearly_capbind: dict[int, list[int]] = {}   # year -> [binds, checks]
    yearly_flips: dict[int, int] = {}
    # gross-notional diagnostics
    max_etf_gross = 0.0
    etf_gross_sum = 0.0
    etf_gross_n = 0
    max_etf_lev = 0.0
    max_etf_lev_date = None
    leg_notional_sum = np.zeros(len(cols))      # ETF legs: sum of |notional|
    leg_notional_max = np.zeros(len(cols))
    # futures margin usage diagnostics
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

        # ── ADDITION 1: leverage cap at every rebalance ──
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

        # per-leg activity/dropout accounting (post-cap: a cap-zeroed leg
        # counts as dropped — honest granularity accounting)
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
        # ETF short borrow (unchanged from hybrid study)
        short_notional = float(np.where(is_etf & (new_n < 0), -new_n * px, 0.0).sum())
        borrow = BORROW_RATE / TRADING_DAYS * short_notional
        total_borrow += borrow
        # gross-notional diagnostics
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
        # futures margin usage (diagnostic only)
        marg = float(np.where(~is_etf, np.abs(new_n) * fut_margin, 0.0).sum())
        if eq > 0:
            mf = marg / eq
            margin_frac_sum += mf
            margin_frac_n += 1
            max_margin_frac = max(max_margin_frac, mf)
        # ── ADDITION 2: financing on ETF notional above equity ──
        financing = FIN_RATE / TRADING_DAYS * max(0.0, gross_etf - max(eq, 0.0))
        if financing > 0:
            fin_days += 1
        total_financing += financing

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
    print(f"=== HYBRID TREND BOOK v2 — leverage-capped implementability test ({TAG}) ===")
    print("Frozen design: hybrid_trend_20260706 + EXACTLY (1) 1.5x gross-ETF-notional")
    print("cap with pro-rata scale-down, (2) 5%/yr financing on gross ETF notional")
    print("above equity. Numbers reported as they fall.\n")
    print(f"RATE ASSUMPTION (prominent): financing = {FIN_RATE:.0%}/yr flat, charged"
          f" daily on max(0, gross ETF notional - equity). Short borrow unchanged"
          f" at {BORROW_RATE:.1%}/yr on short notional.")

    # ── reproduce prior books: level A, level B (futures), hybrid uncapped ──
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

    # same CPER sufficiency check as the hybrid study (KEPT there: 14.6y)
    legs = list(LEGS)
    cper = fetch("CPER", auto_adjust=True)
    cper_yrs = (cper.index.max() - cper.index.min()).days / 365.25
    if cper_yrs < MIN_ETF_YEARS:
        legs = [lg for lg in legs if lg.ticker != "CPER"]
        print(f"CPER history {cper_yrs:.1f}y < {MIN_ETF_YEARS:.0f}y — HG DROPPED.")
    else:
        print(f"CPER history {cper_yrs:.1f}y >= {MIN_ETF_YEARS:.0f}y — HG kept (as in hybrid study).")
    print(f"Book: {len(legs)} legs ({sum(1 for lg in legs if lg.kind == 'fut')} futures,"
          f" {sum(1 for lg in legs if lg.kind == 'etf')} ETF). NG dropped (pre-registered, carried over).")

    idx, C, R, sig, vol, _quality, _depth = build_hybrid_panel(legs)

    eq_h0, hstats = level_b_hybrid(idx, C, sig, vol, legs)
    fh = eq_h0.index[np.argmax(eq_h0.values != START_EQUITY)]
    rh = eq_h0.pct_change().loc[fh:].dropna()
    eq_h = eq_h0.loc[fh:]
    print(f"Reproduced HYBRID uncapped $50k:         Sharpe {ann_sharpe(rh):.2f}"
          f"  ann_ret {ann_ret(rh):.2%}  maxDD {max_dd(eq_h):.1%}"
          f"  final ${eq_h.iloc[-1]:,.0f}  max ETF lev {hstats['max_etf_lev']:.1f}x")

    # ── THE DELIVERABLE: v2 capped + financed ──
    eq_v0, vstats = level_b_hybrid_v2(idx, C, sig, vol, legs)
    fv = eq_v0.index[np.argmax(eq_v0.values != START_EQUITY)]
    rv = eq_v0.pct_change().loc[fv:].dropna()
    eq_v = eq_v0.loc[fv:]
    n_days = len(rv)
    print(f"\nHYBRID v2 (1.5x cap + {FIN_RATE:.0%} financing) — THE DELIVERABLE:"
          f" {rv.index.min().date()}..{rv.index.max().date()}")
    print(f"  Sharpe {ann_sharpe(rv):.2f}  ann_ret {ann_ret(rv):.2%}"
          f"  maxDD {max_dd(eq_v):.1%}  final equity ${eq_v.iloc[-1]:,.0f}")
    all_in = vstats["total_costs"] + vstats["total_borrow"] + vstats["total_financing"]
    print(f"  costs ${vstats['total_costs']:,.0f} (futures ${vstats['fut_costs']:,.0f}"
          f" + ETF half-spread ${vstats['etf_costs']:,.0f})"
          f"  short borrow ${vstats['total_borrow']:,.0f}"
          f"  FINANCING ${vstats['total_financing']:,.0f}"
          f"  all-in ${all_in:,.0f}  sign-flips {vstats['total_flips']}")

    # ── cap-binding diagnostics ──
    bind_frac_rebal = vstats["cap_binds"] / vstats["cap_checks"] if vstats["cap_checks"] else float("nan")
    bind_frac_all = vstats["cap_binds"] / n_days
    real_vol_v = float(rv.std() * np.sqrt(TRADING_DAYS))
    real_vol_h = float(rh.std() * np.sqrt(TRADING_DAYS))
    print("\nCAP-BINDING DIAGNOSTICS:")
    print(f"  rebalance days (cap checked): {vstats['cap_checks']}  of {n_days} trading days")
    print(f"  cap binds: {vstats['cap_binds']} days = {bind_frac_rebal:.1%} of rebalance days"
          f" ({bind_frac_all:.1%} of all days)")
    print(f"  avg scale-down when binding: {vstats['avg_scale_when_binding']:.2f}"
          f"  (min {vstats['min_scale']:.2f})")
    print(f"  gross ETF notional: max ${vstats['max_etf_gross']:,.0f},"
          f" mean ${vstats['mean_etf_gross']:,.0f};"
          f" max gross/equity = {vstats['max_etf_lev']:.2f}x on {vstats['max_etf_lev_date']}"
          f" (uncapped study: {hstats['max_etf_lev']:.1f}x)")
    print(f"  financing accrued on {vstats['fin_days']} days"
          f" ({vstats['fin_days'] / n_days:.1%} of days), total ${vstats['total_financing']:,.0f}")
    print("  ETF gross-notional dominance (share of cumulative gross | max $):")
    etf_legs = [lg for lg in legs if lg.kind == "etf"]
    for lg in sorted(etf_legs, key=lambda g: -vstats["leg_notional_share"][g.code]):
        print(f"    {lg.code:4s} ({lg.ticker:4s}): {vstats['leg_notional_share'][lg.code]:6.1%}"
              f"  | max ${vstats['leg_notional_max'][lg.code]:,.0f}")
    print(f"  realized portfolio vol: v2 {real_vol_v:.2%} vs uncapped {real_vol_h:.2%}"
          f" vs {PORT_VOL_TARGET:.0%} target — cap shortfall {PORT_VOL_TARGET - real_vol_v:+.2%}"
          f" (a binding cap lowers realized vol below target)")
    print(f"  futures margin usage (approx static micro margins): mean"
          f" {vstats['mean_margin_frac']:.1%} of equity, max {vstats['max_margin_frac']:.1%}")
    print("  margin table assumed ($/contract): " +
          ", ".join(f"{k} {v:,.0f}" for k, v in FUT_MARGIN.items()))

    print("\nPer-leg dropout (fraction of active days sized to 0 units, post-cap):")
    for lg in legs:
        f = vstats["dropped_frac"][lg.code]
        d = " <- effectively drops out" if np.isfinite(f) and f > 0.5 else ""
        print(f"  {lg.code:4s} ({lg.trade_as:10s}) [{lg.kind}]"
              f" first_active={vstats['first_active'][lg.code]}: {f:.1%}{d}")

    # ── gates (all four books) ──
    rows_v, xv = gate_rows(rv, eq_v, vstats["total_flips"])
    rows_h, xh = gate_rows(rh, eq_h, hstats["total_flips"])
    rows_b, xb = gate_rows(rb, eq_b, bstats0["total_flips"])
    rows_a, xa = gate_rows(ra, eq_a, bstats0["total_flips"])
    print("\nSTAGE-6 GATE — HYBRID v2 (deliverable):")
    for name, ok, val in rows_v:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    strict_pass = all(ok for _, ok, _ in rows_v)
    relaxed = all(ok for (nm, ok, _) in rows_v if not nm.startswith("Worst-year")) \
        and np.isfinite(xv["worst_year"]) and xv["worst_year"] >= -0.5
    print(f"  STRICT VERDICT: {'PASS' if strict_pass else 'FAIL'}")
    print(f"  Relaxed worst-year (>= -0.5) information line: would_{'PASS' if relaxed else 'FAIL'}")

    # ── IMPLEMENTABILITY LINE (pre-stated, distinct from the promotion gate) ──
    impl_rows = [
        ("Sharpe >= 0.5", np.isfinite(xv["full_sharpe"]) and xv["full_sharpe"] >= 0.5,
         f"{xv['full_sharpe']:.2f}"),
        ("OOS Sharpe >= 0.4", np.isfinite(xv["oos_sharpe"]) and xv["oos_sharpe"] >= 0.4,
         f"{xv['oos_sharpe']:.2f}"),
        ("maxDD <= 20%", xv["max_dd"] >= -0.20, f"{xv['max_dd']:.1%}"),
    ]
    impl_pass = all(ok for _, ok, _ in impl_rows)
    print("\nIMPLEMENTABILITY LINE (pre-registered, NOT the promotion gate):")
    for name, ok, val in impl_rows:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    print(f"  IMPLEMENTABILITY VERDICT: {'YES' if impl_pass else 'NO'} — the diversifier"
          f" {'IS' if impl_pass else 'is NOT'} implementable within real margin at $50k"
          f" under the 1.5x cap + {FIN_RATE:.0%} financing.")

    # ── four-way comparison ──
    comp = [
        ("Sharpe (full)", f"{xa['full_sharpe']:.2f}", f"{xb['full_sharpe']:.2f}",
         f"{xh['full_sharpe']:.2f}", f"{xv['full_sharpe']:.2f}"),
        ("Ann. return", f"{ann_ret(ra):.2%}", f"{ann_ret(rb):.2%}",
         f"{ann_ret(rh):.2%}", f"{ann_ret(rv):.2%}"),
        ("Max drawdown", f"{max_dd(eq_a):.1%}", f"{max_dd(eq_b):.1%}",
         f"{max_dd(eq_h):.1%}", f"{max_dd(eq_v):.1%}"),
        ("IS Sharpe (first 70%)", f"{xa['is_sharpe']:.2f}", f"{xb['is_sharpe']:.2f}",
         f"{xh['is_sharpe']:.2f}", f"{xv['is_sharpe']:.2f}"),
        ("OOS Sharpe (last 30%)", f"{xa['oos_sharpe']:.2f}", f"{xb['oos_sharpe']:.2f}",
         f"{xh['oos_sharpe']:.2f}", f"{xv['oos_sharpe']:.2f}"),
        ("MC 5th-pct Sharpe", f"{xa['mc5']:.2f}", f"{xb['mc5']:.2f}",
         f"{xh['mc5']:.2f}", f"{xv['mc5']:.2f}"),
        ("Worst-year Sharpe", f"{xa['worst_year']:.2f}", f"{xb['worst_year']:.2f}",
         f"{xh['worst_year']:.2f}", f"{xv['worst_year']:.2f}"),
        ("Final equity ($50k start)", "n/a (fractional)", f"${eq_b.iloc[-1]:,.0f}",
         f"${eq_h.iloc[-1]:,.0f}", f"${eq_v.iloc[-1]:,.0f}"),
        ("Total costs", "$0 (costless)", f"${bstats0['total_costs']:,.0f}",
         f"${hstats['total_costs'] + hstats['total_borrow']:,.0f} (no financing modeled)",
         f"${all_in:,.0f} (incl. ${vstats['total_financing']:,.0f} financing)"),
        ("Max gross ETF / equity", "n/a", "n/a", f"{hstats['max_etf_lev']:.1f}x",
         f"{vstats['max_etf_lev']:.2f}x (cap {CAP_MULT}x)"),
        ("Window", f"{ra.index.min().date()}..{ra.index.max().date()}",
         f"{rb.index.min().date()}..{rb.index.max().date()}",
         f"{rh.index.min().date()}..{rh.index.max().date()}",
         f"{rv.index.min().date()}..{rv.index.max().date()}"),
    ]
    print("\nFOUR-WAY COMPARISON — A (ideal) | B (all-fut $50k) | H (hybrid uncapped) | V2 (capped+financed):")
    for row in comp:
        print(f"  {row[0]:26s} A {row[1]:>16s} | B {row[2]:>16s} | H {row[3]:>16s} | V2 {row[4]:>28s}")

    # ── per-year + era + SPY correlation ──
    spy = fetch("SPY", auto_adjust=True)
    spy_r = spy.pct_change().dropna()
    by_a, by_b = per_year_sharpe(ra), per_year_sharpe(rb)
    by_h, by_v = per_year_sharpe(rh), per_year_sharpe(rv)
    years = sorted(set(by_a) | set(by_b) | set(by_h) | set(by_v))
    rows_csv = []
    print("\nPER-YEAR (Sharpe/ann-ret: A | B | H | V2; v2 flips; v2 cap-bind frac; v2 corr vs SPY):")
    for y in years:
        a_y, b_y = ra[ra.index.year == y], rb[rb.index.year == y]
        h_y, v_y = rh[rh.index.year == y], rv[rv.index.year == y]
        s = spy_r[spy_r.index.year == y]
        j = v_y.align(s, join="inner")
        corr = float(j[0].corr(j[1])) if len(j[0]) > 30 else float("nan")
        fl = vstats["yearly_flips"].get(y, 0)
        cb = vstats["yearly_capbind"].get(y, [0, 0])
        cb_frac = cb[0] / cb[1] if cb[1] else float("nan")
        rows_csv.append({
            "year": y,
            "lvlA_sharpe": round(by_a.get(y, float("nan")), 2), "lvlA_ret": round(ann_ret(a_y), 4),
            "lvlB_sharpe": round(by_b.get(y, float("nan")), 2), "lvlB_ret": round(ann_ret(b_y), 4),
            "hybrid_sharpe": round(by_h.get(y, float("nan")), 2), "hybrid_ret": round(ann_ret(h_y), 4),
            "v2_sharpe": round(by_v.get(y, float("nan")), 2), "v2_ret": round(ann_ret(v_y), 4),
            "v2_flips": fl, "v2_capbind_frac": round(cb_frac, 3),
            "v2_corr_spy": round(corr, 2)})
        print(f"  {y}: A {by_a.get(y, float('nan')):6.2f}/{ann_ret(a_y):8.2%}"
              f" | B {by_b.get(y, float('nan')):6.2f}/{ann_ret(b_y):8.2%}"
              f" | H {by_h.get(y, float('nan')):6.2f}/{ann_ret(h_y):8.2%}"
              f" | V2 {by_v.get(y, float('nan')):6.2f}/{ann_ret(v_y):8.2%}"
              f" | flips {fl:3d} | bind {cb_frac:5.1%} | corrSPY {corr:5.2f}")
    pd.DataFrame(rows_csv).to_csv(OUT / f"{TAG}.csv", index=False)

    print("\nERA BREAKDOWN (v2):")
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
    print(f"\nDIVERSIFICATION: corr(v2 daily, SPY daily) = {full_corr:.3f},"
          f" beta = {beta:.3f} over {len(j[0])} overlapping days")

    write_report(legs, ra, eq_a, rb, eq_b, bstats0, rh, eq_h, hstats, rv, eq_v, vstats,
                 rows_v, rows_h, rows_b, rows_a, xv, xh, xb, xa, strict_pass, relaxed,
                 impl_rows, impl_pass, comp, rows_csv, era_lines, full_corr, beta,
                 bind_frac_rebal, bind_frac_all, real_vol_v, real_vol_h, all_in, n_days)
    print(f"\nOutputs: {OUT / (TAG + '.md')}, .log, .csv")
    sys.stdout = sys.__stdout__
    tee.f.close()


def write_report(legs, ra, eq_a, rb, eq_b, bstats0, rh, eq_h, hstats, rv, eq_v, vstats,
                 rows_v, rows_h, rows_b, rows_a, xv, xh, xb, xa, strict_pass, relaxed,
                 impl_rows, impl_pass, comp, rows_csv, era_lines, full_corr, beta,
                 bind_frac_rebal, bind_frac_all, real_vol_v, real_vol_h, all_in, n_days):
    L = []
    A = L.append
    A("# HYBRID TREND BOOK v2 — leverage-capped implementability test — 2026-07-06\n")
    A("RESEARCH-ONLY. Pre-registered frozen design: IDENTICAL to"
      " hybrid_trend_20260706 (same 15 legs, TSMOM-252d sign, 12% vol target with"
      " equal-risk (12%/N_active)/ann_vol_20d budgeting, same costs incl. 0.3%/yr"
      " short borrow, same window) with EXACTLY TWO additions: (1) a 1.5x"
      " gross-ETF-notional/equity LEVERAGE CAP enforced pro-rata across all ETF"
      " legs at every rebalance, futures legs unaffected; (2) a FINANCING COST on"
      " ETF notional held above equity. One design, no variants, numbers as they"
      " fall. No promoter call; validated_strategies.yaml untouched.\n")
    A("## Rate and margin assumptions (prominent)\n")
    A(f"- **FINANCING RATE: {5}%/yr flat**, charged daily on max(0, gross ETF"
      " notional − equity). This models the borrow-to-hold cost the uncapped study"
      " ignored. Simplification: real Reg-T margin interest accrues on the net"
      " debit balance and floats with prevailing rates; 5% is a round, stated"
      " assumption.")
    A(f"- ETF short borrow unchanged: 0.3%/yr on short notional (general"
      " collateral, as in the hybrid study).")
    A("- Approximate static micro initial margins used ONLY for the"
      " separately-reported futures margin diagnostic (real margins float):\n")
    A("| contract | assumed initial margin |")
    A("|---|---|")
    for k, v in FUT_MARGIN.items():
        A(f"| {k} | ${v:,.0f} |")
    A("")
    A("## Headline results\n")
    A(f"- LEVEL A (fractional ideal, reproduced): Sharpe {ann_sharpe(ra):.2f},"
      f" ann ret {ann_ret(ra):.2%}, maxDD {max_dd(eq_a):.1%}")
    A(f"- LEVEL B (all-futures $50k, reproduced): Sharpe {ann_sharpe(rb):.2f},"
      f" ann ret {ann_ret(rb):.2%}, maxDD {max_dd(eq_b):.1%}, final ${eq_b.iloc[-1]:,.0f}")
    A(f"- HYBRID uncapped (reproduced): Sharpe {ann_sharpe(rh):.2f}, ann ret"
      f" {ann_ret(rh):.2%}, maxDD {max_dd(eq_h):.1%}, final ${eq_h.iloc[-1]:,.0f},"
      f" max ETF gross/equity {hstats['max_etf_lev']:.1f}x — NOT implementable under Reg-T")
    A(f"- **HYBRID v2 (1.5x cap + 5%/yr financing — THE DELIVERABLE): Sharpe"
      f" {ann_sharpe(rv):.2f}, ann ret {ann_ret(rv):.2%}, maxDD {max_dd(eq_v):.1%},"
      f" final equity ${eq_v.iloc[-1]:,.0f} from $50k**, all-in costs ${all_in:,.0f}"
      f" (trading ${vstats['total_costs']:,.0f} + short borrow"
      f" ${vstats['total_borrow']:,.0f} + financing ${vstats['total_financing']:,.0f}),"
      f" sign-flips {vstats['total_flips']}, window"
      f" {rv.index.min().date()}..{rv.index.max().date()}\n")
    A("## MANDATORY four-way comparison\n")
    A("| metric | A: fractional ideal | B: all-futures $50k | H: hybrid uncapped | V2: capped + financed |")
    A("|---|---|---|---|---|")
    for row in comp:
        A(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |")
    A("")
    A("## Cap-binding diagnostics\n")
    A(f"- Cap checked on {vstats['cap_checks']} rebalance days (of {n_days} trading"
      f" days); **binds on {vstats['cap_binds']} days = {bind_frac_rebal:.1%} of"
      f" rebalance days ({bind_frac_all:.1%} of all days)**.")
    A(f"- Average scale-down factor when binding: {vstats['avg_scale_when_binding']:.2f}"
      f" (minimum {vstats['min_scale']:.2f} — i.e. at the worst point the ETF book"
      f" is cut to {vstats['min_scale']:.0%} of its vol-target size).")
    A(f"- Gross ETF notional: max ${vstats['max_etf_gross']:,.0f}, mean"
      f" ${vstats['mean_etf_gross']:,.0f}; max gross/equity ="
      f" {vstats['max_etf_lev']:.2f}x (uncapped study: {hstats['max_etf_lev']:.1f}x)."
      f" The cap holds exactly (<= {CAP_MULT}x, integer floor) on every rebalance"
      f" day; any excess over {CAP_MULT}x in the measured max is inter-rebalance"
      f" price/equity drift on hold days (see cap-mechanics caveat).")
    A(f"- Financing accrued on {vstats['fin_days']} days"
      f" ({vstats['fin_days'] / n_days:.1%} of all days), total"
      f" ${vstats['total_financing']:,.0f}.")
    A("- ETF gross-notional dominance (share of cumulative gross | max single-day $):\n")
    A("| leg | ETF | share of gross | max notional |")
    A("|---|---|---|---|")
    etf_legs = [lg for lg in legs if lg.kind == "etf"]
    for lg in sorted(etf_legs, key=lambda g: -vstats["leg_notional_share"][g.code]):
        A(f"| {lg.code} | {lg.ticker} | {vstats['leg_notional_share'][lg.code]:.1%} |"
          f" ${vstats['leg_notional_max'][lg.code]:,.0f} |")
    A("")
    A(f"- **Realized portfolio vol: v2 {real_vol_v:.2%} vs uncapped {real_vol_h:.2%}"
      f" vs the 12% target** — the equal-risk budget already realizes below target"
      f" (correlations ignored by design), and the binding cap lowers it further;"
      f" the honest shortfall vs target is {PORT_VOL_TARGET - real_vol_v:.2%}.")
    A(f"- Futures margin usage (approx static micro margins, diagnostic only):"
      f" mean {vstats['mean_margin_frac']:.1%} of equity, max"
      f" {vstats['max_margin_frac']:.1%} — futures legs fit comfortably; the"
      f" binding constraint is the ETF side, as expected.\n")
    A("## Per-leg dropout (fraction of active days sized to 0 units, post-cap)\n")
    A("| leg | instrument | kind | first active | dropout |")
    A("|---|---|---|---|---|")
    for lg in legs:
        f = vstats["dropped_frac"][lg.code]
        A(f"| {lg.code} | {lg.trade_as} | {lg.kind} |"
          f" {vstats['first_active'][lg.code]} | {f:.1%}"
          f"{' — drops out' if np.isfinite(f) and f > 0.5 else ''} |")
    A("")
    A("## Stage-6 gate — HYBRID v2 (deliverable)\n")
    A("| criterion | verdict | actual |")
    A("|---|---|---|")
    for name, ok, val in rows_v:
        A(f"| {name} | {'PASS' if ok else 'FAIL'} | {val} |")
    A(f"\n**STRICT GATE VERDICT (hybrid v2): {'PASS' if strict_pass else 'FAIL'}**\n")
    A(f"> {GATE_DESIGN_FLAG}\n")
    A(f"Relaxed-worst-year information line: with worst-year relaxed to >= -0.5 the"
      f" strategy would **{'PASS' if relaxed else 'FAIL'}** (worst year Sharpe"
      f" {xv['worst_year']:.2f}). This is information for a human gate-design"
      f" discussion, not a pass claim.\n")
    A("Reference gates: HYBRID uncapped: " + "; ".join(
        f"{n}={'PASS' if ok else 'FAIL'}({v})" for n, ok, v in rows_h))
    A("LEVEL B: " + "; ".join(
        f"{n}={'PASS' if ok else 'FAIL'}({v})" for n, ok, v in rows_b))
    A("LEVEL A: " + "; ".join(
        f"{n}={'PASS' if ok else 'FAIL'}({v})" for n, ok, v in rows_a) + "\n")
    A("## IMPLEMENTABILITY LINE (pre-registered, distinct from the promotion gate)\n")
    A("Pre-stated question this study exists for: *is the diversifier implementable"
      " within real margin at $50k* — does v2 retain Sharpe >= 0.5 AND OOS Sharpe"
      " >= 0.4 AND maxDD <= 20% with the cap and financing applied?\n")
    A("| criterion | verdict | actual |")
    A("|---|---|---|")
    for name, ok, val in impl_rows:
        A(f"| {name} | {'PASS' if ok else 'FAIL'} | {val} |")
    A(f"\n**IMPLEMENTABILITY VERDICT: {'YES' if impl_pass else 'NO'}** — the"
      f" diversifier {'RETAINS' if impl_pass else 'does NOT retain'} the pre-stated"
      f" performance floor under the 1.5x gross cap and 5%/yr financing. This is an"
      f" implementability finding, NOT a promotion-gate pass claim.\n")
    A("## Per-year table (A | B | H | V2)\n")
    A("| year | A Sharpe | A ret | B Sharpe | B ret | H Sharpe | H ret |"
      " V2 Sharpe | V2 ret | V2 flips | V2 cap-bind | V2 corr SPY |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows_csv:
        A(f"| {r['year']} | {r['lvlA_sharpe']} | {r['lvlA_ret']:.2%} |"
          f" {r['lvlB_sharpe']} | {r['lvlB_ret']:.2%} |"
          f" {r['hybrid_sharpe']} | {r['hybrid_ret']:.2%} |"
          f" {r['v2_sharpe']} | {r['v2_ret']:.2%} |"
          f" {r['v2_flips']} | {r['v2_capbind_frac']:.1%} | {r['v2_corr_spy']} |")
    A("")
    A("## Era breakdown (v2)\n")
    for name, line in era_lines:
        A(f"- {name}: {line}")
    A("")
    A("## Diversification claim, quantified\n")
    A(f"corr(v2 daily returns, SPY daily returns) = **{full_corr:.3f}**"
      f" (beta {beta:.3f}).\n")
    A("## Honest caveats and assumptions (carried forward + new)\n")
    A("- FINANCING MODEL (new, simplification): 5%/yr flat on max(0, gross ETF"
      " notional − equity). Real margin interest is charged on the net debit"
      " balance (cash actually borrowed), floats with rates (near 0% 2009-15 and"
      " 2020-21, >6% at retail brokers 2023-24), and futures margin also consumes"
      " cash — so this proxy overstates financing in ZIRP years and may understate"
      " it recently. The 1.5x cap keeps the financed amount small, limiting the"
      " sensitivity to this assumption.")
    A("- CAP MECHANICS: enforced at every rebalance (monthly refresh or any leg"
      " change) using prior-day prices — no lookahead. Between rebalances,"
      " positions are held, so price drift can push gross marginally above 1.5x"
      " intraperiod; a real account would also mark daily. The pro-rata scale-down"
      " trades ALL ETF legs on binding days, incurring extra half-spread costs"
      " (included).")
    A("- Reg-T context: the 1.5x gross cap is deliberately INSIDE the ~2x Reg-T"
      " overnight limit, leaving headroom for marks against the position and for"
      " the cash consumed by futures margin (reported separately above).")
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
    A("- No collateral interest credited on futures margin or free cash; Sharpe on"
      " raw equity changes, consistent with project conventions.")
    A("- MC5 bootstraps DAILY portfolio returns per monte_carlo.py"
      " bootstrap_sharpe; the per-trade convention does not map to a"
      " continuously-held book.")
    A("- Static margin table: real futures initial margins vary substantially over"
      " 25 years; the diagnostic is indicative, not a day-accurate margin-call"
      " simulation.")
    (OUT / f"{TAG}.md").write_text("\n".join(L))


if __name__ == "__main__":
    main()
