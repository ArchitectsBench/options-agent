#!/usr/bin/env python3
"""ENTRY-TIMING SCAN — decision support for a discretionary trader.

WHAT THIS IS. Not a setup scanner and not a signal. It answers ONE question
about a name you have ALREADY decided to buy: *is today a historically good or
bad day to enter it?* That question has a real, replicated answer; "what should
I buy" does not.

THE EVIDENCE (strategy-chase 630-cell grid, Sharadar 1998-2017 discovery,
`analysis/c1_grid_results_v2.csv`; per-trade figures are the post-2010 alpha
spread over 252/5 holding periods, i.e. what one 5-day trade is worth):

  ENTERING ON STRENGTH  (28 cells, mean post-2010 alpha -4.6%/yr, mean t -1.50)
    20-day breakout, below SMA200      -13.7%/yr  t -4.04   -27 bps per trade
    gap up >=3%, below SMA200          -13.4%/yr  t -2.47   -27 bps
    gap up >=3%, ADX >= 25             -11.6%/yr  t -2.56   -23 bps
    gap up >=3%, high ATR%             -10.5%/yr  t -2.36   -21 bps
    20-day breakout, ADX >= 25          -6.5%/yr  t -2.78   -13 bps
    volume surge, ADX >= 25             -5.6%/yr  t -2.25   -11 bps
    squeeze breakout, unconditional     -4.7%/yr  t -1.29    -9 bps

  ENTERING ON WEAKNESS  (35 cells, mean post-2010 alpha +0.7%/yr, mean t +0.62)
    3 down closes, RVOL >= 1.5          +4.3%/yr  t  2.14    +9 bps per trade
    3 down closes, ADX >= 25            +3.9%/yr  t  2.45    +8 bps
    3 down closes, below SMA200         +3.8%/yr  t  2.76    +8 bps
    RSI(2) <= 10, ADX >= 25             +3.6%/yr  t  2.17    +7 bps

The asymmetry IS the finding: the avoid side is three times the magnitude of
the prefer side and far more consistent. Chasing costs more than patience earns.

HONEST LIMITS, stated on every output surface:
  * horizon is 5 sessions, measured cross-sectionally against the SAME DAY's
    peers — it says nothing about direction, only about timing;
  * +8 bps per trade is invisible against ~450 bps of 5-day single-name noise.
    You will not feel the prefer side. You WILL feel the avoid side at -27 bps;
  * post-2010 effects are roughly a third of the 1998-2017 full-sample ones —
    the documented post-publication decay. Assume further decay;
  * nothing here is a validated strategy. No program gate was passed. This is
    decision support, and every alert is scored forward so it can be withdrawn.

    .venv/bin/python scripts/entry_timing_scan.py [--tickers AAPL,NVDA] [--notify]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GROUPED = ROOT / "data" / "research_orb" / "grouped"
LEDGER = ROOT / "logs" / "alerts" / "entry_timing.jsonl"
STAMP = ("5-session horizon, cross-sectional vs same-day peers, costs on. "
         "Discovery 1998-2017 + post-2010 split. NOT a validated strategy — "
         "entry TIMING support only.")

# (label, post-2010 alpha %/yr, t, bps per 5-day trade) — from the frozen grid.
AVOID = {
    "breakout_below_200": ("20-day breakout, below SMA200", -13.7, -4.04, -27),
    "gap_up_below_200": ("gap up >=3%, below SMA200", -13.4, -2.47, -27),
    "gap_up_adx": ("gap up >=3%, ADX >= 25", -11.6, -2.56, -23),
    "gap_up_highatr": ("gap up >=3%, high ATR%", -10.5, -2.36, -21),
    "breakout_adx": ("20-day breakout, ADX >= 25", -6.5, -2.78, -13),
    "volsurge_adx": ("volume surge, ADX >= 25", -5.6, -2.25, -11),
    "squeeze_break": ("squeeze breakout", -4.7, -1.29, -9),
}
PREFER = {
    "three_down_rvol": ("3 down closes, RVOL >= 1.5", 4.3, 2.14, 9),
    "three_down_adx": ("3 down closes, ADX >= 25", 3.9, 2.45, 8),
    "three_down_below200": ("3 down closes, below SMA200", 3.8, 2.76, 8),
    "rsi2_adx": ("RSI(2) <= 10, ADX >= 25", 3.6, 2.17, 7),
}


def load_history(tickers: set[str], n_files: int = 700) -> pd.DataFrame:
    files = sorted(GROUPED.glob("2*.parquet"))[-n_files:]
    if not files:
        raise FileNotFoundError(f"no grouped store under {GROUPED}")
    parts = []
    for f in files:
        d = pd.read_parquet(f, columns=["ticker", "open", "high", "low", "close", "volume"])
        d = d[d["ticker"].isin(tickers)]
        if not d.empty:
            d["date"] = pd.Timestamp(f.stem)
            parts.append(d)
    if not parts:
        raise ValueError("none of the requested tickers are in the grouped store")
    df = pd.concat(parts, ignore_index=True).rename(
        columns={"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"})
    return df.sort_values(["ticker", "date"], kind="mergesort").reset_index(drop=True)


def _rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    au = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    ad = (-d).clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + au / ad.replace(0, np.nan))).fillna(50.0)


def _adx(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    up, dn = h.diff(), -l.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean().replace(0, np.nan)
    pdi = 100 * pd.Series(plus, index=h.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus, index=h.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


MIN_PRICE, MIN_DDV = 5.0, 5_000_000      # c1_grid_screen_v2.py:44 eligibility
# The tested exit: 5 sessions, stop FROZEN at entry - 1.5 x ATR(14) (never
# trailed). Both numbers come from the study the base rates were measured on.
HORIZON = 5
STOP_ATR_MULT = 1.5
NYSE_HOLIDAYS = frozenset({
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})


def trading_days_after(d: pd.Timestamp, n: int) -> pd.Timestamp:
    """`n` NYSE sessions after `d` (weekends + the holiday list). Approximate
    beyond the list's horizon — it is a planning date, not an execution trigger:
    the exit rule is '5 sessions held', which the operator counts on the tape."""
    out = pd.Timestamp(d)
    while n > 0:
        out += pd.Timedelta(days=1)
        if out.weekday() < 5 and out.strftime("%Y-%m-%d") not in NYSE_HOLIDAYS:
            n -= 1
    return out
# A split in a RAW point-in-time store shows as a price ratio at a simple
# fraction (2:1 -> 0.5, 10:1 -> 0.1, reverse 1:10 -> 10). A big earnings gap
# does not: SMCI's +36% on 2024-01-19 is a real move and the name must stay in
# the scan. Matching the RATIO, not the magnitude, is what separates them —
# an earlier |move| > 35% guard silently dropped every volatile name forever.
SPLIT_RATIOS = (0.5, 1 / 3, 0.25, 0.2, 0.1, 2.0, 3.0, 4.0, 5.0, 10.0)
SPLIT_TOL = 0.03


def state_for(g: pd.DataFrame) -> dict:
    """Latest-bar entry-timing state for ONE name.

    Every definition below is copied from the study the base rates come from
    (`strategy-chase/analysis/c1_grid_screen_v2.py:100-153`) — attaching a
    measured base rate to a differently-defined trigger is how a tool quietly
    lies. Six of these differed on my first pass: relative volume is DOLLAR
    volume over its 20-day mean (not share volume), high-ATR% is against the
    name's own 252-day MEDIAN, the squeeze breakout is bandwidth within 5% of
    its 120-day minimum WITH a close above the upper band, the gap threshold is
    3% (not 2%), the volume surge also requires an UP day, and RSI(2) is a
    strict `< 10`.

    SPLIT GUARD: the grouped store is a raw point-in-time snapshot, so a split
    inside the lookback corrupts SMA200/ADX. A single-day move beyond
    SPLIT_GUARD is treated as an unadjusted corporate action and the name is
    skipped with a reason rather than silently mis-flagged."""
    c, h, l, v, o = g["c"], g["h"], g["l"], g["v"], g["o"]
    if len(g) < 260:
        return {"skipped": "insufficient history"}
    ret = c.pct_change()
    ratio = (c / c.shift(1)).dropna()
    susp = ratio[[any(abs(r / s - 1) <= SPLIT_TOL for s in SPLIT_RATIOS) for r in ratio]]
    if len(susp):
        when = g["date"].loc[susp.index[-1]].strftime("%Y-%m-%d")
        return {"skipped": f"suspected unadjusted split on {when} (ratio {susp.iloc[-1]:.2f})"}

    sma20, sma50, sma200 = c.rolling(20).mean(), c.rolling(50).mean(), c.rolling(200).mean()
    sd20 = c.rolling(20).std(ddof=0)
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1 / 14, adjust=False).mean()
    adx = _adx(h, l, c)
    rsi2 = _rsi(c, 2)
    bb_up, bb_lo = sma20 + 2 * sd20, sma20 - 2 * sd20
    bbw = (bb_up - bb_lo) / sma20.replace(0, np.nan)
    bbw_min120 = bbw.rolling(120).min()
    ddvol = c * v
    dv20 = ddvol.rolling(20).mean()
    rvol = ddvol / dv20.replace(0, np.nan)          # DOLLAR-volume relative
    atrpct = atr14 / c
    atrpct_med = atrpct.rolling(252).median()
    hi20_prev = h.rolling(20).max().shift(1)
    gap = o / c.shift(1) - 1.0
    three_dn = (ret < 0) & (ret.shift(1) < 0) & (ret.shift(2) < 0)

    i = -1
    if not (float(c.iloc[i]) >= MIN_PRICE and float(dv20.iloc[i]) >= MIN_DDV):
        return {"skipped": "below the study's price/liquidity floor"}

    below200 = bool(c.iloc[i] < sma200.iloc[i])
    adx_hi = bool(adx.iloc[i] >= 25)
    high_atr = bool(atrpct.iloc[i] > atrpct_med.iloc[i])
    atr_abs = float(atr14.iloc[i])
    rv = float(rvol.iloc[i])
    gp = float(gap.iloc[i])
    breakout = bool(c.iloc[i] > hi20_prev.iloc[i])
    sqz_break = bool(bbw.iloc[i] <= bbw_min120.iloc[i] * 1.05 and c.iloc[i] > bb_up.iloc[i])
    td = bool(three_dn.iloc[i])

    flags = {
        "breakout_below_200": breakout and below200,
        "breakout_adx": breakout and adx_hi,
        "gap_up_below_200": gp > 0.03 and below200,
        "gap_up_adx": gp > 0.03 and adx_hi,
        "gap_up_highatr": gp > 0.03 and high_atr,
        "volsurge_adx": rv > 2.0 and float(ret.iloc[i]) > 0 and adx_hi,
        "squeeze_break": sqz_break,
        "three_down_rvol": td and rv >= 1.5,
        "three_down_adx": td and adx_hi,
        "three_down_below200": td and below200,
        "rsi2_adx": bool(rsi2.iloc[i] < 10) and adx_hi,
    }
    sig_date = g["date"].iloc[i]
    return {"date": sig_date.strftime("%Y-%m-%d"), "close": round(float(c.iloc[i]), 2),
            "adx": round(float(adx.iloc[i]), 1), "rsi2": round(float(rsi2.iloc[i]), 1),
            "rvol_dollar": round(rv, 2), "gap_pct": round(gp * 100, 2),
            "below_sma200": below200,
            # everything the operator needs to place the trade without doing
            # any arithmetic: the stop DISTANCE is known tonight even though
            # the fill price is not, so it is given in dollars off the fill.
            # derived from the DISPLAYED ATR so the ticket's own arithmetic
            # reconciles: a reader who checks 1.5 x ATR against the stop
            # distance must get the printed number back, not a cent off.
            "atr14": round(atr_abs, 2),
            "stop_distance": round(STOP_ATR_MULT * round(atr_abs, 2), 2),
            "indicative_stop": round(float(c.iloc[i])
                                     - STOP_ATR_MULT * round(atr_abs, 2), 2),
            "entry_day": trading_days_after(sig_date, 1).strftime("%Y-%m-%d"),
            "exit_day": trading_days_after(sig_date, 1 + HORIZON).strftime("%Y-%m-%d"),
            "flags": {k: val for k, val in flags.items() if val}}


def scan(tickers: list[str]) -> list[dict]:
    df = load_history(set(tickers))
    rows, skipped = [], []
    for t, g in df.groupby("ticker", sort=False):
        st = state_for(g.reset_index(drop=True))
        if st.get("skipped"):
            skipped.append((t, st["skipped"]))
            continue
        if not st.get("flags"):
            continue
        av = [(AVOID[k], k) for k in st["flags"] if k in AVOID]
        pf = [(PREFER[k], k) for k in st["flags"] if k in PREFER]
        rows.append({"ticker": t, **st,
                     "avoid": [{"rule": a[0], "alpha_pct_yr": a[1], "t": a[2],
                                "bps_per_trade": a[3], "key": k} for a, k in av],
                     "prefer": [{"rule": p[0], "alpha_pct_yr": p[1], "t": p[2],
                                 "bps_per_trade": p[3], "key": k} for p, k in pf]})
    # worst (most negative) first — the avoid side is what actually matters
    rows.sort(key=lambda r: min([a["bps_per_trade"] for a in r["avoid"]], default=999))
    if skipped:
        print("skipped: " + ", ".join(f"{t} ({why})" for t, why in skipped))
    return rows


def render(rows: list[dict]) -> str:
    """A trade ticket, not a data dump: every number needed to place and manage
    the trade, with no arithmetic left for the reader. The stop is expressed as
    a DOLLAR DISTANCE off the fill because tomorrow's open is unknown tonight —
    'your fill minus $17.54' is exact whatever the open turns out to be."""
    av = [r for r in rows if r["avoid"]]
    pf = [r for r in rows if r["prefer"] and not r["avoid"]]
    out = []
    if pf:
        out.append("BUY WINDOW — enter at the open on " + pf[0]["entry_day"])
        for r in pf[:5]:
            b = max(r["prefer"], key=lambda p: p["bps_per_trade"])
            out.append(f"\n  {r['ticker']}  ({b['rule']}; close {r['close']}, ADX {r['adx']})")
            out.append(f"    ENTER  {r['entry_day']} at the open")
            out.append(f"    STOP   your fill - ${r['stop_distance']}   "
                       f"(1.5 x ATR {r['atr14']}; ~{r['indicative_stop']} if it opens flat)")
            out.append(f"    EXIT   {r['exit_day']} at the open, or sooner on a CLOSE below the stop")
            out.append(f"    odds   {b['bps_per_trade']:+d} bps/trade (t {b['t']}), ~53% win. Small tilt, not a promise.")
    if av:
        out.append("\nDON'T CHASE — historically the worst day to enter these:")
        for r in av[:5]:
            w = min(r["avoid"], key=lambda a: a["bps_per_trade"])
            out.append(f"  {r['ticker']:6s} {w['rule']:30s} {w['bps_per_trade']:+4d} bps/trade (t {w['t']})"
                       f" — wait for a pullback")
    if not out:
        out.append("No entry-timing flag on any watched name today. Nothing to do.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="comma-separated; default = config watchlist")
    ap.add_argument("--notify", action="store_true", help="push via ntfy")
    a = ap.parse_args()
    if a.tickers:
        tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    else:
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
        tickers = list(cfg["watchlist"]["default"])
    rows = scan(tickers)
    body = render(rows)
    print(body)
    print(f"\n{STAMP}")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps({"schema": "entry_timing_v1",
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "universe": tickers, "rows": rows, "stamp": STAMP}) + "\n")
    if a.notify and rows:
        from src.engine.notify import send
        send("Entry timing", body + "\n" + STAMP, priority="default", tags="hourglass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
