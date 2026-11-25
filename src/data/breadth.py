"""Breadth proxy — a no-vendor stand-in for NYSE $TICK / advance-decline.

Why: $TICK has no Tradier feed, so the `tick` and `tick_extreme`
applicability conditions are permanently unevaluable, which structurally
blocks the 0DTE and reversal strategy families. Rather than buy a feed for
an unproven need, we estimate breadth from a batched quote over a curated
~50-name liquid basket (one Tradier call) — the same play as the
chain-derived GEX estimate.

Measures, per cycle:
  net_advancers_pct = %(last > prevclose) - %(last < prevclose)  [-100..+100]
  → a breadth-thrust reading. Logged raw every cycle so Track A can later
    CALIBRATE the real extreme threshold from outcomes. The provisional
    extreme threshold below is a documented placeholder, NOT validated edge.

Honesty: this is a 5-minute SNAPSHOT of net direction, not true tick-by-tick
uptick/downtick. It cannot see intra-bar TICK spikes. Labeled
"breadth_proxy (estimated)" wherever it surfaces. NOT wired into the live
pipeline until validated against live market data.
"""

import json
from datetime import datetime
from pathlib import Path

from src.data.feeds import fetch_quotes

ROOT = Path(__file__).resolve().parents[2]
BREADTH_DIR = ROOT / "logs" / "observation"

# Curated ~50 liquid large-caps spanning sectors — a market-breadth proxy
# basket (not tech-only). Fixed list = stable, comparable readings day to day.
BASKET = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "AMD", "ORCL",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "AXP", "BLK",
    "XOM", "CVX", "COP", "JNJ", "PFE", "MRK", "ABBV", "UNH", "LLY", "TMO",
    "WMT", "COST", "HD", "MCD", "NKE", "PG", "KO", "PEP", "DIS", "CMCSA",
    "CAT", "BA", "GE", "HON", "UPS", "LIN", "RTX", "T", "VZ", "IBM",
]

# Provisional extreme threshold (placeholder — Track A calibrates the real
# one from logged readings vs outcomes). |net| beyond this = "breadth extreme".
EXTREME_THRESHOLD = 60.0


def compute_breadth() -> dict | None:
    """One batched quote over the basket → net advancers reading. Returns
    None (callers treat as unevaluable) if the quote layer is unavailable."""
    quotes = fetch_quotes(BASKET)
    if not quotes:
        return None
    adv = dec = unch = counted = 0
    for sym, q in quotes.items():
        last = q.get("last")
        prev = q.get("prevclose") or q.get("close")
        if last is None or prev is None or prev == 0:
            continue
        counted += 1
        if last > prev:
            adv += 1
        elif last < prev:
            dec += 1
        else:
            unch += 1
    if counted < 20:  # too thin to be meaningful
        return None
    net = round((adv - dec) / counted * 100, 1)
    return {
        "net_advancers_pct": net,
        "advancing": adv, "declining": dec, "unchanged": unch, "counted": counted,
        "extreme": abs(net) >= EXTREME_THRESHOLD,
        "extreme_side": ("bullish" if net > 0 else "bearish") if abs(net) >= EXTREME_THRESHOLD else None,
        "threshold": EXTREME_THRESHOLD,
        "label": "breadth_proxy (estimated; 5m snapshot, not tick-level)",
    }


def log_breadth(reading: dict):
    """Persist each reading to a per-day file for intraday-extreme calibration."""
    path = BREADTH_DIR / f"breadth-{datetime.now():%Y-%m-%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **reading}) + "\n")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    r = compute_breadth()
    if r is None:
        print("breadth unavailable (quote layer down)")
    else:
        print(json.dumps(r, indent=2))
