"""Per-strategy indicator applicability filter (Track B step 7).

Loads the requires/confirms/ignores map from config/strategy_rules.yaml and
evaluates it against a ticker's signal snapshot + the regime object.

Tri-state per indicator:
  True  — condition met
  False — condition checkable and not met (strategy BLOCKED)
  None  — underlying feed unavailable (strategy UNEVALUABLE, not blocked):
          GEX/TICK/put-call/IV-rank have no live source yet; observation
          mode logs the gap instead of pretending.
`ignores` entries are never consulted — encoded so a future scorer cannot
accidentally veto a condor on a flat MACD.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

_FIRED = ("fired_long", "fired_short")


def _sq(signals, tf):
    return (signals.get("squeeze") or {}).get(tf)


def _stack(signals, tf):
    return (signals.get("ema_stack") or {}).get(tf)


def _et_hour() -> int:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).hour


# indicator name -> evaluator(signals, regime) -> True | False | None
EVALUATORS = {
    # GEX: chain-derived estimate in the regime object (None when Tradier is down)
    "gex": lambda s, r: None if r.get("gex") is None else True,  # context available
    "gex_positive": lambda s, r: None if r.get("gex") is None else r["gex"]["sign"] == "positive",
    # GEX regime (Track A 2026-06-16 validated: +gamma -> calmer next-day moves).
    # Used as CONFIRMS only — biases family selection, never hard-blocks.
    "gex_suppressive": lambda s, r: None if r.get("gex_regime") is None else r["gex_regime"] == "suppressive",
    "gex_amplifying": lambda s, r: None if r.get("gex_regime") is None else r["gex_regime"] == "amplifying",
    # IV term-structure outlook (Track A 2026-06-16, strongest signal, corr -0.42).
    # CONFIRMS only — calm favors premium/range, active favors directional.
    "vol_calm": lambda s, r: None if r.get("vol_outlook") is None else r["vol_outlook"] == "calm",
    "vol_active": lambda s, r: None if r.get("vol_outlook") is None else r["vol_outlook"] == "active",
    # breadth proxy substitutes for NYSE $TICK (estimated, 5m snapshot).
    # `tick` = breadth context available; `tick_extreme` = breadth at an
    # extreme. None (unevaluable) only when the proxy itself is unavailable.
    "tick": lambda s, r: None if r.get("breadth") is None else True,
    "tick_extreme": lambda s, r: None if r.get("breadth") is None else bool(r["breadth"].get("extreme")),
    # tiered from accumulated daily IV snapshots: None (unevaluable) below 60
    # sessions; evaluates from 60 up (provisional 60-150, full >150 — the
    # tier travels in the ticker's signal snapshot for the observation log)
    "iv_rank_over_40": lambda s, r: (
        None if (s.get("iv_rank") or {}).get("rank") is None
        else (s["iv_rank"]["rank"] > 0.40)
    ),
    # computable today
    "vwap_tight": lambda s, r: None if s.get("vwap_dist_atr") is None else abs(s["vwap_dist_atr"]) <= 0.5,
    "vwap_with_trend": lambda s, r: None if s.get("vwap_dist_atr") is None else (
        s["vwap_dist_atr"] > 0 if _stack(s, "15m") == "bullish" else s["vwap_dist_atr"] < 0
    ),
    "vwap_rangebound": lambda s, r: None if s.get("vwap_dist_atr") is None else abs(s["vwap_dist_atr"]) <= 1.0,
    "squeeze_5m": lambda s, r: _sq(s, "5m") in _FIRED,
    "squeeze_15m_or_30m": lambda s, r: None
    if _sq(s, "15m") == "insufficient_data" and _sq(s, "30m") == "insufficient_data"
    else (_sq(s, "15m") in _FIRED or _sq(s, "30m") in _FIRED),
    "ema_stack_aligned": lambda s, r: None
    if "insufficient" in str(_stack(s, "5m")) or "insufficient" in str(_stack(s, "15m"))
    else _stack(s, "5m") == _stack(s, "15m"),
    "rs_leading": lambda s, r: None if s.get("rs_vs_spy_30min_pct") is None else abs(s["rs_vs_spy_30min_pct"]) >= 0.10,
    "macd_confirms": lambda s, r: None if not s.get("macd_5m") else (
        (s["macd_5m"]["sign"] == "positive") == (_stack(s, "5m") == "bullish")
    ),
    "volume_surge": lambda s, r: None if s.get("volume_ratio") is None else s["volume_ratio"] >= 1.5,
    "adx_low": lambda s, r: None if r.get("adx_15m_spy") is None else r["adx_15m_spy"] < 20,
    "vix_spike": lambda s, r: None if r.get("vix_1h_change") is None else r["vix_1h_change"] >= 1.5,
    "at_key_level": lambda s, r: None if not s.get("opening_range") else s["opening_range"]["position"] != "inside",
    # ── ORB breakout family (gen-4a, long-side only) ──
    # price broke ABOVE the opening range (long breakout)
    "orb_break_long": lambda s, r: None if not s.get("opening_range") else s["opening_range"]["position"] == "above",
    # regime aligned with a long breakout: trending (not chop/unknown) and not bearish
    "trend_aligned_long": lambda s, r: r.get("trend") in ("mild", "strong") and r.get("bias") != "bearish",
    # day-trading cutoff: no new ORB entries at/after 13:00 ET (gen-4a)
    "before_orb_cutoff": lambda s, r: _et_hour() < 13,
}


def load_applicability_map() -> dict:
    rules = yaml.safe_load((ROOT / "config" / "strategy_rules.yaml").read_text())
    return rules["applicability"]


def evaluate_strategies(signals: dict, regime: dict, app_map: dict | None = None) -> dict:
    """For one ticker: verdict per strategy family.
    viable      — all requires True
    blocked     — some checkable require is False (listed)
    unevaluable — some require has no live data source (listed)"""
    app_map = app_map or load_applicability_map()
    out = {}
    for strategy, spec in app_map.items():
        failed, missing = [], []
        for name in spec.get("requires", []):
            result = EVALUATORS[name](signals, regime)
            if result is None:
                missing.append(name)
            elif not result:
                failed.append(name)
        confirms = [n for n in spec.get("confirms", []) if EVALUATORS[n](signals, regime) is True]
        if failed:
            verdict = "blocked"
        elif missing:
            verdict = "unevaluable"
        else:
            verdict = "viable"
        out[strategy] = {
            "verdict": verdict,
            "blocked_by": failed,
            "missing_feeds": missing,
            "confirms_met": confirms,
            "ignored": spec.get("ignores", []),
        }
    return out
