"""Options chain layer (Tradier, greeks=true via ORATS).

Provides for step 9:
  - normalized chains with greeks/IV per strike
  - CLAUDE.md liquidity filters: bid-ask spread <= $0.20, OI >= 500,
    delta targeting per strategy type
  - expiration bucket selection: 0dte | 2-7dte | 7-21dte
  - chain-derived GEX estimate: sum(gamma * OI * 100 * spot), calls
    positive / puts negative, plus the zero-gamma level. Always labeled
    "estimated (chain-derived)" — it is NOT vendor GEX (no dealer
    positioning assumptions beyond the standard sign convention).
  - ATM IV, persisted daily to data/iv_history/{ticker}.csv so IV rank
    becomes computable as history accumulates.
"""

import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from src.data.tradier import get_client

ROOT = Path(__file__).resolve().parents[2]
IV_DIR = ROOT / "data" / "iv_history"

MAX_SPREAD = 0.20      # absolute floor: spreads under this always acceptable
MAX_SPREAD_PCT = 0.03  # weekend fix #3: OR within 3% of mid — evidence: all
MIN_OI = 500           # 6/11-6/12 liquidity rejections were absolute-cap
                       # kills on percentage-tight contracts (e.g. MU 3.1%
                       # of mid = $1.70 absolute)


def spread_ok(spread: float, mid: float,
              max_spread: float = MAX_SPREAD, max_pct: float = MAX_SPREAD_PCT) -> bool:
    return spread <= max(max_spread, max_pct * mid)

# |delta| target and tolerance per leg role
DELTA_TARGETS = {
    "directional_atm": (0.50, 0.10),   # 0DTE directional
    "long_leg": (0.60, 0.10),          # debit spread long leg / long call/put
    "short_leg": (0.30, 0.08),         # debit spread short leg
    "credit_short": (0.20, 0.06),      # premium selling short strikes
    "credit_wing": (0.10, 0.05),       # protective wings
}

EXPIRY_BUCKETS = {"0dte": (0, 0), "2-7dte": (2, 7), "7-21dte": (7, 21),
                  "25-45dte": (25, 45), "50-75dte": (50, 75)}  # for IV term structure

# ── put/call volume proxy (2026-07-01) ──
# Labeled stand-in for the CBOE equity put/call ratio (no affordable live
# feed): total put volume / total call volume over the SPY+QQQ chains, reusing
# the chain chain_snapshot() already downloads for the GEX estimate.
# Thresholds are UNCALIBRATED placeholders — a chain-derived SPY/QQQ ratio is
# NOT on the CBOE equity P/C scale (index hedging flow skews it put-heavy).
# They will be calibrated from accumulated observation-mode history
# (logs/observation/*.jsonl) once enough sessions exist.
PC_FEAR_THRESHOLD = 1.2          # UNCALIBRATED: ratio above -> "fear"
PC_COMPLACENCY_THRESHOLD = 0.6   # UNCALIBRATED: ratio below -> "complacency"
PC_PROXY_SOURCE = "chain_proxy_spy_qqq"


def normalize_chain(raw: list[dict]) -> list[dict]:
    out = []
    for o in raw:
        bid, ask = o.get("bid") or 0.0, o.get("ask") or 0.0
        g = o.get("greeks") or {}
        out.append({
            "symbol": o["symbol"],
            "type": o["option_type"],           # call | put
            "strike": float(o["strike"]),
            "expiration": o["expiration_date"],
            "bid": bid,
            "ask": ask,
            "mid": round((bid + ask) / 2, 3),
            "spread": round(ask - bid, 3),
            "oi": int(o.get("open_interest") or 0),
            "volume": int(o.get("volume") or 0),
            "delta": g.get("delta"),
            "gamma": g.get("gamma"),
            "theta": g.get("theta"),
            "iv": g.get("mid_iv") or g.get("smv_vol"),
        })
    return out


def get_chain(ticker: str, expiration: str) -> list[dict] | None:
    raw = get_client().chain(ticker, expiration)
    return normalize_chain(raw) if raw else None


def select_expiration(ticker: str, bucket: str) -> str | None:
    """Nearest expiration whose DTE falls in the bucket window."""
    expirations = get_client().expirations(ticker)
    if not expirations:
        return None
    lo, hi = EXPIRY_BUCKETS[bucket]
    today = date.today()
    for exp in sorted(expirations):
        dte = (date.fromisoformat(exp) - today).days
        if lo <= dte <= hi:
            return exp
    return None


def filter_contracts_stats(chain: list[dict], option_type: str, leg_role: str,
                           max_spread: float = MAX_SPREAD, min_oi: int = MIN_OI
                           ) -> tuple[list[dict], dict]:
    """Liquidity + delta-target filter with per-rule rejection stats so the
    caller can report WHY a leg has no candidates."""
    target, tol = DELTA_TARGETS[leg_role]
    out = []
    stats = {"role": leg_role, "delta_target": target, "in_delta_range": 0,
             "rejected_spread": 0, "rejected_oi": 0, "rejected_no_bid": 0}
    for c in chain:
        if c["type"] != option_type or c["delta"] is None:
            continue
        if abs(abs(c["delta"]) - target) > tol:
            continue
        stats["in_delta_range"] += 1
        if c["bid"] <= 0:
            stats["rejected_no_bid"] += 1
            continue
        if not spread_ok(c["spread"], c["mid"], max_spread):
            stats["rejected_spread"] += 1
            continue
        if c["oi"] < min_oi:
            stats["rejected_oi"] += 1
            continue
        out.append(c)
    return sorted(out, key=lambda c: abs(abs(c["delta"]) - target)), stats


def filter_contracts(chain: list[dict], option_type: str, leg_role: str,
                     max_spread: float = MAX_SPREAD, min_oi: int = MIN_OI) -> list[dict]:
    return filter_contracts_stats(chain, option_type, leg_role, max_spread, min_oi)[0]


def gex_per_strike(chain: list[dict], spot: float) -> dict[float, float]:
    """Per-strike net GEX map (calls +, puts −) — same math as gex_estimate,
    exposed for the cockpit's per-position GEX diagram (2026-07-05)."""
    per_strike: dict[float, float] = {}
    for c in chain:
        if c["gamma"] is None or not c["oi"]:
            continue
        sign = 1 if c["type"] == "call" else -1
        per_strike[c["strike"]] = per_strike.get(c["strike"], 0.0) + \
            sign * c["gamma"] * c["oi"] * 100 * spot
    return per_strike


def gex_estimate(chain: list[dict], spot: float) -> dict | None:
    """Net GEX = sum(gamma * OI * 100 * spot); calls +, puts -.
    Zero-gamma level: strike where cumulative net GEX (ascending strikes)
    crosses zero. Labeled estimated (chain-derived)."""
    rows = [c for c in chain if c["gamma"] is not None and c["oi"] > 0]
    if not rows:
        return None
    per_strike: dict[float, float] = {}
    for c in rows:
        sign = 1 if c["type"] == "call" else -1
        per_strike[c["strike"]] = per_strike.get(c["strike"], 0.0) + sign * c["gamma"] * c["oi"] * 100 * spot
    strikes = sorted(per_strike)
    net = sum(per_strike.values())
    zero_gamma = None
    cum = 0.0
    for i, k in enumerate(strikes):
        prev = cum
        cum += per_strike[k]
        if prev < 0 <= cum or prev > 0 >= cum:
            zero_gamma = k if i == 0 else (strikes[i - 1] + k) / 2
    return {
        "net_gex": round(net),
        "sign": "positive" if net > 0 else "negative",
        "zero_gamma_level": zero_gamma,
        "spot": spot,
        "label": "estimated (chain-derived)",
    }


def atm_iv(chain: list[dict], spot: float) -> float | None:
    """Mean IV of the call+put nearest the spot."""
    with_iv = [c for c in chain if c["iv"]]
    if not with_iv:
        return None
    calls = sorted((c for c in with_iv if c["type"] == "call"), key=lambda c: abs(c["strike"] - spot))
    puts = sorted((c for c in with_iv if c["type"] == "put"), key=lambda c: abs(c["strike"] - spot))
    legs = ([calls[0]["iv"]] if calls else []) + ([puts[0]["iv"]] if puts else [])
    return round(sum(legs) / len(legs), 4) if legs else None


def persist_iv_snapshot(ticker: str, iv: float):
    """One row per ticker per day (idempotent); IV rank computes from this
    history once enough days accumulate."""
    IV_DIR.mkdir(parents=True, exist_ok=True)
    path = IV_DIR / f"{ticker}.csv"
    today = datetime.now().strftime("%Y-%m-%d")
    if path.exists():
        df = pd.read_csv(path)
        df = df[df["date"] != today]
    else:
        df = pd.DataFrame(columns=["date", "atm_iv"])
    df = pd.concat([df, pd.DataFrame([{"date": today, "atm_iv": iv}])])
    df.to_csv(path, index=False)


def iv_rank(ticker: str, current_iv: float, min_history: int = 60) -> float | None:
    """Percentile of current IV within accumulated history (None until
    min_history days exist — honesty over fabrication)."""
    path = IV_DIR / f"{ticker}.csv"
    if not path.exists():
        return None
    hist = pd.read_csv(path)["atm_iv"].dropna()
    if len(hist) < min_history:
        return None
    return round(float((hist < current_iv).mean()), 2)


def iv_rank_status(ticker: str) -> dict:
    """Tiered IV-rank from accumulated daily snapshots (data-gap fix #2):
    < 60 sessions unevaluable | 60-150 provisional | > 150 full.
    Uses the latest persisted IV — no live chain call needed."""
    path = IV_DIR / f"{ticker}.csv"
    if not path.exists():
        return {"rank": None, "sessions": 0, "tier": "unevaluable"}
    hist = pd.read_csv(path)["atm_iv"].dropna()
    sessions = len(hist)
    tier = "unevaluable" if sessions < 60 else ("provisional" if sessions <= 150 else "full")
    rank = None
    if tier != "unevaluable":
        current = float(hist.iloc[-1])
        rank = round(float((hist.iloc[:-1] < current).mean()), 2)
    return {"rank": rank, "sessions": sessions, "tier": tier}


_TERM_CACHE: dict[str, tuple[float, dict | None]] = {}
_TERM_TTL = 1200    # 20 min — term structure is a slow signal; no need to refetch
                    # 4 chains every 5-min cycle (that load helped trigger the
                    # 2026-06-17 intraday data failures).


def term_structure(ticker: str, spot: float) -> dict | None:
    """Live IV term-structure slope = ATM iv(~60d) - ATM iv(~30d).
    Validated (Track A 2026-06-16): slope predicts next-day |move| at corr ~-0.42
    cross-ETF — backwardation (slope<0) precedes ~2x bigger moves. None when the
    needed expiries/chains are unavailable (degraded mode). Cached 20 min to keep
    per-cycle Tradier load down."""
    cached = _TERM_CACHE.get(ticker)
    if cached and time.time() - cached[0] < _TERM_TTL:
        return cached[1]
    e30 = select_expiration(ticker, "25-45dte")
    e60 = select_expiration(ticker, "50-75dte")
    result = None
    if e30 and e60:
        iv30 = atm_iv(get_chain(ticker, e30) or [], spot)
        iv60 = atm_iv(get_chain(ticker, e60) or [], spot)
        if iv30 is not None and iv60 is not None:
            result = {"iv30": iv30, "iv60": iv60, "slope": round(iv60 - iv30, 4)}
    if result is not None:                     # only cache successes (retry on degraded)
        _TERM_CACHE[ticker] = (time.time(), result)
    return result


_CYCLE_CHAIN_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CYCLE_CHAIN_TTL = 240  # < the 5-min cycle: dedupes chain fetches WITHIN a cycle
                        # (GEX + put/call proxy share one download), refreshes
                        # every cycle. Recommendation-time pricing still uses
                        # get_chain() directly (always fresh).


def _stash_chain(ticker: str, chain: list[dict] | None):
    if chain:
        _CYCLE_CHAIN_CACHE[ticker] = (time.time(), chain)


def _cycle_chain(ticker: str, bucket: str = "2-7dte") -> list[dict] | None:
    """Chain for `ticker`, downloaded at most once per cycle: reuses the chain
    chain_snapshot() already fetched for the GEX estimate when fresh, else
    fetches once and stashes it."""
    cached = _CYCLE_CHAIN_CACHE.get(ticker)
    if cached and time.time() - cached[0] < _CYCLE_CHAIN_TTL:
        return cached[1]
    if not get_client().available():
        return None
    exp = select_expiration(ticker, bucket) or select_expiration(ticker, "7-21dte")
    chain = get_chain(ticker, exp) if exp else None
    _stash_chain(ticker, chain)
    return chain


def put_call_from_chains(chains: list[list[dict]]) -> tuple[float | None, int]:
    """Pure math: (total put volume / total call volume, #contracts counted)
    across the given normalized chains. Only contracts with volume > 0 are
    counted. Ratio is None when there is no call volume (closed session /
    empty chains) — honesty over fabrication."""
    put_vol = call_vol = n = 0
    for chain in chains:
        for c in chain:
            vol = c.get("volume") or 0
            if vol <= 0:
                continue
            if c["type"] == "put":
                put_vol += vol
            elif c["type"] == "call":
                call_vol += vol
            else:
                continue
            n += 1
    if call_vol <= 0:
        return None, n
    return round(put_vol / call_vol, 3), n


def classify_put_call(ratio: float | None) -> str | None:
    """Conservative extreme classification (thresholds UNCALIBRATED — see
    PC_FEAR_THRESHOLD / PC_COMPLACENCY_THRESHOLD comment above)."""
    if ratio is None:
        return None
    if ratio > PC_FEAR_THRESHOLD:
        return "fear"
    if ratio < PC_COMPLACENCY_THRESHOLD:
        return "complacency"
    return None


def put_call_proxy(tickers: tuple[str, ...] = ("SPY", "QQQ"),
                   bucket: str = "2-7dte") -> dict:
    """Labeled proxy for the CBOE equity put/call ratio, from the SPY+QQQ
    chains (chain fetched at most once per cycle — SPY reuses the GEX
    download). Always returns the proxy dict; `value` is None when no chain
    is available so callers can mark the feed missing without crashing."""
    chains = []
    for t in tickers:
        try:
            chain = _cycle_chain(t, bucket)
        except Exception:
            chain = None
        if chain:
            chains.append(chain)
    value, n = put_call_from_chains(chains)
    return {"value": value, "source": PC_PROXY_SOURCE, "n_contracts": n,
            "extreme": classify_put_call(value)}


def chain_snapshot(ticker: str, spot: float, bucket: str = "2-7dte") -> dict | None:
    """One-call-per-cycle summary for a ticker: GEX + ATM IV + IV rank.
    Returns None when Tradier is unavailable (feeds stay honest)."""
    if not get_client().available():
        return None
    exp = select_expiration(ticker, bucket) or select_expiration(ticker, "7-21dte")
    if exp is None:
        return None
    chain = get_chain(ticker, exp)
    if not chain:
        return None
    _stash_chain(ticker, chain)  # reused by put_call_proxy() this cycle
    iv = atm_iv(chain, spot)
    if iv:
        persist_iv_snapshot(ticker, iv)
    return {
        "expiration": exp,
        "gex": gex_estimate(chain, spot),
        "atm_iv": iv,
        "iv_rank": iv_rank(ticker, iv) if iv else None,
        "tradable_contracts": len([c for c in chain if c["spread"] <= MAX_SPREAD and c["oi"] >= MIN_OI]),
    }
