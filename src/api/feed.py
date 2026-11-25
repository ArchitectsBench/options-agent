"""Cockpit feed builder (2026-07-04) — assembles the UI's CockpitFeed JSON
(ui/src/feed/types.ts is the contract) from the REAL system state files. The
API server runs as its own process and, like the CLI and ops tooling, reads
the same files the trader writes — no coupling into the trader process.

Everything here is read-only and fault-isolated: a failing section returns a
degraded-but-honest value, never a 500. Expensive pieces (forward-acceptance
evaluation, market clock) are cached briefly.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml
from src.engine.paper_trader import is_scalp as _is_scalp_id

ROOT = Path(__file__).resolve().parents[2]

_CACHE: dict = {}


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.monotonic(), val)
    return val


def _jload(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _jsonl(path: Path) -> list[dict]:
    out = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _cfg() -> dict:
    return _cached("cfg", 30, lambda: yaml.safe_load((ROOT / "config" / "config.yaml").read_text()))


def _state() -> dict:
    return _jload(ROOT / "data" / "agent_state.json", {})


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _market_open() -> bool:
    def probe():
        try:
            import sys
            sys.path.insert(0, str(ROOT))
            from src.scheduler import market_open
            return bool(market_open())
        except Exception:
            return False
    return _cached("market_open", 60, probe)


def _fills_today() -> list[dict]:
    today = f"{datetime.now():%Y-%m-%d}"
    return [e for e in _jsonl(ROOT / "logs" / "observation" / "fills.jsonl")
            if str(e.get("ts", "")).startswith(today)]


_TRUTH_CACHE: dict = {"mtime": None, "map": {}}


def _truth_map() -> dict:
    """rec_id -> true_pnl_mid from the enriched ledger (mtime-cached).
    TRUTH-ONLY cockpit (user directive 2026-07-23): booked sandbox P&L is
    never displayed; unscored trades show as pending."""
    f = ROOT / "logs" / "observation" / "enriched_trades_v1.jsonl"
    if not f.exists():
        return {}
    mt = f.stat().st_mtime
    if _TRUTH_CACHE["mtime"] != mt:
        m = {}
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("status") == "ok" and r.get("true_pnl_mid") is not None:
                m[r.get("rec_id")] = float(r["true_pnl_mid"])
        _TRUTH_CACHE.update(mtime=mt, map=m)
    return _TRUTH_CACHE["map"]


def _pnl(e: dict):
    """Market-truth P&L for an exit event, or None if not yet scored."""
    return _truth_map().get(str(e.get("rec_id", "")))


def _is_funnel(rec_id: str) -> bool:
    from src.engine.paper_trader import is_scalp
    return not is_scalp(rec_id)


_PROC_STARTED = datetime.now().isoformat(timespec="seconds")


@lru_cache(maxsize=1)
def _build_stamp() -> dict:
    """SHA at import time + process start. Resolved once: the point is to
    report what THIS process is running, so re-reading git later would defeat
    it — a stale process would happily report the newest commit."""
    sha = "unknown"
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        pass
    return {"sha": sha, "startedAt": _PROC_STARTED}


def _fmt_money(n: float) -> str:
    return f"{'+' if n >= 0 else '−'}${abs(n):,.0f}"


# ── sections ──────────────────────────────────────────────────────────────

def agent_section(state: dict) -> dict:
    alive = _pid_alive(state.get("pid"))
    kill = (ROOT / "data" / "KILL_SWITCH").exists()
    agent = "dead" if not alive else ("orders-blocked" if kill else
                                      ("running" if _market_open() else "heartbeat"))
    cfg = _cfg()
    mode = (cfg.get("agent") or {}).get("mode", "observation")
    # cycle countdown: next 5-min boundary
    now = datetime.now()
    nxt = (now + timedelta(minutes=5 - now.minute % 5)).replace(second=0, microsecond=0)
    return {
        "agent": agent,
        "mode": mode if mode in ("observation", "live") else "observation",
        "marketOpen": _market_open(),
        "cycleSeconds": max(0, int((nxt - now).total_seconds())),
        "killEngaged": kill,
    }


def regime_section(state: dict) -> dict:
    r = state.get("last_regime") or {}
    g = r.get("gex") or {}
    sign = g.get("sign")
    # GEX is CONTEXT/DISPLAY only (decision 2026-07-09) — surfaced here for the
    # regime bar, never used to score or gate a trade.
    return {"bias": r.get("bias", "—"), "trend": r.get("trend", "—"),
            "vix": str(r.get("vix", "—")),
            "gex": "POS" if sign == "positive" else "NEG" if sign == "negative" else "—",
            "gexNet": g.get("net_gex"),
            "gexRegime": r.get("gex_regime"),          # amplifying | suppressive | neutral
            "gexFlip": g.get("zero_gamma_level"),
            "gexSpot": g.get("spot"),
            "gexEstimated": "estimated" in (g.get("label") or "")}


def claude_section() -> dict:
    cfg = _cfg()
    ccfg = (cfg.get("llm") or {}).get("claude") or {}
    max_calls = ccfg.get("max_calls_per_day", 30)
    budget = _jload(ROOT / "data" / "claude_budget.json", {})
    today = f"{datetime.now():%Y-%m-%d}"
    used = 0
    day = budget.get("days", {}).get(today) if isinstance(budget.get("days"), dict) else None
    if isinstance(day, (int, float)):
        used = int(day)
    elif isinstance(budget.get(today), (int, float)):
        used = int(budget[today])
    elif isinstance(budget.get("calls"), dict):
        used = int(budget["calls"].get(today, 0))
    return {"used": used, "max": int(max_calls), "enabled": bool(ccfg.get("enabled", True))}


def forward_clock_section() -> dict:
    def build():
        import sys
        sys.path.insert(0, str(ROOT))
        from scripts.forward_acceptance_check import evaluate
        r = evaluate(ROOT)
        p, o = r.get("progress", {}), r.get("overall", {})
        n = (p.get("trades") or {}).get("n", 0)

        def chip(label, value, note, met, evaluable=True):
            return {"label": label, "value": value, "n": note,
                    "state": "pass" if met else ("fail" if evaluable else "wait")}

        sharpe = p.get("sharpe_ann") or {}
        sharpe_evaluable = n >= 20
        pf = p.get("profit_factor") or {}
        dd = p.get("max_drawdown") or {}
        sleeve = p.get("per_sleeve") or {}
        conc = p.get("concentration") or {}
        adverse = p.get("adverse_regime") or {}
        span = p.get("trading_day_span") or {}
        return {
            "rings": [
                {"done": n, "goal": (p.get("trades") or {}).get("required", 100),
                 "title": "Qualifying trades", "sub": "funnel-validated round trips"},
                {"done": span.get("trading_days", 0), "goal": span.get("required", 60),
                 "title": "Trading-day span", "sub": "distinct sessions elapsed"},
            ],
            "adverse": {"met": bool(adverse.get("met")),
                        "label": "SURVIVED" if adverse.get("met") else "NOT YET"},
            "chips": [
                chip("Sharpe", "—" if not sharpe_evaluable else str(sharpe.get("value", "—")),
                     f"n={n} · need 20" if not sharpe_evaluable else f"n={n}",
                     bool(sharpe.get("met")) and sharpe_evaluable, sharpe_evaluable),
                chip("Profit factor", str(pf.get("value", "—")), f"n={n}", bool(pf.get("met")),
                     pf.get("value") is not None),
                chip("Max drawdown", f"{dd.get('pct_of_risk_capital', '—')}%", "ok",
                     bool(dd.get("met")), dd.get("pct_of_risk_capital") is not None),
                chip("Per-sleeve edge",
                     f"{sum(1 for v in (sleeve.get('totals') or {}).values() if v >= 0)}/"
                     f"{max(1, len(sleeve.get('totals') or {}))}",
                     "all ≥ 0 required", bool(sleeve.get("met"))),
                chip("Concentration", _fmt_money(conc.get("pnl_after_top2_winners_removed", 0)),
                     "top-2 removed", bool(conc.get("met"))),
            ],
            "gate": {"label": "LOCKED" if not o.get("pass") else "READY",
                     "icon": "🔒" if not o.get("pass") else "✓", "eta": "Q4 2026"},
        }
    try:
        return _cached("forward", 60, build)
    except Exception:
        return {"rings": [{"done": 0, "goal": 100, "title": "Qualifying trades",
                           "sub": "evaluator unavailable"},
                          {"done": 0, "goal": 60, "title": "Trading-day span", "sub": "—"}],
                "adverse": {"met": False, "label": "UNKNOWN"},
                "chips": [], "gate": {"label": "LOCKED", "icon": "🔒", "eta": "—"}}


def positions_section(state: dict) -> tuple[list[dict], float]:
    # LANE REPOINT (2026-08-10): this read data/paper_positions.json — the
    # SHARED book retired by L-e. The engine writes data/lane/<lane>/positions
    # .json, so the cockpit showed an empty positions panel all session while
    # the engine held live positions. Route through the engine's own loader
    # rather than hardcoding paths: it is the thing that knows where the books
    # live, and a second hardcoded path is how this bug happened.
    from src.engine.paper_trader import load_positions
    positions = load_positions()
    out, total_unreal = [], 0.0
    watchdog = "stream" if (_pid_alive(state.get("pid")) and _market_open()) else "idle"
    for p in positions:
        entry = float(p.get("entry_price") or 0)
        mark = p.get("last_value") if p.get("last_value") is not None else entry
        qty = int(p.get("quantity", 1))
        unreal = round((float(mark) - entry) * 100 * qty, 2)
        total_unreal += unreal
        hi = p.get("mark_high", mark)
        lo = p.get("mark_low", mark)
        span = max(float(hi) - float(lo), 1e-9)
        marker = round((float(mark) - float(lo)) / span * 100)
        age = "—"
        try:
            dt = datetime.now() - datetime.fromisoformat(p.get("opened_at"))
            age = f"{int(dt.total_seconds() // 3600)}h{int(dt.total_seconds() % 3600 // 60):02d}" \
                if dt.total_seconds() >= 3600 else f"{int(dt.total_seconds() // 60)}m"
        except Exception:
            pass
        out.append({
            "recId": p.get("rec_id", "?"),
            "sleeve": "funnel" if _is_funnel(p.get("rec_id", "")) else "test",
            "ticker": p.get("ticker", "?"),
            "structure": (p.get("structure") or "").replace("_", " ").capitalize(),
            "dir": "LONG" if p.get("direction") == "long" else "SHORT",
            "qty": qty,
            "mae": _fmt_money(round((float(lo) - entry) * 100 * qty, 0)),
            "mfe": _fmt_money(round((float(hi) - entry) * 100 * qty, 0)),
            "markerL": min(100, max(0, marker)),
            "entry": f"{entry:.2f}", "mark": f"{float(mark):.2f}",
            "unreal": _fmt_money(unreal), "unrealN": unreal,
            "maxLoss": f"${abs(float(p.get('max_loss') or 0) * 100 * qty):,.0f}",
            "age": age,
            "exits": [
                {"k": "V", "armed": True, "title": "VWAP invalidation armed"},
                {"k": "S", "armed": True, "title": "Stop armed"},
                {"k": "T", "armed": bool(p.get("max_profit")), "title": "Target armed"},
                {"k": "F", "armed": True, "title": "15:45 flatten"},
            ],
            "watchdog": watchdog,
        })
    return out, round(total_unreal, 2)


def today_section() -> dict:
    exits = [e for e in _fills_today() if e.get("event") == "paper_exit"]
    funnel = [e for e in exits if _is_funnel(e.get("rec_id", ""))]
    # TWO STREAMS (standing rule): SCALP is the formal frozen-gate candidate,
    # not scenery, and must be reported as its own stream — never folded into
    # 'blended' as the only place it appears.
    scalp = [e for e in exits if not _is_funnel(e.get("rec_id", ""))]

    def _booked(e):
        """BOOKED realized P&L — the EXACT input the daily-loss breaker sums
        (risk.realized_pnl_today reads pnl_total from these same rows)."""
        v = e.get("pnl_total")
        if v is None:
            v = e.get("pnl_per_contract")
        return float(v or 0)

    def agg(rows):
        # DUAL RULER (2026-08-07). The strip previously labelled a MARKET-TRUTH
        # number "realized" while the breaker ran on BOOKED fills — on 08-06 that
        # read +246.50 beside a breaker at -1651.01, a ~1,900 disagreement with
        # no indication the two were different measures. Both are legitimate;
        # they answer different questions, so both are shown and named.
        booked = round(sum(_booked(e) for e in rows), 2)
        scored = [x for x in (_pnl(e) for e in rows) if x is not None]
        truth = round(sum(scored), 2)
        return {
            # HEADLINE: booked — identical to the breaker's input by construction
            "realized": booked,
            "realizedBooked": booked,
            "marketTruth": truth if scored else None,
            # what execution actually cost vs trading at the mid
            "executionCost": round(booked - truth, 2) if scored else None,
            "roundTrips": len(rows),
            "scored": len(scored), "pendingTruth": len(rows) - len(scored),
            # win/loss on the TRUTH ruler (the scoring basis), unchanged
            "wins": sum(1 for x in scored if x > 0),
            "losses": sum(1 for x in scored if x <= 0)}

    cfg = _cfg()
    rcfg = (cfg.get("risk") or {})
    limit = -abs(rcfg.get("account_size", 50000) * rcfg.get("max_daily_loss_pct", 3.0) / 100)
    blended_agg = agg(exits)
    scalp_agg = agg(scalp)
    # SCALP-v2 mechanics today (2026-09-14): the fill-mechanism window's own
    # moving parts — IOC attempts, fills, fill rate vs the frozen 25% floor, and
    # when the attempt cap bound. Data only; the gate is the nightly tracker.
    v2_att = [e for e in _fills_today()
              if e.get("event") == "entry_ioc_unfilled"
              # marketable_day (2026-09-16): a timeout-cancelled entry is an
              # ATTEMPT, exactly as an unfilled IOC is. v1's resting_mid
              # expiries carry no fill_mode and stay out of the v2 counters.
              or (e.get("event") == "entry_expired"
                  and str(e.get("fill_mode", "")).startswith("marketable"))
              or (e.get("event") in ("paper_fill", "partial_fill_booked")
                  and str(e.get("fill_mode", "")).startswith("marketable"))]
    # positive match: "not entry_ioc_unfilled" would count an expiry as a FILL
    v2_fills = sum(1 for e in v2_att
                   if e.get("event") in ("paper_fill", "partial_fill_booked"))
    v2_cap = int(((cfg.get("agent") or {}).get("scalp_v2") or {}).get("max_attempts_per_day") or 30)
    scalp_agg["v2"] = {
        "attempts": len(v2_att), "fills": v2_fills,
        "fillRate": round(v2_fills / len(v2_att), 3) if v2_att else None,
        "fillRateFloor": 0.25, "attemptCap": v2_cap,
        "capBoundAt": (str(v2_att[v2_cap - 1].get("ts", ""))[11:16] if len(v2_att) >= v2_cap else None),
    }
    return {
        "sessionDate": f"{datetime.now():%Y-%m-%d}",
        "funnel": agg(funnel),
        "scalp": scalp_agg,
        "blended": blended_agg,
        "markAge": "file", "breakerLimit": int(limit),
        "breakerTripped": blended_agg["realized"] <= limit,
    }


def recs_section() -> tuple[str, list[dict], int]:
    today = f"{datetime.now():%Y-%m-%d}"
    state = _state()
    cycle_id = f"#{state.get('cycle_count', '—')}"
    cards, confirm_secs = [], 0
    pending = _jload(ROOT / "data" / "pending_confirmations.json", [])

    # SESSION SCOPING + RENDER DEDUP (2026-08-07). Two display defects:
    #  * out of session the queue kept rendering the last session's cards as if
    #    live (a month-stale expiry was shown on 08-07);
    #  * the same rec_id rendered repeatedly because nothing deduped at render.
    # Display scoping only — NOTHING is deleted here.
    from src.engine.paper_trader import lane_of  # noqa: F401 (kept for clarity)
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "_liveness", ROOT / "scripts" / "ops" / "liveness.py")
        _lv = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_lv)
        in_session = _lv.session_open()
    except Exception:  # noqa: BLE001
        in_session = True

    seen: set = set()
    deduped = []
    for e in sorted(pending, key=lambda x: str(x.get("queued_at", "")), reverse=True):
        rid = str(e.get("rec_id"))
        if rid in seen:          # newest wins; older duplicates are not rendered
            continue
        seen.add(rid)
        deduped.append(e)
    pending = list(reversed(deduped))

    for e in pending:
        if not str(e.get("queued_at", "")).startswith(today):
            continue
        if not in_session and str(e.get("status", "")) == "pending":
            # out of session an unactioned card is NOT actionable; it is shown
            # (if at all) as a last-session view, never as a live queue item
            continue
        trade = (e.get("rec") or {}).get("trade") or {}
        status = e.get("status")
        legs = "\n".join(
            f"{'BUY ' if l.get('action', '').startswith('buy') else 'SELL'} {l.get('symbol', '')}"
            for l in trade.get("legs", [])) or "—"
        card = {
            "id": e["rec_id"], "ticker": e.get("ticker", "?"),
            "dir": "LONG" if e.get("direction") == "long" else "SHORT",
            "structure": f"{e.get('structure', '')} · exp {trade.get('expiration', '—')}",
            "legs": f"{legs}\nsized ×{e.get('quantity', 1)}  ·  net debit {e.get('net_debit')}",
            "math": [
                {"k": "NET DEBIT", "v": _fmt_money(float(e.get("net_debit") or 0) * 100 * e.get("quantity", 1)).lstrip("+"), "kind": "neutral"},
                {"k": "MAX LOSS", "v": _fmt_money(float(e.get("max_loss") or 0) * 100 * e.get("quantity", 1)).lstrip("+"), "kind": "loss"},
                {"k": "MAX PROFIT", "v": _fmt_money(float(trade.get("max_profit") or 0) * 100 * e.get("quantity", 1)).lstrip("+"), "kind": "profit"},
            ],
            "status": "pending" if status == "pending" else
                      ("approved" if status in ("confirmed", "executed") else "skipped"),
            "statusLabel": {"pending": "● CONFIRM NEEDED", "confirmed": "CONFIRMED",
                            "executed": "APPROVED · FILLED", "declined": "DECLINED",
                            "expired": "EXPIRED", "blocked_by_risk": "BLOCKED · RISK"}.get(status, str(status).upper()),
            "verdict": "APPROVED", "verdictIcon": "✓", "verdictKind": "approve",
            "reasoning": (e.get("rec") or {}).get("local_reasoning") or "Claude-approved funnel recommendation.",
        }
        if status == "pending":
            try:
                confirm_secs = max(confirm_secs, int(
                    (datetime.fromisoformat(e["expires_at"]) - datetime.now()).total_seconds()))
            except Exception:
                pass
        elif status in ("executed", "confirmed"):
            card["decidedLabel"] = "✓ CONFIRMED — order routed to sandbox"
            card["decidedKind"] = "confirmed"
        else:
            card["decidedLabel"] = f"✕ {str(status).upper().replace('_', ' ')}"
            card["decidedKind"] = "declined"
        cards.append(card)
    # today's posted (auto-submitted) recommendations
    for r in _jsonl(ROOT / "logs" / "recommendations.jsonl"):
        if not str(r.get("generated", "")).startswith(today):
            continue
        if any(c["id"] == r.get("rec_id") for c in cards):
            continue
        trade = r.get("trade") or {}
        cards.append({
            "id": r.get("rec_id"), "ticker": r.get("ticker", "?"),
            "dir": "LONG" if trade.get("direction") == "long" else "SHORT",
            "structure": f"{trade.get('structure', '')} · exp {trade.get('expiration', '—')}",
            "legs": "\n".join(
                f"{'BUY ' if l.get('action', '').startswith('buy') else 'SELL'} {l.get('symbol', '')}"
                for l in trade.get("legs", [])) + f"\nnet debit {trade.get('net_debit')}",
            "math": None,
            "status": "approved", "statusLabel": "APPROVED · SUBMITTED",
            "verdict": "APPROVED", "verdictIcon": "✓", "verdictKind": "approve",
            "reasoning": r.get("local_reasoning") or "Claude-approved; auto-submitted (paper).",
            "decidedLabel": "✓ SUBMITTED — see open positions / fills",
            "decidedKind": "confirmed",
        })
    return cycle_id, cards, max(0, confirm_secs)


def logs_section(state: dict) -> list[dict]:
    entries = []
    for e in reversed(_fills_today()[-12:]):
        t = str(e.get("ts", ""))[11:19]
        if e.get("event") == "paper_fill":
            entries.append({"time": t, "tag": "FILL", "kind": "fill",
                            "text": f"{e.get('ticker')} ×{e.get('quantity', 1)} filled @ {e.get('fill_price')}",
                            "meta": f"slippage {e.get('slippage')}", "metaKind": "mut"})
        elif e.get("event") == "paper_exit":
            pnl = _pnl(e)
            entries.append({"time": t, "tag": "EXIT", "kind": "exit",
                            "text": f"{e.get('ticker')} closed · {e.get('exit_reason')}",
                            "meta": ("truth pending" if pnl is None
                                     else f"{_fmt_money(pnl)} truth"),
                            "metaKind": "mut" if pnl is None else ("p" if pnl > 0 else "n")})
        elif e.get("event") in ("entry_ioc_unfilled", "entry_blocked_v2_price_floor") \
                or (e.get("event") == "entry_expired"
                    and str(e.get("fill_mode", "")).startswith("marketable")):
            # SCALP-v2 flow events are the DESIGN working, not safety incidents.
            # A marketable_day timeout-cancel is one of them; a v1 entry_expired
            # carries no fill_mode and keeps its existing classification.
            entries.append({"time": t, "tag": "V2", "kind": "v2",
                            "text": f"{e.get('event')} · {e.get('ticker', '')}"})
        else:
            entries.append({"time": t, "tag": "SAFETY", "kind": "safety",
                            "text": f"{e.get('event')} · {e.get('ticker', '')}"})
    lc = state.get("last_cycle")
    if lc:
        entries.insert(0, {"time": str(lc)[11:19], "tag": "CYCLE", "kind": "cycle",
                           "text": f"Cycle #{state.get('cycle_count')} complete · regime "
                                   f"{(state.get('last_regime') or {}).get('bias', '—')}"})
    return entries[:12]


def perf_section() -> dict:
    def build():
        # TRUTH-ONLY perf surface (user directive 2026-07-24): every figure on
        # the Perf page reads market-truth NBBO mids from the enriched ledger;
        # booked sandbox prices/P&L never appear. Unscored exits show as pending.
        fills = _jsonl(ROOT / "logs" / "observation" / "fills.jsonl")
        exits = [e for e in fills if e.get("event") == "paper_exit"]
        # enriched truth ledger: rec_id -> row (true mids, MFE/MAE, divergence)
        enr: dict[str, dict] = {}
        ep = ROOT / "logs" / "observation" / "enriched_trades_v1.jsonl"
        if ep.exists():
            for line in ep.read_text().splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("status") == "ok":
                    enr[r.get("rec_id")] = r

        by_day: dict[str, dict] = {}
        for e in exits:
            d = str(e.get("ts", ""))[:10]
            slot = by_day.setdefault(d, {"funnel": 0.0, "blended": 0.0})
            tp = _pnl(e)
            if tp is None:
                continue
            slot["blended"] += tp
            if _is_funnel(e.get("rec_id", "")):
                slot["funnel"] += tp
        days = sorted(by_day)[-16:]
        daily = [{"day": d[5:].replace("-", "/"), "funnel": round(by_day[d]["funnel"], 0),
                  "blended": round(by_day[d]["blended"], 0)} for d in days]

        # market-truth coverage (full period): realized + scored/pending, so the
        # Perf page reads the same ruler as the gates and the TODAY strip.
        def coverage(rows):
            scored = [e for e in rows if _pnl(e) is not None]
            return {"realized": round(sum(_pnl(e) for e in scored), 0),
                    "scored": len(scored), "pending": len(rows) - len(scored)}
        truth = {"funnel": coverage([e for e in exits if _is_funnel(e.get("rec_id", ""))]),
                 "blended": coverage(exits)}

        def sleeve_of(e):
            rid = str(e.get("rec_id", ""))
            return "funnel" if _is_funnel(rid) else rid.rsplit("-", 1)[-1]

        sl: dict[str, list] = {}
        for e in exits:
            tp = _pnl(e)
            if tp is not None:
                sl.setdefault(sleeve_of(e), []).append(tp)
        sleeves = [{"name": ("Funnel · validated" if k == "funnel" else k.replace("_", " ")),
                    "tag": "FUNNEL" if k == "funnel" else "—", "accent": k == "funnel",
                    "note": "Claude-approved round trips" if k == "funnel" else "execution-test",
                    "win": f"{round(sum(1 for x in v if x > 0) / len(v) * 100)}%",
                    "trades": len(v), "pnl": round(sum(v), 0)}
                   for k, v in sorted(sl.items(), key=lambda kv: kv[0] != "funnel")]

        reasons: dict[str, list] = {}
        for e in exits:
            tp = _pnl(e)
            if tp is not None:
                reasons.setdefault(e.get("exit_reason", "?"), []).append(tp)
        exit_reasons = [{"label": k, "count": len(v), "pnl": round(sum(v), 0),
                         "tone": "g" if sum(v) > 0 else "r"}
                        for k, v in sorted(reasons.items(), key=lambda kv: -len(kv[1]))]

        # give-back on TRUTH basis: peak truth unrealized (mfe_mid_1m) minus the
        # truth exit mid, in dollars — never booked marks.
        gb = []
        for e in exits:
            r = enr.get(str(e.get("rec_id", "")))
            if r and r.get("mfe_mid_1m") is not None and r.get("true_mid_exit") is not None:
                gb.append(max(0.0, (r["mfe_mid_1m"] - r["true_mid_exit"]) * 100 * int(r.get("quantity", 1))))
        give_back = {"avg": f"{round(sum(gb) / len(gb)):+.0f}$" if gb else "—",
                     "worst": _fmt_money(max(gb)) if gb else "—",
                     "note": "Give-back vs peak TRUTH unrealized (mfe_mid_1m) — market-truth "
                             "NBBO mids, not booked marks."}

        # execution divergence (replaces sandbox slippage): booked fill vs truth
        # NBBO mid, per contract. The only honest execution-quality read.
        divs = [r["exit_divergence"] for r in enr.values() if r.get("exit_divergence") is not None]
        buckets: dict[str, int] = {}
        for s in divs:
            b = f"{(round(s, 1) or 0.0):+.1f}"  # `or 0.0` collapses signed-zero (-0.0 -> +0.0)
            buckets[b] = buckets.get(b, 0) + 1
        top = sorted(buckets.items(), key=lambda kv: -kv[1])[:9]
        slippage = {"buckets": [{"bucket": k, "n": v} for k, v in sorted(top, key=lambda kv: float(kv[0]))],
                    "median": f"{sorted(divs)[len(divs) // 2]:+.3f}" if divs else "—",
                    "avg": f"{sum(divs) / len(divs):+.3f}" if divs else "—"}

        def trip(e):
            rid = str(e.get("rec_id", ""))
            pnl = _pnl(e)
            r = enr.get(rid) or {}
            me, mx = r.get("true_mid_entry"), r.get("true_mid_exit")
            px = f"{me:.2f}→{mx:.2f}" if (me is not None and mx is not None) else "pending"
            div = r.get("exit_divergence")
            return {"time": str(e.get("ts", ""))[5:16], "ticker": e.get("ticker", "?"),
                    "sleeve": "funnel" if _is_funnel(rid) else "test",
                    "dir": "LONG", "struct": rid.rsplit("-", 1)[-1].replace("_", " ")[:18],
                    "px": px,
                    "exit": str(e.get("exit_reason", ""))[:14],
                    "exitTone": "mut" if pnl is None else ("g" if pnl > 0 else "r"),
                    "pnl": "pending" if pnl is None else _fmt_money(pnl),
                    "pnlN": pnl or 0.0,
                    "slip": f"{div:+.2f}" if div is not None else "—", "giveBack": "—",
                    "test": not _is_funnel(rid)}

        funnel_trips = [trip(e) for e in exits if _is_funnel(e.get("rec_id", ""))][-20:]
        test_trips = [trip(e) for e in exits if not _is_funnel(e.get("rec_id", ""))][-10:]
        return {"daily": daily, "truth": truth, "sleeves": sleeves, "exitReasons": exit_reasons,
                "giveBack": give_back, "slippage": slippage,
                "trades": funnel_trips, "testTrades": test_trips}
    try:
        return _cached("perf", 60, build)
    except Exception:
        return {"daily": [], "truth": {"funnel": {"realized": 0, "scored": 0, "pending": 0},
                                       "blended": {"realized": 0, "scored": 0, "pending": 0}},
                "sleeves": [], "exitReasons": [],
                "giveBack": {"avg": "—", "worst": "—", "note": "unavailable"},
                "slippage": {"buckets": [], "median": "—", "avg": "—"},
                "trades": [], "testTrades": []}


def health_section(state: dict) -> dict:
    def build():
        alive = _pid_alive(state.get("pid"))
        cal = _jload(ROOT / "data" / "earnings_calendar.json", {})
        import sys
        sys.path.insert(0, str(ROOT))
        horizon = (datetime.now() + timedelta(days=7)).date().isoformat()
        earnings = []
        try:
            from src.engine.earnings import earnings_veto, _etf_symbols
            etfs = _etf_symbols()
            watch = _cfg().get("watchlist", {}).get("default", [])
            for t in sorted(watch)[:12]:
                if t in etfs:
                    earnings.append({"ticker": t, "date": "—", "state": "etf"})
                    continue
                dates = (cal.get(t) or {}).get("dates") or []
                nxt = dates[0] if dates else "?"
                veto = earnings_veto(t, horizon, cal)
                earnings.append({"ticker": t, "date": nxt,
                                 "state": "block" if veto and "earnings" in veto else
                                          ("watch" if veto else "clear")})
        except Exception:
            pass
        # errors/warnings today from agent.log
        errors = warnings = 0
        log = ROOT / "logs" / "agent.log"
        today = f"{datetime.now():%Y-%m-%d}"
        if log.exists():
            for line in log.read_text()[-300_000:].splitlines():
                if line.startswith(today):
                    if " ERROR " in line:
                        errors += 1
                    elif " WARNING " in line:
                        warnings += 1
        jobs = []
        for label, logfile in [("Nightly ops", "launchd_nightlyops.log"),
                               ("Weekly review", "launchd_weeklyreview.log"),
                               ("Forward push", "launchd_weeklyfwd.log"),
                               ("Bar snapshot", "launchd_snapshot.log")]:
            p = ROOT / "logs" / logfile
            last = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M") if p.exists() else "never"
            jobs.append({"name": label, "detail": logfile, "last": last, "status": "ok" if p.exists() else "warn"})
        market = _market_open()
        return {
            "errors24h": errors, "warnings24h": warnings,
            "feeds": [
                {"name": "Tradier", "role": "primary quotes/chains",
                 "status": "live" if alive else "down", "latency": "—", "last": "agent-managed"},
                {"name": "yfinance", "role": "fallback bars", "status": "standby", "latency": "—", "last": "—"},
                {"name": "Put/Call proxy", "role": "SPY/QQQ chain volumes",
                 "status": "live" if alive and market else "standby", "latency": "—", "last": "per cycle"},
                {"name": "TICK proxy", "role": "breadth 50-name", "status": "live" if alive and market else "standby",
                 "latency": "—", "last": "per cycle"},
            ],
            "host": {"sleepPrevention": "ACTIVE (caffeinate)", "uptime": "—", "cpu": "—", "mem": "—"},
            "watchdog": "stream" if alive and market else ("offline" if not alive else "stream"),
            "earnings": earnings,
            "jobs": jobs,
        }
    try:
        return _cached("health", 60, build)
    except Exception:
        return {"errors24h": 0, "warnings24h": 0, "feeds": [], "host": {},
                "watchdog": "offline", "earnings": [], "jobs": []}


def reports_section() -> list[dict]:
    def md_to_blocks(text: str) -> tuple[list[dict], int]:
        blocks, actions, items = [], 0, []

        def flush():
            nonlocal items
            if items:
                blocks.append({"kind": "list", "items": [[{"text": i}] for i in items]})
                items = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if "action needed" in low or "action_needed" in low:
                actions += s.count("ACTION")
            if s.startswith("## "):
                flush()
                blocks.append({"kind": "h2", "text": s[3:]})
            elif s.startswith("# "):
                flush()
                blocks.append({"kind": "h2", "text": s[2:]})
            elif s.startswith(("- ", "* ")):
                items.append(s[2:])
            else:
                flush()
                blocks.append({"kind": "para", "spans": [{"text": s}]})
        flush()
        return blocks[:60], actions

    out = []
    ops = ROOT / "logs" / "ops"
    for kind, pattern, title in [("weekly", "weekly-review-*.md", "Weekly Check-in"),
                                 ("nightly", "report-*.md", "Nightly Readiness")]:
        files = sorted(ops.glob(pattern))
        if not files:
            continue
        latest = files[-1]
        text = latest.read_text()
        blocks, _ = md_to_blocks(text)
        action_count = 1 if ("ACTION" in text and "None." not in text.split("ACTION")[-1][:200]) else 0
        out.append({"id": kind, "title": title, "date": latest.stem.split("-", 2)[-1],
                    "actionCount": action_count,
                    "kicker": f"{title.upper()} · {latest.name}",
                    "heading": title, "subtitle": latest.name, "blocks": blocks})
    return out


def build_feed() -> dict:
    state = _state()
    a = agent_section(state)
    positions, total_unreal = positions_section(state)
    today = today_section()
    today["unrealized"] = total_unreal
    # per-view open-position stats: FUNNEL ONLY must not count test-lane
    # positions (user 2026-07-14 — the shared tile was a contamination leak)
    fun_rows = [r for r in positions if r.get("sleeve") == "funnel"]
    today["funnel"]["openPositions"] = len(fun_rows)
    today["funnel"]["unrealized"] = round(sum(r["unrealN"] for r in fun_rows), 2)
    scalp_rows = [r for r in positions if r.get("sleeve") != "funnel"]
    today["scalp"]["openPositions"] = len(scalp_rows)
    today["scalp"]["unrealized"] = round(sum(r["unrealN"] for r in scalp_rows), 2)
    today["blended"]["openPositions"] = len(positions)
    today["blended"]["unrealized"] = total_unreal
    # market-truth realized (L21): what the gates actually count — shown so
    # the user's eyes and the scoreboard read the same ruler (2026-07-15)
    try:
        enr = {}
        p = ROOT / "logs" / "observation" / "enriched_trades_v1.jsonl"
        if p.exists():
            for line in p.read_text().splitlines():
                r = json.loads(line)
                enr[r["rec_id"]] = r
        # keep: None = every lane; True = funnel only; False = scalp only
        for key, want_funnel in (("funnel", True), ("scalp", False), ("blended", None)):
            scored = pending = 0
            true_sum = 0.0
            for e in _fills_today():
                if e.get("event") != "paper_exit":
                    continue
                if want_funnel is not None and \
                        want_funnel == _is_scalp_id(e.get("rec_id", "")):
                    continue
                row = enr.get(e.get("rec_id", ""))
                if row and row.get("status") == "ok" and row.get("true_pnl_mid") is not None:
                    scored += 1
                    true_sum += row["true_pnl_mid"]
                else:
                    pending += 1
            today[key]["trueRealized"] = round(true_sum, 2)
            today[key]["trueScored"] = scored
            today[key]["truePending"] = pending
    except Exception:
        pass
    cycle_id, recs, confirm_secs = recs_section()
    # The flatten is risk.paper_exits.flat_time_et = 15:45 **ET** (12:45 PT).
    # The note said "15:45 PT", an hour after the close — read from config so
    # the label cannot drift from the rule again.
    _flat_et = str((( _cfg().get("risk") or {}).get("paper_exits") or {})
                   .get("flat_time_et", "15:45"))
    market_note = (f"Market closed — flattened at {_flat_et} ET prior session."
                   if not a["marketOpen"] else "Scanner active · awaiting a qualifying setup.")
    return {
        "simulated": False,
        # STALENESS STAMP (2026-08-10): the cockpit served code from a process
        # started 17 days earlier — through a lane repoint and a rec_id rename —
        # and nothing in the payload said so. Report the SHA this process
        # IMPORTED and when it started, so a stale server is visible, not
        # inferred from wrong numbers.
        "build": _build_stamp(),
        **{k: a[k] for k in ("agent", "mode", "marketOpen", "cycleSeconds")},
        "regime": regime_section(state),
        "claude": claude_section(),
        "forwardClock": forward_clock_section(),
        "today": today,
        "positions": positions,
        "noPositionsReason": market_note,
        "cycleId": cycle_id,
        "recs": recs,
        "noRecsReason": ("Market closed — scanner resumes at next session open."
                         if not a["marketOpen"] else
                         "Scanner running · no setups cleared the gate this cycle."),
        "confirmSeconds": confirm_secs,
        "logs": logs_section(state),
        "dataAge": f"updated {datetime.now():%H:%M:%S}",
        "killEngaged": a["killEngaged"],
        "perf": perf_section(),
        "health": health_section(state),
        "reports": reports_section(),
    }
