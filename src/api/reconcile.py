"""Live position reconciliation (2026-07-24) — the "reconcile now" action.

The cockpit positions panel normally reads data/paper_positions.json, a LOCAL
cache the paper-trader poll cycle refreshes. Between polls it can lag the broker
(e.g. a closed leg still showing, the 2026-07-24 "phantom QQQ" report). This
module does a LIVE Tradier sandbox query on demand and reconciles it against the
agent's tracker so the UI can show ground truth without waiting for the cycle.

Read-only: it queries positions, never places or cancels anything.

SAFETY: the user holds their OWN manual trades in the same sandbox account
(HON, GOOGL, DELL, ...). Broker legs the agent does not track are reported as
`manual` — informational, "not agent-managed" — and are NEVER presented as
discrepancies to act on. The agent monitors only agent-placed positions.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_OCC = re.compile(r"^([A-Z]+)\d{6}[CP]\d{8}$")


def _underlying(symbol: str) -> str:
    """Underlying ticker from an OCC option symbol, or the symbol itself for
    an equity position (e.g. 'QQQ260717C00500000' -> 'QQQ', 'GOOGL' -> 'GOOGL')."""
    m = _OCC.match(symbol or "")
    return m.group(1) if m else (symbol or "?").upper()


def build_reconciliation() -> dict:
    # LANE REPOINT (2026-08-10) — the shared book is retired; reading it made
    # this endpoint silently compare the broker against an EMPTY tracked set,
    # which reads as "broker has orphans" rather than as a broken read.
    from src.engine.paper_trader import load_positions

    tracked = load_positions()
    tracked_tickers = sorted({(p.get("ticker") or "?").upper() for p in tracked})

    # ── live broker query ──
    # BROKER RESOLUTION (2026-08-10): this hardcoded the Tradier sandbox while
    # the configured broker is ibkr_paper. It was harmless only because the
    # retired book above made `tracked` empty — repointing to the live lane
    # books would otherwise have compared REAL positions against an abandoned
    # broker and reported every one of them as missing: a false alarm on the
    # money surface. Resolve the broker the same way the engine does.
    try:
        from src.data.order_client import get_order_client
        client = get_order_client()
    except Exception as exc:
        return {"ok": False, "error": f"broker client unavailable: {exc}",
                "tracked": tracked_tickers}
    if not client.available():
        return {"ok": False,
                "error": "configured broker unavailable — cannot reconcile "
                         "(this is a FAILED READ, not an empty broker)",
                "tracked": tracked_tickers}

    raw = client.get_positions()
    # get_positions returns [{"error": ...}] shape only via _request on HTTPError;
    # a bare list of position dicts on success. Detect an error envelope.
    if isinstance(raw, dict) and raw.get("error"):
        return {"ok": False, "error": f"broker error: {raw.get('error')}",
                "tracked": tracked_tickers}
    # R2 — a FAILED READ IS NOT AN EMPTY BROKER. get_positions() returns None
    # when the read fails; `raw or []` would have turned that into "the broker
    # holds nothing" and reported every real position as a stale phantom.
    if raw is None:
        return {"ok": False,
                "error": "broker position read FAILED (not an empty account) — "
                         "nothing reconciled; retry rather than acting on this",
                "tracked": tracked_tickers}

    # group broker legs by underlying
    by_underlying: dict[str, list[dict]] = {}
    for pos in raw:
        u = _underlying(pos.get("symbol", ""))
        by_underlying.setdefault(u, []).append({
            "symbol": pos.get("symbol"),
            "quantity": pos.get("quantity"),
            "cost_basis": pos.get("cost_basis"),
            "acquired": pos.get("date_acquired"),
        })
    broker_underlyings = set(by_underlying)
    tracked_set = set(tracked_tickers)

    confirmed = sorted(tracked_set & broker_underlyings)
    # in the tracker but NOT at the broker = stale cache row (the phantom case)
    stale = sorted(tracked_set - broker_underlyings)
    # at the broker but not agent-tracked = the user's manual positions
    manual = sorted(broker_underlyings - tracked_set)

    return {
        "ok": True,
        "broker_positions": [
            {"underlying": u, "legs": by_underlying[u]}
            for u in sorted(by_underlying)
        ],
        "tracked": tracked_tickers,
        "reconciliation": {
            "confirmed": confirmed,      # agent position present at broker
            "stale_tracked": stale,      # in local cache, gone at broker (lag/phantom)
            "manual": manual,            # broker legs not agent-managed (user's own)
        },
        "counts": {
            "broker_underlyings": len(broker_underlyings),
            "broker_legs": len(raw),
            "tracked": len(tracked_tickers),
            "confirmed": len(confirmed),
            "stale_tracked": len(stale),
            "manual": len(manual),
        },
    }
