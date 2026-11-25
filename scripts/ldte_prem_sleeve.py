#!/usr/bin/env python3
"""LDTE-PREM observation sleeve — daily post-close decision (17:30 PT).

Criteria FROZEN in config/ldte_prem_forward_criteria.yaml (2026-07-18).
K21 corner: short put spread, 60-90 DTE, short leg ~30-delta / long wing
~10-delta, entered when term_slope sits in the BOTTOM QUINTILE of its
trailing 252-session window (data/iv_term, ThetaData single-source, straddle
proxy). HELD TO EXPIRY. One open position per ticker. Entry assumed at next
open; recommendation records the same-day EOD NBBO of both legs (entry
credit = short bid - long ask, full crossing, K1-conservative).

Leg deltas are Black-Scholes approximations using the proxy vol (r=4%, q=0)
— stated, not hidden. Settlement: scripts side — at expiry, intrinsic vs the
daily close of the expiry session (settle_expired() below, runs every night
within this same job). Every record stamped UNVALIDATED — PAPER ONLY.

Run: .venv/bin/python scripts/ldte_prem_sleeve.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.data import thetadata as td  # noqa: E402
from scripts.ldte_iv_backfill import (eod_range, list_strikes, pick_expiry,  # noqa: E402
                                      spot_series, UNIVERSE, OUT as IVDIR)

STAMP = "UNVALIDATED — PAPER ONLY"
LEDGER = ROOT / "logs" / "observation" / "ldte_prem_sleeve.jsonl"
ENTRY_EXPIRY = (60, 90, 75)      # DTE window for the traded expiry
SHORT_DELTA, WING_DELTA = 0.30, 0.10
PCTL_MAX = 0.20                  # bottom quintile
PCT_WIN, PCT_MIN = 252, 120
QTY = 1                          # 1 spread per entry; observation sizing


def bs_put_delta(spot, k, t_yr, sigma, r=0.04):
    if t_yr <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(spot / k) + (r + sigma**2 / 2) * t_yr) / (sigma * math.sqrt(t_yr))
    # put delta = N(d1) - 1
    return 0.5 * (1 + math.erf(d1 / math.sqrt(2))) - 1


def open_positions(ledger_rows):
    opens = {}
    for r in ledger_rows:
        if r.get("event") == "entry":
            opens[r["rec_id"]] = r
        elif r.get("event") in ("settlement",):
            opens.pop(r["rec_id"], None)
    return opens


def load_ledger():
    if not LEDGER.exists():
        return []
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]


def append(rec):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def settle_expired(rows, today: str):
    """Settle open positions whose expiry session has passed: intrinsic vs
    that session's daily close (single crossing already paid at entry)."""
    for rec in open_positions(rows).values():
        exp = rec["expiration"]
        if exp > today:
            continue
        t = rec["ticker"]
        s = spot_series(t, 400)
        if s is None or exp not in s.index:
            # expiry close not in store yet (holiday lag) — try next run
            sub = s[s.index <= exp] if s is not None else None
            if sub is None or sub.empty or \
               (date.fromisoformat(today) - date.fromisoformat(sub.index[-1])).days > 5:
                continue
            settle_spot = float(sub.iloc[-1])
        else:
            settle_spot = float(s.loc[exp])
        intr_short = max(rec["short_k"] - settle_spot, 0.0)
        intr_long = max(rec["long_k"] - settle_spot, 0.0)
        pnl = (rec["entry_credit"] - (intr_short - intr_long)) * 100 * QTY
        max_loss = (rec["short_k"] - rec["long_k"] - rec["entry_credit"]) * 100 * QTY
        append({"event": "settlement", "rec_id": rec["rec_id"], "stamp": STAMP,
                "ticker": t, "expiration": exp, "settle_spot": settle_spot,
                "pnl": round(pnl, 2),
                "R": round(pnl / max_loss, 4) if max_loss > 0 else None,
                "settled_at": datetime.now().isoformat(timespec="seconds")})
        print(f"settled {rec['rec_id']}: pnl {pnl:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    args = ap.parse_args()
    today = args.date or date.today().isoformat()
    if not td.ensure_terminal():
        print("theta terminal unavailable — no decision today (reported)")
        return 1
    rows = load_ledger()
    settle_expired(rows, today)
    rows = load_ledger()
    opens = open_positions(rows)
    open_tickers = {r["ticker"] for r in opens.values()}
    decisions = {"fired": [], "entered": [], "skipped": [], "warming": []}
    for t in UNIVERSE:
        ivf = IVDIR / f"{t}.csv"
        if not ivf.exists():
            print(f"{t}: no iv_term history — skipped (reported)"); continue
        hist = pd.read_csv(ivf)
        # append today's row if missing (same construction as backfill)
        if today not in set(hist["date"]):
            s = spot_series(t, 5)
            if s is None or today not in s.index:
                print(f"{t}: no spot for {today} — skipped (reported)"); continue
            spot = float(s.loc[today])
            exps = td.list_expirations(t)
            row = {"date": today, "spot": spot}
            ok = True
            for tag, (lo, hi, tgt) in (("front", (25, 40, 30)), ("back", (75, 105, 90))):
                e = pick_expiry(exps, today, lo, hi, tgt)
                ks = list_strikes(t, e) if e else []
                if not e or not ks:
                    ok = False; break
                k = min(ks, key=lambda x: abs(x - spot))
                mids = {}
                for right in ("C", "P"):
                    df = eod_range(t, e, k, right, today, today)
                    time.sleep(0.2)
                    mids[right] = float(df["mid"].iloc[0]) if df is not None and len(df) else None
                if mids["C"] is None or mids["P"] is None:
                    ok = False; break
                dte = (date.fromisoformat(e) - date.fromisoformat(today)).days
                row[tag + "_proxy"] = (mids["C"] + mids["P"]) / (spot * math.sqrt(dte / 365))
                row[tag + "_exp"] = e
            if not ok:
                print(f"{t}: incomplete term data for {today} — skipped (reported)")
                decisions["skipped"].append(t); continue
            row["term_slope"] = row["front_proxy"] - row["back_proxy"]
            hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
            hist.to_csv(ivf, index=False)
        hist = hist.sort_values("date").reset_index(drop=True)
        win = hist[hist["date"] < today].tail(PCT_WIN)["term_slope"]
        if len(win) < PCT_MIN:
            print(f"{t}: only {len(win)} history rows (<{PCT_MIN}) — warming up")
            decisions["warming"].append(t); continue
        cur = float(hist[hist["date"] == today]["term_slope"].iloc[0])
        pctl = float((win < cur).mean())
        fired = pctl <= PCTL_MAX
        print(f"{t}: term_slope={cur:.4f} pctl={pctl:.2f} "
              f"{'FIRED' if fired else 'no-entry'}"
              f"{' (position open)' if t in open_tickers else ''}")
        if fired:
            decisions["fired"].append(t)
        if not fired or t in open_tickers:
            continue
        # build the spread from today's EOD chain
        spot = float(hist[hist["date"] == today]["spot"].iloc[0])
        exps = td.list_expirations(t)
        e = pick_expiry(exps, today, *ENTRY_EXPIRY)
        if e is None:
            print(f"{t}: no 60-90 DTE expiry — skipped (reported)"); continue
        ks = list_strikes(t, e)
        dte = (date.fromisoformat(e) - date.fromisoformat(today)).days
        sigma = float(hist[hist["date"] == today]["front_proxy"].iloc[0])
        legs = {}
        for name, tgt in (("short", SHORT_DELTA), ("long", WING_DELTA)):
            k = min(ks, key=lambda x: abs(abs(bs_put_delta(spot, x, dte / 365, sigma)) - tgt))
            df = eod_range(t, e, k, "P", today, today)
            time.sleep(0.2)
            if df is None or not len(df):
                legs = None; break
            legs[name] = {"k": k, "bid": float(df["bid"].iloc[0]),
                          "ask": float(df["ask"].iloc[0])}
        if not legs or legs["short"]["k"] <= legs["long"]["k"]:
            print(f"{t}: leg selection failed (short_k must exceed long_k) "
                  f"— skipped (reported)"); continue
        credit = legs["short"]["bid"] - legs["long"]["ask"]   # full crossing
        if credit <= 0:
            print(f"{t}: no positive credit at crossing — skipped (reported)")
            continue
        rec_id = f"{today}-LDTE-{t}"
        append({"event": "entry", "rec_id": rec_id, "stamp": STAMP,
                "criteria": "config/ldte_prem_forward_criteria.yaml",
                "ticker": t, "expiration": e, "dte": dte,
                "short_k": legs["short"]["k"], "long_k": legs["long"]["k"],
                "short_bid": legs["short"]["bid"], "long_ask": legs["long"]["ask"],
                "entry_credit": round(credit, 3), "qty": QTY,
                "term_slope_pctl": round(pctl, 3), "spot": spot,
                "generated": datetime.now().isoformat(timespec="seconds"),
                "note": "entry assumed next open; EOD NBBO full-cross credit"})
        print(f"  ENTRY {rec_id}: {legs['short']['k']}/{legs['long']['k']}P "
              f"{e} credit {credit:.2f} [{STAMP}]")
        decisions["entered"].append(rec_id)
    append({"event": "daily_decision", "date": today, "stamp": STAMP,
            "generated": datetime.now().isoformat(timespec="seconds"),
            **{k: v for k, v in decisions.items()},
            "open_positions": len(open_tickers)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
