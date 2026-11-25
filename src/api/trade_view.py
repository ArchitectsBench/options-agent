"""Trade view (2026-09-14) — everything the cockpit TRADE page draws for one
option trade, open or closed, plus the day's trade list.

Operator ask: "I want this available for ongoing and completed trades ... as
the trade is in progress." Decisions (operator, 2026-09-14): a dedicated page;
explanations are RULES-BASED (no model writes them); candles are the engine's
own logged bars; every option trade from 2026-09-14 onward.

Sources, all read-only:
  logs/observation/fills.jsonl          fills, exits, round-trip mark series
  logs/recommendations.jsonl            trade spec at decision (legs, quotes)
  logs/observation/<date>.jsonl         the indicators the engine saw each cycle
  logs/observation/bars5m-<date>.jsonl  the 5-minute bars it computed them from
  data/lane/<lane>/positions.json       live marks for a trade still open
  config/config.yaml risk.paper_exits   the exit levels (target / stop)

The checklist, timeline and verdict describe what happened against the
engine's own rules. They never claim the setup has an edge.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[2]
PT = ZoneInfo("America/Los_Angeles")
STAMP = "UNVALIDATED — PAPER ONLY"
ENTRY_MARGIN_ATR = 0.25          # pipeline's vwap-consistency guard (O-15)
_CACHE: dict = {}


# ── small readers ────────────────────────────────────────────────────────

def _jsonl(path: Path):
    if not path.exists():
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _pt(ts) -> datetime | None:
    """Ledger stamps are naive PT; broker stamps are tz-aware UTC."""
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(PT) if d.tzinfo else d.replace(tzinfo=PT)


def _hm(ts) -> str | None:
    d = _pt(ts)
    return d.strftime("%H:%M") if d else None


def _mins(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _floor5(hm: str) -> str:
    m = _mins(hm) // 5 * 5
    return f"{m // 60:02d}:{m % 60:02d}"


def _is_scalp(rec_id: str) -> bool:
    # the canonical predicate — never a marker literal (the 08-08 rename
    # stranded literal filters once; test_scalp_rename guards this)
    from src.engine.paper_trader import is_scalp
    return is_scalp(rec_id)


def _ledger(root: Path) -> list[dict]:
    p = root / "logs" / "observation" / "fills.jsonl"
    key = ("ledger", str(p), p.stat().st_mtime if p.exists() else 0)
    if key not in _CACHE:
        _CACHE.clear() if len(_CACHE) > 64 else None
        _CACHE[key] = list(_jsonl(p))
    return _CACHE[key]


def _open_positions(root: Path) -> dict[str, dict]:
    out = {}
    for f in (root / "data" / "lane").glob("*/positions.json"):
        if f.parent.name.startswith("backup"):
            continue
        try:
            d = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        rows = d if isinstance(d, list) else list(d.values()) if isinstance(d, dict) else []
        for p in rows:
            if isinstance(p, dict) and p.get("rec_id"):
                out[p["rec_id"]] = p
    return out


def _exit_cfg(root: Path) -> dict:
    try:
        cfg = yaml.safe_load((root / "config" / "config.yaml").read_text()) or {}
        ex = (cfg.get("risk") or {}).get("paper_exits") or {}
    except (OSError, yaml.YAMLError):
        ex = {}
    return {"profit_target_pct": float(ex.get("profit_target_pct", 0.5)),
            "stop_loss_pct": float(ex.get("stop_loss_pct", 0.5)),
            "single_leg_target_gain_pct": float(ex.get("single_leg_target_gain_pct", 0.5)),
            "flat_time_et": ex.get("flat_time_et", "15:45")}


def _spec(root: Path, rec_id: str, date: str) -> dict:
    """Decision time + trade spec: recommendations.jsonl, else the cycle log."""
    for r in _jsonl(root / "logs" / "recommendations.jsonl"):
        if r.get("rec_id") == rec_id:
            return {"decided": _hm(r.get("generated")), "cycle": r.get("cycle"), "trade": r.get("trade") or {}}
    for r in _jsonl(root / "logs" / "observation" / f"{date}.jsonl"):
        for t in r.get("paper_execution_tests") or []:
            if t.get("rec_id") == rec_id:
                return {"decided": _hm(r.get("ts")), "cycle": r.get("cycle"), "trade": t.get("trade") or {}}
    return {"decided": None, "cycle": None, "trade": {}}


def _cycles(root: Path, date: str, ticker: str) -> list[dict]:
    out = []
    for r in _jsonl(root / "logs" / "observation" / f"{date}.jsonl"):
        s = (r.get("signals") or {}).get(ticker) or {}
        if s.get("last") is None:
            continue
        vd, atr = s.get("vwap_dist_atr"), s.get("atr")
        reg = r.get("regime") or {}
        out.append({
            "t": _hm(r.get("ts")), "cycle": r.get("cycle"), "last": s["last"], "atr": atr, "vd": vd,
            "vwap": round(s["last"] - vd * atr, 2) if vd is not None and atr else None,
            "ema5": (s.get("ema_stack") or {}).get("5m"), "ema15": (s.get("ema_stack") or {}).get("15m"),
            "sq5": (s.get("squeeze") or {}).get("5m"), "sq15": (s.get("squeeze") or {}).get("15m"),
            "macd": (s.get("macd_5m") or {}).get("sign"), "macdDir": (s.get("macd_5m") or {}).get("direction"),
            "vol": s.get("volume_ratio"), "rs": s.get("rs_vs_spy_30min_pct"),
            "bias": reg.get("bias"), "gex": reg.get("gex_regime"),
        })
    return out


def _bars(root: Path, date: str, ticker: str, fetch_fallback: bool = True) -> tuple[list[dict], str]:
    """The engine's logged bars; if none (day predates the log, or the engine
    was down) a Tradier/yfinance fetch labelled as NOT engine-seen."""
    seen = {}
    for r in _jsonl(root / "logs" / "observation" / f"bars5m-{date}.jsonl"):
        if r.get("ticker") == ticker and r.get("t"):
            seen[r["t"]] = {"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r.get("v", 0)}
    if seen:
        return [seen[k] for k in sorted(seen)], "engine"
    if not fetch_fallback:
        return [], "unavailable"
    key = ("bars", ticker, date, int(time.time() // 120))
    if key in _CACHE:
        return _CACHE[key]
    try:
        from src.data.feeds import fetch_intraday
        df = fetch_intraday(ticker, "5m", "5d")
        rows = []
        if df is not None and len(df):
            idx = df.index if df.index.tz is not None else df.index.tz_localize("America/New_York")
            for ts, b in zip(idx.tz_convert("America/Los_Angeles"), df.itertuples()):
                if str(ts.date()) != date:
                    continue
                rows.append({"t": ts.strftime("%H:%M"), "o": round(float(b.Open), 4), "h": round(float(b.High), 4),
                             "l": round(float(b.Low), 4), "c": round(float(b.Close), 4),
                             "v": int(b.Volume) if b.Volume == b.Volume else 0})
        res = (rows, "fetched — not the engine's logged bars" if rows else "unavailable")
    except Exception:  # noqa: BLE001 — a missing chart source is a label, never an error
        res = ([], "unavailable")
    _CACHE[key] = res
    return res


# ── the trade list ───────────────────────────────────────────────────────

def list_trades(date: str, root: Path = ROOT) -> dict:
    fills, exits = {}, {}
    for e in _ledger(root):
        rid = e.get("rec_id")
        if not rid:
            continue
        if e.get("event") in ("paper_fill", "partial_fill_booked") and str(e.get("ts", "")).startswith(date):
            fills.setdefault(rid, e)
        elif e.get("event") == "paper_exit":
            exits[rid] = e
    live = _open_positions(root)
    rows = []
    for rid, f in fills.items():
        x = exits.get(rid)
        pos = live.get(rid)
        entry = float(f.get("fill_price") or 0)
        qty = int(f.get("quantity") or 1)
        row = {"recId": rid, "ticker": f.get("ticker"), "lane": "SCALP" if _is_scalp(rid) else "FUNNEL",
               "structure": rid.rsplit("-", 1)[-1] if _is_scalp(rid) else (pos or {}).get("structure", ""),
               "qty": qty, "entry": entry, "entryT": _hm(f.get("ts")),
               "state": "closed" if x else ("open" if pos else "closed")}
        if x:
            row.update({"exit": x.get("exit_price"), "exitT": _hm((x.get("round_trip") or {}).get("exit_filled_at") or x.get("ts")),
                        "pnl": x.get("pnl_total"), "exitReason": x.get("exit_reason")})
        elif pos:
            lv = pos.get("last_value")
            row.update({"lastValue": lv, "lastValueT": _hm(pos.get("last_value_at")),
                        "unrealized": round((lv - entry) * 100 * qty, 2) if lv is not None else None,
                        "direction": pos.get("direction")})
        rows.append(row)
    rows.sort(key=lambda r: (r["state"] != "open", -_mins(r["entryT"] or "00:00")))
    return {"date": date, "stamp": STAMP, "trades": rows}


# ── rules: checklist, timeline, verdict ──────────────────────────────────

def checklist(at: dict | None, direction: str) -> list[dict]:
    """Direction-aware FOR / AGAINST / neutral grades of the engine's own
    indicators at the decision cycle."""
    if not at:
        return []
    long = direction == "long"
    out = []

    def add(label, value, verdict):
        out.append({"label": label, "value": value, "verdict": verdict})

    vd = at.get("vd")
    if vd is not None:
        right = vd >= ENTRY_MARGIN_ATR if long else vd <= -ENTRY_MARGIN_ATR
        wrong = vd < 0 if long else vd > 0
        add("VWAP distance", f"{vd:+.2f} ATR", "for" if right else "against" if wrong else "neutral")
    e5, e15 = at.get("ema5"), at.get("ema15")
    want = "bullish" if long else "bearish"
    other = "bearish" if long else "bullish"
    add("EMA stack 5m / 15m", f"{e5} / {e15}",
        "for" if e5 == want and e15 == want else "against" if e5 == other and e15 == other else "neutral")
    if at.get("macdDir"):
        add("MACD 5m", f"{at.get('macd')}, {at['macdDir']}",
            "for" if at["macdDir"] == ("rising" if long else "falling") else "against")
    if at.get("vol") is not None:
        v = at["vol"]
        add("Volume vs 20-bar avg", f"{v:.2f}×", "for" if v >= 1.5 else "against" if v < 1.0 else "neutral")
    if at.get("rs") is not None:
        rs = at["rs"]
        add("RS vs SPY, 30 min", f"{rs:+.2f}%", "for" if (rs > 0) == long and rs != 0 else "against" if rs != 0 else "neutral")
    if at.get("bias"):
        b = at["bias"]
        add("Market regime", b, "for" if b == want else "against" if b == other else "neutral")
    add("Squeeze 5m / 15m", f"{at.get('sq5')} / {at.get('sq15')}", "info")
    if at.get("gex"):
        add("Gamma (GEX)", at["gex"], "info")
    return out


def _levels(entry: float, trade: dict, cfg: dict) -> dict:
    mp = trade.get("max_profit")
    if mp:
        target = entry + cfg["profit_target_pct"] * float(mp)
    else:
        target = entry * (1 + cfg["single_leg_target_gain_pct"])
    return {"target": round(target, 4), "stop": round(entry * (1 - cfg["stop_loss_pct"]), 4)}


def timeline(v: dict) -> list[dict]:
    """Deterministic event list from the view's own numbers."""
    ev = []
    long = v["direction"] == "long"
    cyc = {c["t"]: c for c in v["cycles"]}
    at = v.get("atDecision") or {}
    if v.get("decided"):
        vd = at.get("vd")
        txt = f"Decided {v['structure'] or 'entry'}"
        if vd is not None:
            txt += f": {v['ticker']} {vd:+.2f} ATR from VWAP"
            if abs(vd) >= ENTRY_MARGIN_ATR:
                txt += f", clears the {ENTRY_MARGIN_ATR} entry margin"
        ev.append({"t": v["decided"], "kind": "decision", "text": txt + "."})
    f = v["fill"]
    txt = f"Filled {v['qty']} at {v['entry']:g}"
    if f.get("midAtFill") is not None and f.get("spreadAtFill"):
        spreads = (v["entry"] - float(f["midAtFill"])) / float(f["spreadAtFill"])
        mid = float(f["midAtFill"])
        txt += (f"; mid was {mid:g}, so it paid {spreads:.1f} spreads over mid" if spreads >= 0
                else f"; mid was {mid:g}, so it filled {-spreads:.1f} spreads below mid (the quote moved)")
    ev.append({"t": v["fillT"], "kind": "fill", "text": txt + "."})

    end = v.get("exitT") or (v["cycles"][-1]["t"] if v["cycles"] else v["fillT"])
    hold = [c for c in v["cycles"] if _floor5(v["fillT"]) <= c["t"] <= end and c.get("vd") is not None]
    if hold:
        worst = min(hold, key=lambda c: c["vd"]) if long else max(hold, key=lambda c: c["vd"])
        margin = worst["vd"] if long else -worst["vd"]
        if 0 <= margin < 0.5:
            ev.append({"t": worst["t"], "kind": "risk",
                       "text": f"Closest to the VWAP exit line: {worst['vd']:+.2f} ATR ({worst['last']:g})."})
        d0 = hold[0].get("macdDir")
        flip = next((c for c in hold[1:] if c.get("macdDir") and c["macdDir"] != d0), None)
        if flip:
            good = flip["macdDir"] == ("rising" if long else "falling")
            ev.append({"t": flip["t"], "kind": "for" if good else "against",
                       "text": f"MACD 5m turned {flip['macdDir']} ({'with' if good else 'against'} the trade)."})
    tgt = next((m for m in v["marks"] if m[1] >= v["levels"]["target"]), None)
    if tgt:
        ev.append({"t": tgt[0], "kind": "for", "text": f"Mark {tgt[1]:g} reached the target ({v['levels']['target']:g})."})
    stp = next((m for m in v["marks"] if m[1] <= v["levels"]["stop"]), None)

    if v["state"] == "closed":
        reason = v.get("exitReason") or "exit"
        txt = f"Exit ({reason.replace('_', ' ')}) filled at {v['exit']:g}: {'+' if v['pnl'] >= 0 else '−'}${abs(v['pnl']):,.0f} booked."
        if reason == "vwap_invalidation" and stp:
            txt += f" The stop ({v['levels']['stop']:g}) was also breached; VWAP is checked first."
        ev.append({"t": v["exitT"], "kind": "exit", "text": txt})
        a = cyc.get(_floor5(v["exitT"]))
        later = [c for c in v["cycles"] if c["t"] > v["exitT"] and _mins(c["t"]) <= _mins(v["exitT"]) + 30]
        if a and later and a.get("atr"):
            b = later[-1]
            move = (b["last"] - a["last"]) / a["atr"] * (1 if long else -1)
            if move >= 0.5:
                txt = f"After the exit, price kept going the trade's way: {move:+.1f} ATR by {b['t']}. The exit left that on the table."
            elif move <= -0.5:
                txt = f"After the exit, price kept going against the trade: {move:+.1f} ATR by {b['t']}. The exit avoided that."
            else:
                txt = f"Little movement in the 30 minutes after the exit ({move:+.1f} ATR)."
            ev.append({"t": b["t"], "kind": "after", "text": txt})
    else:
        if stp:
            ev.append({"t": stp[0], "kind": "against", "text": f"Mark {stp[1]:g} is at or below the stop ({v['levels']['stop']:g})."})
        if v["marks"]:
            m = v["marks"][-1]
            ev.append({"t": m[0], "kind": "live", "text": f"In progress: last mark {m[1]:g}."})
    ev.sort(key=lambda e: e["t"] or "")
    return ev


def verdict(v: dict) -> dict:
    grades = [c["verdict"] for c in v["checklist"]]
    n_for, n_against = grades.count("for"), grades.count("against")
    against = [c["label"] for c in v["checklist"] if c["verdict"] == "against"]
    long = v["direction"] == "long"
    hold = [c for c in v["cycles"] if c.get("vd") is not None
            and _floor5(v["fillT"]) <= c["t"] <= (v.get("exitT") or "23:59")]
    margin = min(((c["vd"] if long else -c["vd"]) for c in hold), default=None)
    if v["state"] == "open":
        head = "In progress"
    else:
        r = v.get("exitReason") or ""
        head = {"target_50": "Worked: hit the profit target",
                "vwap_invalidation": "Failed: price crossed VWAP against the trade",
                "stop_loss": "Failed: hit the stop",
                "time_flat": "Closed at the day's flat time"}.get(r)
        if head is None:
            head = ("Worked" if v["pnl"] >= 0 else "Failed") + f": {r.replace('_', ' ')}"
        if v["pnl"] >= 0 and margin is not None and 0 <= margin < ENTRY_MARGIN_ATR:
            head += f", but it came within {margin:.2f} ATR of the exit line"
    lines = [f"At the decision, {n_for} of the engine's indicators were for the trade and {n_against} against"
             + (f" ({', '.join(against)})." if against else ".")]
    if margin is not None:
        lines.append(f"While held, price crossed the VWAP exit line, reaching {-margin:.2f} ATR on the wrong side."
                     if margin < 0 else
                     f"While held, the tightest distance to the VWAP exit line was {margin:.2f} ATR.")
    lines.append("Rules-based summary of what happened against the engine's own rules. It does not say whether the setup has an edge.")
    return {"headline": head, "lines": lines, "for": n_for, "against": n_against}


# ── the view ─────────────────────────────────────────────────────────────

def build_trade_view(rec_id: str, root: Path = ROOT, fetch_fallback: bool = True) -> dict | None:
    fill = x = None
    for e in _ledger(root):
        if e.get("rec_id") != rec_id:
            continue
        if e.get("event") in ("paper_fill", "partial_fill_booked") and fill is None:
            fill = e
        elif e.get("event") == "paper_exit":
            x = e
    if fill is None:
        return None
    date = str(fill.get("ts", ""))[:10]
    pos = _open_positions(root).get(rec_id)
    spec = _spec(root, rec_id, date)
    trade = spec["trade"] or {}
    ticker = fill.get("ticker") or trade.get("ticker")
    direction = trade.get("direction") or (pos or {}).get("direction") or "long"
    entry = float(fill.get("fill_price") or 0)
    qty = int(fill.get("quantity") or 1)
    cycles = _cycles(root, date, ticker)
    bars, bars_source = _bars(root, date, ticker, fetch_fallback)
    state = "closed" if x else ("open" if pos else "closed")
    if x:
        marks = (x.get("round_trip") or {}).get("mark_series") or []
    else:
        marks = (pos or {}).get("mark_series") or []
    marks = [[_hm(t), float(val)] for t, val in marks if _hm(t) and val is not None]
    legs = trade.get("legs") or []
    decided = spec["decided"]
    at = {c["t"]: c for c in cycles}
    v = {
        "recId": rec_id, "date": date, "stamp": STAMP, "state": state,
        "ticker": ticker, "lane": "SCALP" if _is_scalp(rec_id) else "FUNNEL",
        "structure": trade.get("structure") or (pos or {}).get("structure") or "",
        "direction": direction, "qty": qty,
        "contract": " / ".join(f"{'+' if str(l.get('action', '')).startswith('buy') else '−'}{l.get('strike', ''):g}{'C' if l.get('type') == 'call' else 'P'}"
                               for l in legs if isinstance(l.get("strike"), (int, float))) + (f" · exp {trade.get('expiration')}" if trade.get("expiration") else ""),
        "legs": [{"action": l.get("action"), "strike": l.get("strike"), "type": l.get("type"),
                  "bid": l.get("bid"), "ask": l.get("ask"), "delta": l.get("delta"), "iv": l.get("iv")} for l in legs],
        "decided": decided, "cycle": spec["cycle"],
        "entry": entry, "fillT": _hm(fill.get("ts")),
        "fill": {"limitSent": fill.get("limit_sent"), "midAtFill": fill.get("mid_at_fill"),
                 "spreadAtFill": fill.get("spread_at_fill"), "fillMode": fill.get("fill_mode")},
        "levels": _levels(entry, trade, _exit_cfg(root)),
        "marks": marks, "cycles": cycles, "bars": bars, "barsSource": bars_source,
        "atDecision": at.get(decided) if decided else None,
    }
    if x:
        v.update({"exit": float(x.get("exit_price") or 0), "pnl": float(x.get("pnl_total") or 0),
                  "exitT": _hm((x.get("round_trip") or {}).get("exit_filled_at") or x.get("ts")),
                  "exitReason": x.get("exit_reason"), "closeReason": x.get("close_reason")})
    else:
        lv = (pos or {}).get("last_value")
        v.update({"lastValue": lv, "unrealized": round((lv - entry) * 100 * qty, 2) if lv is not None else None})
    v["checklist"] = checklist(v["atDecision"], direction)
    v["timeline"] = timeline(v)
    v["verdict"] = verdict(v)
    return v
