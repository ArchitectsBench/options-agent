"""Cockpit TRADE page backend (src/api/trade_view.py, 2026-09-14).

Built on a tmp ROOT with a real-shaped ledger, cycle log, bar log and positions
file (never the live logs). Pins: the list shows open trades first with live
marks; a closed trade carries its exit, booked P&L and round-trip marks; target
and stop follow config (single-leg vs spread); the checklist grades the
engine's own values by direction; the timeline finds the closest approach to
the VWAP exit line, the same-cycle stop breach and the post-exit move; bars
come from the engine log and a missing log is labelled, never faked.
"""
import json
from pathlib import Path

import src.api.trade_view as tv

DAY = "2026-09-14"
MSFT = "20260914-SCALP-4473-MSFT-0dte_directional"
AMD = "20260914-SCALP-4481-AMD-0dte_directional"
OPEN = "20260914-SCALP-4500-NVDA-debit_spread"


def _sig(last, vd, macd_dir="rising", ema="bullish", vol=0.7, rs=0.1):
    return {"last": last, "atr": 1.0, "vwap_dist_atr": vd, "ema_stack": {"5m": ema, "15m": ema},
            "macd_5m": {"sign": "positive", "direction": macd_dir}, "squeeze": {"5m": "off", "15m": "off"},
            "volume_ratio": vol, "rs_vs_spy_30min_pct": rs}


def _root(tmp_path: Path) -> Path:
    obs = tmp_path / "logs" / "observation"
    obs.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        "risk:\n  paper_exits:\n    profit_target_pct: 0.5\n    stop_loss_pct: 0.5\n    single_leg_target_gain_pct: 0.5\n")
    # MSFT long: decided 07:30 at +0.46, dips to +0.11 at 07:35, runs; AMD short: reclaims VWAP 08:30
    msft = {"07:30": _sig(499.4, 0.46, "falling"), "07:35": _sig(499.0, 0.11, "falling"),
            "07:50": _sig(500.2, 1.05, "rising"), "08:05": _sig(502.0, 2.5), "08:35": _sig(504.4, 3.6)}
    amd = {"08:10": _sig(485.2, -0.36, "rising", "bearish", 0.52, 0.28), "08:15": _sig(483.6, -0.91, "rising", "bearish"),
           "08:30": _sig(487.4, 0.45, "rising", "bearish"), "09:00": _sig(491.9, 2.1, "rising", "bearish")}
    times = sorted(set(msft) | set(amd))
    with open(obs / f"{DAY}.jsonl", "w") as fh:
        for i, t in enumerate(times):
            sig = {}
            if t in msft:
                sig["MSFT"] = msft[t]
            if t in amd:
                sig["AMD"] = amd[t]
            fh.write(json.dumps({"ts": f"{DAY}T{t}:10", "cycle": 4470 + i, "signals": sig,
                                 "regime": {"bias": "bearish", "gex_regime": "amplifying"}}) + "\n")
    recs = [
        {"rec_id": MSFT, "generated": f"{DAY}T07:30:12", "cycle": 4473,
         "trade": {"structure": "0DTE long call", "direction": "long", "expiration": DAY, "max_profit": None,
                   "legs": [{"action": "buy_to_open", "strike": 500.0, "type": "call", "bid": 1.28, "ask": 1.35}]}},
        {"rec_id": AMD, "generated": f"{DAY}T08:10:07", "cycle": 4481,
         "trade": {"structure": "0DTE long put", "direction": "short", "expiration": DAY, "max_profit": None,
                   "legs": [{"action": "buy_to_open", "strike": 485.0, "type": "put", "bid": 2.66, "ask": 2.8}]}},
        {"rec_id": OPEN, "generated": f"{DAY}T09:00:05", "cycle": 4490,
         "trade": {"structure": "call debit spread", "direction": "long", "expiration": DAY, "max_profit": 3.0,
                   "legs": [{"action": "buy_to_open", "strike": 180.0, "type": "call"},
                            {"action": "sell_to_open", "strike": 185.0, "type": "call"}]}},
    ]
    (tmp_path / "logs" / "recommendations.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    ledger = [
        {"ts": f"{DAY}T07:35:11", "event": "paper_fill", "rec_id": MSFT, "ticker": "MSFT", "fill_price": 1.35,
         "quantity": 7, "mid_at_fill": 1.235, "spread_at_fill": 0.07, "fill_mode": "marketable_ioc"},
        {"ts": f"{DAY}T08:05:10", "event": "paper_exit", "rec_id": MSFT, "ticker": "MSFT", "exit_price": 2.45,
         "pnl_total": 770.0, "exit_reason": "target_50", "close_reason": "profit_target",
         "round_trip": {"exit_filled_at": "2026-09-14T15:05:10+00:00",
                        "mark_series": [[f"{DAY}T07:35:12", 1.24], [f"{DAY}T08:00:09", 2.45]]}},
        {"ts": f"{DAY}T08:15:11", "event": "paper_fill", "rec_id": AMD, "ticker": "AMD", "fill_price": 2.79, "quantity": 3},
        {"ts": f"{DAY}T08:35:11", "event": "paper_exit", "rec_id": AMD, "ticker": "AMD", "exit_price": 1.37,
         "pnl_total": -426.0, "exit_reason": "vwap_invalidation",
         "round_trip": {"exit_filled_at": "2026-09-14T15:30:23+00:00",
                        "mark_series": [[f"{DAY}T08:15:12", 3.1], [f"{DAY}T08:30:11", 1.37]]}},
        {"ts": f"{DAY}T09:00:20", "event": "paper_fill", "rec_id": OPEN, "ticker": "NVDA", "fill_price": 2.0, "quantity": 2},
        {"ts": "2026-09-11T09:00:00", "event": "paper_fill", "rec_id": "20260911-SCALP-1-SPY-debit_spread",
         "ticker": "SPY", "fill_price": 1.0, "quantity": 1},
    ]
    (obs / "fills.jsonl").write_text("\n".join(json.dumps(e) for e in ledger) + "\n")
    (tmp_path / "data" / "lane" / "scalp").mkdir(parents=True)
    (tmp_path / "data" / "lane" / "scalp" / "positions.json").write_text(json.dumps([
        {"rec_id": OPEN, "ticker": "NVDA", "direction": "long", "structure": "call debit spread",
         "last_value": 2.6, "last_value_at": f"{DAY}T09:10:00", "mark_series": [[f"{DAY}T09:05:00", 2.2], [f"{DAY}T09:10:00", 2.6]]}]))
    with open(obs / f"bars5m-{DAY}.jsonl", "w") as fh:
        for t, c in (("07:30", 499.4), ("07:35", 499.0), ("07:35", 499.1)):   # duplicate: last wins
            fh.write(json.dumps({"date": DAY, "t": t, "ticker": "MSFT", "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 10}) + "\n")
    tv._CACHE.clear()
    return tmp_path


def test_list_puts_open_trades_first_with_live_marks(tmp_path):
    out = tv.list_trades(DAY, _root(tmp_path))
    assert [t["recId"] for t in out["trades"]] == [OPEN, AMD, MSFT]
    o = out["trades"][0]
    assert o["state"] == "open" and o["lastValue"] == 2.6 and o["unrealized"] == 120.0
    assert out["trades"][1]["pnl"] == -426.0 and out["trades"][1]["exitT"] == "08:30"


def test_closed_long_view_levels_checklist_and_thin_margin(tmp_path):
    v = tv.build_trade_view(MSFT, _root(tmp_path), fetch_fallback=False)
    assert (v["state"], v["decided"], v["fillT"], v["exitT"]) == ("closed", "07:30", "07:35", "08:05")
    assert v["levels"] == {"target": 2.025, "stop": 0.675}
    grades = {c["label"]: c["verdict"] for c in v["checklist"]}
    assert grades["VWAP distance"] == "for" and grades["MACD 5m"] == "against"
    assert grades["Volume vs 20-bar avg"] == "against" and grades["Market regime"] == "against"
    texts = " ".join(e["text"] for e in v["timeline"])
    assert "paid 1.6 spreads over mid" in texts
    assert "Closest to the VWAP exit line: +0.11 ATR" in texts
    assert "The exit left that on the table" in texts           # 502 -> 504.4 within 30 min
    assert v["verdict"]["headline"] == "Worked: hit the profit target, but it came within 0.11 ATR of the exit line"
    assert [b["c"] for b in v["bars"]] == [499.4, 499.1] and v["barsSource"] == "engine"


def test_closed_short_view_invalidation_with_same_cycle_stop(tmp_path):
    v = tv.build_trade_view(AMD, _root(tmp_path), fetch_fallback=False)
    assert v["direction"] == "short" and v["levels"]["stop"] == 1.395
    ex = next(e for e in v["timeline"] if e["kind"] == "exit")
    assert "vwap invalidation" in ex["text"] and "also breached; VWAP is checked first" in ex["text"]
    assert v["verdict"]["headline"] == "Failed: price crossed VWAP against the trade"
    assert "crossed the VWAP exit line, reaching 0.45 ATR on the wrong side" in " ".join(v["verdict"]["lines"])
    assert v["bars"] == [] and v["barsSource"] == "unavailable"   # no engine log, fallback off


def test_open_spread_uses_spread_target_and_live_marks(tmp_path):
    v = tv.build_trade_view(OPEN, _root(tmp_path), fetch_fallback=False)
    assert v["state"] == "open" and v["marks"] == [["09:05", 2.2], ["09:10", 2.6]]
    assert v["levels"] == {"target": 3.5, "stop": 1.0}               # 2.0 + 0.5 x max_profit 3.0
    assert v["verdict"]["headline"] == "In progress"
    assert v["timeline"][-1]["kind"] == "live"
    assert tv.build_trade_view("nope", tmp_path) is None
