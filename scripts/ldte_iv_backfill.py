#!/usr/bin/env python3
"""LDTE-PREM sleeve — IV-term feature history backfill (ThetaData EOD).

Builds data/iv_term/{TICKER}.csv: per session, the ATM-straddle vol proxy at
the front (25-40 DTE, nearest 30) and back (75-105, nearest 90) expiries, and
term_slope = front - back. Proxy: sigma ~ straddle_mid / (spot * sqrt(T));
model-free, monotone in IV — valid for the PERCENTILE feature the sleeve
conditions on (config/ldte_prem_forward_criteria.yaml). Single source
(ThetaData EOD) for history AND go-forward, per the frozen criteria.

Run: .venv/bin/python scripts/ldte_iv_backfill.py [--sessions 270]
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.data import thetadata as td  # noqa: E402

BASE = "http://127.0.0.1:25503"
OUT = ROOT / "data" / "iv_term"
# Universe per config/ldte_prem_forward_criteria.yaml AMENDMENT 2026-07-18
UNIVERSE = ["SPY", "QQQ", "IWM", "AAPL", "ADBE", "AMD", "AMZN", "APP", "ARM",
            "ASML", "AVGO", "BA", "BABA", "BAC", "BIDU", "CAT", "COST", "CRM",
            "DELL", "FDX", "FICO", "FSLR", "GOOG", "GOOGL", "GS", "IBM",
            "INTC", "JPM", "LLY", "LRCX", "LULU", "MARA", "MSFT", "MSTR",
            "MU", "NFLX", "NKE", "NVDA", "ORCL", "OXY", "PYPL", "SMCI",
            "TSLA", "TSM", "UAL", "UNH", "V", "WFC", "WMT", "XOM"]
FRONT = (25, 40, 30)
BACK = (75, 105, 90)


def list_strikes(symbol: str, expiration: str) -> list[float]:
    r = requests.get(f"{BASE}/v3/option/list/strikes",
                     params={"symbol": symbol, "expiration": expiration},
                     timeout=30)
    if r.status_code != 200 or r.text.startswith(("No data", "<")):
        return []
    df = pd.read_csv(pd.io.common.StringIO(r.text))
    col = "strike" if "strike" in df.columns else df.columns[-1]
    return sorted(float(x) for x in df[col].unique())


def eod_range(symbol, expiration, strike, right, d0, d1) -> pd.DataFrame | None:
    r = requests.get(f"{BASE}/v3/option/history/eod", params={
        "symbol": symbol, "expiration": expiration, "strike": f"{strike:g}",
        "right": right, "start_date": d0, "end_date": d1}, timeout=60)
    if r.status_code != 200 or r.text.startswith(("No data", "<")):
        return None
    df = pd.read_csv(pd.io.common.StringIO(r.text))
    if df.empty or "bid" not in df.columns:
        return None
    df["date"] = df["created"].str[:10]
    df["mid"] = (df["bid"] + df["ask"]) / 2
    return df[["date", "mid", "bid", "ask"]]


def pick_expiry(exps: list[str], d: str, lo: int, hi: int, target: int):
    dd = date.fromisoformat(d)
    best, bdist = None, 10**9
    for e in exps:
        dte = (date.fromisoformat(e) - dd).days
        if lo <= dte <= hi and abs(dte - target) < bdist:
            best, bdist = e, abs(dte - target)
    return best


def spot_series(t: str, n: int) -> pd.Series | None:
    f = ROOT / "data" / "historical" / f"{t}.parquet"
    if f.exists():
        df = pd.read_parquet(f)
        close = df["close" if "close" in df.columns else "Close"]
        idx = pd.to_datetime(df.index if df.index.dtype.kind == "M" else df["date"])
        return pd.Series(close.values, index=idx.strftime("%Y-%m-%d")).tail(n)
    # fallback: ThetaData stock EOD close (amendment 2026-07-18 — names
    # outside the local daily store; same source family as the option feed)
    from datetime import timedelta
    d1 = date.today().isoformat()
    d0 = (date.today() - timedelta(days=int(n * 1.6) + 10)).isoformat()
    r = requests.get(f"{BASE}/v3/stock/history/eod",
                     params={"symbol": t, "start_date": d0, "end_date": d1},
                     timeout=60)
    if r.status_code != 200 or r.text.startswith(("No data", "<")):
        return None
    df = pd.read_csv(pd.io.common.StringIO(r.text))
    if df.empty or "close" not in df.columns:
        return None
    return pd.Series(df["close"].values,
                     index=df["created"].str[:10].values).tail(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=270)
    args = ap.parse_args()
    if not td.ensure_terminal():
        print("theta terminal unavailable"); return 1
    OUT.mkdir(parents=True, exist_ok=True)
    for t in UNIVERSE:
        if (OUT / f"{t}.csv").exists():
            print(f"{t}: already backfilled — skipped")
            continue
        spots = spot_series(t, args.sessions)
        if spots is None or spots.empty:
            print(f"{t}: no spot history — SKIPPED (report, do not hide)")
            continue
        exps = td.list_expirations(t)
        if not exps:
            print(f"{t}: no expirations — SKIPPED"); continue
        # plan contracts: per session, front+back expiry, ATM strike
        strikes_cache: dict[str, list[float]] = {}
        need: dict[tuple, list[tuple]] = {}   # (exp,K) -> [(date,leg)]
        plan = []
        for d, spot in spots.items():
            row = {"date": d, "spot": float(spot)}
            ok = True
            for tag, (lo, hi, tgt) in (("front", FRONT), ("back", BACK)):
                e = pick_expiry(exps, d, lo, hi, tgt)
                if e is None:
                    ok = False; break
                if e not in strikes_cache:
                    strikes_cache[e] = list_strikes(t, e)
                    time.sleep(0.2)
                ks = strikes_cache[e]
                if not ks:
                    ok = False; break
                k = min(ks, key=lambda x: abs(x - spot))
                row[tag + "_exp"], row[tag + "_k"] = e, k
                need.setdefault((e, k), []).append((d, tag))
            if ok:
                plan.append(row)
        # fetch EOD ranges per contract (call+put), index by date
        quotes: dict[tuple, dict] = {}
        for i, ((e, k), uses) in enumerate(sorted(need.items()), 1):
            d0 = min(u[0] for u in uses); d1 = max(u[0] for u in uses)
            for right in ("C", "P"):
                df = eod_range(t, e, k, right, d0, d1)
                time.sleep(0.25)
                if df is None:
                    continue
                for _, r in df.iterrows():
                    quotes[(e, k, right, r["date"])] = float(r["mid"])
            if i % 25 == 0:
                print(f"  {t}: contracts {i}/{len(need)}", flush=True)
        rows = []
        for row in plan:
            d = row["date"]
            out = dict(date=d, spot=row["spot"])
            ok = True
            for tag in ("front", "back"):
                e, k = row[tag + "_exp"], row[tag + "_k"]
                cm = quotes.get((e, k, "C", d)); pm = quotes.get((e, k, "P", d))
                if cm is None or pm is None or cm + pm <= 0:
                    ok = False; break
                dte = (date.fromisoformat(e) - date.fromisoformat(d)).days
                out[tag + "_proxy"] = (cm + pm) / (row["spot"] * math.sqrt(dte / 365))
                out[tag + "_exp"] = e
            if ok:
                out["term_slope"] = out["front_proxy"] - out["back_proxy"]
                rows.append(out)
        df = pd.DataFrame(rows)
        df.to_csv(OUT / f"{t}.csv", index=False)
        print(f"{t}: {len(df)}/{len(spots)} sessions with full term data "
              f"-> {OUT / (t + '.csv')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
