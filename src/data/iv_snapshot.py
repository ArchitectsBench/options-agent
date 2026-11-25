"""Daily ATM IV snapshot for the full watchlist (data-gap fix #2).

Runs at 12:55 PT (launchd com.optionsagent.ivsnapshot, weekdays) so IV is
sampled at a consistent in-session time; the nightly chain re-runs it in
--fallback mode to fill any ticker missed that day (after-hours chains
still carry ORATS IV — acceptable fallback, flagged in the row).

Each row appended to data/iv_history/{ticker}.csv via the idempotent
persist (one row per ticker per day). history_sessions tiers (consumed by
the iv_rank applicability condition):
    < 60 sessions   -> unevaluable
    60-150 sessions -> provisional
    > 150 sessions  -> full

Usage: python src/data/iv_snapshot.py [--fallback]
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.data.feeds import fetch_quotes
from src.data.options_chain import IV_DIR, atm_iv, get_chain, persist_iv_snapshot, select_expiration
from src.data.tradier import get_client

# single source of truth: config watchlist
import yaml
WATCHLIST = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())["watchlist"]["default"]
LOG_FILE = ROOT / "logs" / "iv_snapshots.log"


def history_sessions(ticker: str) -> int:
    path = IV_DIR / f"{ticker}.csv"
    if not path.exists():
        return 0
    return len(pd.read_csv(path))


def iv_tier(sessions: int) -> str:
    if sessions < 60:
        return "unevaluable"
    if sessions <= 150:
        return "provisional"
    return "full"


def is_trading_day() -> bool:
    """Never write IV rows for non-trading days — weekend/holiday rows would
    pollute the 60/150-session tier counts with stale chain data."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    cal = get_client().calendar(now.month, now.year)
    if cal and now.strftime("%Y-%m-%d") in cal:
        return cal[now.strftime("%Y-%m-%d")]["status"] == "open"
    return True  # calendar unavailable: weekday heuristic already applied


def snapshot(fallback: bool = False) -> list[dict]:
    if not is_trading_day():
        print("non-trading day (weekend/holiday) — no IV snapshot")
        return []
    if not get_client().available():
        print("Tradier unavailable — no IV snapshot")
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    quotes = fetch_quotes(WATCHLIST) or {}
    rows = []
    for ticker in WATCHLIST:
        path = IV_DIR / f"{ticker}.csv"
        if fallback and path.exists():
            df = pd.read_csv(path)
            if today in set(df["date"]):
                continue  # already captured at 12:55 — fallback skips
        q = quotes.get(ticker, {})
        spot = q.get("last")
        if not spot:
            rows.append({"ticker": ticker, "error": "no quote"})
            continue
        exp = select_expiration(ticker, "2-7dte") or select_expiration(ticker, "7-21dte")
        if exp is None:
            rows.append({"ticker": ticker, "error": "no expiration"})
            continue
        chain = get_chain(ticker, exp)
        iv = atm_iv(chain, float(spot)) if chain else None
        if iv is None:
            rows.append({"ticker": ticker, "error": "no IV in chain"})
            continue
        persist_iv_snapshot(ticker, iv)
        sessions = history_sessions(ticker)
        rows.append({"ticker": ticker, "date": today, "atm_iv": iv,
                     "expiration": exp, "spot": spot,
                     "history_sessions": sessions, "iv_rank_tier": iv_tier(sessions),
                     "captured": "fallback (after-hours chain)" if fallback else "12:55 PT in-session"})
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as fh:
        fh.write(f"=== iv snapshot {stamp} fallback={fallback} ===\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    for r in rows:
        if "error" in r:
            print(f"  {r['ticker']}: FAILED ({r['error']})")
        else:
            print(f"  {r['ticker']}: atm_iv={r['atm_iv']} exp={r['expiration']} "
                  f"sessions={r['history_sessions']} tier={r['iv_rank_tier']}")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fallback", action="store_true",
                    help="only fill tickers missing today's row")
    snapshot(fallback=ap.parse_args().fallback)
