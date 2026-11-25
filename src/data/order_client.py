"""Broker order-client factory.

`get_order_client()` returns the concrete order client selected by
config.yaml `agent.broker`:

  tradier_sandbox  (DEFAULT)  -> PaperOrderClient   (src/data/tradier_orders.py)
  ibkr_paper                  -> IbkrOrderClient     (src/data/ibkr_orders.py)

DEFAULT is tradier_sandbox so the current behavior is preserved bit-for-bit
when the key is absent (it is not yet in config.yaml).

Two deliberate design points:

1. IBKR is a SINGLETON. paper_trader instantiates a client at ~7 call sites,
   several times per 5-minute cycle. A Tradier client is a cheap, stateless
   HTTP wrapper, so we hand back a fresh one every time (identical to the old
   `PaperOrderClient()` behavior). An IBKR client owns a stateful gateway
   socket (one connection per clientId), so it is created ONCE and reused.

2. The tradier_sandbox branch resolves `PaperOrderClient` from the
   paper_trader module namespace, not from tradier_orders directly. paper_trader
   is the canonical owner of that symbol and the existing engine tests patch
   `paper_trader.PaperOrderClient`; routing through its namespace keeps those
   patches — and thus the current behavior — intact. The import is lazy to
   avoid an import cycle (paper_trader imports this factory at module load).
"""

from __future__ import annotations

from pathlib import Path

from src.data.ibkr_orders import IbkrOrderClient

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "config.yaml"

VALID_BROKERS = ("tradier_sandbox", "ibkr_paper")
DEFAULT_BROKER = "tradier_sandbox"

# ── 3-state POST outcome contract (R1) ───────────────────────────────────────
# The broker place_*_order response is AMBIGUOUS in one dangerous case: a POST
# that timed out AFTER the broker accepted the order. Blindly treating that as a
# reject destroys the intent and hides a live order (double-close on the close
# path). classify_post_result normalizes BOTH adapters' raw responses into
# exactly three states so paper_trader branches on it once — and so the IBKR
# adapter inherits the same contract without adapter-specific engine logic:
#
#   ok               -> a live order exists (order id returned)
#   definitive_reject-> the broker gave a definite "no order placed" (safe to
#                       drop the intent; safe to retry a close)
#   ambiguous        -> no broker response was received; the order MAY be live.
#                       Keep the intent UNBOUND, NEVER blind-resubmit — let
#                       resolve_intents adjudicate against the live broker book.
OK = "ok"
DEFINITIVE_REJECT = "definitive_reject"
AMBIGUOUS = "ambiguous"


def classify_post_result(result) -> dict:
    """Map a raw place_*_order response to {"status", ...}. Never raises."""
    if result is None:
        # place_*_order returns None only when the client was unavailable — no
        # order was ever attempted, so nothing is live: a definitive non-placement.
        return {"status": DEFINITIVE_REJECT, "reason": "no_order_placed", "raw": None}
    if not isinstance(result, dict):
        return {"status": AMBIGUOUS, "reason": "unrecognized_response", "raw": result}
    if result.get("transport_error"):
        # POST never got a broker response — the order may have landed.
        return {"status": AMBIGUOUS,
                "reason": result.get("error") or "transport_error", "raw": result}
    order = result.get("order") or {}
    if order.get("status") == "rejected":
        # broker/adapter classified it rejected — definitive, no live order.
        return {"status": DEFINITIVE_REJECT,
                "reason": result.get("reason") or result.get("error") or "rejected",
                "raw": result}
    if "error" in result:
        # an error envelope WITHOUT a transport flag = the broker answered "no"
        # (HTTP 4xx, duplicate-order block, qualify failure): no live order.
        return {"status": DEFINITIVE_REJECT, "reason": result.get("error"), "raw": result}
    if order.get("id") is not None:
        return {"status": OK, "order_id": order.get("id"),
                "order_status": order.get("status"), "raw": result}
    # a success-shaped envelope with no order id is unexpected — treat it as
    # ambiguous (do not destroy the intent on a shape we don't recognize).
    return {"status": AMBIGUOUS, "reason": "no_order_id_in_response", "raw": result}

_ibkr_singleton: IbkrOrderClient | None = None


def _resolve_broker(config_path: Path | None = None) -> str:
    """Read agent.broker from config, defaulting to tradier_sandbox. Never
    raises — a missing/unreadable config yields the safe default."""
    path = config_path or CONFIG_PATH
    try:
        import yaml
        cfg = yaml.safe_load(Path(path).read_text()) or {}
        broker = (cfg.get("agent", {}) or {}).get("broker")
    except Exception:  # noqa: BLE001
        broker = None
    if broker not in VALID_BROKERS:
        # FAIL CLOSED (2026-09-12): an unreadable config used to resolve to the
        # tradier_sandbox default — a broker that cannot honour IOC — so a torn
        # config would have silently traded v2 as resting DAY orders. Return a
        # non-broker sentinel; get_order_client() refuses it outside pytest.
        import logging
        logging.getLogger("trader").error("broker config unreadable at %s — refusing to pick a default", path)
        return "unresolved"
    return broker


def get_order_client(broker: str | None = None, config_path: Path | None = None):
    """Return the order client for the configured broker.

    broker: explicit override (mainly for tests); when None it is read from
    config.yaml. Unknown values fall back to the tradier_sandbox default.
    """
    # ITEM 8 HERMETICITY (2026-08-05): under pytest, NEVER let the operator's
    # live config choose the broker. A test that wants IBKR must ask for it
    # explicitly (broker=... or an explicit config_path). Leaving
    # broker=ibkr_paper in config.yaml otherwise steers the whole suite at the
    # live gateway — that is how 21 tests failed with real IBKR Error 200s.
    if broker is None and config_path is None:
        import os
        import sys
        if broker == "unresolved" and not ("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules):
            raise ValueError("broker config unreadable — refusing to pick a default broker")
        if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
            if os.environ.get("ALLOW_LIVE_GATEWAY_TESTS") != "1":
                b = DEFAULT_BROKER
                from src.engine import paper_trader
                return paper_trader.PaperOrderClient()
    b = broker if broker in VALID_BROKERS else _resolve_broker(config_path)
    if b == "ibkr_paper":
        global _ibkr_singleton
        if _ibkr_singleton is None:
            # THREAD-CONFINED (2026-08-05, after the first-session hang): the
            # singleton is reused across APScheduler worker threads, and
            # ib_async binds its event loop to the CREATING thread — calling it
            # from another thread deadlocks. ThreadConfinedIbkrClient owns the
            # real client on one dedicated thread and bounds every call with a
            # timeout, so a wedged IBKR call can no longer freeze a cycle.
            from src.data.ibkr_thread import ThreadConfinedIbkrClient
            # Construct through THIS module's IbkrOrderClient name so the
            # existing test patch point (patch.object(oc, "IbkrOrderClient"))
            # still intercepts — the wrapper only changes WHICH THREAD builds it.
            _ibkr_singleton = ThreadConfinedIbkrClient(
                factory=lambda: IbkrOrderClient())
        return _ibkr_singleton
    # DEFAULT: tradier_sandbox. Resolve through paper_trader's namespace so
    # existing tests patching paper_trader.PaperOrderClient keep intercepting.
    from src.engine import paper_trader
    return paper_trader.PaperOrderClient()


def reset_order_client() -> None:
    """Drop the cached IBKR singleton (test hygiene / reconnect)."""
    global _ibkr_singleton
    _ibkr_singleton = None
