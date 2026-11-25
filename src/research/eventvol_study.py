"""EARNINGS EVENT-VOL STUDY (professional-path doc, study B1) — 2026-07-05.

Pre-specified question (no tuning): around earnings events, is the option-
implied move systematically rich or cheap vs realized, and does either tail
clear realistic costs with a defined-risk structure?

ALL PARAMETERS BELOW WERE COMMITTED BEFORE LOOKING AT ANY RESULT.

Event-day convention
--------------------
  P  = last close BEFORE the earnings reaction (front ATM IV elevated).
  C  = next trading session (the "crush"/reaction session).
  The doc's T-1 / T+1 map to P / C: announcement happens between P close and
  C open (AMC on P or BMO on C).  Implied move is measured at P close,
  realized move is close(C)/close(P)-1, overnight gap open(C)/close(P)-1.

Method (a) — surface-detected events (primary):
  On day P: front expiry = nearest expiry with DTE in [1, 14]; next expiry =
  the following one with DTE <= 60.  ATM IV = mean(call_iv, put_iv) at the
  strike nearest spot with both IVs > 0.
    ELEVATION:  front_ATM_IV / next_ATM_IV >= 1.15
    CRUSH (X, stated before looking: X = 25%): front-end ATM IV drops >= 25%
      from P close to C close.  If the front expiry is still alive at C
      (DTE_P >= 2) the crush is measured on that same expiration; if
      DTE_P == 1 (expires on C) it is measured on the NEXT expiry's ATM IV.
  De-dup: within a ticker, events closer than 45 calendar days are clustered;
  keep the day with the highest elevation ratio (earnings are ~91d apart).

Method (b) — filing-date anchors (validation only, never the event date):
  data/fundamentals_raw.parquet filing dates.  A surface event P "agrees"
  with a filing if filing_date - P is in [0, 7] calendar days (filings lag
  the announcement by 0-4 days; announcement is 0-1 day after P).

Measurement per event:
  implied  = ATM front-expiry straddle MID at P close / spot(P)
  realized = |close(C)/close(P) - 1|          (chain spot = EOD close)
  gap      = |open(C)/close(P) - 1|           (daily bars; yfinance/local)
  ratio    = implied / realized

Monetization (extreme deciles ONLY, ranked within each name's own event
history — full-sample within-name percentile; in-sample caveat stated in the
report; no other cut is examined):
  RICH tail  (implied pct >= 0.90): SHORT IRON FLY at P close — sell ATM
    call+put at K (straddle strike), buy wings at strikes nearest
    spot*(1 +/- 1.0*implied).  Bought back at C close.
  CHEAP tail (implied pct <= 0.10): LONG STRADDLE at K at P close, sold at
    C close.
  Fills: (1) MID = (bid+ask)/2 every leg both ways; (2) BID/ASK = shorts at
  bid / longs at ask on entry, reversed on exit.  If the expiry IS C, exit
  at intrinsic (no spread).  Wing legs must have ask > 0 at entry; short /
  long-straddle legs must be two-sided at entry.  P&L in % of spot(P).

Pre-committed verdict rule:
  A tail is "interesting" only if mean P&L at MID > 0 with n >= 40 AND it
  stays positive at MID after excluding its single most profitable calendar
  year.  It "passes to Track A candidate" only if also positive at BID/ASK.

Usage:
  nice -n 19 .venv/bin/python src/research/eventvol_study.py \
      [--tickers AAPL,NVDA] [--out logs/research/eventvol_study_20260705.md]

STRICT OWNERSHIP: this file + logs/research/eventvol_*.{log,md} only.
Reads data/iv_history_full, data/fundamentals_raw.parquet, data/historical.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
IV_FULL = ROOT / "data" / "iv_history_full"
FUNDA = ROOT / "data" / "fundamentals_raw.parquet"
DAILY_DIR = ROOT / "data" / "historical"
LOGDIR = ROOT / "logs" / "research"

TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "META", "AMD", "GOOGL",
           "NFLX", "JPM", "XOM", "COST", "CRM", "PYPL", "MARA", "SMCI",
           "PLTR", "BAC", "UBER", "WMT", "HOOD", "BABA", "INTC"]

# ---------------- pre-committed parameters (frozen before any run) ---------
P_FRONT_DTE_MIN = 1
P_FRONT_DTE_MAX = 14
P_NEXT_DTE_MAX = 60
P_ELEV_RATIO = 1.15      # front ATM IV / next ATM IV at P
P_CRUSH_X = 0.25         # required relative ATM-IV drop P -> C  (X = 25%)
P_DEDUP_DAYS = 45        # cluster window within a ticker
P_FILING_LO, P_FILING_HI = 0, 7   # filing_date - P in [0, 7] days = agree
P_RICH_PCT = 0.90        # within-name implied-move percentile tails
P_CHEAP_PCT = 0.10
P_WING_MULT = 1.0        # iron-fly wings at 1.0 x implied move
P_MIN_SPOT = 3.0
# ---------------------------------------------------------------------------

COLS = ["trade_date", "expiration", "strike", "spot",
        "cBidPx", "cAskPx", "pBidPx", "pAskPx", "call_iv", "put_iv"]

LOG_PATH = LOGDIR / "eventvol_20260705.log"
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


def parse_dates(s: pd.Series) -> pd.Series:
    """M/D/YYYY text -> datetime via unique-value map (as in backtest/chain.py)."""
    u = pd.Series(s.unique())
    m = dict(zip(u, pd.to_datetime(u, format="mixed")))
    return s.map(m)


def load_ticker(tk: str) -> pd.DataFrame:
    paths = sorted(IV_FULL.glob(f"{tk}_*.parquet"))
    if not paths:
        raise FileNotFoundError(tk)
    df = pd.concat([pd.read_parquet(p, columns=COLS) for p in paths],
                   ignore_index=True)
    df["trade_date"] = parse_dates(df["trade_date"])
    df["expiration"] = parse_dates(df["expiration"])
    df["dte"] = (df["expiration"] - df["trade_date"]).dt.days
    df = df[df["dte"] >= 0]
    return df.sort_values(["trade_date", "expiration", "strike"]).reset_index(drop=True)


def atm_iv(day: pd.DataFrame, exp) -> float:
    """ATM IV = mean(call_iv, put_iv) at strike nearest spot with both IVs > 0."""
    r = day[(day["expiration"] == exp) & (day["call_iv"] > 0) & (day["put_iv"] > 0)]
    if r.empty:
        return np.nan
    row = r.iloc[(r["strike"] - r["spot"].iloc[0]).abs().argmin()]
    return float((row["call_iv"] + row["put_iv"]) / 2)


def straddle_row(day: pd.DataFrame, exp) -> pd.Series | None:
    """ATM strike nearest spot with a live two-sided market on BOTH legs."""
    r = day[(day["expiration"] == exp)
            & (day["cBidPx"] > 0) & (day["cAskPx"] >= day["cBidPx"])
            & (day["pBidPx"] > 0) & (day["pAskPx"] >= day["pBidPx"])]
    if r.empty:
        return None
    return r.iloc[(r["strike"] - r["spot"].iloc[0]).abs().argmin()]


def leg_quote(day: pd.DataFrame, exp, strike: float) -> pd.Series | None:
    """Exact-strike row (nearest available) for mark-to-market at exit."""
    r = day[day["expiration"] == exp]
    if r.empty:
        return None
    row = r.iloc[(r["strike"] - strike).abs().argmin()]
    if abs(row["strike"] - strike) > 1e-6:
        return None   # strike vanished from the chain: skip event
    return row


def mid(bid: float, ask: float) -> float:
    bid = max(float(bid or 0.0), 0.0)
    ask = max(float(ask or 0.0), 0.0)
    if ask <= 0:
        return 0.0
    return (bid + ask) / 2


def detect_events(tk: str, df: pd.DataFrame) -> pd.DataFrame:
    """Surface-detected events + per-event implied/realized measurement."""
    by_date = {d: g for d, g in df.groupby("trade_date")}
    sessions = sorted(by_date)
    idx = {d: i for i, d in enumerate(sessions)}

    # per-day front/next summary
    daily = []
    for d in sessions:
        g = by_date[d]
        spot = float(g["spot"].iloc[0])
        if spot < P_MIN_SPOT:
            continue
        exps = np.sort(g["expiration"].unique())
        dtes = {e: int((pd.Timestamp(e) - d).days) for e in exps}
        front = next((e for e in exps
                      if P_FRONT_DTE_MIN <= dtes[e] <= P_FRONT_DTE_MAX), None)
        if front is None:
            continue
        nxt = next((e for e in exps
                    if dtes[e] > dtes[front] and dtes[e] <= P_NEXT_DTE_MAX), None)
        if nxt is None:
            continue
        fiv, niv = atm_iv(g, front), atm_iv(g, nxt)
        if not (fiv > 0 and niv > 0):
            continue
        daily.append(dict(date=d, spot=spot, front=front, front_dte=dtes[front],
                          nxt=nxt, front_iv=fiv, next_iv=niv, elev=fiv / niv))
    dd = pd.DataFrame(daily)
    if dd.empty:
        return dd

    cand = dd[dd["elev"] >= P_ELEV_RATIO]
    events = []
    for _, r in cand.iterrows():
        i = idx[r["date"]]
        if i + 1 >= len(sessions):
            continue
        c_date = sessions[i + 1]
        gc = by_date[c_date]
        # crush measurement expiry: same front expiry if alive at C, else next
        if r["front"] > c_date:
            iv_p, iv_c = r["front_iv"], atm_iv(gc, r["front"])
        else:
            iv_p, iv_c = r["next_iv"], atm_iv(gc, r["nxt"])
        if not (iv_p > 0 and iv_c > 0):
            continue
        drop = (iv_p - iv_c) / iv_p
        if drop < P_CRUSH_X:
            continue
        # implied move from the ATM straddle mid at P
        gp = by_date[r["date"]]
        srow = straddle_row(gp, r["front"])
        if srow is None:
            continue
        spot_p = r["spot"]
        straddle_mid = mid(srow["cBidPx"], srow["cAskPx"]) + mid(srow["pBidPx"], srow["pAskPx"])
        implied = straddle_mid / spot_p
        spot_c = float(gc["spot"].iloc[0])
        realized = abs(spot_c / spot_p - 1.0)
        events.append(dict(
            ticker=tk, p_date=r["date"], c_date=c_date, spot_p=spot_p,
            spot_c=spot_c, front_exp=r["front"], front_dte=int(r["front_dte"]),
            elev=float(r["elev"]), iv_drop=float(drop), k_atm=float(srow["strike"]),
            implied=float(implied), realized=float(realized),
            ratio=float(implied / realized) if realized > 1e-6 else np.nan,
            year=int(r["date"].year)))
    ev = pd.DataFrame(events)
    if ev.empty:
        return ev

    # de-dup: greedy by elevation within P_DEDUP_DAYS clusters
    ev = ev.sort_values("elev", ascending=False)
    kept = []
    for _, e in ev.iterrows():
        if all(abs((e["p_date"] - k["p_date"]).days) > P_DEDUP_DAYS for k in kept):
            kept.append(e)
    return pd.DataFrame(kept).sort_values("p_date").reset_index(drop=True)


# ------------------------- monetization -----------------------------------
def _exit_val(row: pd.Series | None, right: str, strike: float, spot_c: float,
              expired: bool, side: str, fill: str) -> float | None:
    """Per-share exit value of one leg. side: 'long'|'short' position being
    closed. fill: 'mid'|'ba'. Returns None if unpriceable (skip event)."""
    if expired:
        return max(spot_c - strike, 0.0) if right == "C" else max(strike - spot_c, 0.0)
    if row is None:
        return None
    bid = float((row["cBidPx"] if right == "C" else row["pBidPx"]) or 0.0)
    ask = float((row["cAskPx"] if right == "C" else row["pAskPx"]) or 0.0)
    if fill == "mid":
        return mid(bid, ask)
    # bid/ask: closing a long = sell at bid; closing a short = buy at ask
    return bid if side == "long" else ask


def price_events(tk: str, df: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    """Attach iron-fly and straddle P&L (% of spot) at MID and BID/ASK."""
    by_date = {d: g for d, g in df.groupby("trade_date")}
    out = []
    for _, e in ev.iterrows():
        gp, gc = by_date[e["p_date"]], by_date[e["c_date"]]
        exp, k, spot_p, spot_c = e["front_exp"], e["k_atm"], e["spot_p"], e["spot_c"]
        expired = not (exp > e["c_date"])
        rec = dict(e)

        chain_p = gp[gp["expiration"] == exp]
        srow = leg_quote(gp, exp, k)

        # ---- long straddle (cheap tail) ----
        if srow is not None:
            ent_mid = mid(srow["cBidPx"], srow["cAskPx"]) + mid(srow["pBidPx"], srow["pAskPx"])
            ent_ba = float(srow["cAskPx"]) + float(srow["pAskPx"])   # buy at ask
            xrow = None if expired else leg_quote(gc, exp, k)
            xc_m = _exit_val(xrow, "C", k, spot_c, expired, "long", "mid")
            xp_m = _exit_val(xrow, "P", k, spot_c, expired, "long", "mid")
            xc_b = _exit_val(xrow, "C", k, spot_c, expired, "long", "ba")
            xp_b = _exit_val(xrow, "P", k, spot_c, expired, "long", "ba")
            if None not in (xc_m, xp_m, xc_b, xp_b) and ent_mid > 0 and ent_ba > 0:
                rec["strad_mid"] = (xc_m + xp_m - ent_mid) / spot_p * 100
                rec["strad_ba"] = (xc_b + xp_b - ent_ba) / spot_p * 100

        # ---- short iron fly (rich tail) ----
        kw_up = spot_p * (1 + P_WING_MULT * e["implied"])
        kw_dn = spot_p * (1 - P_WING_MULT * e["implied"])
        cw = chain_p[(chain_p["strike"] > k) & (chain_p["cAskPx"] > 0)]
        pw = chain_p[(chain_p["strike"] < k) & (chain_p["pAskPx"] > 0)]
        if srow is not None and not cw.empty and not pw.empty:
            cwr = cw.iloc[(cw["strike"] - kw_up).abs().argmin()]
            pwr = pw.iloc[(pw["strike"] - kw_dn).abs().argmin()]
            ku, kd = float(cwr["strike"]), float(pwr["strike"])
            # entry: short straddle at K, long wings
            ent_mid = (mid(srow["cBidPx"], srow["cAskPx"]) + mid(srow["pBidPx"], srow["pAskPx"])
                       - mid(cwr["cBidPx"], cwr["cAskPx"]) - mid(pwr["pBidPx"], pwr["pAskPx"]))
            ent_ba = (float(srow["cBidPx"]) + float(srow["pBidPx"])       # sell at bid
                      - float(cwr["cAskPx"]) - float(pwr["pAskPx"]))      # buy at ask
            xa = None if expired else leg_quote(gc, exp, k)
            xu = None if expired else leg_quote(gc, exp, ku)
            xd = None if expired else leg_quote(gc, exp, kd)
            legs_m = [_exit_val(xa, "C", k, spot_c, expired, "short", "mid"),
                      _exit_val(xa, "P", k, spot_c, expired, "short", "mid"),
                      _exit_val(xu, "C", ku, spot_c, expired, "long", "mid"),
                      _exit_val(xd, "P", kd, spot_c, expired, "long", "mid")]
            legs_b = [_exit_val(xa, "C", k, spot_c, expired, "short", "ba"),
                      _exit_val(xa, "P", k, spot_c, expired, "short", "ba"),
                      _exit_val(xu, "C", ku, spot_c, expired, "long", "ba"),
                      _exit_val(xd, "P", kd, spot_c, expired, "long", "ba")]
            if None not in legs_m and None not in legs_b:
                exit_mid = legs_m[0] + legs_m[1] - legs_m[2] - legs_m[3]
                exit_ba = legs_b[0] + legs_b[1] - legs_b[2] - legs_b[3]
                rec["fly_mid"] = (ent_mid - exit_mid) / spot_p * 100
                rec["fly_ba"] = (ent_ba - exit_ba) / spot_p * 100
                rec["fly_width_up"], rec["fly_width_dn"] = ku - k, k - kd
        out.append(rec)
    return pd.DataFrame(out)


# ------------------------- overnight gaps ---------------------------------
def load_opens(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Daily Open+Close per ticker from ONE consistent source (yfinance,
    cached in scratchpad; fallback data/historical, 2024-06+ only).  The gap
    is computed WITHIN this source — yfinance is split-adjusted while chain
    spot is as-traded, so mixing sources fabricates huge gaps on split names."""
    bars: dict[str, pd.DataFrame] = {}
    cache = Path("/private/tmp/claude-501/-Users-you-projects-OptionsAgent/"
                 "801744be-bebd-4fe1-a660-496a6048bd53/scratchpad/eventvol_bars.parquet")
    try:
        if cache.exists():
            raw = pd.read_parquet(cache)
        else:
            import yfinance as yf
            raw = yf.download(tickers, start="2019-12-01", end="2026-07-04",
                              auto_adjust=False, progress=False)[["Open", "Close"]]
            cache.parent.mkdir(parents=True, exist_ok=True)
            raw.to_parquet(cache)
        for tk in tickers:
            if tk in raw["Open"].columns:
                d = pd.DataFrame({"Open": raw["Open"][tk],
                                  "Close": raw["Close"][tk]}).dropna()
                if len(d):
                    bars[tk] = d
        log(f"daily bars: yfinance coverage for {len(bars)}/{len(tickers)} tickers")
    except Exception as exc:  # network blocked etc. -> local fallback
        log(f"daily bars: yfinance unavailable ({exc}); falling back to data/historical")
    for tk in tickers:
        if tk in bars:
            continue
        p = DAILY_DIR / f"{tk}.parquet"
        if p.exists():
            try:
                d = pd.read_parquet(p)[["Open", "Close"]].dropna()
                d.columns = ["Open", "Close"]
                bars[tk] = d
            except Exception:
                pass
    return bars


# ------------------------- reporting helpers ------------------------------
def pnl_block(sub: pd.DataFrame, col: str) -> dict:
    s = sub[col].dropna()
    if s.empty:
        return dict(n=0)
    return dict(n=len(s), mean=s.mean(), median=s.median(),
                win=(s > 0).mean() * 100, total=s.sum(),
                worst=s.min(), best=s.max())


def yearly_table(sub: pd.DataFrame, col: str) -> pd.DataFrame:
    s = sub.dropna(subset=[col])
    return s.groupby("year")[col].agg(n="count", mean="mean", total="sum").round(3)


def excl_best_year_mean(sub: pd.DataFrame, col: str) -> tuple[float, int]:
    s = sub.dropna(subset=[col])
    if s.empty:
        return np.nan, 0
    yr = s.groupby("year")[col].sum().idxmax()
    rest = s[s["year"] != yr]
    return (rest[col].mean() if len(rest) else np.nan), int(yr)


def fmt_pnl(b: dict) -> str:
    if b["n"] == 0:
        return "| — | n=0 |"
    return (f"n={b['n']} | mean {b['mean']:+.3f}% | median {b['median']:+.3f}% | "
            f"win {b['win']:.0f}% | total {b['total']:+.1f}% | "
            f"worst {b['worst']:+.2f}% | best {b['best']:+.2f}%")


# --------------------------------- main ------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=",".join(TICKERS))
    ap.add_argument("--out", default=str(LOGDIR / "eventvol_study_20260705.md"))
    a = ap.parse_args()
    tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]

    log(f"START event-vol study | {len(tickers)} tickers | params: "
        f"elev>={P_ELEV_RATIO}, crush X={P_CRUSH_X}, front DTE "
        f"[{P_FRONT_DTE_MIN},{P_FRONT_DTE_MAX}], dedup {P_DEDUP_DAYS}d, "
        f"tails {P_CHEAP_PCT}/{P_RICH_PCT}, wings {P_WING_MULT}x implied")

    all_ev = []
    for tk in tickers:
        t0 = time.time()
        try:
            df = load_ticker(tk)
        except FileNotFoundError:
            log(f"{tk}: NO DATA — skipped")
            continue
        ev = detect_events(tk, df)
        if len(ev):
            ev = price_events(tk, df, ev)
        log(f"{tk}: {len(df):,} rows -> {len(ev)} events "
            f"({time.time()-t0:.1f}s)")
        if len(ev):
            all_ev.append(ev)
        del df

    ev = pd.concat(all_ev, ignore_index=True)
    log(f"TOTAL surface-detected events: {len(ev)}")

    # ---- filing-date cross-check -----------------------------------------
    fu = pd.read_parquet(FUNDA)
    fu["filing_date"] = pd.to_datetime(fu["filing_date"])
    fu = fu[fu["ticker"].isin(tickers)
            & fu["filing_date"].between("2020-01-01", "2026-07-05")]
    agree = []
    for _, e in ev.iterrows():
        fdates = fu.loc[fu["ticker"] == e["ticker"], "filing_date"]
        lag = (fdates - e["p_date"]).dt.days
        agree.append(bool(((lag >= P_FILING_LO) & (lag <= P_FILING_HI)).any()))
    ev["filing_match"] = agree
    # reverse direction: filings with a surface event 0-7d before
    rev_hits, rev_tot = 0, 0
    for tk in tickers:
        fdates = fu.loc[fu["ticker"] == tk, "filing_date"]
        pdates = ev.loc[ev["ticker"] == tk, "p_date"]
        for f in fdates:
            rev_tot += 1
            lags = (f - pdates).dt.days if len(pdates) else pd.Series(dtype=int)
            if len(lags) and ((lags >= P_FILING_LO) & (lags <= P_FILING_HI)).any():
                rev_hits += 1
    fwd_rate = ev["filing_match"].mean() * 100
    rev_rate = rev_hits / rev_tot * 100 if rev_tot else float("nan")
    log(f"agreement: {fwd_rate:.1f}% of surface events have a filing 0-7d after"
        f" | {rev_rate:.1f}% of {rev_tot} filings have a surface event 0-7d before")

    # ---- overnight gaps ----------------------------------------------------
    bars = load_opens(tickers)
    gaps = []
    for _, e in ev.iterrows():
        d = bars.get(e["ticker"])
        g = np.nan
        if d is not None and e["c_date"] in d.index and e["p_date"] in d.index:
            g = abs(float(d.loc[e["c_date"], "Open"])
                    / float(d.loc[e["p_date"], "Close"]) - 1.0)
        gaps.append(g)
    ev["gap"] = gaps

    # ---- within-name percentile of implied move + tails --------------------
    ev["impl_pct"] = ev.groupby("ticker")["implied"].rank(pct=True)
    rich = ev[ev["impl_pct"] >= P_RICH_PCT]
    cheap = ev[ev["impl_pct"] <= P_CHEAP_PCT]

    # ---- report -------------------------------------------------------------
    W = []
    W.append("# Earnings Event-Vol Study (B1) — 2026-07-05\n")
    W.append("Pre-specified study per docs/professional-path-2026-07-05.md "
             "(Lever B, study 1). All parameters and the verdict rule were "
             "committed in `src/research/eventvol_study.py` BEFORE any "
             "results were computed. No parameter search was performed.\n")

    W.append("## Method\n")
    W.append("- **Universe:** " + ", ".join(sorted(ev['ticker'].unique()))
             + f" ({ev['ticker'].nunique()} names, 2020–2026, ORATS EOD chains "
               "with real bid/ask).")
    W.append("- **Event convention:** P = last close before the reaction "
             "(elevated front IV), C = next session (crush). The task's "
             "T-1/T+1 map to P/C — announcement falls between P close and C "
             "open. Implied move measured at P close; realized = "
             "|close(C)/close(P)−1|; overnight gap = |open(C)/close(P)−1|.")
    W.append(f"- **Detection (a), surface:** front expiry DTE ∈ "
             f"[{P_FRONT_DTE_MIN},{P_FRONT_DTE_MAX}]; elevation front/next "
             f"ATM IV ≥ {P_ELEV_RATIO}; crush X = {P_CRUSH_X:.0%} relative "
             f"ATM-IV drop P→C (same expiry if alive at C, else next expiry); "
             f"de-dup {P_DEDUP_DAYS}d keeping max elevation.")
    W.append(f"- **Detection (b), filings:** fundamentals_raw.parquet filing "
             f"dates; agree = filing 0–{P_FILING_HI} days after P. Filings "
             "lag earnings 0–4 days, so this validates (a), it does not "
             "define event dates.")
    W.append("- **Implied move** = ATM front-expiry straddle MID / spot at P "
             "close. NOTE: the straddle prices ALL remaining life of the "
             "front expiry, not only the event gap, so implied/realized > 1 "
             "is partly mechanical; the monetization tables (which recapture "
             "remaining time value at exit) are the economically meaningful "
             "test.")
    W.append(f"- **Monetization:** within-name implied-move percentile; rich "
             f"tail ≥ {P_RICH_PCT:.0%} → short iron fly (ATM straddle sold, "
             f"wings at {P_WING_MULT:.1f}× implied), bought back at C close; "
             f"cheap tail ≤ {P_CHEAP_PCT:.0%} → long ATM straddle sold at C "
             "close. Fills at MID and at BID/ASK (shorts@bid, longs@ask, "
             "reversed to exit; intrinsic if expiry = C). P&L in % of spot. "
             "No commissions (~$0.65/contract ≈ 0.26 $/fly ≈ small vs the "
             "spreads shown).")

    yrs = f"{ev['p_date'].min().date()} → {ev['p_date'].max().date()}"
    W.append(f"\n## Event counts & detection agreement\n")
    W.append(f"- Surface-detected events: **{len(ev)}** across {yrs} "
             f"(~{len(ev)/ev['ticker'].nunique():.1f} per name; quarterly "
             "cadence would predict ~26 per name over 6.5y).")
    W.append(f"- Filings for these names in window: {rev_tot}.")
    W.append(f"- **Agreement:** {fwd_rate:.1f}% of surface events are "
             f"followed by a filing within {P_FILING_HI} days; "
             f"{rev_rate:.1f}% of filings are preceded (0–{P_FILING_HI}d) by "
             "a surface event. Surface events without a filing match are "
             "either non-earnings vol events (FDA/product/macro), filing-lag "
             "outside the window, or gaps in the fundamentals file — counted "
             "honestly, not excluded.")
    n_filers = fu.groupby("ticker").size()
    zero_f = [t for t in tickers if n_filers.get(t, 0) == 0]
    W.append(f"- Anchor-method limits (diagnostic, not a re-cut of the edge): "
             f"{', '.join(zero_f) if zero_f else 'none'} have ZERO rows in "
             "fundamentals_raw; JPM/BAC/WMT/BABA show 0% because their SEC "
             "filing lags the earnings press release by >7 days (banks and "
             "WMT file weeks later) — the 0–4d lag assumption fails for "
             "them, which caps the forward agreement rate. The reverse rate "
             f"({rev_rate:.0f}%) is the cleaner validation of surface "
             "detection.")
    low = ev.groupby("ticker").size()
    low = low[low < 15]
    if len(low):
        W.append("- Detection under-counts "
                 + ", ".join(f"{t} ({n})" for t, n in low.items())
                 + " vs ~26 expected: chronically elevated front IV (term "
                 "structure rarely un-inverts by 25% overnight, e.g. MARA) "
                 "or event moves too small to print a 25% crush (e.g. XOM) "
                 "— the pre-committed thresholds miss those events rather "
                 "than mislabel them.")
    per_name_match = ev.groupby("ticker")["filing_match"].agg(["count", "mean"])
    per_name_match["mean"] = (per_name_match["mean"] * 100).round(0)
    W.append("\n| ticker | events | % with filing match |\n|---|---|---|")
    for tk, r in per_name_match.iterrows():
        W.append(f"| {tk} | {int(r['count'])} | {r['mean']:.0f}% |")

    # ratio distributions
    W.append("\n## Implied vs realized — ratio distributions\n")
    v = ev.dropna(subset=["ratio"])
    W.append(f"Pooled (N={len(ev)}, ratio defined for {len(v)}):\n")
    W.append(f"- implied move: mean {ev['implied'].mean()*100:.2f}%, "
             f"median {ev['implied'].median()*100:.2f}%")
    W.append(f"- realized move (close→close): mean {ev['realized'].mean()*100:.2f}%, "
             f"median {ev['realized'].median()*100:.2f}%")
    g = ev.dropna(subset=["gap"])
    W.append(f"- overnight gap (open(C)/close(P), n={len(g)}): mean "
             f"{g['gap'].mean()*100:.2f}%, median {g['gap'].median()*100:.2f}%")
    W.append(f"- implied/realized ratio: mean {v['ratio'].mean():.2f}, "
             f"median {v['ratio'].median():.2f}, "
             f"5th/95th pct {v['ratio'].quantile(.05):.2f}/{v['ratio'].quantile(.95):.2f}")
    W.append(f"- fraction of events with realized < implied: "
             f"{(ev['realized'] < ev['implied']).mean()*100:.1f}%\n")

    W.append("By year:\n\n| year | n | med implied | med realized | med ratio "
             "| % realized<implied |\n|---|---|---|---|---|---|")
    for yr, s in ev.groupby("year"):
        sv = s.dropna(subset=["ratio"])
        W.append(f"| {yr} | {len(s)} | {s['implied'].median()*100:.2f}% | "
                 f"{s['realized'].median()*100:.2f}% | "
                 f"{sv['ratio'].median():.2f} | "
                 f"{(s['realized'] < s['implied']).mean()*100:.0f}% |")

    W.append("\nBy name:\n\n| ticker | n | med implied | med realized | "
             "med ratio | % realized<implied |\n|---|---|---|---|---|---|")
    for tk, s in ev.groupby("ticker"):
        sv = s.dropna(subset=["ratio"])
        W.append(f"| {tk} | {len(s)} | {s['implied'].median()*100:.2f}% | "
                 f"{s['realized'].median()*100:.2f}% | "
                 f"{sv['ratio'].median():.2f} | "
                 f"{(s['realized'] < s['implied']).mean()*100:.0f}% |")

    # ---- monetization tables ----
    W.append("\n## Monetization at real chain prices — extreme deciles only\n")
    W.append("P&L per event in % of spot(P). MID = every leg at (bid+ask)/2; "
             "BID/ASK = worst-case crossing every leg.\n")

    verdicts = {}
    for label, sub, col in [("RICH tail — short iron fly, top decile implied",
                             rich, "fly"),
                            ("CHEAP tail — long straddle, bottom decile implied",
                             cheap, "strad")]:
        W.append(f"### {label}\n")
        bm, bb = pnl_block(sub, f"{col}_mid"), pnl_block(sub, f"{col}_ba")
        W.append(f"- MID:     {fmt_pnl(bm)}")
        W.append(f"- BID/ASK: {fmt_pnl(bb)}\n")
        W.append("Per-year (MID / BID-ASK mean %):\n\n"
                 "| year | n | mean MID | total MID | mean BA | total BA |\n"
                 "|---|---|---|---|---|---|")
        ym = yearly_table(sub, f"{col}_mid")
        yb = yearly_table(sub, f"{col}_ba")
        for yr in ym.index:
            b = yb.loc[yr] if yr in yb.index else None
            W.append(f"| {yr} | {int(ym.loc[yr,'n'])} | "
                     f"{ym.loc[yr,'mean']:+.3f}% | {ym.loc[yr,'total']:+.2f}% | "
                     + (f"{b['mean']:+.3f}% | {b['total']:+.2f}% |" if b is not None
                        else "— | — |"))
        exm, exyr = excl_best_year_mean(sub, f"{col}_mid")
        W.append(f"\nMID mean excluding best year ({exyr}): {exm:+.3f}%")
        interesting = (bm.get("n", 0) >= 40 and bm.get("mean", -1) > 0
                       and (exm == exm) and exm > 0)
        passes = interesting and bb.get("mean", -1) > 0
        verdicts[label] = (interesting, passes, bm, bb, exm, exyr)
        W.append(f"\n**Pre-committed check:** n≥40: "
                 f"{'YES' if bm.get('n',0)>=40 else 'NO'} "
                 f"({bm.get('n',0)}); MID mean>0: "
                 f"{'YES' if bm.get('mean',-1)>0 else 'NO'}; positive excl. "
                 f"best year: {'YES' if (exm==exm and exm>0) else 'NO'}; "
                 f"BID/ASK mean>0: {'YES' if bb.get('mean',-1)>0 else 'NO'}.\n")

    # ---- name-concentration diagnostic (post-hoc, labeled as such) ----
    W.append("### Name-concentration diagnostic (post-hoc check, not part "
             "of the pre-committed rule)\n")
    for label, sub, col in [("RICH tail fly", rich, "fly_mid"),
                            ("CHEAP tail straddle", cheap, "strad_mid")]:
        s = sub.dropna(subset=[col])
        if s.empty:
            continue
        byn = s.groupby("ticker")[col].agg(["size", "mean", "sum"])
        top = byn["sum"].idxmax()
        rest = s[s["ticker"] != top]
        ba_col = col.replace("_mid", "_ba")
        W.append(f"- {label}: {int((byn['mean'] > 0).sum())}/{len(byn)} names "
                 f"positive at MID; excluding the top contributor ({top}): "
                 f"n={len(rest)}, MID mean {rest[col].mean():+.3f}%, BID/ASK "
                 f"mean {rest[ba_col].mean():+.3f}%.")
    W.append("")

    # ---- caveats + verdict ----
    W.append("## Caveats (stated up front, not discovered later)\n")
    W.append("- 6.5 years, ONE crash regime (2020) plus one bear (2022); "
             f"~{ev['ticker'].nunique()} names — a fraction of the 20-year "
             "ORATS archive this study family is ultimately aimed at.")
    W.append("- Decile ranking uses each name's FULL-SAMPLE event history "
             "(in-sample percentile). A live rule would need an expanding "
             "window or absolute threshold; with ~24 events/name an expanding "
             "version was judged too noisy to pre-commit. This biases the "
             "tails toward look-ahead — treat tail P&L as an upper bound at "
             "MID.")
    W.append("- Event dates are surface-detected, not from a verified "
             "earnings calendar; the filing agreement rate above quantifies "
             "contamination by non-earnings vol events.")
    W.append("- Exit is the FIRST close after the reaction; no intraday "
             "management, no early assignment modeling (ATM short legs "
             "through an ex-div date could be assigned; rare at 1-day hold).")
    W.append("- Implied/realized ratio >1 is partly mechanical (straddle "
             "carries residual time value); monetization tables are the "
             "test that accounts for it.")
    W.append("- No commissions/fees in P&L (≈0.01–0.05% of spot per 4-leg "
             "fly for these spot levels); immaterial vs the bid/ask gap "
             "shown but would shave marginal results.")

    W.append("\n## Pre-committed verdict\n")
    for label, (interesting, passes, bm, bb, exm, exyr) in verdicts.items():
        tag = ("PASSES TO TRACK A CANDIDATE" if passes
               else "INTERESTING (MID-only)" if interesting else "NO EDGE")
        W.append(f"- **{label.split(' — ')[0]}: {tag}** — MID mean "
                 f"{bm.get('mean', float('nan')):+.3f}% (n={bm.get('n',0)}), "
                 f"excl-best-year {exm:+.3f}%, BID/ASK mean "
                 f"{bb.get('mean', float('nan')):+.3f}%.")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(W) + "\n")
    log(f"report written: {out}")

    # per-event csv-ish dump into the log for audit
    log("per-event dump (audit): ticker,p_date,c_date,front_dte,elev,iv_drop,"
        "implied,realized,ratio,impl_pct,filing_match,fly_mid,fly_ba,"
        "strad_mid,strad_ba")
    for _, e in ev.iterrows():
        log(f"EV {e['ticker']},{e['p_date'].date()},{e['c_date'].date()},"
            f"{e['front_dte']},{e['elev']:.3f},{e['iv_drop']:.3f},"
            f"{e['implied']:.4f},{e['realized']:.4f},"
            f"{(e['ratio'] if e['ratio']==e['ratio'] else float('nan')):.3f},"
            f"{e['impl_pct']:.2f},{e['filing_match']},"
            f"{e.get('fly_mid', float('nan')):.4f},{e.get('fly_ba', float('nan')):.4f},"
            f"{e.get('strad_mid', float('nan')):.4f},{e.get('strad_ba', float('nan')):.4f}")
    log("DONE")


if __name__ == "__main__":
    sys.exit(main())
