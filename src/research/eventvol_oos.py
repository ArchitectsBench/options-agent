"""EVENT-VOL OOS CONFIRMATION — pre-registered follow-up to study B1 — 2026-07-05.

This is the test that decides whether the B1 rich-tail result
(+0.813%/spot at BID/ASK, logs/research/eventvol_study_20260705.md) was real.

THE FLAW BEING KILLED: B1 ranked each event's implied move against the name's
FULL-SAMPLE event distribution — look-ahead. Its rich-tail P&L is therefore an
upper bound. This script re-selects the rich tail with NO look-ahead and
reports the number as it falls.

PRE-REGISTERED DESIGN (frozen before any result was computed; no variants,
no tuning):

1. Same 528 events, same short-iron-fly structure, same MID and BID/ASK fill
   scenarios, same P->C convention as B1. Detection + pricing machinery is
   IMPORTED from src/research/eventvol_study.py (unmodified); the re-run is
   verified event-for-event against B1's audit dump in
   logs/research/eventvol_20260705.log before any selection is applied.

2. SELECTION RULE A (primary): an event is 'rich' iff its implied move is
   >= the 90th percentile (np.quantile, default linear interpolation) of THAT
   NAME'S PRIOR events only — expanding window, strictly earlier p_dates,
   minimum 8 prior events required. Names/dates without 8 priors are simply
   not tradeable yet. The tradeable sample therefore starts later and is
   smaller — reported exactly.

3. SELECTION RULE B (secondary, corroboration only): implied move >= 7.5%
   absolute. One pre-stated round number near B1's top-decile median; not
   searched.

4. VERDICT RULE (pre-committed): the edge is REAL only if rule A's tradeable
   set has n >= 30 priced fly trades AND mean P&L/spot > 0 at BID/ASK AND
   total BID/ASK P&L is positive in >= 60% of calendar years having >= 3
   rule-A trades. Rule B is corroboration, not rescue — if A fails and B
   passes, the verdict is NOT CONFIRMED with a note.

5. Also reported: equity curve of rule A trades (BID/ASK, time order), worst
   single event and worst peak-to-trough drawdown, and per-name concentration
   (P&L after removing the top-contributing name).

Usage:
  nice -n 19 .venv/bin/python src/research/eventvol_oos.py

STRICT OWNERSHIP: this file + logs/research/eventvol_oos_20260705.{md,log}
only. Reads data/iv_history_full and logs/research/eventvol_20260705.log.
Modifies nothing existing.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eventvol_study as base  # noqa: E402  (detection + pricing machinery, unmodified)

LOGDIR = base.LOGDIR
OUT_MD = LOGDIR / "eventvol_oos_20260705.md"
LOG_PATH = LOGDIR / "eventvol_oos_20260705.log"

# ---------------- pre-registered parameters (frozen, not searched) ----------
MIN_PRIORS = 8            # rule A warm-up: events with < 8 priors untradeable
A_PCT = 0.90              # rule A percentile of PRIOR implied moves
B_ABS = 0.075             # rule B absolute implied-move threshold (7.5%)
V_MIN_N = 30              # verdict: min priced rule-A trades
V_YR_MIN = 3              # verdict: a year counts if it has >= 3 rule-A trades
V_YR_FRAC = 0.60          # verdict: >= 60% of counted years positive at BID/ASK
# -----------------------------------------------------------------------------

_logf = None


def log(msg: str):
    global _logf
    if _logf is None:
        LOGDIR.mkdir(parents=True, exist_ok=True)
        _logf = open(LOG_PATH, "a")
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _logf.write(line + "\n")
    _logf.flush()


# ---------------- verification against B1's audit dump ----------------------
def load_b1_dump() -> pd.DataFrame:
    """Parse the per-event audit lines B1 wrote to its log."""
    rows = []
    for line in open(base.LOG_PATH):
        if "] EV " not in line:
            continue
        f = line.split("] EV ", 1)[1].strip().split(",")
        rows.append(dict(ticker=f[0], p_date=pd.Timestamp(f[1]),
                         implied=float(f[6]), impl_pct=float(f[9]),
                         fly_mid=float(f[11]), fly_ba=float(f[12])))
    return pd.DataFrame(rows)


def verify_rerun(ev: pd.DataFrame, dump: pd.DataFrame) -> bool:
    """Event set and fly P&L must match B1's dump (within its 4-dp rounding)."""
    a = set(zip(ev["ticker"], ev["p_date"]))
    b = set(zip(dump["ticker"], dump["p_date"]))
    if a != b:
        log(f"VERIFY FAIL: event sets differ (rerun {len(a)}, dump {len(b)}; "
            f"only-rerun {sorted(a - b)[:5]}, only-dump {sorted(b - a)[:5]})")
        return False
    m = ev.merge(dump, on=["ticker", "p_date"], suffixes=("", "_d"))
    bad = 0
    for col in ("implied", "fly_mid", "fly_ba"):
        x, y = m[col], m[f"{col}_d"]
        both = x.notna() & (y == y)
        nan_mismatch = int((x.isna() != (y != y)).sum())
        off = int((abs(x[both] - y[both]) > 5e-4).sum())
        if nan_mismatch or off:
            log(f"VERIFY FAIL: {col}: {off} values off by >5e-4, "
                f"{nan_mismatch} NaN mismatches")
            bad += 1
    if not bad:
        log(f"VERIFY OK: {len(m)} events match B1 dump on (ticker, p_date), "
            f"implied, fly_mid, fly_ba (4-dp tolerance)")
    return bad == 0


# ---------------- selection rules (frozen) -----------------------------------
def apply_rules(ev: pd.DataFrame) -> pd.DataFrame:
    """Attach n_prior, rule-A threshold/selection, rule-B selection."""
    ev = ev.sort_values(["ticker", "p_date"]).reset_index(drop=True)
    n_prior, thr_a, sel_a = [], [], []
    for _, g in ev.groupby("ticker", sort=False):
        imp = g["implied"].to_numpy()
        for i in range(len(g)):
            n_prior.append(i)
            if i >= MIN_PRIORS:
                t = float(np.quantile(imp[:i], A_PCT))
                thr_a.append(t)
                sel_a.append(bool(imp[i] >= t))
            else:
                thr_a.append(np.nan)
                sel_a.append(False)
    ev["n_prior"] = n_prior
    ev["thr_a"] = thr_a
    ev["sel_a"] = sel_a
    ev["sel_b"] = ev["implied"] >= B_ABS
    # B1's original full-sample (look-ahead) percentile, for the accounting
    ev["impl_pct_full"] = ev.groupby("ticker")["implied"].rank(pct=True)
    ev["orig_rich"] = ev["impl_pct_full"] >= base.P_RICH_PCT
    return ev


# ---------------- reporting helpers ------------------------------------------
def pnl_line(sub: pd.DataFrame, col: str) -> str:
    return base.fmt_pnl(base.pnl_block(sub, col))


def year_rows(sub: pd.DataFrame) -> list[str]:
    rows = []
    ym = base.yearly_table(sub, "fly_mid")
    yb = base.yearly_table(sub, "fly_ba")
    for yr in sorted(set(ym.index) | set(yb.index)):
        m = ym.loc[yr] if yr in ym.index else None
        b = yb.loc[yr] if yr in yb.index else None
        rows.append(
            f"| {yr} | {int(b['n']) if b is not None else 0} | "
            + (f"{m['mean']:+.3f}% | {m['total']:+.2f}% | " if m is not None else "— | — | ")
            + (f"{b['mean']:+.3f}% | {b['total']:+.2f}% |" if b is not None else "— | — |"))
    return rows


def main():
    log(f"START event-vol OOS confirmation | frozen params: min_priors={MIN_PRIORS}, "
        f"A pct={A_PCT} (np.quantile linear, prior events only), B abs>={B_ABS:.1%}, "
        f"verdict n>={V_MIN_N} & BA mean>0 & >={V_YR_FRAC:.0%} of years "
        f"(years with >={V_YR_MIN} trades) positive at BA")

    # ---- re-run B1 machinery (imported, unmodified) -------------------------
    all_ev = []
    for tk in base.TICKERS:
        t0 = time.time()
        try:
            df = base.load_ticker(tk)
        except FileNotFoundError:
            log(f"{tk}: NO DATA — skipped")
            continue
        e = base.detect_events(tk, df)
        if len(e):
            e = base.price_events(tk, df, e)
            all_ev.append(e)
        log(f"{tk}: {len(e)} events ({time.time()-t0:.1f}s)")
        del df
    ev = pd.concat(all_ev, ignore_index=True)
    log(f"TOTAL events re-detected: {len(ev)}")

    dump = load_b1_dump()
    verified = verify_rerun(ev, dump)

    ev = apply_rules(ev)
    eligible = ev[ev["n_prior"] >= MIN_PRIORS]
    A = ev[ev["sel_a"]].sort_values("p_date")
    B = ev[ev["sel_b"]].sort_values("p_date")
    A_priced = A.dropna(subset=["fly_ba"])
    B_priced = B.dropna(subset=["fly_ba"])
    orig = ev[ev["orig_rich"]]

    # ---- tradeable-sample accounting ----------------------------------------
    o_warm = orig[orig["n_prior"] < MIN_PRIORS]
    o_elig = orig[orig["n_prior"] >= MIN_PRIORS]
    o_pass = o_elig[o_elig["sel_a"]]
    a_new = A[~A["orig_rich"]]

    # ---- verdict inputs ------------------------------------------------------
    n_a = len(A_priced)
    ba_mean = A_priced["fly_ba"].mean() if n_a else float("nan")
    yr_tot = A_priced.groupby("year")["fly_ba"].agg(n="count", total="sum")
    counted = yr_tot[yr_tot["n"] >= V_YR_MIN]
    pos_frac = float((counted["total"] > 0).mean()) if len(counted) else float("nan")
    c1 = n_a >= V_MIN_N
    c2 = n_a > 0 and ba_mean > 0
    c3 = len(counted) > 0 and pos_frac >= V_YR_FRAC
    real = c1 and c2 and c3

    b_ba_mean = B_priced["fly_ba"].mean() if len(B_priced) else float("nan")
    b_passes = len(B_priced) >= V_MIN_N and b_ba_mean > 0

    # ---- equity curve / drawdown / concentration (rule A, BID/ASK) ----------
    eq = A_priced.sort_values(["p_date", "ticker"]).reset_index(drop=True)
    eq["cum_ba"] = eq["fly_ba"].cumsum()
    eq["peak"] = eq["cum_ba"].cummax()
    eq["dd"] = eq["peak"] - eq["cum_ba"]
    if len(eq):
        i_tr = int(eq["dd"].idxmax())
        max_dd = float(eq.loc[i_tr, "dd"])
        i_pk = int(eq.loc[:i_tr, "cum_ba"].idxmax())
        i_worst = int(eq["fly_ba"].idxmin())
    byname = (eq.groupby("ticker")
                .agg(n=("fly_ba", "size"), mid_sum=("fly_mid", "sum"),
                     ba_sum=("fly_ba", "sum"))
                .sort_values("ba_sum", ascending=False)) if len(eq) else pd.DataFrame()
    if len(byname):
        top = byname.index[0]
        rest = eq[eq["ticker"] != top]

    # ---- audit dump ----------------------------------------------------------
    log("rule-A trade dump: ticker,p_date,n_prior,implied,thr_a,orig_rich,"
        "fly_mid,fly_ba")
    for _, e in A.iterrows():
        log(f"A {e['ticker']},{e['p_date'].date()},{int(e['n_prior'])},"
            f"{e['implied']:.4f},{e['thr_a']:.4f},{bool(e['orig_rich'])},"
            f"{e.get('fly_mid', float('nan')):.4f},{e.get('fly_ba', float('nan')):.4f}")
    log(f"rule-B selected: {len(B)} events, {len(B_priced)} priced")

    # ---- report --------------------------------------------------------------
    W = []
    W.append("# Event-Vol OOS Confirmation — pre-registered follow-up to B1 — 2026-07-05\n")
    W.append("Decides whether B1's rich-tail result "
             "(`logs/research/eventvol_study_20260705.md`: BID/ASK mean "
             "+0.813%/spot, n=64) survives removal of its look-ahead: B1 "
             "ranked each event's implied move against the name's FULL-SAMPLE "
             "distribution. Design, both selection rules, and the verdict "
             "rule were frozen in `src/research/eventvol_oos.py` BEFORE any "
             "result was computed. No variants were run, nothing was tuned. "
             "This test existed to be allowed to fail.\n")

    W.append("## Method (frozen)\n")
    W.append("- Same events, structures, fills, P→C convention as B1: "
             "detection + pricing imported unmodified from "
             "`src/research/eventvol_study.py`; short iron fly at P close "
             "(sell ATM straddle, wings at 1.0× implied), closed at C close; "
             "fills at MID and BID/ASK; P&L in % of spot(P).")
    W.append(f"- Re-run verification against B1's audit dump: "
             f"**{'PASSED' if verified else 'FAILED — results below suspect'}** "
             f"({len(ev)} events; implied, fly_mid, fly_ba matched to B1's "
             "4-dp log precision).")
    W.append(f"- **Rule A (primary, no look-ahead):** rich iff implied move ≥ "
             f"90th percentile (np.quantile, linear) of that name's PRIOR "
             f"events only; expanding window; minimum {MIN_PRIORS} prior "
             f"events (else not tradeable yet).")
    W.append(f"- **Rule B (secondary, corroboration only):** implied move ≥ "
             f"{B_ABS:.1%} absolute — one pre-stated number, not searched.")
    W.append(f"- **Verdict rule (pre-committed):** REAL only if rule A has "
             f"n ≥ {V_MIN_N} priced trades AND mean P&L/spot > 0 at BID/ASK "
             f"AND total BID/ASK P&L positive in ≥ {V_YR_FRAC:.0%} of "
             f"calendar years having ≥ {V_YR_MIN} rule-A trades. Rule B "
             f"cannot rescue a rule-A failure.\n")

    W.append("## Tradeable-sample accounting\n")
    W.append(f"- All events: {len(ev)}. Events with ≥ {MIN_PRIORS} priors "
             f"(rule-A eligible): {len(eligible)} "
             f"({len(eligible)/len(ev)*100:.0f}% — the warm-up removes "
             f"{len(ev)-len(eligible)} events; first eligible event "
             f"{eligible['p_date'].min().date() if len(eligible) else '—'}).")
    W.append(f"- **Rule A selects {len(A)} events** ({len(A_priced)} with a "
             f"priced fly at BID/ASK, {len(A) - len(A_priced)} unpriceable) "
             f"vs B1's look-ahead tail of {len(orig)}.")
    W.append(f"- Of B1's {len(orig)} original tail events: "
             f"**{len(o_pass)} survive rule A**; {len(o_warm)} fall in the "
             f"warm-up (< {MIN_PRIORS} priors, never tradeable); "
             f"{len(o_elig) - len(o_pass)} were eligible but fail the "
             f"prior-only threshold. Rule A also picks up {len(a_new)} events "
             f"B1's full-sample rank did NOT call rich (rich vs history-to-"
             f"date, not vs future).")
    W.append(f"- Rule B (≥ {B_ABS:.1%} absolute) selects {len(B)} events "
             f"({len(B_priced)} priced), no warm-up needed.\n")

    W.append("## Rule A (primary) — short iron fly P&L\n")
    W.append(f"- MID:     {pnl_line(A, 'fly_mid')}")
    W.append(f"- BID/ASK: {pnl_line(A, 'fly_ba')}\n")
    W.append("Per-year:\n\n| year | n | mean MID | total MID | mean BA | "
             "total BA |\n|---|---|---|---|---|---|")
    W += year_rows(A)
    exm, exyr = base.excl_best_year_mean(A, "fly_ba")
    W.append(f"\nBID/ASK mean excluding best year ({exyr}): {exm:+.3f}% "
             "(diagnostic, not part of the verdict rule)\n")

    W.append("## Rule B (secondary) — short iron fly P&L\n")
    W.append(f"- MID:     {pnl_line(B, 'fly_mid')}")
    W.append(f"- BID/ASK: {pnl_line(B, 'fly_ba')}\n")
    W.append("Per-year:\n\n| year | n | mean MID | total MID | mean BA | "
             "total BA |\n|---|---|---|---|---|---|")
    W += year_rows(B)
    W.append("")

    W.append("## Rule A equity curve (BID/ASK, time order)\n")
    W.append("| # | p_date | ticker | implied | thr_A | P&L BA | cum BA |\n"
             "|---|---|---|---|---|---|---|")
    for i, e in eq.iterrows():
        W.append(f"| {i+1} | {e['p_date'].date()} | {e['ticker']} | "
                 f"{e['implied']*100:.2f}% | {e['thr_a']*100:.2f}% | "
                 f"{e['fly_ba']:+.3f}% | {e['cum_ba']:+.3f}% |")
    if len(eq):
        W.append(f"\n- Worst single event: {eq.loc[i_worst,'ticker']} "
                 f"{eq.loc[i_worst,'p_date'].date()} "
                 f"({eq.loc[i_worst,'fly_ba']:+.3f}% at BID/ASK).")
        W.append(f"- Worst peak-to-trough drawdown of the cumulative curve: "
                 f"{max_dd:.3f} pct-pts of spot, from "
                 f"{eq.loc[i_pk,'p_date'].date()} (peak "
                 f"{eq.loc[i_pk,'cum_ba']:+.3f}%) to "
                 f"{eq.loc[i_tr,'p_date'].date()} (trough "
                 f"{eq.loc[i_tr,'cum_ba']:+.3f}%).\n")

    W.append("## Per-name concentration (rule A)\n")
    W.append("| ticker | n | total MID | total BA |\n|---|---|---|---|")
    for tk, r in byname.iterrows():
        W.append(f"| {tk} | {int(r['n'])} | {r['mid_sum']:+.2f}% | "
                 f"{r['ba_sum']:+.2f}% |")
    if len(byname):
        W.append(f"\nExcluding the top contributor ({top}): n={len(rest)}, "
                 f"MID mean {rest['fly_mid'].mean():+.3f}%, BID/ASK mean "
                 f"{rest['fly_ba'].mean():+.3f}%.\n")

    W.append("## Pre-committed verdict\n")
    W.append(f"- Condition 1 — rule A priced n ≥ {V_MIN_N}: "
             f"**{'YES' if c1 else 'NO'}** (n={n_a}).")
    W.append(f"- Condition 2 — rule A mean P&L/spot > 0 at BID/ASK: "
             f"**{'YES' if c2 else 'NO'}** (mean {ba_mean:+.3f}%).")
    W.append(f"- Condition 3 — positive total BA in ≥ {V_YR_FRAC:.0%} of "
             f"years with ≥ {V_YR_MIN} trades: **{'YES' if c3 else 'NO'}** "
             f"({int((counted['total'] > 0).sum()) if len(counted) else 0}"
             f"/{len(counted)} years = "
             f"{pos_frac*100 if pos_frac==pos_frac else float('nan'):.0f}%).")
    W.append(f"- Rule B corroboration: n={len(B_priced)}, BID/ASK mean "
             f"{b_ba_mean:+.3f}% — {'positive' if b_passes else 'not positive'} "
             "(cannot rescue rule A).")
    if real:
        W.append("\n**VERDICT: REAL — the rich-tail edge survives no-look-"
                 "ahead selection. Proceed to Track A gates.**")
    else:
        note = (" Note: rule B passed, but per the pre-committed rule B is "
                "corroboration, not rescue." if (b_passes and not real) else "")
        W.append("\n**VERDICT: NOT CONFIRMED — the pre-committed rule A "
                 f"conditions were not all met.{note}**")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(W) + "\n")
    log(f"report written: {OUT_MD}")
    log(f"VERDICT: {'REAL' if real else 'NOT CONFIRMED'} | ruleA n={n_a} "
        f"BA mean {ba_mean:+.4f}% | years {int((counted['total']>0).sum()) if len(counted) else 0}"
        f"/{len(counted)} positive | ruleB n={len(B_priced)} BA mean {b_ba_mean:+.4f}%")
    log("DONE")


if __name__ == "__main__":
    sys.exit(main())
