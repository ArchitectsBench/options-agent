"""Per-position detail builder (2026-07-05) — the data behind the cockpit's
payoff / price / GEX panels for an open position. Read-only; cached where
the source is a network fetch (bars, chains).

  payoff — P&L-at-expiry curve computed from the position's OCC legs
           (pure math: intrinsics signed by action, minus entry debit),
           plus breakevens, max profit/loss, and current spot marker.
  price  — the underlying's latest-session 5m closes + session VWAP and
           the entry-time marker.
  gex    — per-strike net dealer gamma (calls +, puts −) around spot with
           the zero-gamma level, chain-derived (same estimate the regime
           uses — labeled as such).
"""

import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_CACHE: dict = {}


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.monotonic(), val)
    return val


# ── OCC leg parsing + payoff (pure math, unit-tested) ─────────────────────

def parse_occ(symbol: str) -> dict | None:
    """OCC: ROOT + YYMMDD + C/P + 8-digit strike(thousandths)."""
    try:
        strike = int(symbol[-8:]) / 1000.0
        opt_type = symbol[-9].upper()
        if opt_type not in ("C", "P"):
            return None
        return {"strike": strike, "type": "call" if opt_type == "C" else "put"}
    except (ValueError, IndexError, TypeError):
        return None


def payoff_curve(legs: list[dict], entry_price: float, quantity: int,
                 spot: float | None = None, n_points: int = 81) -> dict | None:
    """P&L at expiry (total dollars for the position) across underlying
    prices. legs: [{symbol, action}]. entry_price: net debit per share."""
    parsed = []
    for leg in legs:
        p = parse_occ(leg.get("symbol") or "")
        if p is None:
            return None
        parsed.append({**p, "sign": 1 if str(leg.get("action", "")).startswith("buy") else -1})
    if not parsed:
        return None
    strikes = [p["strike"] for p in parsed]
    anchor = spot or sum(strikes) / len(strikes)
    lo = min(min(strikes), anchor) * 0.9
    hi = max(max(strikes), anchor) * 1.1

    def pnl_at(s: float) -> float:
        val = 0.0
        for p in parsed:
            intrinsic = max(0.0, s - p["strike"]) if p["type"] == "call" \
                else max(0.0, p["strike"] - s)
            val += p["sign"] * intrinsic
        return round((val - entry_price) * 100 * quantity, 2)

    # include the strikes (payoff kinks) in the grid so every segment is
    # truly linear — makes breakeven interpolation exact
    xs = sorted({round(lo + (hi - lo) * i / (n_points - 1), 4) for i in range(n_points)}
                | {float(k) for k in strikes if lo <= k <= hi})
    points = [{"s": x, "pnl": pnl_at(x)} for x in xs]

    # breakevens: zero-touches and sign changes, linear-refined, deduped
    breakevens: list[float] = []

    def add_be(v: float):
        v = round(v, 2)
        if not breakevens or abs(breakevens[-1] - v) > 0.005:
            breakevens.append(v)

    for a, b in zip(points, points[1:]):
        if a["pnl"] == 0:
            add_be(a["s"])
        elif (a["pnl"] < 0) != (b["pnl"] < 0) and b["pnl"] != 0:
            frac = abs(a["pnl"]) / (abs(a["pnl"]) + abs(b["pnl"]))
            add_be(a["s"] + (b["s"] - a["s"]) * frac)
    if points and points[-1]["pnl"] == 0:
        add_be(points[-1]["s"])
    return {
        "points": points,
        "breakevens": breakevens[:2],
        "maxProfit": max(p["pnl"] for p in points),
        "maxLoss": min(p["pnl"] for p in points),
        "spot": spot,
        "entryDebit": entry_price,
        "quantity": quantity,
    }


# ── live data panels ───────────────────────────────────────────────────────

def price_panel(ticker: str, opened_at: str | None) -> dict | None:
    def fetch():
        import sys
        sys.path.insert(0, str(ROOT))
        from src.data.feeds import batch_intraday
        bars = batch_intraday([ticker]).get(ticker)
        if bars is None or bars.empty:
            return None
        last_day = bars.index[-1].date()
        day = bars[[d.date() == last_day for d in bars.index]]
        if day.empty:
            return None
        vol = day["Volume"].clip(lower=0)
        typical = (day["High"] + day["Low"] + day["Close"]) / 3
        cum_v = vol.cumsum().replace(0, 1)
        vwap = (typical * vol).cumsum() / cum_v
        return {
            "session": str(last_day),
            "bars": [{"t": t.strftime("%H:%M"), "c": round(float(c), 2),
                      "vwap": round(float(v), 2)}
                     for t, c, v in zip(day.index, day["Close"], vwap)],
            "entryT": (opened_at or "")[11:16] or None,
        }
    try:
        return _cached(f"price:{ticker}", 300, fetch)
    except Exception:
        return None


def gex_panel(ticker: str, expiration: str | None) -> dict | None:
    def fetch():
        import sys
        sys.path.insert(0, str(ROOT))
        from src.data.options_chain import get_chain, gex_per_strike, select_expiration
        from src.data.tradier import get_client
        exp = expiration or select_expiration(ticker, "weekly")
        if not exp:
            return None
        chain = get_chain(ticker, exp)
        if not chain:
            return None
        q = (get_client().quotes([ticker]) or {}).get(ticker) or {}
        spot = q.get("last") or q.get("prevclose")
        if not spot:
            return None
        per = gex_per_strike(chain, float(spot))
        if not per:
            return None
        strikes = sorted(per)
        # window: 12 strikes nearest spot
        nearest = sorted(strikes, key=lambda k: abs(k - float(spot)))[:12]
        window = sorted(nearest)
        # zero-gamma: cumulative sign flip across ALL strikes
        cum, zero_gamma, prev = 0.0, None, None
        for k in strikes:
            nxt = cum + per[k]
            if cum != 0 and (cum < 0) != (nxt < 0) and zero_gamma is None:
                zero_gamma = k if prev is None else (prev + k) / 2
            cum, prev = nxt, k
        return {
            "expiration": exp,
            "spot": float(spot),
            "zeroGamma": zero_gamma,
            "netGex": round(sum(per.values())),
            "strikes": [{"k": k, "gex": round(per[k])} for k in window],
            "label": "estimated (chain-derived, OI-weighted)",
        }
    try:
        return _cached(f"gex:{ticker}:{expiration}", 300, fetch)
    except Exception:
        return None


def build_detail(rec_id: str) -> dict | None:
    # LANE REPOINT (2026-08-10) — see positions_section(); the shared book is
    # retired, so this raised/returned nothing for every live position.
    from src.engine.paper_trader import load_positions
    positions = load_positions()
    pos = next((p for p in positions if p.get("rec_id") == rec_id), None)
    if pos is None:
        return None
    gex = gex_panel(pos.get("ticker"), pos.get("expiration"))
    spot = (gex or {}).get("spot")
    return {
        "recId": rec_id,
        "ticker": pos.get("ticker"),
        "structure": pos.get("structure"),
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "payoff": payoff_curve(pos.get("legs", []), float(pos.get("entry_price") or 0),
                               int(pos.get("quantity", 1)), spot=spot),
        "price": price_panel(pos.get("ticker"), pos.get("opened_at")),
        "gex": gex,
    }
