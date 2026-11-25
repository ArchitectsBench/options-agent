"""TRACK C — TREND-BOOK SHADOW MANAGER (hybrid_trend_v3, frozen design).

Once-daily job that runs the v3 hybrid trend book as a SHADOW forward test per
config/trend_forward_criteria.yaml: no broker, no orders — the IBKR paper
executor attaches later and will write mode="paper" records into the same
ledger. Everything here is OBSERVATION-ONLY.

Design rules (and why):

- REUSE, DON'T REIMPLEMENT. Signals/vol/panel come from
  src.research.hybrid_trend.build_hybrid_panel; legs, costs, cap and financing
  constants are imported from the research modules; the DFF rate machinery is
  hybrid_trend_v3's own fetch_dff/fin_rate_on. The one thing that cannot be
  imported — the per-day sizing/cap/accounting step, which lives inside the
  monolithic level_b_hybrid_v3 loop — is transcribed 1:1 (same order of
  floating-point operations) in v3_step() below, and --verify proves the
  transcription bit-exact against the imported level_b_hybrid_v3 over the
  full 2001-.. history (equity path np.array_equal + aggregate cost/cap/flip
  stats all exactly equal). The tracking criterion
  (daily_return_tracking_error_ann_max: 0.02, unexplained_position_diffs: 0)
  is only meaningful because of this.

- ACCOUNTING CONVENTION (judgment call, documented): the run at the end of
  trading day D executes the v3 loop step t=D exactly: targets are computed
  from day D-1 info (signals/vol/prior close), the traded delta is costed with
  the v3 cost model, and the new position earns the full D-1 -> D close-to-
  close move — i.e. the implied fill is at the PRIOR close, which is v3's own
  convention. The ledger records both prev_close (the accounting fill
  reference) and close (day-D mark). Booking literal fills at day-D close
  would deviate from the frozen simulation and manufacture tracking error by
  construction; the real fill-vs-model gap is exactly what the IBKR paper leg
  is meant to measure later.

- DATA: append-only refresh of the existing data/futures_daily caches
  (yfinance for the 15 legs, FRED CSV for DFF). New rows are appended and
  deduped by date; existing rows are NEVER rewritten, even if the source
  revises them (a warning is logged when the source disagrees with the frozen
  history — e.g. an ETF dividend-adjustment seam). A trading day is processed
  only once EVERY leg's cache has reached it (frontier = min over legs of the
  cache max date), so a processed day's inputs can never change afterwards.
  On any data failure the day is skipped loudly with a 'skipped' ledger
  record — prices are never fabricated.

Usage:
  .venv/bin/python -m src.trendbook.manager            # daily shadow run
  .venv/bin/python -m src.trendbook.manager --verify   # bit-exactness check
  Options: --no-refresh (offline), --as-of YYYY-MM-DD (tests/backfill guard)

State:  data/trendbook/state.json          (atomic write)
Ledger: logs/trendbook/ledger.jsonl        (append-only)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

import src.research.futures_trend as ft
import src.research.hybrid_trend as ht
import src.research.hybrid_trend_v3 as ht3
from src.research.futures_trend import (
    PORT_VOL_TARGET, START_EQUITY, TRADING_DAYS, refresh_days,
)
from src.research.hybrid_trend import BORROW_RATE, MIN_ETF_YEARS
from src.research.hybrid_trend_v2 import CAP_MULT, FIN_RATE

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "data" / "trendbook" / "state.json"
LEDGER_PATH = ROOT / "logs" / "trendbook" / "ledger.jsonl"

MODE = "shadow"   # the IBKR executor will later write "paper"
STAMP = "OBSERVATION-ONLY — UNVALIDATED, SHADOW BOOK, NOT ACTIONABLE"
FILL_CONVENTION = (
    "v3 close-to-close: targets from prior-day info; traded delta costed per "
    "the v3 cost model; position earns the full prior-close->close move "
    "(implied fill at prev_close). Identical to the frozen backtest."
)

# stats keys of level_b_hybrid_v3 that the replay must reproduce EXACTLY
_VERIFY_STAT_KEYS = [
    "total_costs", "fut_costs", "etf_costs", "total_borrow", "total_financing",
    "total_fin_flat5", "fin_days", "cap_checks", "cap_binds", "total_flips",
    "max_etf_gross", "mean_etf_gross", "max_etf_lev",
    "avg_scale_when_binding", "min_scale",
]


def log(msg: str) -> None:
    print(f"[trendbook] {msg}")


def log_err(msg: str) -> None:
    print(f"[trendbook] ERROR: {msg}", file=sys.stderr)
    print(f"[trendbook] ERROR: {msg}")


# ── legs (v3's own selection rule, incl. the CPER >= 10y check) ─────────────

def active_legs():
    legs = list(ht.LEGS)
    cper = ft.fetch("CPER", auto_adjust=True)
    yrs = (cper.index.max() - cper.index.min()).days / 365.25
    if yrs < MIN_ETF_YEARS:
        legs = [lg for lg in legs if lg.ticker != "CPER"]
    return legs


# ── the frozen v3 day step (transcribed 1:1 from level_b_hybrid_v3) ─────────

def v3_step(t, S, V, P, monthly, mult, cost_side, is_etf, n, eq, fin_rate_t):
    """One trading day of the v3 loop. Every arithmetic operation and its
    order is transcribed from src.research.hybrid_trend_v3.level_b_hybrid_v3
    (lines inside its `for t in range(1, len(idx))` body). Bit-exactness vs
    the imported function is enforced by verify_against_backtest()."""
    ncols = len(mult)
    p_prev = P[t - 1]
    valid = (np.isfinite(S[t - 1]) & np.isfinite(V[t - 1]) & (V[t - 1] > 1e-4)
             & np.isfinite(p_prev) & (p_prev > 0))
    n_active = int(valid.sum())
    new_n = n.copy()
    sized_today = np.zeros(ncols, bool)
    flips_today = 0
    if n_active > 0 and eq > 0:
        budget = (PORT_VOL_TARGET / n_active) * eq
        for i in range(ncols):
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
                    flips_today += 1
            sized_today[i] = True

    px = np.where(np.isfinite(p_prev), p_prev, 0.0)

    # leverage cap at every rebalance (identical to v2/v3)
    rebal = bool(monthly[t]) or bool(np.any(new_n != n))
    cap_checked = cap_bind = False
    scale = 1.0
    if rebal and eq > 0:
        cap_checked = True
        gross_target = float(np.where(is_etf, np.abs(new_n) * px, 0.0).sum())
        cap = CAP_MULT * eq
        if gross_target > cap:
            f = cap / gross_target
            cap_bind = True
            scale = f
            new_n = np.where(
                is_etf, np.sign(new_n) * np.floor(np.abs(new_n) * f), new_n)

    dn = np.abs(new_n - n)
    c_vec = dn * cost_side
    costs = float(c_vec.sum())
    fut_costs = float(c_vec[~is_etf].sum())
    etf_costs = float(c_vec[is_etf].sum())
    short_notional = float(np.where(is_etf & (new_n < 0), -new_n * px, 0.0).sum())
    borrow = BORROW_RATE / TRADING_DAYS * short_notional
    etf_abs = np.where(is_etf, np.abs(new_n) * px, 0.0)
    gross_etf = float(etf_abs.sum())

    base = max(0.0, gross_etf - max(eq, 0.0))
    financing = fin_rate_t / TRADING_DAYS * base
    fin_flat5 = FIN_RATE / TRADING_DAYS * base

    dp = P[t] - p_prev
    dp = np.where(np.isfinite(dp), dp, 0.0)
    pnl = float(np.nansum(new_n * mult * dp))
    equity = eq + pnl - costs - borrow - financing

    diag = {
        "pnl": pnl, "costs": costs, "fut_costs": fut_costs,
        "etf_costs": etf_costs, "borrow": borrow, "financing": financing,
        "fin_flat5": fin_flat5, "fin_rate": float(fin_rate_t),
        "gross_etf": gross_etf, "cap_checked": cap_checked,
        "cap_bind": cap_bind, "cap_scale": scale, "n_active": n_active,
        "flips": flips_today, "sized_today": sized_today,
    }
    return new_n, equity, diag


def replay(idx, C, sig, vol, legs, fin_rate, n0=None, eq0=START_EQUITY,
           t_start=1, t_end=None, collect=False):
    """Iterate v3_step over idx[t_start..t_end] from state (n0, eq0) taken as
    of idx[t_start-1]. With defaults this reproduces level_b_hybrid_v3 from
    scratch. Returns (equity array over idx, aggregate stats, final
    positions, per-day records if collect)."""
    cols = [lg.code for lg in legs]
    S, V, P = sig[cols].values, vol[cols].values, C[cols].values
    monthly = refresh_days(idx)
    mult = np.array([lg.mult for lg in legs])
    cost_side = np.array([lg.cost_side for lg in legs])
    is_etf = np.array([lg.kind == "etf" for lg in legs])
    if t_end is None:
        t_end = len(idx) - 1

    n = np.zeros(len(cols)) if n0 is None else np.asarray(n0, float).copy()
    eq = float(eq0)
    equity = np.full(len(idx), np.nan)
    if t_start == 1:
        equity[0] = eq
    agg = {k: 0.0 for k in ("total_costs", "fut_costs", "etf_costs",
                            "total_borrow", "total_financing",
                            "total_fin_flat5")}
    agg.update(fin_days=0, cap_checks=0, cap_binds=0, total_flips=0,
               max_etf_gross=0.0, max_etf_lev=0.0)
    etf_gross_sum, etf_gross_n = 0.0, 0
    scale_sum, min_scale = 0.0, 1.0
    records = []

    for t in range(t_start, t_end + 1):
        eq_prev = eq
        prev_n = n.copy()
        n, eq, d = v3_step(t, S, V, P, monthly, mult, cost_side, is_etf,
                           n, eq_prev, fin_rate[t])
        equity[t] = eq
        agg["total_costs"] += d["costs"]
        agg["fut_costs"] += d["fut_costs"]
        agg["etf_costs"] += d["etf_costs"]
        agg["total_borrow"] += d["borrow"]
        agg["total_financing"] += d["financing"]
        agg["total_fin_flat5"] += d["fin_flat5"]
        if d["financing"] > 0:
            agg["fin_days"] += 1
        if d["cap_checked"]:
            agg["cap_checks"] += 1
        if d["cap_bind"]:
            agg["cap_binds"] += 1
            scale_sum += d["cap_scale"]
            min_scale = min(min_scale, d["cap_scale"])
        agg["total_flips"] += d["flips"]
        if d["gross_etf"] > 0:
            agg["max_etf_gross"] = max(agg["max_etf_gross"], d["gross_etf"])
            etf_gross_sum += d["gross_etf"]
            etf_gross_n += 1
            if eq_prev > 0 and d["gross_etf"] / eq_prev > agg["max_etf_lev"]:
                agg["max_etf_lev"] = d["gross_etf"] / eq_prev
        if collect:
            records.append({"t": t, "date": idx[t], "prev_n": prev_n,
                            "new_n": n.copy(), "equity": eq, "diag": d,
                            "close": P[t].copy(), "prev_close": P[t - 1].copy()})

    agg["mean_etf_gross"] = etf_gross_sum / etf_gross_n if etf_gross_n else 0.0
    agg["avg_scale_when_binding"] = (scale_sum / agg["cap_binds"]
                                     if agg["cap_binds"] else float("nan"))
    agg["min_scale"] = min_scale if agg["cap_binds"] else float("nan")
    return equity, agg, n, records


# ── verify mode: bit-exactness against the frozen backtest ──────────────────

def verify_against_backtest(quiet=False):
    """Recompute the v3 backtest from the same caches with the IMPORTED
    level_b_hybrid_v3 and assert the manager's replay matches it exactly:
    the full equity path bit-for-bit (np.array_equal) and every aggregate
    cost/cap/flip statistic. Equality of the full 25y equity path implies the
    positions match on every day (any position difference changes P&L/costs
    and diverges equity), so the manager's targets for the most recent common
    date are exactly the backtest's positions for that date."""
    legs = active_legs()
    cols = [lg.code for lg in legs]
    idx, C, _R, sig, vol, _q, _d = ht.build_hybrid_panel(legs)
    dff = ht3.fetch_dff()
    fin_rate = ht3.fin_rate_on(idx, dff)

    eq_ref, stats_ref = ht3.level_b_hybrid_v3(idx, C, sig, vol, legs, fin_rate)
    eq_mgr, agg, n_final, _ = replay(idx, C, sig, vol, legs, fin_rate)

    equity_ok = bool(np.array_equal(eq_ref.values, eq_mgr, equal_nan=True))
    first_diff = None
    if not equity_ok:
        neq = ~((eq_ref.values == eq_mgr)
                | (np.isnan(eq_ref.values) & np.isnan(eq_mgr)))
        i = int(np.argmax(neq))
        first_diff = (str(idx[i].date()), float(eq_ref.values[i]), float(eq_mgr[i]))

    stat_mismatches = {}
    for k in _VERIFY_STAT_KEYS:
        a, b = agg[k], stats_ref[k]
        same = (a == b) or (isinstance(a, float) and isinstance(b, float)
                            and np.isnan(a) and np.isnan(b))
        if not same:
            stat_mismatches[k] = {"manager": a, "backtest": b}

    ok = equity_ok and not stat_mismatches
    last_date = idx[-1]
    targets = {c: float(v) for c, v in zip(cols, n_final)}

    if not quiet:
        log(f"VERIFY vs level_b_hybrid_v3 over {idx[0].date()}..{last_date.date()}"
            f" ({len(idx)} days, {len(legs)} legs)")
        log(f"  equity path bit-exact: {equity_ok}"
            + (f"  FIRST DIFF {first_diff}" if first_diff else ""))
        log(f"  aggregate stats exact: {not stat_mismatches}"
            + (f"  MISMATCHES {stat_mismatches}" if stat_mismatches else ""))
        log(f"  final backtest equity: ${eq_ref.values[-1]:,.2f}")
        log(f"  targets for most recent common date {last_date.date()}"
            f" (== backtest positions for that date):")
        for lg in legs:
            log(f"    {lg.code:4s} ({lg.trade_as:10s}) [{lg.kind}]"
                f" {targets[lg.code]:+,.0f}")
        log(f"  VERIFY RESULT: {'EXACT MATCH' if ok else 'MISMATCH — DO NOT TRUST TARGETS'}")
    return {"ok": ok, "equity_ok": equity_ok, "first_diff": first_diff,
            "stat_mismatches": stat_mismatches, "date": str(last_date.date()),
            "targets": targets, "final_equity": float(eq_ref.values[-1])}


# ── append-only cache refresh ────────────────────────────────────────────────

def _yf_close(ticker, start, auto_adjust):
    import yfinance as yf
    d = yf.download(ticker, start=start, interval="1d",
                    auto_adjust=auto_adjust, progress=False)
    if d is None or len(d) == 0:
        raise RuntimeError(f"yfinance returned no rows for {ticker}")
    c = d["Close"]
    if isinstance(c, pd.DataFrame):
        c = c.iloc[:, 0]
    s = c.dropna().astype(float)
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    return s[~s.index.duplicated(keep="last")].sort_index()

def refresh_price_cache(ticker, auto_adjust, before=None):
    """Append new daily closes to the existing cache. Append-only: rows dated
    on/before the cached max are never touched; a disagreement between the
    source and frozen history (e.g. dividend-adjustment seam on ETFs) is
    warned about, never applied. Returns number of rows appended.

    `before` (date-like): drop source rows dated on/after it. The morning
    executor MUST pass today — during the session yfinance reports a PARTIAL
    bar for the current day, and appending it would freeze a mid-session
    print into the append-only history."""
    fp = ft.CACHE / (ticker.replace("=", "_") + ".parquet")
    if not fp.exists():
        ft.fetch(ticker, auto_adjust=auto_adjust)   # research seeder (max history)
        return -1
    old = pd.read_parquet(fp)["close"]
    old.index = pd.DatetimeIndex(old.index)
    last = old.index.max()
    s = _yf_close(ticker, (last - pd.Timedelta(days=10)).date().isoformat(),
                  auto_adjust)
    if before is not None:
        s = s[s.index < pd.Timestamp(before).normalize()]
    overlap = s.index.intersection(old.index)
    if len(overlap):
        a, b = old.loc[overlap], s.loc[overlap]
        rel = (a - b).abs() / a.abs().clip(lower=1e-9)
        if (rel > 1e-4).any():
            worst = rel.idxmax()
            log(f"WARNING {ticker}: source disagrees with frozen history on "
                f"{len(rel[rel > 1e-4])} overlap row(s) (worst {worst.date()} "
                f"{rel.max():.2%}) — adjustment seam or revision; cache kept "
                f"append-only, history NOT rewritten.")
    new = s[s.index > last]
    if len(new):
        merged = pd.concat([old, new]).rename("close")
        tmp = fp.with_suffix(".parquet.tmp")
        merged.to_frame().to_parquet(tmp)
        os.replace(tmp, fp)
    return len(new)


def refresh_dff():
    """Append new FRED DFF observations (append-only, dedup by date)."""
    fp = ht3.DFF_CACHE
    if not fp.exists():
        ht3.fetch_dff()
        return -1
    old = pd.read_parquet(fp)["dff"]
    old.index = pd.DatetimeIndex(old.index)
    with urllib.request.urlopen(ht3.DFF_URL, timeout=60) as resp:
        raw = pd.read_csv(resp)
    raw.columns = [c.strip().lower() for c in raw.columns]
    date_col = "observation_date" if "observation_date" in raw.columns else "date"
    s = pd.Series(pd.to_numeric(raw["dff"], errors="coerce").values,
                  index=pd.DatetimeIndex(pd.to_datetime(raw[date_col])),
                  name="dff").dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]
    new = s[s.index > old.index.max()]
    if len(new):
        merged = pd.concat([old, new]).rename("dff")
        tmp = fp.with_suffix(".parquet.tmp")
        merged.to_frame().to_parquet(tmp)
        os.replace(tmp, fp)
    return len(new)


# ── state + ledger ───────────────────────────────────────────────────────────

def load_state():
    if not Path(STATE_PATH).exists():
        return None
    return json.loads(Path(STATE_PATH).read_text())


def save_state(state):
    p = Path(STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, p)


def append_ledger(rec):
    p = Path(LEDGER_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def _base_record(kind):
    return {"record": kind, "mode": MODE, "stamp": STAMP,
            "book": "hybrid_trend_v3 (frozen 2026-07-06)",
            "generated_at": dt.datetime.now().isoformat(timespec="seconds")}


def _skip(run_date, reason, extra=None):
    rec = _base_record("skipped")
    rec.update({"date": str(run_date), "reason": reason})
    if extra:
        rec.update(extra)
    append_ledger(rec)
    log_err(f"SKIPPED {run_date}: {reason}")


# ── the daily job ────────────────────────────────────────────────────────────

def run_daily(as_of=None, refresh=True):
    """Idempotent once-daily shadow run. Processes every not-yet-processed
    trading day up to the data frontier (min over legs of cache max date) with
    the frozen v3 step, persists state, appends ledger records, prints a
    one-line summary. Never fabricates prices: a day is processed only when
    every leg's cache has reached it."""
    as_of = pd.Timestamp(as_of if as_of is not None else dt.date.today()).normalize()

    refresh_errors = {}
    if refresh:
        for lg in ht.LEGS:
            try:
                n_new = refresh_price_cache(lg.ticker, lg.auto_adjust)
                if n_new:
                    log(f"refreshed {lg.ticker}: {'seeded' if n_new < 0 else f'+{n_new} row(s)'}")
            except Exception as e:  # noqa: BLE001 — must never crash the job
                refresh_errors[lg.ticker] = str(e)
                log_err(f"refresh failed for {lg.ticker}: {e}")
        try:
            n_new = refresh_dff()
            if n_new:
                log(f"refreshed DFF: +{n_new} row(s)")
        except Exception as e:  # noqa: BLE001
            refresh_errors["DFF"] = str(e)
            log_err(f"refresh failed for DFF: {e}")

    try:
        legs = active_legs()
        cols = [lg.code for lg in legs]
        series = {lg.code: ft.fetch(lg.ticker, auto_adjust=lg.auto_adjust)
                  for lg in legs}
        idx, C, _R, sig, vol, _q, _d = ht.build_hybrid_panel(legs)
        dff = ht3.fetch_dff()
        fin_rate = ht3.fin_rate_on(idx, dff)
    except Exception as e:  # noqa: BLE001
        _skip(str(as_of.date()), f"data layer failure: {e}",
              {"refresh_errors": refresh_errors})
        return None

    leg_last = {c: s.index.max() for c, s in series.items()}
    frontier = min(leg_last.values())
    if (as_of - dff.index.max()).days > 14:
        log_err(f"DFF cache stale ({dff.index.max().date()}); financing uses "
                f"forward-fill per the frozen v3 convention — investigate FRED feed.")

    state = load_state()
    if state is not None and state["legs"] != cols:
        _skip(str(as_of.date()),
              f"leg set changed: state has {state['legs']}, current is {cols}; "
              f"manual intervention required", {"refresh_errors": refresh_errors})
        return None

    if state is None:
        cand = idx[idx <= frontier]
        if len(cand) < 2:
            _skip(str(as_of.date()), "insufficient history to start the book",
                  {"refresh_errors": refresh_errors})
            return None
        t_first = int(idx.get_loc(cand[-1]))
        new_ts = [t_first]
        n0 = np.zeros(len(cols))
        eq0 = float(START_EQUITY)
        start_date = str(idx[t_first].date())
        cycle0 = 0
    else:
        lp = pd.Timestamp(state["last_processed"])
        mask = (idx > lp) & (idx <= frontier)
        new_ts = [int(t) for t in np.where(mask)[0]]
        n0 = np.array([state["positions"][c] for c in cols], float)
        eq0 = float(state["equity"])
        start_date = state["start_date"]
        cycle0 = int(state["cycles"])
        if new_ts and idx[new_ts[0] - 1] != lp:
            _skip(str(as_of.date()),
                  f"panel calendar shifted: date {idx[new_ts[0] - 1].date()} "
                  f"appeared before last_processed {lp.date()}; refusing to "
                  f"process (would break exact tracking)",
                  {"refresh_errors": refresh_errors})
            return None

    if not new_ts:
        lp = pd.Timestamp(state["last_processed"]) if state else None
        if lp is not None and lp >= as_of:
            log(f"no-op: already processed {lp.date()} (as-of {as_of.date()})")
            return None
        if as_of.weekday() >= 5:
            log(f"no-op: {as_of.date()} is a weekend")
            return None
        stale = {c: str(d.date()) for c, d in leg_last.items() if d < as_of}
        _skip(str(as_of.date()),
              "no fresh closes to process (market holiday or data failure)",
              {"stale_legs": stale, "refresh_errors": refresh_errors})
        return None

    t_start, t_end = new_ts[0], new_ts[-1]
    _equity, _agg, n_final, records = replay(
        idx, C, sig, vol, legs, fin_rate,
        n0=n0, eq0=eq0, t_start=t_start, t_end=t_end, collect=True)

    cycle = cycle0
    last = None
    for r in records:
        cycle += 1
        date = r["date"]
        legs_out = {}
        for i, lg in enumerate(legs):
            fresh = date in series[lg.code].index
            legs_out[lg.code] = {
                "kind": lg.kind, "trade_as": lg.trade_as,
                "target": float(r["new_n"][i]), "prev": float(r["prev_n"][i]),
                "traded": float(r["new_n"][i] - r["prev_n"][i]),
                "close": (None if not np.isfinite(r["close"][i]) else float(r["close"][i])),
                "prev_close": (None if not np.isfinite(r["prev_close"][i]) else float(r["prev_close"][i])),
                "fresh_print": bool(fresh),
            }
        d = r["diag"]
        rec = _base_record("daily")
        rec.update({
            "date": str(date.date()),
            "info_date": str(idx[r["t"] - 1].date()),
            "cycle": cycle,
            "equity": r["equity"],
            "pnl": d["pnl"], "trade_costs": d["costs"],
            "fut_costs": d["fut_costs"], "etf_costs": d["etf_costs"],
            "borrow": d["borrow"], "financing": d["financing"],
            "fin_rate_ann": d["fin_rate"],
            "gross_etf_notional": d["gross_etf"],
            "gross_etf_over_equity": (d["gross_etf"] / r["equity"]
                                      if r["equity"] > 0 else None),
            "cap_checked": d["cap_checked"], "cap_bind": d["cap_bind"],
            "cap_scale": d["cap_scale"], "n_active": d["n_active"],
            "sign_flips": d["flips"],
            "legs": legs_out,
            "intended_vs_executed_diffs": [],
            "diff_note": ("shadow mode: fills simulated with the v3 cost "
                          "model, intended == executed by construction; the "
                          "IBKR leg will populate diffs with logged causes"),
            "fill_convention": FILL_CONVENTION,
        })
        append_ledger(rec)
        last = rec

    save_state({
        "mode": MODE, "stamp": STAMP,
        "book": "hybrid_trend_v3 (frozen 2026-07-06)",
        "start_date": start_date, "start_equity": float(START_EQUITY),
        "equity": last["equity"],
        "last_processed": last["date"],
        "cycles": cycle,
        "legs": cols,
        "positions": {c: float(v) for c, v in zip(cols, n_final)},
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    })

    d = last
    traded = sum(1 for v in d["legs"].values() if v["traded"] != 0)
    log(f"{MODE} {d['date']}: eq ${d['equity']:,.2f} | pnl {d['pnl']:+,.2f} | "
        f"costs {d['trade_costs']:.2f} | borrow {d['borrow']:.2f} | "
        f"fin {d['financing']:.2f} @{d['fin_rate_ann']:.2%} | "
        f"grossETF ${d['gross_etf_notional']:,.0f} "
        f"({'CAP BIND' if d['cap_bind'] else 'cap ok'}) | "
        f"trades {traded}/{len(legs)} legs | days processed {len(records)} | "
        f"{STAMP}")
    return last


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verify", action="store_true",
                    help="bit-exactness check vs the imported v3 backtest")
    ap.add_argument("--no-refresh", action="store_true",
                    help="skip data refresh (offline / testing)")
    ap.add_argument("--as-of", default=None,
                    help="treat this date as 'today' (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    if args.verify:
        res = verify_against_backtest()
        return 0 if res["ok"] else 1
    run_daily(as_of=args.as_of, refresh=not args.no_refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
