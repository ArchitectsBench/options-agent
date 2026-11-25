"""Position-reconciliation drift analysis — PURE, side-effect-free.

Formalizes the failure mode where the engine's book (data/paper_positions.json,
via paper_trader.load_positions) disagrees with the broker's book
(PaperOrderClient.get_positions()). This is the general form of lesson L20
(2026-07-14: the sandbox silently deleted SMCI + IWM overnight; every close
rejected and the re-arm loop spent the day firing guaranteed rejects).

`reconcile_drift()` takes the two position lists as plain data (NO broker client
import — positions are injected, keeping it pure and unit-testable) and returns a
structured diff classifying every leg into exactly one drift class:

  - broker_lost       engine holds a leg the broker no longer has
                      -> flag broker_lost, stop close-retries (the L20 case)
  - orphan_at_broker  broker holds a leg the engine doesn't track
                      -> adopt ONLY if adopt_foreign, else leave untouched.
                         adopt_foreign=false protects the user's MANUAL trades
                         (HON/GOOGL/DELL/PLTR) that share the sandbox account.
  - quantity_mismatch same leg present both sides, signed quantities differ
                      -> partial-fill / manual-adjust drift, reconcile quantity
  - in_sync           matched, quantities agree (returned under `matched`)

Matching key: the OCC option symbol. Underlying = symbol[:-15]. Broker quantity
is signed (+long / -short). Engine legs carry an `action` (buy_to_open /
sell_to_open) + a position-level `quantity`; the signed engine leg quantity is
derived as (+qty for a bought/long leg, -qty for a sold/short leg).

Manual-position protection invariant: a leg the engine does NOT track can only
ever be classified `orphan_at_broker`, whose action is `adopt` (when
adopt_foreign) or `leave_untouched` (default). This function NEVER emits a
flag/close action against an untracked (foreign/manual) position.

The caller logs the human-readable one-liners in `actions`; nothing is logged
here so the function stays pure.
"""
from __future__ import annotations

# Long-leg actions map to +1, short-leg actions to -1. Engine open positions use
# the *_to_open forms; the close forms are mapped too for defensiveness.
_SIGN = {
    "buy_to_open": 1,
    "buy_to_close": 1,
    "sell_to_open": -1,
    "sell_to_close": -1,
}

_QTY_EPS = 1e-9


def _num(value, default: float = 0.0) -> float:
    """Coerce a quantity/cost field to float, tolerating None/str/blank."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_zero(qty: float) -> bool:
    return abs(qty) < _QTY_EPS


def _qty_equal(a: float, b: float) -> bool:
    return abs(a - b) < _QTY_EPS


def underlying_of(symbol: str) -> str:
    """Underlying root from an OCC option symbol (drops the 15-char
    YYMMDD+C/P+strike suffix). Falls back to the whole string for non-OCC."""
    return symbol[:-15] if len(symbol) > 15 else symbol


def _fmt(qty: float) -> str:
    """Compact quantity for log lines (10.0 -> '10', -5.0 -> '-5')."""
    return f"{qty:g}"


def _engine_leg_index(engine_positions: list[dict]) -> dict:
    """symbol -> {symbol, engine_qty(signed), rec_ids[], ticker}. Aggregates
    across positions/legs so a symbol appearing twice sums to one signed qty."""
    index: dict[str, dict] = {}
    for pos in engine_positions or []:
        rec_id = pos.get("rec_id")
        # position-level quantity governs every leg (contracts per leg); default 1
        qty = _num(pos.get("quantity", 1), 1.0) or 1.0
        legs = pos.get("legs") or []
        pos_ticker = pos.get("ticker")
        for leg in legs:
            sym = leg.get("symbol")
            if not sym:
                continue
            sign = _SIGN.get(leg.get("action"), 1)
            signed = sign * qty
            ticker = pos_ticker or underlying_of(sym)
            entry = index.setdefault(
                sym, {"symbol": sym, "engine_qty": 0.0, "rec_ids": [], "ticker": ticker}
            )
            entry["engine_qty"] += signed
            if rec_id is not None and rec_id not in entry["rec_ids"]:
                entry["rec_ids"].append(rec_id)
    return index


def _broker_leg_index(broker_positions: list[dict]) -> dict:
    """symbol -> {symbol, broker_qty(signed), cost_basis}. Aggregates duplicates."""
    index: dict[str, dict] = {}
    for bp in broker_positions or []:
        sym = bp.get("symbol")
        if not sym:
            continue
        entry = index.setdefault(sym, {"symbol": sym, "broker_qty": 0.0, "cost_basis": 0.0})
        entry["broker_qty"] += _num(bp.get("quantity", 0))
        entry["cost_basis"] += _num(bp.get("cost_basis", 0))
    return index


def reconcile_drift(
    engine_positions: list[dict],
    broker_positions: list[dict],
    *,
    adopt_foreign: bool = False,
) -> dict:
    """Compare engine book vs broker book at the OCC-leg level.

    Returns::

        {
          "matched": [ {class:"in_sync", rec_id, ticker, symbol,
                        engine_qty, broker_qty}, ... ],
          "drift":   [ {class, rec_id?, ticker, symbol, engine_qty,
                        broker_qty, detail, action}, ... ],
          "actions": [ "<one log line per drift entry>", ... ],
        }

    `actions` is parallel to `drift` (one string per drift entry) so the caller
    can log each while switching on the machine-readable `action`/`class` fields.
    Pure: no I/O, no logging, no broker access.
    """
    engine_legs = _engine_leg_index(engine_positions)
    broker_legs = _broker_leg_index(broker_positions)

    matched: list[dict] = []
    drift: list[dict] = []
    actions: list[str] = []

    # 1) Every leg the engine tracks: in_sync, quantity_mismatch, or broker_lost.
    for sym, e in engine_legs.items():
        engine_qty = e["engine_qty"]
        rec_id = e["rec_ids"][0] if e["rec_ids"] else None
        ticker = e["ticker"]
        b = broker_legs.get(sym)
        broker_qty = b["broker_qty"] if b is not None else 0.0

        if b is None or _is_zero(broker_qty):
            detail = {
                "reason": "engine holds a leg the broker no longer has",
                "close_retries": "stop",
                "lesson": "L20",
            }
            if len(e["rec_ids"]) > 1:
                detail["rec_ids"] = list(e["rec_ids"])
            drift.append({
                "class": "broker_lost",
                "rec_id": rec_id,
                "ticker": ticker,
                "symbol": sym,
                "engine_qty": engine_qty,
                "broker_qty": broker_qty,
                "action": "flag_broker_lost",
                "detail": detail,
            })
            actions.append(
                f"[broker_lost] {ticker} {rec_id} leg {sym}: engine holds "
                f"{_fmt(engine_qty)} but broker holds none — flag broker_lost, "
                f"stop close-retries (L20)"
            )
        elif _qty_equal(engine_qty, broker_qty):
            matched.append({
                "class": "in_sync",
                "rec_id": rec_id,
                "ticker": ticker,
                "symbol": sym,
                "engine_qty": engine_qty,
                "broker_qty": broker_qty,
            })
        else:
            detail = {
                "reason": "same leg, engine_qty != broker_qty",
                "delta": round(broker_qty - engine_qty, 6),
                "cause": "partial-fill / manual-adjust drift",
            }
            if len(e["rec_ids"]) > 1:
                detail["rec_ids"] = list(e["rec_ids"])
            drift.append({
                "class": "quantity_mismatch",
                "rec_id": rec_id,
                "ticker": ticker,
                "symbol": sym,
                "engine_qty": engine_qty,
                "broker_qty": broker_qty,
                "action": "reconcile_quantity",
                "detail": detail,
            })
            actions.append(
                f"[quantity_mismatch] {ticker} {rec_id} leg {sym}: engine "
                f"{_fmt(engine_qty)} != broker {_fmt(broker_qty)} "
                f"(partial-fill/manual-adjust drift)"
            )

    # 2) Broker legs the engine does NOT track -> orphan_at_broker.
    #    These are the user's manual trades (or an un-adopted crash orphan). With
    #    adopt_foreign=false they are left untouched — NEVER flagged or closed.
    for sym, b in broker_legs.items():
        if sym in engine_legs:
            continue  # already classified above
        broker_qty = b["broker_qty"]
        if _is_zero(broker_qty):
            continue  # broker flat on a symbol we don't track — nothing to do
        ticker = underlying_of(sym)
        if adopt_foreign:
            drift.append({
                "class": "orphan_at_broker",
                "rec_id": None,
                "ticker": ticker,
                "symbol": sym,
                "engine_qty": 0.0,
                "broker_qty": broker_qty,
                "action": "adopt",
                "detail": {
                    "reason": "broker holds a leg the engine doesn't track",
                    "cost_basis": round(b["cost_basis"], 4),
                    "adopt_foreign": True,
                },
            })
            actions.append(
                f"[orphan_at_broker] {ticker} leg {sym}: broker holds "
                f"{_fmt(broker_qty)}, engine untracked — adopt_foreign=true, "
                f"adopting into book"
            )
        else:
            drift.append({
                "class": "orphan_at_broker",
                "rec_id": None,
                "ticker": ticker,
                "symbol": sym,
                "engine_qty": 0.0,
                "broker_qty": broker_qty,
                "action": "leave_untouched",
                "detail": {
                    "reason": "broker holds a leg the engine doesn't track",
                    "cost_basis": round(b["cost_basis"], 4),
                    "adopt_foreign": False,
                    "protection": "manual position preserved — not flagged, not closed",
                },
            })
            actions.append(
                f"[orphan_at_broker] {ticker} leg {sym}: broker holds "
                f"{_fmt(broker_qty)}, engine untracked — adopt_foreign=false, "
                f"left untouched (manual position protected)"
            )

    return {"matched": matched, "drift": drift, "actions": actions}
