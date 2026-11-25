"""Cockpit API server (2026-07-04) — serves the real CockpitFeed to the UI.

  GET  /api/feed      one feed snapshot (JSON, shape = ui/src/feed/types.ts)
  WS   /ws/live       pushes a fresh snapshot every PUSH_INTERVAL_S seconds
  POST /api/confirm/{rec_id} | /api/decline/{rec_id}
                      human decision on a queued recommendation (same paths
                      the CLI and phone buttons use — confirm.py owns state)
  POST /api/kill      engage/disengage the kill switch {"on": bool, "reason"}
  /                   serves the built UI (ui/dist) when present

Read-only over the trader's state files except the two explicitly
human-initiated controls (confirm/decline, kill switch), which reuse the
exact same functions as the CLI. Runs as its own process:

  .venv/bin/uvicorn src.api.server:app --port 8421
"""

import asyncio
import logging
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.api.feed import build_feed  # noqa: E402

logger = logging.getLogger("cockpit-api")
PUSH_INTERVAL_S = 5

app = FastAPI(title="OptionsAgent Cockpit API")


@app.get("/api/feed")
def feed():
    return build_feed()


@app.get("/api/position/{rec_id}/detail")
def position_detail(rec_id: str):
    from src.api.detail import build_detail
    out = build_detail(rec_id)
    return out if out is not None else {"error": f"{rec_id} not in the open book"}


@app.get("/api/tradeview")
def tradeview_list(date: str | None = None):
    """The TRADE page's list: every option trade filled on `date` (default
    today), open ones first."""
    from datetime import datetime as _dt
    from src.api.trade_view import list_trades
    return list_trades(date or _dt.now().strftime("%Y-%m-%d"))


@app.get("/api/tradeview/{rec_id}")
def tradeview(rec_id: str):
    """One trade's chart stack + rules-based checklist, timeline and verdict."""
    from src.api.trade_view import build_trade_view
    out = build_trade_view(rec_id)
    return out if out is not None else {"error": f"no fill recorded for {rec_id}"}


@app.get("/api/clocks")
def clocks():
    from src.api.clocks import build_clocks
    return build_clocks()


@app.get("/api/sleeves")
def sleeves():
    from src.api.sleeves import build_sleeves
    return build_sleeves()


@app.get("/api/reconcile-positions")
def reconcile_positions():
    """LIVE Tradier sandbox query, reconciled against the agent tracker.
    Read-only; surfaces stale-cache rows and the user's own manual positions."""
    from src.api.reconcile import build_reconciliation
    return build_reconciliation()


@app.get("/api/watchlist-squeeze")
def watchlist_squeeze():
    """Manual-decision-support watchlist (scripts/watch_buyzone.py state).
    `stamp`/`rows` unchanged; additive ops fields last_run_at / data_asof /
    stale (2026-09-13) — builder in src/api/watchlist_squeeze.py."""
    from src.api.watchlist_squeeze import build_watchlist_squeeze
    return build_watchlist_squeeze()


def _wl_severity(ev: dict) -> str:
    """Severity tier for a watchlist event (A2). Stale demotes one tier."""
    tr = ev.get("transition")
    base = ({"fired": "high", "earnings_window": "high",
             "zone_entered": "med", "invalidated": "med",
             "earnings_passed": "low", "removed": "low"}).get(tr, "low")
    if ev.get("counter_trend") and base == "high":
        base = "high"           # counter-trend fire stays high (noteworthy)
    if ev.get("stale_data") and base == "high":
        base = "med"            # stale demotes
    elif ev.get("stale_data") and base == "med":
        base = "low"
    return base


@app.get("/api/watchlist-events")
def watchlist_events():
    """Phase A2: pure reader of logs/alerts/events.jsonl. Returns the flat
    event feed (newest first) + threads grouped by state_instance_id, each
    event enriched with a severity tier. UI never stores alert state."""
    import json as _json
    from pathlib import Path as _P
    f = _P(__file__).resolve().parents[2] / "logs" / "alerts" / "events.jsonl"
    events = []
    if f.exists():
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                ev = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            ev["severity"] = _wl_severity(ev)
            events.append(ev)
    events.sort(key=lambda e: e.get("detected_at", ""), reverse=True)
    threads: dict = {}
    for ev in events:
        iid = ev.get("state_instance_id") or f"_{ev.get('ticker')}_{ev.get('transition')}"
        threads.setdefault(iid, []).append(ev)
    for iid in threads:
        threads[iid].sort(key=lambda e: e.get("detected_at", ""))
    return {"stamp": "MANUAL DECISION SUPPORT — NOT SIGNALS",
            "events": events[:200],
            "threads": threads,
            "stale_count": sum(1 for e in events if e.get("stale_data")),
            "severity_counts": {s: sum(1 for e in events if e.get("severity") == s)
                                for s in ("high", "med", "low")}}


@app.post("/api/confirm/{rec_id}")
def confirm(rec_id: str):
    from src.engine.confirm import confirm as _confirm
    return {"result": _confirm(rec_id)}


@app.post("/api/decline/{rec_id}")
def decline(rec_id: str):
    from src.engine.confirm import decline as _decline
    return {"result": _decline(rec_id)}


@app.post("/api/kill")
def kill(body: dict):
    from src.engine.live_gate import engage_kill_switch, kill_switch_engaged, release_kill_switch
    if body.get("on"):
        engage_kill_switch(str(body.get("reason") or "cockpit UI"))
    else:
        release_kill_switch()
    return {"engaged": kill_switch_engaged()}


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            snapshot = await asyncio.to_thread(build_feed)
            await ws.send_json(snapshot)
            await asyncio.sleep(PUSH_INTERVAL_S)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as exc:  # feed builder must never kill the socket loop silently
        logger.warning("ws feed error: %s", exc)


# serve the built UI when present (production mode: one process on 8421)
_dist = ROOT / "ui" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
