"""Tradier market-data WebSocket probe — RUN AT MONDAY'S OPEN (needs live data).

Single-purpose information gathering before any streaming integration:
  1. Does VIX stream LIVE or carry the same 15-min delay we saw on REST?
     (the one fact that decides the whole streaming roadmap)
  2. What is the per-symbol update cadence at our scale?
  3. Do equities/ETFs and the indices Tradier provides actually push?

Touches nothing in the live system. Prints a verdict and exits after a
short observation window.

⚠️ VERIFY against current Tradier streaming docs before first run — the
session endpoint and WS payload shape below reflect Tradier's documented
market WebSocket protocol but should be confirmed (docs.tradier.com →
"Streaming Market Events"). Requires: pip install websockets
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

PROD_TOKEN = os.environ.get("TRADIER_PROD_TOKEN")
SESSION_URL = "https://api.tradier.com/v1/markets/events/session"
WS_URL = "wss://ws.tradier.com/v1/markets/events"
PROBE_SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "VIX", "VXX"]  # VIX = the key question
OBSERVE_SECONDS = 60


def create_session() -> str | None:
    """POST to get a streaming session id (REST — high confidence)."""
    req = urllib.request.Request(SESSION_URL, data=b"", method="POST", headers={
        "Authorization": f"Bearer {PROD_TOKEN}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = json.loads(resp.read())
        return out.get("stream", {}).get("sessionid")
    except Exception as exc:
        print(f"session creation failed: {exc}")
        return None


async def probe(sessionid: str):
    import websockets

    counts = defaultdict(int)
    first_seen, last_seen = {}, {}
    delayed_flags = {}
    t0 = time.time()

    async with websockets.connect(WS_URL, ping_interval=10) as ws:
        await ws.send(json.dumps({
            "symbols": PROBE_SYMBOLS, "sessionid": sessionid,
            "filter": ["quote", "trade", "summary"], "linebreak": True,
        }))
        print(f"subscribed to {PROBE_SYMBOLS}; observing {OBSERVE_SECONDS}s...\n")
        while time.time() - t0 < OBSERVE_SECONDS:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=OBSERVE_SECONDS)
            except asyncio.TimeoutError:
                break
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sym = msg.get("symbol")
                if not sym:
                    continue
                counts[sym] += 1
                now = time.time() - t0
                first_seen.setdefault(sym, now)
                last_seen[sym] = now
                # delay heuristic: compare event timestamp to wall clock if present
                ts = msg.get("trade", {}).get("date") or msg.get("date")
                if ts and sym not in delayed_flags:
                    try:
                        evt = int(ts) / 1000
                        delayed_flags[sym] = round(time.time() - evt)
                    except (ValueError, TypeError):
                        pass

    print(f"{'symbol':<8}{'msgs':>6}{'msgs/s':>9}{'evt-lag(s)':>12}")
    for sym in PROBE_SYMBOLS:
        n = counts[sym]
        span = (last_seen.get(sym, 0) - first_seen.get(sym, 0)) or 1
        rate = f"{n / span:.2f}" if n else "—"
        lag = delayed_flags.get(sym, "?")
        print(f"{sym:<8}{n:>6}{rate:>9}{str(lag):>12}")
    print()
    vix_msgs = counts.get("VIX", 0)
    vix_lag = delayed_flags.get("VIX")
    if vix_msgs == 0:
        print("VERDICT: VIX did not stream — not in Tradier's live index set. "
              "VIX stays REST/delayed; a real-time index source (Polygon/IBKR) "
              "remains the only fix for the VIX delay.")
    elif vix_lag is not None and vix_lag > 600:
        print(f"VERDICT: VIX streamed but lagged ~{vix_lag}s (≈delayed). "
              "Streaming does NOT solve the VIX delay.")
    else:
        print("VERDICT: VIX streamed live (low lag) — the 15-min delay is SOLVED "
              "for free. No other vendor needed for VIX.")


def main():
    if not PROD_TOKEN:
        print("no TRADIER_PROD_TOKEN")
        sys.exit(1)
    from src.data.tradier import get_client  # noqa: F401 (ensures path set up)
    sid = create_session()
    if not sid:
        sys.exit(1)
    print(f"session {sid[:12]}... created {datetime.now():%H:%M:%S}")
    asyncio.run(probe(sid))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
