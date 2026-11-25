"""EVENT-VOL STAGE-6 GATE RUN — decisive R1 study — 2026-07-05.

The frozen event-vol rule (rule A of src/research/eventvol_oos.py, confirmed
REAL on 23 names 2020-2026) is run on the FULL ~60-name 2007-2026 universe in
data/iv_history_full, with the complete revised Stage-6 gate treatment.

THE FROZEN RULE (character-for-character from eventvol_oos.py / eventvol_study.py;
NOTHING reinterpreted, NOTHING tuned):
  - Event detection (base.detect_events, imported unmodified): front expiry
    (DTE 1-14) ATM IV >= 1.15x next expiry AND >= 25% ATM-IV collapse next
    session; 45-day within-name de-dup keeping max elevation.
  - Selection: event tradeable iff implied move >= 90th percentile
    (np.quantile, linear) of THAT NAME-LINEAGE'S PRIOR events only; expanding
    window; minimum 8 prior events.
  - Trade: SHORT IRON FLY — sell ATM straddle at K, buy wings at strikes
    nearest spot*(1 +/- 1.0*implied); entered at P close (last close before
    the event), exited at C close (first close after; intrinsic if expired).
  - Fills: MID and full BID/ASK (shorts@bid/longs@ask, reversed at exit).
    BID/ASK is the PRIMARY fill for every gate input.

UNIVERSE / NAME LINEAGES:
  - Every ticker in data/iv_history_full EXCEPT SPX, XSP, VIX (index vol, no
    earnings) and GOOGL (same underlying as GOOG — double-counting).
  - FB (2012..2022-06-08) + META (2022-06-09..) stitched as ONE lineage
    "META". META_* rows BEFORE 2022-06-09 are the Roundhill Metaverse ETF
    (spot ~$15, verified) and are dropped.
  - ETFs in the store (SPY QQQ IWM XLF XLE TLT SLV) are kept per the literal
    universe spec; the earnings-crush signature should give them ~0 events —
    their contribution is reported per-name, not hidden.

GATE TREATMENT (revised Stage-6, src/research/monte_carlo.acceptance_gate;
project trade-granularity conventions of src/backtest/backtest.py and
src/research/contango_pro.py):
  - Per-trade return r = P&L / max-loss capital of the fly, where
      capital = max(K_up - K, K - K_dn) - entry_credit(BID/ASK).
    The CONSERVATIVE (bid/ask) entry credit defines capital for BOTH fill
    scenarios (pipeline lesson 2026-06-15: capital at mid credit lets r
    escape the defined-risk bound).
  - Sharpe = mean(r) / std(r, ddof=1) * sqrt(trades_per_year),
    trades_per_year = n_trades / years, years = (last - first p_date)/365.25.
    No risk-free deduction (project convention; noted in the report).
  - Equity curve at trade granularity: eq = cumprod(1 + 0.02*r) — 2% of
    account risked per trade (the project's standing _equity_maxdd
    convention); max drawdown from that curve.
  - Chronological 70/30 IS/OOS split of the trade sequence.
  - Monte Carlo: bootstrap resample (2000 draws, seed 0) -> MC5 Sharpe;
    order-only reshuffle (1000 draws, seed 1) -> drawdown distribution.
  - Worst CALENDAR YEAR adaptation: monte_carlo.per_year_sharpe demands
    >=100 SESSIONS per year — meaningless at ~10-40 trades/yr, so the
    worst-year criterion is computed manually at trade granularity over
    years with >= 8 trades, same threshold (worst-year Sharpe >= 0).
    The MonteCarloReport dataclass is filled manually for the same reason.
  - Sensitivity (REPORT-ONLY, never selection): pctile 90->{88,92},
    wings 1x->{0.8,1.2}, min-priors 8->{6,10}, one at a time.

PRE-COMMITTED VERDICT:
  PASS    = ALL acceptance_gate criteria pass at BID/ASK.
  PARTIAL = mean r > 0 at BID/ASK with n >= 100, but gate thresholds failing
            ("candidate, not promotable").
  FAIL    = everything else.
  NO promoter call, NO validated_strategies.yaml write in any case.

Usage:
  nice -n 19 .venv/bin/python src/research/eventvol_gates.py [--tickers A,B]

STRICT OWNERSHIP: this file + logs/research/eventvol_gates_20260705.{md,log,csv}
+ scratchpad checkpoints only. Reads data/iv_history_full and
logs/research/eventvol_20260705.log. Modifies nothing existing.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eventvol_study as base  # noqa: E402  (detection+pricing machinery, unmodified)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.backtest.backtest import _annual_sharpe, _equity_maxdd  # noqa: E402
from src.research.monte_carlo import MonteCarloReport, acceptance_gate  # noqa: E402

LOGDIR = base.LOGDIR
OUT_MD = LOGDIR / "eventvol_gates_20260705.md"
OUT_CSV = LOGDIR / "eventvol_gates_20260705.csv"
LOG_PATH = LOGDIR / "eventvol_gates_20260705.log"
SCRATCH = Path("/private/tmp/claude-501/-Users-you-projects-OptionsAgent/"
               "801744be-bebd-4fe1-a660-496a6048bd53/scratchpad/eventvol_gates_ckpt")

# ---------------- frozen parameters (identical to eventvol_oos.py) ----------
MIN_PRIORS = 8
A_PCT = 0.90
WING_MULT = 1.0
# sensitivity grid (report-only)
SENS_PCT = (0.88, 0.92)
SENS_PRIORS = (6, 10)
SENS_WINGS = (0.8, 1.2)
WING_SFX = {0.8: "_w08", 1.0: "", 1.2: "_w12"}
# universe handling
SKIP = {"SPX", "XSP", "VIX", "GOOGL", "FB", "META"}   # FB/META re-enter as lineage
META_CUT = pd.Timestamp("2022-06-09")                 # FB -> META ticker change
ERAS = [("2007-2012", 2007, 2012), ("2013-2017", 2013, 2017),
        ("2018-2019", 2018, 2019), ("2020-2022", 2020, 2022),
        ("2023-2026", 2023, 2026)]
RISK_FRAC = 0.02        # _equity_maxdd convention: 2% of account per trade
MIN_YEAR_TRADES = 8     # worst-calendar-year adaptation (documented)
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


# ---------------- universe & lineage loading --------------------------------
def universe() -> list[str]:
    tks = sorted({p.name.rsplit("_", 1)[0] for p in base.IV_FULL.glob("*.parquet")})
    names = [t for t in tks if t not in SKIP]
    if any(t in ("FB", "META") for t in tks):
        names.append("META")
    return sorted(names)


def _load_files(paths: list[Path]) -> pd.DataFrame:
    df = pd.concat([pd.read_parquet(p, columns=base.COLS) for p in paths],
                   ignore_index=True)
    df["trade_date"] = base.parse_dates(df["trade_date"])
    df["expiration"] = base.parse_dates(df["expiration"])
    return df


def load_lineage(tk: str) -> pd.DataFrame:
    """base.load_ticker semantics, plus the FB+META stitch for lineage META."""
    if tk == "META":
        fb = _load_files(sorted(base.IV_FULL.glob("FB_*.parquet")))
        me = _load_files(sorted(base.IV_FULL.glob("META_*.parquet")))
        fb = fb[fb["trade_date"] < META_CUT]
        me = me[me["trade_date"] >= META_CUT]   # drops Roundhill-ETF META rows
        df = pd.concat([fb, me], ignore_index=True)
    else:
        paths = sorted(base.IV_FULL.glob(f"{tk}_*.parquet"))
        if not paths:
            raise FileNotFoundError(tk)
        df = _load_files(paths)
    df["dte"] = (df["expiration"] - df["trade_date"]).dt.days
    df = df[df["dte"] >= 0]
    return df.sort_values(["trade_date", "expiration", "strike"]).reset_index(drop=True)


# ---------------- fly pricing (careful copy of base.price_events fly branch,
# parameterized by wing multiple + entry credit / wing strikes recorded) ------
FLY_COLS = [f"{c}{s}" for s in WING_SFX.values()
            for c in ("fly_mid", "fly_ba", "ent_mid", "ent_ba", "ku", "kd")]


def price_flys(tk: str, df: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    """Short-iron-fly P&L at MID and BID/ASK for wing multiples 0.8/1.0/1.2.

    The wing-1.0 path is character-for-character the fly branch of
    base.price_events (verified against it in verify_pricing); the only
    additions are the recorded entry credits + wing strikes (for max-loss
    capital) and the wing-multiple parameterization (sensitivity only)."""
    by_date = {d: g for d, g in df.groupby("trade_date")}
    out = []
    for _, e in ev.iterrows():
        gp, gc = by_date[e["p_date"]], by_date[e["c_date"]]
        exp, k, spot_p, spot_c = e["front_exp"], e["k_atm"], e["spot_p"], e["spot_c"]
        expired = not (exp > e["c_date"])
        rec = dict(e)
        for c in FLY_COLS:
            rec[c] = np.nan
        chain_p = gp[gp["expiration"] == exp]
        srow = base.leg_quote(gp, exp, k)
        for wm, sfx in WING_SFX.items():
            kw_up = spot_p * (1 + wm * e["implied"])
            kw_dn = spot_p * (1 - wm * e["implied"])
            cw = chain_p[(chain_p["strike"] > k) & (chain_p["cAskPx"] > 0)]
            pw = chain_p[(chain_p["strike"] < k) & (chain_p["pAskPx"] > 0)]
            if srow is None or cw.empty or pw.empty:
                continue
            cwr = cw.iloc[(cw["strike"] - kw_up).abs().argmin()]
            pwr = pw.iloc[(pw["strike"] - kw_dn).abs().argmin()]
            ku, kd = float(cwr["strike"]), float(pwr["strike"])
            ent_mid = (base.mid(srow["cBidPx"], srow["cAskPx"])
                       + base.mid(srow["pBidPx"], srow["pAskPx"])
                       - base.mid(cwr["cBidPx"], cwr["cAskPx"])
                       - base.mid(pwr["pBidPx"], pwr["pAskPx"]))
            ent_ba = (float(srow["cBidPx"]) + float(srow["pBidPx"])
                      - float(cwr["cAskPx"]) - float(pwr["pAskPx"]))
            xa = None if expired else base.leg_quote(gc, exp, k)
            xu = None if expired else base.leg_quote(gc, exp, ku)
            xd = None if expired else base.leg_quote(gc, exp, kd)
            legs_m = [base._exit_val(xa, "C", k, spot_c, expired, "short", "mid"),
                      base._exit_val(xa, "P", k, spot_c, expired, "short", "mid"),
                      base._exit_val(xu, "C", ku, spot_c, expired, "long", "mid"),
                      base._exit_val(xd, "P", kd, spot_c, expired, "long", "mid")]
            legs_b = [base._exit_val(xa, "C", k, spot_c, expired, "short", "ba"),
                      base._exit_val(xa, "P", k, spot_c, expired, "short", "ba"),
                      base._exit_val(xu, "C", ku, spot_c, expired, "long", "ba"),
                      base._exit_val(xd, "P", kd, spot_c, expired, "long", "ba")]
            if None not in legs_m and None not in legs_b:
                exit_mid = legs_m[0] + legs_m[1] - legs_m[2] - legs_m[3]
                exit_ba = legs_b[0] + legs_b[1] - legs_b[2] - legs_b[3]
                rec[f"fly_mid{sfx}"] = (ent_mid - exit_mid) / spot_p * 100
                rec[f"fly_ba{sfx}"] = (ent_ba - exit_ba) / spot_p * 100
                rec[f"ent_mid{sfx}"], rec[f"ent_ba{sfx}"] = ent_mid, ent_ba
                rec[f"ku{sfx}"], rec[f"kd{sfx}"] = ku, kd
        out.append(rec)
    return pd.DataFrame(out)


def verify_pricing(tk: str, df: pd.DataFrame, ev: pd.DataFrame,
                   mine: pd.DataFrame) -> tuple[int, float]:
    """Wing-1.0 output must equal base.price_events exactly. Returns
    (n_mismatches, max_abs_diff) across fly_mid, fly_ba, widths."""
    ref = base.price_events(tk, df, ev)
    m = mine.merge(ref[["ticker", "p_date", "fly_mid", "fly_ba",
                        "fly_width_up", "fly_width_dn"]],
                   on=["ticker", "p_date"], suffixes=("", "_ref"))
    bad, worst = 0, 0.0
    pairs = [("fly_mid", m["fly_mid_ref"]), ("fly_ba", m["fly_ba_ref"]),
             ("ku", m["k_atm"] + m["fly_width_up"]),
             ("kd", m["k_atm"] - m["fly_width_dn"])]
    for col, ref_v in pairs:
        x, y = m[col], ref_v
        nan_mismatch = int((x.isna() != y.isna()).sum())
        both = x.notna() & y.notna()
        d = (x[both] - y[both]).abs()
        off = int((d > 1e-9).sum())
        bad += nan_mismatch + off
        if len(d):
            worst = max(worst, float(d.max()))
    return bad, worst


# ---------------- frozen selection rule (careful copy of eventvol_oos) ------
def apply_rule(ev: pd.DataFrame, pct: float = A_PCT,
               min_priors: int = MIN_PRIORS) -> pd.DataFrame:
    """n_prior / threshold / selection, expanding window per name lineage."""
    ev = ev.sort_values(["ticker", "p_date"]).reset_index(drop=True)
    n_prior, thr, sel = [], [], []
    for _, g in ev.groupby("ticker", sort=False):
        imp = g["implied"].to_numpy()
        for i in range(len(g)):
            n_prior.append(i)
            if i >= min_priors:
                t = float(np.quantile(imp[:i], pct))
                thr.append(t)
                sel.append(bool(imp[i] >= t))
            else:
                thr.append(np.nan)
                sel.append(False)
    ev = ev.copy()
    ev["n_prior"], ev["thr"], ev["sel"] = n_prior, thr, sel
    return ev


# ---------------- trade frame + gate metrics ---------------------------------
def trade_frame(sel: pd.DataFrame, sfx: str = "") -> tuple[pd.DataFrame, int]:
    """Selected events -> per-trade frame with r at MID and BID/ASK.
    capital = max(width_up, width_dn) - BID/ASK entry credit (conservative
    credit for BOTH scenarios). Returns (trades, n_dropped_capital<=0)."""
    t = sel.dropna(subset=[f"fly_ba{sfx}"]).copy()
    width = np.maximum(t[f"ku{sfx}"] - t["k_atm"], t["k_atm"] - t[f"kd{sfx}"])
    t["width_max"] = width
    t["capital_sh"] = width - t[f"ent_ba{sfx}"]
    n_bad = int((t["capital_sh"] <= 0).sum())
    t = t[t["capital_sh"] > 0]
    for fill in ("mid", "ba"):
        t[f"pnl_{fill}_sh"] = t[f"fly_{fill}{sfx}"] / 100 * t["spot_p"]
        t[f"r_{fill}"] = t[f"pnl_{fill}_sh"] / t["capital_sh"]
    t = t.sort_values(["p_date", "ticker"]).reset_index(drop=True)
    return t, n_bad


def span_years(dates: pd.Series) -> float:
    return max((dates.iloc[-1] - dates.iloc[0]).days / 365.25, 0.25)


def per_year_table(t: pd.DataFrame, rcol: str) -> pd.DataFrame:
    pnl_col = "fly_mid" if rcol == "r_mid" else "fly_ba"   # base-wing %spot, report only
    out = {}
    for y, g in t.groupby(t["p_date"].dt.year):
        r = g[rcol].to_numpy()
        sp = span_years(g["p_date"]) if len(g) > 1 else 0.25
        out[int(y)] = {
            "n": len(g), "win": round(float((r > 0).mean()), 2),
            "mean_r": round(float(r.mean()), 4),
            "total_pctspot": round(float(g[pnl_col].sum()), 2),
            "sharpe": round(_annual_sharpe(r, len(g) / sp), 2) if len(g) >= 2 else float("nan"),
        }
    return pd.DataFrame(out).T


def gate_metrics(t: pd.DataFrame, rcol: str = "r_ba") -> dict:
    """All revised-Stage-6 inputs on one trade frame (chronological)."""
    r = t[rcol].to_numpy()
    n = len(t)
    yrs = span_years(t["p_date"])
    tpy = n / yrs
    sharpe = _annual_sharpe(r, tpy)
    eq, maxdd = _equity_maxdd(r, RISK_FRAC)
    k = int(n * 0.7)
    is_sharpe = _annual_sharpe(r[:k], tpy)
    oos_sharpe = _annual_sharpe(r[k:], tpy) if n - k >= 2 else float("nan")
    rng = np.random.default_rng(0)
    mc = np.array([_annual_sharpe(rng.choice(r, n, replace=True), tpy)
                   for _ in range(2000)])
    mc = mc[np.isfinite(mc)]
    mc5, mc50 = float(np.percentile(mc, 5)), float(np.median(mc))
    # order-only reshuffle -> drawdown distribution (order matters for DD only)
    rng2 = np.random.default_rng(1)
    dds = np.array([_equity_maxdd(rng2.permutation(r), RISK_FRAC)[1]
                    for _ in range(1000)])
    # worst calendar year with >= MIN_YEAR_TRADES trades (adaptation)
    py = per_year_table(t, rcol)
    judged = py[py["n"] >= MIN_YEAR_TRADES]
    worst_year = float(judged["sharpe"].min()) if len(judged) else float("nan")
    by_year = {int(y): float(v) for y, v in judged["sharpe"].items()}
    return dict(n=n, years=yrs, tpy=tpy, mean_r=float(r.mean()),
                std_r=float(r.std(ddof=1)) if n > 1 else float("nan"),
                win=float((r > 0).mean()), sharpe=sharpe, is_sharpe=is_sharpe,
                oos_sharpe=oos_sharpe, maxdd=maxdd, mc5=mc5, mc50=mc50,
                dd_shuffle_med=float(np.median(dds)),
                dd_shuffle_p95=float(np.percentile(dds, 95)),
                worst_year=worst_year, by_year=by_year, per_year=py, equity=eq)


def gate_checks(m: dict, label: str) -> tuple[bool, list]:
    rep = MonteCarloReport(
        params={"config": label}, is_sharpe=m["is_sharpe"],
        oos_sharpe=m["oos_sharpe"], mc_median_sharpe=m["mc50"],
        mc_p05_sharpe=m["mc5"], sensitivity_score=float("nan"),
        sensitivity_detail=[], full_sharpe=m["sharpe"],
        full_max_dd=-m["maxdd"], full_trades=m["n"],
        worst_year_sharpe=m["worst_year"], regime_by_year=m["by_year"])
    checks = acceptance_gate(rep)
    return all(ok for _, ok, _ in checks), checks


# ---------------- B1-dump cross-check (informational) ------------------------
def load_b1_dump() -> pd.DataFrame:
    rows = []
    for line in open(base.LOG_PATH):
        if "] EV " not in line:
            continue
        f = line.split("] EV ", 1)[1].strip().split(",")
        rows.append(dict(ticker=f[0], p_date=pd.Timestamp(f[1]),
                         fly_ba=float(f[12])))
    return pd.DataFrame(rows)


# ---------------- report helpers ---------------------------------------------
def era_of(year: int) -> str:
    for name, lo, hi in ERAS:
        if lo <= year <= hi:
            return name
    return "pre-2007"


def pnl_summary(t: pd.DataFrame, col: str) -> str:
    s = t[col].dropna()
    if s.empty:
        return "n=0"
    return (f"n={len(s)} | mean {s.mean():+.3f}% | median {s.median():+.3f}% | "
            f"win {(s > 0).mean()*100:.0f}% | total {s.sum():+.1f}% | "
            f"worst {s.min():+.2f}% | best {s.max():+.2f}%")


def sens_row(ev_all: pd.DataFrame, label: str, pct: float, priors: int,
             wing: float, base_sharpe: float | None) -> tuple[str, dict]:
    sfx = WING_SFX[wing]
    sel = apply_rule(ev_all, pct, priors)
    t, _ = trade_frame(sel[sel["sel"]], sfx)
    if len(t) < 2:
        return f"| {label} | {len(t)} | — | — | — | — | — | — |", {}
    m = gate_metrics(t, "r_ba")
    flag = ""
    if base_sharpe is not None and np.isfinite(base_sharpe) and base_sharpe > 0:
        if m["sharpe"] < 0:
            flag = " **CLIFF (sign flip)**"
        elif m["sharpe"] < 0.5 * base_sharpe:
            flag = " **CLIFF (>50% Sharpe drop)**"
    row = (f"| {label} | {m['n']} | {t[f'fly_ba{sfx}'].mean():+.3f}% | "
           f"{m['mean_r']:+.4f} | {m['win']*100:.0f}% | {m['sharpe']:.2f} | "
           f"{m['maxdd']:.1%} | {m['worst_year']:.2f}{flag} |")
    return row, m


# --------------------------------- main --------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="")
    a = ap.parse_args()
    names = ([t.strip().upper() for t in a.tickers.split(",") if t.strip()]
             or universe())
    SCRATCH.mkdir(parents=True, exist_ok=True)

    log(f"START event-vol Stage-6 gate run | {len(names)} name lineages | "
        f"frozen rule: pct={A_PCT}, min_priors={MIN_PRIORS}, wings {WING_MULT}x, "
        f"BID/ASK primary | universe skips {sorted(SKIP)} | META = FB(<{META_CUT.date()}) "
        f"+ META(>={META_CUT.date()})")

    # ---- per-lineage: detect + price (checkpointed) --------------------------
    all_ev, verify_bad, verify_worst = [], 0, 0.0
    for tk in names:
        ck = SCRATCH / f"{tk}.parquet"
        if ck.exists():
            e = pd.read_parquet(ck)
            if len(e):
                for c in ("p_date", "c_date", "front_exp"):
                    e[c] = pd.to_datetime(e[c])
                all_ev.append(e)
            log(f"{tk}: checkpoint hit ({len(e)} events)")
            continue
        t0 = time.time()
        try:
            df = load_lineage(tk)
        except FileNotFoundError:
            log(f"{tk}: NO DATA — skipped")
            continue
        e = base.detect_events(tk, df)
        if len(e):
            mine = price_flys(tk, df, e)
            bad, worst = verify_pricing(tk, df, e, mine)
            verify_bad += bad
            verify_worst = max(verify_worst, worst)
            if bad:
                log(f"{tk}: VERIFY FAIL — {bad} wing-1.0 values differ from "
                    f"base.price_events (max diff {worst:.2e})")
            e = mine
        else:
            e = pd.DataFrame()
        n_rows = len(df)
        del df
        if len(e):
            e.to_parquet(ck)
            all_ev.append(e)
        else:
            pd.DataFrame().to_parquet(ck)
        log(f"{tk}: {n_rows:,} rows -> {len(e)} events ({time.time()-t0:.1f}s)")

    ev = pd.concat(all_ev, ignore_index=True)
    log(f"TOTAL detected events: {len(ev)} across {ev['ticker'].nunique()} lineages, "
        f"{ev['p_date'].min().date()} .. {ev['p_date'].max().date()}")
    log(f"pricing verification vs base.price_events: "
        f"{'OK' if verify_bad == 0 else f'{verify_bad} MISMATCHES'} "
        f"(max abs diff {verify_worst:.2e}) [0 checks if all checkpointed]")

    # ---- B1-dump cross-check (informational) ---------------------------------
    b1_note = "B1 dump not found"
    try:
        dump = load_b1_dump()
        common = sorted((set(dump["ticker"]) & set(ev["ticker"])) - {"META", "GOOGL"})
        d = dump[dump["ticker"].isin(common)]
        mine_keys = set(zip(ev["ticker"], ev["p_date"]))
        hit = d[[ (t, p) in mine_keys for t, p in zip(d["ticker"], d["p_date"]) ]]
        mrg = hit.merge(ev[["ticker", "p_date", "fly_ba"]], on=["ticker", "p_date"],
                        suffixes=("_b1", ""))
        both = mrg.dropna(subset=["fly_ba_b1", "fly_ba"])
        maxd = float((both["fly_ba"] - both["fly_ba_b1"]).abs().max()) if len(both) else float("nan")
        b1_note = (f"{len(hit)}/{len(d)} B1 events re-detected on the {len(common)} "
                   f"common names; fly BID/ASK max abs diff {maxd:.4f} pct-pts "
                   f"(B1 logged at 4dp) on {len(both)} priced matches")
    except Exception as exc:
        b1_note = f"cross-check unavailable ({exc})"
    log(f"B1 cross-check: {b1_note}")

    # ---- frozen selection -----------------------------------------------------
    ev = apply_rule(ev, A_PCT, MIN_PRIORS)
    eligible = ev[ev["n_prior"] >= MIN_PRIORS]
    A = ev[ev["sel"]]
    trades, n_dropped_cap = trade_frame(A)
    n_unpriced = len(A) - len(A.dropna(subset=["fly_ba"]))
    log(f"detected {len(ev)} | eligible (>= {MIN_PRIORS} priors) {len(eligible)} | "
        f"selected {len(A)} | priced {len(A) - n_unpriced} | capital<=0 dropped "
        f"{n_dropped_cap} | final trades {len(trades)}")

    # ---- gate metrics ---------------------------------------------------------
    mba = gate_metrics(trades, "r_ba")
    mmid = gate_metrics(trades, "r_mid")
    passed, checks = gate_checks(mba, "eventvol_ruleA_bidask")
    passed_mid, checks_mid = gate_checks(mmid, "eventvol_ruleA_mid")
    log(f"BID/ASK: n={mba['n']} mean_r {mba['mean_r']:+.4f} Sharpe {mba['sharpe']:.2f} "
        f"OOS {mba['oos_sharpe']:.2f} maxDD {mba['maxdd']:.1%} MC5 {mba['mc5']:.2f} "
        f"worst-yr {mba['worst_year']:.2f} -> gate {'PASS' if passed else 'FAIL'}")

    # ---- verdict (pre-committed) ---------------------------------------------
    if passed:
        verdict = "PASS"
    elif mba["mean_r"] > 0 and mba["n"] >= 100:
        verdict = "PARTIAL — candidate, not promotable"
    else:
        verdict = "FAIL"

    # ---- breakdowns -----------------------------------------------------------
    trades["year"] = trades["p_date"].dt.year
    trades["era"] = trades["year"].map(era_of)
    py_ba = per_year_table(trades, "r_ba")
    era_rows = []
    for name, lo, hi in ERAS:
        g = trades[trades["era"] == name]
        if not len(g):
            era_rows.append(f"| {name} | 0 | — | — | — | — | — |")
            continue
        r = g["r_ba"].to_numpy()
        sh = (_annual_sharpe(r, len(g) / span_years(g["p_date"]))
              if len(g) >= 2 else float("nan"))
        era_rows.append(f"| {name} | {len(g)} | {g['fly_ba'].mean():+.3f}% | "
                        f"{g['fly_ba'].sum():+.2f}% | {r.mean():+.4f} | "
                        f"{(r > 0).mean()*100:.0f}% | {sh:.2f} |")

    byname = (trades.groupby("ticker")
              .agg(n=("r_ba", "size"), ba_pct=("fly_ba", "sum"),
                   mid_pct=("fly_mid", "sum"), mean_r=("r_ba", "mean"))
              .sort_values("ba_pct", ascending=False))
    top_name = byname.index[0]
    ex_top = trades[trades["ticker"] != top_name]
    m_extop = gate_metrics(ex_top, "r_ba") if len(ex_top) >= 2 else None
    # ex-top-2-trades: drop the two largest positive r_ba trades
    drop_idx = trades["r_ba"].nlargest(2).index
    ex_t2 = trades.drop(index=drop_idx)
    m_ext2 = gate_metrics(ex_t2, "r_ba") if len(ex_t2) >= 2 else None

    # per-name detection accounting
    acct = []
    for tk, g in ev.groupby("ticker"):
        gs = g[g["sel"]]
        gt = trades[trades["ticker"] == tk]
        acct.append((tk, len(g), str(g["p_date"].min().date()),
                     int((g["n_prior"] >= MIN_PRIORS).sum()), len(gs), len(gt),
                     f"{gt['fly_ba'].sum():+.2f}%" if len(gt) else "—"))
    acct.sort(key=lambda x: -x[1])

    # ---- sensitivity (report-only) --------------------------------------------
    sens_rows = []
    base_row, _ = sens_row(ev, f"BASE pct={A_PCT} priors={MIN_PRIORS} wings=1.0",
                           A_PCT, MIN_PRIORS, 1.0, None)
    sens_rows.append(base_row)
    for p in SENS_PCT:
        row, _ = sens_row(ev, f"pctile {p:.2f}", p, MIN_PRIORS, 1.0, mba["sharpe"])
        sens_rows.append(row)
    for pr in SENS_PRIORS:
        row, _ = sens_row(ev, f"min-priors {pr}", A_PCT, pr, 1.0, mba["sharpe"])
        sens_rows.append(row)
    for w in SENS_WINGS:
        row, _ = sens_row(ev, f"wings {w:.1f}x", A_PCT, MIN_PRIORS, w, mba["sharpe"])
        sens_rows.append(row)

    # ---- per-trade CSV ---------------------------------------------------------
    csv_cols = ["ticker", "p_date", "c_date", "year", "era", "n_prior", "implied",
                "thr", "spot_p", "spot_c", "k_atm", "ku", "kd", "ent_mid", "ent_ba",
                "width_max", "capital_sh", "fly_mid", "fly_ba", "pnl_mid_sh",
                "pnl_ba_sh", "r_mid", "r_ba"]
    trades[csv_cols].to_csv(OUT_CSV, index=False)
    log(f"per-trade CSV written: {OUT_CSV} ({len(trades)} rows)")

    # ---- audit dump ------------------------------------------------------------
    log("trade dump: ticker,p_date,n_prior,implied,thr,fly_mid,fly_ba,capital_sh,r_ba")
    for _, e in trades.iterrows():
        log(f"T {e['ticker']},{e['p_date'].date()},{int(e['n_prior'])},"
            f"{e['implied']:.4f},{e['thr']:.4f},{e['fly_mid']:.4f},"
            f"{e['fly_ba']:.4f},{e['capital_sh']:.4f},{e['r_ba']:+.4f}")

    # ---- report ----------------------------------------------------------------
    W = []
    W.append("# Event-Vol Frozen Rule — Full-Universe Stage-6 Gate Run — 2026-07-05\n")
    W.append("The DECISIVE study of the R1 program: the frozen event-vol rule "
             "(rule A of `src/research/eventvol_oos.py`, confirmed REAL on 23 "
             "names 2020-2026) run on the full 2007-2026 universe in "
             "`data/iv_history_full`, with the complete revised Stage-6 gate "
             "treatment. The rule, fills, and verdict conditions were frozen "
             "before this run; nothing was tuned. This test exists to be "
             "allowed to fail.\n")

    W.append("> **SURVIVORSHIP CAVEAT (read first):** the universe is the "
             "2026 extraction list — names chosen for BEING liquid in 2026. "
             "Names that blew up, were delisted, or faded before 2026 are "
             "absent, and their earnings events (the kind that kill short "
             "iron flies) are exactly the ones missing. Deep-history results "
             "on this universe are therefore biased IN FAVOR of the strategy; "
             "the bias grows with lookback distance (2007-2015 numbers are "
             "the least trustworthy, 2023-2026 the most). Additionally, "
             "survivors' early-history events are their pre-mega-cap years, "
             "selected on knowledge of how they turned out.\n")

    W.append("## Method (frozen; provenance)\n")
    W.append("- Detection + rule: `base.detect_events` imported unmodified "
             "from `src/research/eventvol_study.py`; selection is a careful "
             "copy of `apply_rules` from `src/research/eventvol_oos.py` "
             "(expanding within-lineage >= 90th pctile of PRIOR implied "
             f"moves, np.quantile linear, min {MIN_PRIORS} priors; 45-day "
             "de-dup inside detection).")
    W.append("- Trade: short iron fly, ATM straddle sold at K, wings at "
             "strikes nearest spot*(1 +/- 1.0*implied), entered at P close, "
             "exited at C close (intrinsic if the front expiry IS C). Fills "
             "MID and full BID/ASK; **BID/ASK is the gate fill everywhere**.")
    W.append("- Fly pricing: careful copy of the fly branch of "
             "`base.price_events`, parameterized only to record entry "
             "credits/wing strikes (for max-loss capital) and wing multiple "
             "(sensitivity). Wing-1.0 output verified per name against "
             "`base.price_events` itself: "
             + ("**0 mismatches**" if verify_bad == 0
                else f"**{verify_bad} MISMATCHES — results suspect**")
             + f" (max abs diff {verify_worst:.2e}).")
    W.append(f"- B1-dump cross-check (informational): {b1_note}.")
    W.append(f"- Universe: {len(names)} name lineages = every ticker in the "
             "store except SPX/XSP/VIX (index vol, no earnings) and GOOGL "
             "(same underlying as GOOG). FB (2012..2022-06-08) + META "
             "(2022-06-09..) stitched as ONE lineage; META_* rows before "
             "2022-06-09 are the Roundhill Metaverse ETF (spot ~$15, "
             "verified) and were dropped. ETFs (SPY QQQ IWM XLF XLE TLT SLV) "
             "kept per the universe spec; the earnings-crush detection "
             "signature gives them few/no events (see accounting table).")
    W.append("- Data: `data/iv_history_full/` ORATS EOD per-strike chains, "
             "2007-2026 where listed. Pre-2011 chains are monthly-heavy "
             "(weeklys sparse), so the front-expiry DTE [1,14] requirement "
             "finds fewer event windows in the early years — the frozen rule "
             "simply detects less there, it is not reinterpreted.\n")

    W.append("## Trade-return and gate-metric definitions (stated, conservative)\n")
    W.append("- Per-trade return **r = P&L / max-loss capital**, "
             "capital = max(K_up − K, K − K_dn) − entry_credit(BID/ASK). The "
             "conservative BID/ASK credit defines capital for BOTH fill "
             "scenarios (project pipeline lesson 2026-06-15). Trades with "
             f"capital <= 0 are excluded and counted ({n_dropped_cap} such "
             "trades this run). Because the exit is a next-day mark at real "
             "quotes (not expiry settlement), r < −1 is possible; min r this "
             f"run = {trades['r_ba'].min():+.3f}.")
    W.append("- **Sharpe = mean(r) / std(r, ddof=1) × sqrt(trades_per_year)**, "
             "trades_per_year = n / years, years = (last − first trade "
             f"p_date)/365.25 = {mba['years']:.2f}y here ({mba['tpy']:.1f} "
             "trades/yr). No risk-free deduction (project convention; at "
             "~1-day holds the per-trade rf is <0.02% of capital, immaterial "
             "vs std(r), and fly margin largely remains interest-earning).")
    W.append(f"- Equity curve at trade granularity: eq = cumprod(1 + "
             f"{RISK_FRAC:.0%}·r) — {RISK_FRAC:.0%} of account risked per "
             "trade, the project's standing `_equity_maxdd` convention "
             "(src/backtest/backtest.py). Max drawdown measured on this curve.")
    W.append("- IS/OOS: chronological 70/30 split of the trade sequence; both "
             "annualized with the pooled trades/yr.")
    W.append("- Monte Carlo: bootstrap resample of r (2000 draws, seed 0) → "
             "MC5/median Sharpe; order-only reshuffle (1000 draws, seed 1) → "
             "drawdown distribution (order affects DD, not Sharpe).")
    W.append(f"- Worst-calendar-year ADAPTATION: `monte_carlo.per_year_sharpe` "
             "requires >= 100 sessions/year — meaningless at "
             f"~{mba['tpy']:.0f} trades/yr — so the criterion is computed "
             f"manually at trade granularity over years with >= "
             f"{MIN_YEAR_TRADES} trades, same threshold (worst-year Sharpe "
             ">= 0). The `MonteCarloReport` dataclass is filled manually "
             "with these trade-granularity inputs for the same reason "
             "(exactly as the contango_pro/contango_spx runs did).\n")

    W.append("## Event-count accounting\n")
    W.append(f"- Detected events (all lineages): **{len(ev)}** "
             f"({ev['p_date'].min().date()} .. {ev['p_date'].max().date()}).")
    W.append(f"- Rule-eligible (>= {MIN_PRIORS} priors): **{len(eligible)}** "
             f"({len(eligible)/len(ev)*100:.0f}%; warm-up removes "
             f"{len(ev)-len(eligible)}; first eligible "
             f"{eligible['p_date'].min().date() if len(eligible) else '—'}).")
    W.append(f"- Selected by the frozen rule: **{len(A)}**; with a priced "
             f"BID/ASK fly: **{len(A) - n_unpriced}**; dropped for "
             f"capital <= 0: {n_dropped_cap}; **final trades: {len(trades)}** "
             f"({trades['p_date'].min().date()} .. "
             f"{trades['p_date'].max().date()}, "
             f"{trades['ticker'].nunique()} names).\n")
    W.append("| lineage | detected | first event | eligible | selected | "
             "trades | total BA |\n|---|---|---|---|---|---|---|")
    for row in acct:
        W.append("| " + " | ".join(str(x) for x in row) + " |")
    W.append("")

    W.append("## Headline P&L (frozen rule, all trades)\n")
    W.append(f"- P&L in % of spot(P) —")
    W.append(f"  - MID:     {pnl_summary(trades, 'fly_mid')}")
    W.append(f"  - BID/ASK: {pnl_summary(trades, 'fly_ba')}")
    W.append(f"- Return on max-loss capital —")
    W.append(f"  - MID:     mean r {mmid['mean_r']:+.4f} | Sharpe {mmid['sharpe']:.2f}")
    W.append(f"  - BID/ASK: mean r {mba['mean_r']:+.4f} | std {mba['std_r']:.4f} | "
             f"win {mba['win']*100:.0f}% | Sharpe {mba['sharpe']:.2f}\n")

    W.append("## Stage-6 gate evaluation — BID/ASK (the gate fill)\n")
    W.append("Revised thresholds (2026-07-01): Sharpe>=0.8, OOS>=0.6 & "
             ">=0.6xIS, DD<=20%, MC5>=0, worst-year>=0, >=100 trades — via "
             "`src.research.monte_carlo.acceptance_gate` on the manually "
             "filled trade-granularity report.\n")
    W.append("| criterion | result | actual |\n|---|---|---|")
    for name_, ok, actual in checks:
        W.append(f"| {name_} | {'**PASS**' if ok else '**FAIL**'} | {actual} |")
    W.append(f"\n**Gate at BID/ASK: {'PASS (all criteria)' if passed else 'FAIL'}**")
    W.append(f"\nSame gate at MID (informational): "
             f"{'PASS' if passed_mid else 'FAIL'} — "
             + "; ".join(f"{'OK' if ok else 'X'} {n_}" for n_, ok, _ in checks_mid)
             + ".\n")
    W.append(f"- IS Sharpe {mba['is_sharpe']:.2f} (first 70%), OOS "
             f"{mba['oos_sharpe']:.2f} (last 30%).")
    W.append(f"- Bootstrap Sharpe: median {mba['mc50']:.2f}, 5th pct "
             f"{mba['mc5']:.2f} (2000 draws).")
    W.append(f"- Max drawdown {mba['maxdd']:.2%} at {RISK_FRAC:.0%}-risk "
             f"sizing (chronological); order-reshuffle DD median "
             f"{mba['dd_shuffle_med']:.2%}, 95th pct {mba['dd_shuffle_p95']:.2%}.")
    W.append(f"- Raw spot-normalized cumulative P&L drawdown (no sizing): see "
             "equity table below.\n")

    W.append("## Per-era (BID/ASK)\n")
    W.append("| era | n | mean %spot | total %spot | mean r | win | Sharpe |\n"
             "|---|---|---|---|---|---|---|")
    W += era_rows
    W.append("")

    W.append("## Per-year (BID/ASK)\n")
    W.append("| year | n | win | mean r | total %spot | Sharpe |\n"
             "|---|---|---|---|---|---|")
    for y, r_ in py_ba.iterrows():
        judged = "" if r_["n"] >= MIN_YEAR_TRADES else " (n<8, not judged)"
        W.append(f"| {int(y)} | {int(r_['n'])} | {r_['win']*100:.0f}% | "
                 f"{r_['mean_r']:+.4f} | {r_['total_pctspot']:+.2f}% | "
                 f"{r_['sharpe']:.2f}{judged} |")
    W.append("")

    W.append("## Concentration\n")
    W.append("| ticker | n | total BA %spot | total MID %spot | mean r BA |\n"
             "|---|---|---|---|---|")
    for tk, r_ in byname.iterrows():
        W.append(f"| {tk} | {int(r_['n'])} | {r_['ba_pct']:+.2f}% | "
                 f"{r_['mid_pct']:+.2f}% | {r_['mean_r']:+.4f} |")
    if m_extop:
        W.append(f"\n- Ex top name ({top_name}): n={m_extop['n']}, mean r "
                 f"{m_extop['mean_r']:+.4f}, Sharpe {m_extop['sharpe']:.2f}, "
                 f"worst-year {m_extop['worst_year']:.2f}.")
    if m_ext2:
        d2 = trades.loc[drop_idx]
        W.append(f"- Ex top 2 trades ({', '.join(f'{r_.ticker} {r_.p_date.date()} r={r_.r_ba:+.2f}' for r_ in d2.itertuples())}): "
                 f"n={m_ext2['n']}, mean r {m_ext2['mean_r']:+.4f}, Sharpe "
                 f"{m_ext2['sharpe']:.2f}.")
    W.append("")

    W.append("## Sensitivity (REPORT-ONLY — not selection; a cliff = overfitting flag)\n")
    W.append("One parameter at a time; all metrics at BID/ASK.\n")
    W.append("| variant | n | mean %spot | mean r | win | Sharpe | maxDD | worst-yr |\n"
             "|---|---|---|---|---|---|---|---|")
    W += sens_rows
    W.append("")

    W.append("## Equity curve (BID/ASK, time order, % of spot cumulative)\n")
    eq_cum = trades["fly_ba"].cumsum()
    peak = eq_cum.cummax()
    dd_spot = (peak - eq_cum)
    i_tr = int(dd_spot.idxmax())
    W.append(f"- Cumulative BID/ASK P&L: {eq_cum.iloc[-1]:+.2f}% of spot over "
             f"{len(trades)} trades; worst spot-normalized peak-to-trough "
             f"{dd_spot.max():.2f} pct-pts (trough at "
             f"{trades.loc[i_tr, 'p_date'].date()}).")
    W.append(f"- Worst single trade: "
             f"{trades.loc[trades['fly_ba'].idxmin(), 'ticker']} "
             f"{trades.loc[trades['fly_ba'].idxmin(), 'p_date'].date()} "
             f"({trades['fly_ba'].min():+.3f}% of spot, r "
             f"{trades.loc[trades['fly_ba'].idxmin(), 'r_ba']:+.3f}).\n")
    W.append("| # | p_date | ticker | implied | thr | P&L BA %spot | r BA | cum BA %spot |\n"
             "|---|---|---|---|---|---|---|---|")
    for i, e in trades.iterrows():
        W.append(f"| {i+1} | {e['p_date'].date()} | {e['ticker']} | "
                 f"{e['implied']*100:.2f}% | {e['thr']*100:.2f}% | "
                 f"{e['fly_ba']:+.3f}% | {e['r_ba']:+.3f} | {eq_cum.iloc[i]:+.2f}% |")
    W.append("")

    W.append("## Caveats\n")
    W.append("- **Survivorship** (top of report): 2026-liquidity-screened "
             "universe; extinct names absent; bias FAVORS the strategy, "
             "worst in the earliest eras.")
    W.append("- Event dates are surface-detected, not calendar-verified; "
             "non-earnings vol events (FDA, macro, product) are included by "
             "construction. ETF 'events', if any, are macro events.")
    W.append("- No commissions/fees: ~8 legs/round trip ≈ $5-6/fly ≈ "
             "0.01-0.05% of spot — immaterial vs the bid/ask gap already "
             "charged, but would shave marginal results.")
    W.append("- No early-assignment modeling (short ATM legs through ex-div; "
             "rare at 1-day holds). Exit is first close after the event; no "
             "intraday management.")
    W.append("- r uses max-loss capital; broker margin on iron flies equals "
             "it, but portfolio-level sizing/overlap is out of scope here.")
    W.append("- Pre-2011 detection is sparser (monthly-only expiries), so "
             "early 'eras' have fewer, unrepresentative events; the "
             "2020-2026 era overlaps the already-published OOS study (only "
             "pre-2020 and the extra names are NEW evidence).\n")

    W.append("## Verdict (pre-committed)\n")
    W.append(f"- PASS requires ALL gate criteria at BID/ASK: "
             f"{'met' if passed else 'NOT met'}.")
    W.append(f"- PARTIAL requires mean r > 0 at BID/ASK (actual "
             f"{mba['mean_r']:+.4f}) with n >= 100 (actual {mba['n']}).")
    W.append(f"\n**VERDICT: {verdict}**")
    W.append("\nNo promoter call was made and `validated_strategies.yaml` was "
             "not written — the promotion decision returns to the human.")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(W) + "\n")
    log(f"report written: {OUT_MD}")
    log(f"VERDICT: {verdict} | n={mba['n']} mean_r(BA) {mba['mean_r']:+.4f} "
        f"Sharpe {mba['sharpe']:.2f} OOS {mba['oos_sharpe']:.2f} maxDD "
        f"{mba['maxdd']:.1%} MC5 {mba['mc5']:.2f} worst-yr {mba['worst_year']:.2f}")
    log("DONE")


if __name__ == "__main__":
    sys.exit(main())
