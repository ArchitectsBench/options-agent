"""HYBRID TREND BOOK — RESEARCH-ONLY follow-up (b) to the futures TSMOM pass.

Pre-registered design (frozen 2026-07-06). ONE design, no variants, numbers
reported as they fall. The ONLY change vs src/research/futures_trend.py is the
INSTRUMENT MAP — the study measures how much of the level-A (fractional ideal)
to level-B (all-futures $50k) gap is closed by routing the granularity-starved
legs through ETFs (share-level granularity: dollars, not thousands).

INSTRUMENT MAP (frozen):
  equity ES NQ RTY YM  -> micro futures MES MNQ M2K MYM (as before)
  fx     6E 6B 6J 6A   -> micro FX futures (as before)
  bonds  ZN ZF ZB      -> ETFs IEF SHY TLT (short via ETF short position;
                          assume shortable at 0.3%/yr borrow — stated assumption)
  metals GC SI         -> GLD SLV
  metal  HG            -> CPER if history >= 10y else DROP (report either way)
  energy CL            -> MCL micro future (kept as futures)
  energy NG            -> DROPPED, pre-registered: no micro, no clean ETF
                          (UNG roll decay disqualifies it as a trend proxy)

COSTS: futures legs $1.50 commission + 1-tick slippage per side per contract
(unchanged). ETF legs $0 commission + half-spread $0.01/share per side; sized
in INTEGER SHARES. ETF shorts pay 0.3%/yr borrow on short notional.

DATA: futures legs reuse the cached Yahoo continuous splices (same caveats as
the prior study). ETF legs use yfinance DIVIDEND-ADJUSTED closes (auto_adjust
=True — total return): honest for long legs; for SHORT legs the short pays the
dividend, and using the adjusted series symmetrically approximates that drag —
documented assumption.

SIGNAL / SIZING / WINDOW / GATE: identical to the prior study — TSMOM sign of
252d trailing return (flips only), 12% portfolio vol target, equal-risk
(12%/N_active)/ann_vol_20d budgeting, monthly vol refresh, max common history,
revised Stage-6 gate + GATE-DESIGN FLAG + relaxed worst-year info line.

VERDICT (frozen): strict gate PASS/FAIL + relaxed info line + gap-closure
percentage. No promoter call; no iteration.

Run:  .venv/bin/python -m src.research.hybrid_trend
Outputs: logs/research/hybrid_trend_20260706.{md,log,csv}
Strict ownership: creates only this file and the three outputs; extends
data/futures_daily/ only with NEW ETF series (SHY, CPER). Touches nothing else.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.research.futures_trend import (
    CACHE, COMMISSION, ERAS, GATE_DESIGN_FLAG, LOOKBACK, MA_WIN, MARKETS, OUT,
    PORT_VOL_TARGET, START_EQUITY, TRADING_DAYS, VOL_WIN, Tee, ann_ret,
    ann_sharpe, build_panel, clean_returns, drop_bad_prints, era_slice, fetch,
    gate_rows, level_a, level_b, max_dd, refresh_days,
)
from src.research.monte_carlo import per_year_sharpe

TAG = "hybrid_trend_20260706"

ETF_HALF_SPREAD = 0.01     # $/share per side, $0 commission (frozen)
BORROW_RATE = 0.003        # /yr on ETF short notional (stated assumption)
MIN_ETF_YEARS = 10.0       # CPER sufficiency bar (prior study's <10y flag line)

FUT = {m.code: m for m in MARKETS}


@dataclass(frozen=True)
class Leg:
    code: str        # asset identity carried over from the prior study
    ticker: str      # yahoo source series
    sector: str
    kind: str        # "fut" | "etf"
    trade_as: str
    mult: float      # $ per 1.0 quote per contract; 1.0 for ETF shares
    cost_side: float  # $ per unit per side (fut: comm + tick*mult; etf: half-spread)
    auto_adjust: bool


def fut_leg(code: str) -> Leg:
    m = FUT[code]
    return Leg(code, m.yahoo, m.sector, "fut", m.trade_as, m.mult,
               COMMISSION + m.tick * m.mult, False)


def etf_leg(code: str, etf: str, sector: str) -> Leg:
    return Leg(code, etf, sector, "etf", f"{etf} (ETF)", 1.0, ETF_HALF_SPREAD, True)


LEGS = [
    fut_leg("ES"), fut_leg("NQ"), fut_leg("RTY"), fut_leg("YM"),
    etf_leg("ZN", "IEF", "bond"), etf_leg("ZF", "SHY", "bond"),
    etf_leg("ZB", "TLT", "bond"),
    etf_leg("GC", "GLD", "metal"), etf_leg("SI", "SLV", "metal"),
    etf_leg("HG", "CPER", "metal"),
    fut_leg("CL"),                       # MCL micro exists — stays futures
    # NG: DROPPED (pre-registered — no micro, no clean ETF; UNG decay)
    fut_leg("6E"), fut_leg("6B"), fut_leg("6J"), fut_leg("6A"),
]


# ── hybrid panel (same machinery as build_panel, over the hybrid leg list) ──

def build_hybrid_panel(legs):
    closes, rets, quality, depth = {}, {}, {}, {}
    for lg in legs:
        c = fetch(lg.ticker, auto_adjust=lg.auto_adjust)
        c, n_prints = drop_bad_prints(c)
        r, q = clean_returns(c)
        q["dropped_bad_print_rows"] = n_prints
        closes[lg.code], rets[lg.code], quality[lg.code] = c, r, q
        yrs = (c.index.max() - c.index.min()).days / 365.25
        depth[lg.code] = (str(c.index.min().date()), str(c.index.max().date()),
                          len(c), round(yrs, 1), yrs < 10)
    idx = pd.DatetimeIndex(sorted(set().union(*[c.index for c in closes.values()])))
    C = pd.DataFrame({k: v for k, v in closes.items()}).reindex(idx).ffill(limit=10)
    R = pd.DataFrame({k: v for k, v in rets.items()}).reindex(idx).fillna(0.0)
    have = C.notna()
    A = (1.0 + R.where(have, 0.0)).cumprod().where(have)
    obs = have.cumsum()
    tsr = A / A.shift(LOOKBACK) - 1.0
    sig = np.sign(tsr).where(obs.shift(1) > LOOKBACK + 1)
    vol = (R.where(have).rolling(VOL_WIN).std() * np.sqrt(TRADING_DAYS)).where(obs > VOL_WIN)
    return idx, C, R, sig, vol, quality, depth


# ── hybrid $50k book: integer micro contracts + integer ETF shares ──────────

def level_b_hybrid(idx, C, sig, vol, legs):
    cols = [lg.code for lg in legs]
    S, V, P = sig[cols].values, vol[cols].values, C[cols].values
    monthly = refresh_days(idx)
    mult = np.array([lg.mult for lg in legs])
    cost_side = np.array([lg.cost_side for lg in legs])
    is_etf = np.array([lg.kind == "etf" for lg in legs])

    n = np.zeros(len(cols))
    equity = np.full(len(idx), np.nan)
    equity[0] = START_EQUITY
    flips = np.zeros(len(cols), int)
    dropped_days = np.zeros(len(cols), int)
    active_days = np.zeros(len(cols), int)
    first_active = [None] * len(cols)
    total_costs = fut_costs = etf_costs = total_borrow = 0.0
    max_etf_gross = 0.0
    etf_gross_sum = 0.0
    etf_gross_n = 0
    max_etf_lev = 0.0          # gross ETF notional / concurrent equity
    max_etf_lev_date = None
    shy_i = next((i for i, lg in enumerate(legs) if lg.ticker == "SHY"), None)
    max_shy_gross = 0.0
    yearly_flips: dict[int, int] = {}

    for t in range(1, len(idx)):
        eq = equity[t - 1]
        p_prev = P[t - 1]
        valid = (np.isfinite(S[t - 1]) & np.isfinite(V[t - 1]) & (V[t - 1] > 1e-4)
                 & np.isfinite(p_prev) & (p_prev > 0))
        n_active = int(valid.sum())
        new_n = n.copy()
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
        # ETF short borrow: 0.3%/yr on short notional, charged per held day
        px = np.where(np.isfinite(p_prev), p_prev, 0.0)
        short_notional = float(np.where(is_etf & (new_n < 0), -new_n * px, 0.0).sum())
        borrow = BORROW_RATE / TRADING_DAYS * short_notional
        total_borrow += borrow
        gross_etf = float(np.where(is_etf, np.abs(new_n) * px, 0.0).sum())
        if gross_etf > 0:
            max_etf_gross = max(max_etf_gross, gross_etf)
            etf_gross_sum += gross_etf
            etf_gross_n += 1
            if eq > 0 and gross_etf / eq > max_etf_lev:
                max_etf_lev = gross_etf / eq
                max_etf_lev_date = idx[t]
            if shy_i is not None:
                max_shy_gross = max(max_shy_gross, abs(new_n[shy_i]) * px[shy_i])
        dp = P[t] - p_prev
        dp = np.where(np.isfinite(dp), dp, 0.0)
        pnl = float(np.nansum(new_n * mult * dp))
        equity[t] = eq + pnl - costs - borrow
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
        "total_borrow": total_borrow,
        "max_etf_gross": max_etf_gross,
        "mean_etf_gross": (etf_gross_sum / etf_gross_n if etf_gross_n else 0.0),
        "max_etf_lev": max_etf_lev,
        "max_etf_lev_date": (str(max_etf_lev_date.date()) if max_etf_lev_date is not None else "n/a"),
        "max_shy_gross": max_shy_gross,
    }
    return eq, stats


# ── main ────────────────────────────────────────────────────────────────────

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tee = Tee(OUT / f"{TAG}.log")
    sys.stdout = tee
    print(f"=== HYBRID TREND BOOK — pre-registered follow-up (b) ({TAG}) ===")
    print("Frozen design: same signal/sizing/window/gate as futures_trend_20260706;")
    print("only the instrument map changes. Numbers reported as they fall.\n")

    # ── reproduce the prior study's level A and level B (same caches, same code)
    idx0, C0, R0, sig0, _sig_ma0, vol0, _q0, _d0 = build_panel()
    ra = level_a(idx0, R0, sig0, vol0)
    ra = ra[ra.index >= ra.loc[ra != 0].index.min()]
    eq_a = (1 + ra).cumprod() * START_EQUITY
    eq_b0, bstats0 = level_b(idx0, C0, sig0, vol0)
    fb = eq_b0.index[np.argmax(eq_b0.values != START_EQUITY)]
    rb = eq_b0.pct_change().loc[fb:].dropna()
    eq_b = eq_b0.loc[fb:]
    print(f"Reproduced LEVEL A (fractional ideal):   Sharpe {ann_sharpe(ra):.2f}"
          f"  ann_ret {ann_ret(ra):.2%}  maxDD {max_dd(eq_a):.1%}"
          f"  {ra.index.min().date()}..{ra.index.max().date()}")
    print(f"Reproduced LEVEL B (all-futures $50k):   Sharpe {ann_sharpe(rb):.2f}"
          f"  ann_ret {ann_ret(rb):.2%}  maxDD {max_dd(eq_b):.1%}"
          f"  final ${eq_b.iloc[-1]:,.0f}  costs ${bstats0['total_costs']:,.0f}")

    # ── CPER sufficiency check (pre-registered: keep if >= 10y else DROP) ──
    legs = list(LEGS)
    cper = fetch("CPER", auto_adjust=True)
    cper_yrs = (cper.index.max() - cper.index.min()).days / 365.25
    if cper_yrs < MIN_ETF_YEARS:
        legs = [lg for lg in legs if lg.ticker != "CPER"]
        cper_note = (f"CPER history {cper_yrs:.1f}y < {MIN_ETF_YEARS:.0f}y — HG leg "
                     f"DROPPED per pre-registration.")
    else:
        cper_note = (f"CPER history {cper_yrs:.1f}y >= {MIN_ETF_YEARS:.0f}y — HG leg "
                     f"KEPT as CPER.")
    print(f"\n{cper_note}")
    print("NG leg: DROPPED (pre-registered — no micro future, no clean ETF; UNG "
          "roll decay disqualifies it).")
    print(f"Hybrid book: {len(legs)} legs "
          f"({sum(1 for lg in legs if lg.kind == 'fut')} futures, "
          f"{sum(1 for lg in legs if lg.kind == 'etf')} ETF).")

    # ── hybrid panel + $50k book ──
    idx, C, R, sig, vol, quality, depth = build_hybrid_panel(legs)
    print("\nPer-leg source series (flag = <10y):")
    for lg in legs:
        d = depth[lg.code]
        flag = "  <10y FLAG" if d[4] else ""
        print(f"  {lg.code:4s} -> {lg.trade_as:10s} [{lg.kind}] {lg.ticker:6s} "
              f"{d[0]} .. {d[1]}  {d[2]:5d} rows  {d[3]:5.1f}y{flag}")
    print("\nData quality (cleaned-return diagnostics):")
    for lg in legs:
        q = quality[lg.code]
        print(f"  {lg.code:4s} dropped_bad_prints={q['dropped_bad_print_rows']}"
              f" zeroed_nonpos_price={q['zeroed_nonpositive_price_days']}"
              f" |r|>10%={q['abs_ret_gt_10pct_days']} |r|>50%={q['abs_ret_gt_50pct_days']}"
              f" max|r|={q['max_abs_ret']:.3f}")

    eq_h0, hstats = level_b_hybrid(idx, C, sig, vol, legs)
    fh = eq_h0.index[np.argmax(eq_h0.values != START_EQUITY)]
    rh = eq_h0.pct_change().loc[fh:].dropna()
    eq_h = eq_h0.loc[fh:]
    print(f"\nHYBRID $50k BOOK: {rh.index.min().date()}..{rh.index.max().date()}")
    print(f"  Sharpe {ann_sharpe(rh):.2f}  ann_ret {ann_ret(rh):.2%}  maxDD {max_dd(eq_h):.1%}"
          f"  final equity ${eq_h.iloc[-1]:,.0f}")
    print(f"  costs ${hstats['total_costs']:,.0f} (futures ${hstats['fut_costs']:,.0f}"
          f" + ETF half-spread ${hstats['etf_costs']:,.0f})"
          f"  ETF short borrow ${hstats['total_borrow']:,.0f}"
          f"  sign-flips {hstats['total_flips']}")
    print(f"  ETF gross notional: max ${hstats['max_etf_gross']:,.0f}, "
          f"mean ${hstats['mean_etf_gross']:,.0f}; max gross/equity = "
          f"{hstats['max_etf_lev']:.1f}x on {hstats['max_etf_lev_date']} "
          f"(SHY alone max ${hstats['max_shy_gross']:,.0f}) — see feasibility note")

    print("\nPer-leg quantization dropout (fraction of active days sized to 0 units):")
    for lg in legs:
        f = hstats["dropped_frac"][lg.code]
        d = " <- effectively drops out" if np.isfinite(f) and f > 0.5 else ""
        print(f"  {lg.code:4s} ({lg.trade_as:10s}) [{lg.kind}]"
              f" first_active={hstats['first_active'][lg.code]}: {f:.1%}{d}")
    etf_drop = [hstats["dropped_frac"][lg.code] for lg in legs if lg.kind == "etf"]
    print(f"  ETF-leg dropout check (should be ~zero): max {max(etf_drop):.2%}")

    # ── gates ──
    rows_h, xh = gate_rows(rh, eq_h, hstats["total_flips"])
    rows_b, xb = gate_rows(rb, eq_b, bstats0["total_flips"])
    rows_a, xa = gate_rows(ra, eq_a, bstats0["total_flips"])
    print("\nSTAGE-6 GATE — HYBRID $50k (deliverable):")
    for name, ok, val in rows_h:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    strict_pass = all(ok for _, ok, _ in rows_h)
    relaxed = all(ok for (nm, ok, _) in rows_h if not nm.startswith("Worst-year")) \
        and np.isfinite(xh["worst_year"]) and xh["worst_year"] >= -0.5
    print(f"  STRICT VERDICT: {'PASS' if strict_pass else 'FAIL'}")
    print(f"  Relaxed worst-year (>= -0.5) information line: would_{'PASS' if relaxed else 'FAIL'}")

    # ── the comparison the study exists for ──
    def gap_pct(a, b, h):
        return float("nan") if not np.isfinite(a - b) or abs(a - b) < 1e-9 \
            else (h - b) / (a - b) * 100.0
    gc_sharpe = gap_pct(xa["full_sharpe"], xb["full_sharpe"], xh["full_sharpe"])
    gc_oos = gap_pct(xa["oos_sharpe"], xb["oos_sharpe"], xh["oos_sharpe"])
    comp = [
        ("Sharpe (full)", f"{xa['full_sharpe']:.2f}", f"{xb['full_sharpe']:.2f}", f"{xh['full_sharpe']:.2f}"),
        ("Ann. return", f"{ann_ret(ra):.2%}", f"{ann_ret(rb):.2%}", f"{ann_ret(rh):.2%}"),
        ("Max drawdown", f"{max_dd(eq_a):.1%}", f"{max_dd(eq_b):.1%}", f"{max_dd(eq_h):.1%}"),
        ("IS Sharpe (first 70%)", f"{xa['is_sharpe']:.2f}", f"{xb['is_sharpe']:.2f}", f"{xh['is_sharpe']:.2f}"),
        ("OOS Sharpe (last 30%)", f"{xa['oos_sharpe']:.2f}", f"{xb['oos_sharpe']:.2f}", f"{xh['oos_sharpe']:.2f}"),
        ("MC 5th-pct Sharpe", f"{xa['mc5']:.2f}", f"{xb['mc5']:.2f}", f"{xh['mc5']:.2f}"),
        ("Worst-year Sharpe", f"{xa['worst_year']:.2f}", f"{xb['worst_year']:.2f}", f"{xh['worst_year']:.2f}"),
        ("Final equity ($50k start)", "n/a (fractional)", f"${eq_b.iloc[-1]:,.0f}", f"${eq_h.iloc[-1]:,.0f}"),
        ("Total costs", "$0 (costless)", f"${bstats0['total_costs']:,.0f}",
         f"${hstats['total_costs'] + hstats['total_borrow']:,.0f} (incl. borrow)"),
        ("Window", f"{ra.index.min().date()}..{ra.index.max().date()}",
         f"{rb.index.min().date()}..{rb.index.max().date()}",
         f"{rh.index.min().date()}..{rh.index.max().date()}"),
    ]
    print("\nCOMPARISON — level A (ideal) | prior level B (all-futures $50k) | HYBRID $50k:")
    for row in comp:
        print(f"  {row[0]:26s} A {row[1]:>16s} | B {row[2]:>16s} | H {row[3]:>16s}")
    print(f"  GAP CLOSURE (Sharpe): hybrid closes {gc_sharpe:.0f}% of the A-to-B gap"
          f"  [A {xa['full_sharpe']:.2f}, B {xb['full_sharpe']:.2f}, H {xh['full_sharpe']:.2f}]")
    print(f"  GAP CLOSURE (OOS Sharpe): {gc_oos:.0f}%"
          f"  [A {xa['oos_sharpe']:.2f}, B {xb['oos_sharpe']:.2f}, H {xh['oos_sharpe']:.2f}]")

    # ── per-year + era + SPY correlation ──
    spy = fetch("SPY", auto_adjust=True)
    spy_r = spy.pct_change().dropna()
    by_a, by_b, by_h = per_year_sharpe(ra), per_year_sharpe(rb), per_year_sharpe(rh)
    years = sorted(set(by_a) | set(by_b) | set(by_h))
    rows_csv = []
    print("\nPER-YEAR (Sharpe/ann-ret: A | B | HYBRID; hybrid flips; hybrid corr vs SPY):")
    for y in years:
        a_y, b_y, h_y = ra[ra.index.year == y], rb[rb.index.year == y], rh[rh.index.year == y]
        s = spy_r[spy_r.index.year == y]
        j = h_y.align(s, join="inner")
        corr = float(j[0].corr(j[1])) if len(j[0]) > 30 else float("nan")
        fl = hstats["yearly_flips"].get(y, 0)
        rows_csv.append({
            "year": y,
            "lvlA_sharpe": round(by_a.get(y, float("nan")), 2), "lvlA_ret": round(ann_ret(a_y), 4),
            "lvlB_sharpe": round(by_b.get(y, float("nan")), 2), "lvlB_ret": round(ann_ret(b_y), 4),
            "hybrid_sharpe": round(by_h.get(y, float("nan")), 2), "hybrid_ret": round(ann_ret(h_y), 4),
            "hybrid_flips": fl, "hybrid_corr_spy": round(corr, 2)})
        print(f"  {y}: A {by_a.get(y, float('nan')):6.2f}/{ann_ret(a_y):8.2%}"
              f" | B {by_b.get(y, float('nan')):6.2f}/{ann_ret(b_y):8.2%}"
              f" | H {by_h.get(y, float('nan')):6.2f}/{ann_ret(h_y):8.2%}"
              f" | flips {fl:3d} | corrSPY {corr:5.2f}")
    pd.DataFrame(rows_csv).to_csv(OUT / f"{TAG}.csv", index=False)

    print("\nERA BREAKDOWN (hybrid $50k):")
    era_lines = []
    for name, (y0, y1) in ERAS.items():
        e = era_slice(rh, y0, y1)
        if len(e) < 60:
            era_lines.append((name, "insufficient data"))
            continue
        eqe = (1 + e).cumprod()
        line = f"Sharpe {ann_sharpe(e):5.2f}  ann_ret {ann_ret(e):7.2%}  maxDD {max_dd(eqe):6.1%}"
        era_lines.append((name, line))
        print(f"  {name:22s} {line}")

    j = rh.align(spy_r, join="inner")
    full_corr = float(j[0].corr(j[1]))
    beta = float(np.cov(j[0], j[1])[0, 1] / np.var(j[1]))
    print(f"\nDIVERSIFICATION: corr(hybrid daily, SPY daily) = {full_corr:.3f}, "
          f"beta = {beta:.3f} over {len(j[0])} overlapping days")

    write_report(legs, depth, cper_note, ra, eq_a, rb, eq_b, bstats0, rh, eq_h, hstats,
                 rows_h, rows_b, rows_a, xh, xb, xa, strict_pass, relaxed,
                 gc_sharpe, gc_oos, comp, rows_csv, era_lines, full_corr, beta)
    print(f"\nOutputs: {OUT / (TAG + '.md')}, .log, .csv ; ETF caches extended in {CACHE}")
    sys.stdout = sys.__stdout__
    tee.f.close()


def write_report(legs, depth, cper_note, ra, eq_a, rb, eq_b, bstats0, rh, eq_h, hstats,
                 rows_h, rows_b, rows_a, xh, xb, xa, strict_pass, relaxed,
                 gc_sharpe, gc_oos, comp, rows_csv, era_lines, full_corr, beta):
    L = []
    A = L.append
    A("# HYBRID TREND BOOK — pre-registered follow-up (b) to the futures TSMOM pass — 2026-07-06\n")
    A("RESEARCH-ONLY. Frozen design: SAME signal (TSMOM 252d sign, flips only), SAME"
      " 12% portfolio vol target with equal-risk (12%/N_active)/ann_vol_20d budgeting,"
      " SAME window treatment (max common history), SAME revised Stage-6 gate as"
      " futures_trend_20260706. The ONLY change is the instrument map. One design, no"
      " variants, numbers as they fall. No promoter call; validated_strategies.yaml"
      " untouched.\n")
    A("## Instrument map (the only change)\n")
    A("| leg | prior instrument | hybrid instrument | kind | cost model |")
    A("|---|---|---|---|---|")
    prior_map = {m.code: m.trade_as for m in MARKETS}
    for lg in legs:
        cost = (f"$1.50 + 1 tick/side/contract" if lg.kind == "fut"
                else "$0 comm + $0.01 half-spread/share/side; integer shares")
        A(f"| {lg.code} | {prior_map[lg.code]} | {lg.trade_as} | {lg.kind} | {cost} |")
    A("| NG | QG(e-mini) | **DROPPED** | — | pre-registered: no micro, no clean ETF"
      " (UNG roll decay disqualifies it) |")
    if not any(lg.ticker == "CPER" for lg in legs):
        A("| HG | MHG | **DROPPED** | — | CPER history insufficient |")
    A("")
    A(f"- {cper_note}")
    A("- ETF shorts assumed shortable at **0.3%/yr borrow** on short notional (stated"
      " assumption; IEF/SHY/TLT/GLD/SLV" +
      ("/CPER" if any(lg.ticker == "CPER" for lg in legs) else "") +
      " are large, general-collateral ETFs).")
    A("- ETF series are DIVIDEND-ADJUSTED closes (yfinance auto_adjust): total-return"
      " is the honest basis for long legs; for SHORT legs the short pays the dividend"
      " — using the adjusted series symmetrically approximates that drag (documented"
      " approximation).")
    A("- Futures legs reuse the prior study's cached Yahoo front-month splices; all"
      " data caveats from futures_trend_20260706.md carry over for those legs.\n")
    A("## Headline results\n")
    A(f"- LEVEL A (fractional ideal, costless, prior study reproduced): Sharpe"
      f" {ann_sharpe(ra):.2f}, ann ret {ann_ret(ra):.2%}, maxDD {max_dd(eq_a):.1%}")
    A(f"- LEVEL B (all-futures $50k, prior study reproduced): Sharpe {ann_sharpe(rb):.2f},"
      f" ann ret {ann_ret(rb):.2%}, maxDD {max_dd(eq_b):.1%}, final ${eq_b.iloc[-1]:,.0f}")
    A(f"- **HYBRID $50k (THE DELIVERABLE): Sharpe {ann_sharpe(rh):.2f}, ann ret"
      f" {ann_ret(rh):.2%}, maxDD {max_dd(eq_h):.1%}, final equity ${eq_h.iloc[-1]:,.0f}"
      f" from $50k**, costs ${hstats['total_costs']:,.0f} (futures"
      f" ${hstats['fut_costs']:,.0f} + ETF half-spread ${hstats['etf_costs']:,.0f}),"
      f" ETF short borrow ${hstats['total_borrow']:,.0f}, sign-flips"
      f" {hstats['total_flips']}, window {rh.index.min().date()}..{rh.index.max().date()}\n")
    A("## MANDATORY comparison — A (ideal) vs B (all-futures $50k) vs HYBRID $50k\n")
    A("| metric | level A (ideal) | level B (futures $50k) | HYBRID $50k |")
    A("|---|---|---|---|")
    for row in comp:
        A(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} |")
    A("")
    A(f"**GAP CLOSURE: the hybrid closes {gc_sharpe:.0f}% of the A-to-B full-period"
      f" Sharpe gap** (A {xa['full_sharpe']:.2f} / B {xb['full_sharpe']:.2f} / H"
      f" {xh['full_sharpe']:.2f}); on OOS Sharpe it closes {gc_oos:.0f}%"
      f" (A {xa['oos_sharpe']:.2f} / B {xb['oos_sharpe']:.2f} / H {xh['oos_sharpe']:.2f}).\n")
    A("## Stage-6 gate — HYBRID $50k (deliverable)\n")
    A("| criterion | verdict | actual |")
    A("|---|---|---|")
    for name, ok, val in rows_h:
        A(f"| {name} | {'PASS' if ok else 'FAIL'} | {val} |")
    A(f"\n**STRICT GATE VERDICT (hybrid $50k): {'PASS' if strict_pass else 'FAIL'}**\n")
    A(f"> {GATE_DESIGN_FLAG}\n")
    A(f"Relaxed-worst-year information line: with worst-year relaxed to >= -0.5 the"
      f" strategy would **{'PASS' if relaxed else 'FAIL'}** (worst year Sharpe"
      f" {xh['worst_year']:.2f}). This is information for a human gate-design"
      f" discussion, not a pass claim.\n")
    A("Reference gates: LEVEL B (prior): " + "; ".join(
        f"{n}={'PASS' if ok else 'FAIL'}({v})" for n, ok, v in rows_b))
    A("LEVEL A (prior): " + "; ".join(
        f"{n}={'PASS' if ok else 'FAIL'}({v})" for n, ok, v in rows_a) + "\n")
    A("## Per-leg dropout (fraction of active days sized to 0 units at $50k)\n")
    A("| leg | instrument | kind | first active | dropout |")
    A("|---|---|---|---|---|")
    for lg in legs:
        f = hstats["dropped_frac"][lg.code]
        A(f"| {lg.code} | {lg.trade_as} | {lg.kind} |"
          f" {hstats['first_active'][lg.code]} | {f:.1%}"
          f"{' — drops out' if np.isfinite(f) and f > 0.5 else ''} |")
    etf_drop = [hstats["dropped_frac"][lg.code] for lg in legs if lg.kind == "etf"]
    A("")
    A(f"ETF-leg dropout verification (pre-registered expectation ~zero): max ETF-leg"
      f" dropout = {max(etf_drop):.2%}. Share-level granularity restores the legs the"
      f" all-futures book could not hold.\n")
    A("## Per-year table (A | B | HYBRID)\n")
    A("| year | A Sharpe | A ret | B Sharpe | B ret | H Sharpe | H ret | H flips | H corr SPY |")
    A("|---|---|---|---|---|---|---|---|---|")
    for r in rows_csv:
        A(f"| {r['year']} | {r['lvlA_sharpe']} | {r['lvlA_ret']:.2%} | {r['lvlB_sharpe']} |"
          f" {r['lvlB_ret']:.2%} | {r['hybrid_sharpe']} | {r['hybrid_ret']:.2%} |"
          f" {r['hybrid_flips']} | {r['hybrid_corr_spy']} |")
    A("")
    A("## Era breakdown (hybrid $50k)\n")
    for name, line in era_lines:
        A(f"- {name}: {line}")
    A("")
    A("## Diversification claim, quantified\n")
    A(f"corr(hybrid daily returns, SPY daily returns) = **{full_corr:.3f}**"
      f" (beta {beta:.3f}).\n")
    A("## Honest caveats and assumptions\n")
    A(f"- CAPITAL FEASIBILITY (important — this book is NOT implementable as-sized):"
      f" ETF legs consume real notional, unlike futures margin. Gross ETF notional:"
      f" max ${hstats['max_etf_gross']:,.0f}, mean ${hstats['mean_etf_gross']:,.0f};"
      f" max gross-ETF-notional/equity = {hstats['max_etf_lev']:.1f}x on"
      f" {hstats['max_etf_lev_date']}, dominated by SHY (alone max"
      f" ${hstats['max_shy_gross']:,.0f}): vol-targeting a leg with at-times <1.5%"
      f" ann vol produces enormous share positions. Reg-T caps overnight gross at"
      f" ~2x equity, so the realized SHY (and at times IEF) positions exceed what a"
      f" $50k cash account can hold, and MARGIN INTEREST ON NOTIONAL ABOVE CASH IS"
      f" NOT MODELED (the frozen cost model priced commissions, half-spread, and"
      f" short borrow only). The gap-closure result is therefore a GRANULARITY"
      f" finding (shares fix quantization dropout), not a claim that this exact"
      f" book fits in a $50k Reg-T account; a portfolio-margin account or a"
      f" leverage cap would be needed, and the latter is a design change outside"
      f" this pre-registration.")
    A("- ETF short legs: assumed shortable at 0.3%/yr general-collateral borrow —"
      " realistic for IEF/SHY/TLT/GLD/SLV; short dividend payments approximated via"
      " the dividend-adjusted series (symmetric treatment).")
    A("- Futures-leg data remains unadjusted front-month splices (roll-day error in"
      " signal and P&L, documented in the prior study). ETF legs are clean"
      " total-return series — one reason ETF-leg results are more trustworthy than"
      " the futures-leg proxies they replace.")
    A("- ETF legs enter the book only once the ETF has 252 trading days of history"
      " (first-active dates in the dropout table): the early-2000s hybrid book is"
      " less diversified than the futures paper book; comparisons over the full"
      " window embed that.")
    A("- Duration mismatch: IEF (7-10y) / SHY (1-3y) / TLT (20y+) are cash-bond"
      " proxies for ZN / ZF / ZB futures — similar but not identical exposures"
      " (carry and roll-down differ).")
    A("- No collateral interest credited on futures margin or free cash; Sharpe on"
      " raw equity changes, consistent with project conventions.")
    A("- MC5 bootstraps DAILY portfolio returns per monte_carlo.py bootstrap_sharpe;"
      " the per-trade convention does not map to a continuously-held book.")
    (OUT / f"{TAG}.md").write_text("\n".join(L))


if __name__ == "__main__":
    main()
