"""Thread-confined IBKR client — the fix for the 2026-08-05 engine hang.

INCIDENT: the first live IBKR submit blocked the engine for ~3 minutes.
`ib_async` binds its asyncio event loop to the thread that CREATES the IB
object. The order client was a module-level singleton, but APScheduler runs
cycles on different worker threads, so a later cycle called a client whose loop
belonged to another thread and deadlocked. (The trend book never hit this: it
creates, uses and destroys its IB object inside one call on one thread, in its
own process.)

THIS MODULE ENFORCES TWO INVARIANTS:

  1. THREAD CONFINEMENT. Exactly one long-lived worker thread creates and owns
     the IbkrOrderClient. Every call from any caller thread is marshalled onto
     that worker. The IB event loop therefore only ever runs on its own thread,
     which is the condition ib_async requires.

  2. NO UNBOUNDED WAIT. Every marshalled call has a wall-clock timeout. A call
     that blocks inside IBKR can no longer freeze the caller (and therefore can
     no longer hold the scheduler's cycle lock). This is the piece the
     cooperative cycle deadline could not provide — it bounds work between
     stages, but cannot interrupt a call already blocked mid-request.

TIMEOUT SEMANTICS map onto the broker contracts already in place, so a timeout
degrades exactly like a transport failure:

  place_*_order -> {"error": ..., "transport_error": True}
                   => classify_post_result() -> AMBIGUOUS -> the intent stays
                      UNBOUND and resolve_intents adjudicates it. A timed-out
                      submit is the textbook ambiguous case: the order may be
                      live at IBKR, so it must never be read as a reject.
  get_orders / get_positions / get_order -> None
                   => "read FAILED, broker view unknown" (R2). Recovery paths
                      abort rather than dropping intents on a phantom empty.
  cancel_order  -> None      (unknown; caller re-GETs)
  available()   -> False     (cannot confirm -> treat as unavailable)

Nothing here weakens the paper-only guard: the wrapped client still refuses any
non-'D' (non-paper) account, and that check runs on the worker thread.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any

logger = logging.getLogger("trader")

# Generous enough for a real placeOrder round trip, short enough that a wedged
# call can never span a 5-minute cycle. Reads are quicker than writes.
DEFAULT_CALL_TIMEOUT_S = 20.0
READ_TIMEOUT_S = 15.0
PLACE_TIMEOUT_S = 30.0

_WRITE_METHODS = ("place_option_order", "place_spread_order")
_READ_METHODS = ("get_orders", "get_positions", "get_order")


class IbkrCallTimeout(TimeoutError):
    """A marshalled IBKR call exceeded its wall-clock budget."""


class _Job:
    __slots__ = ("method", "args", "kwargs", "done", "result", "error")

    def __init__(self, method, args, kwargs):
        self.method, self.args, self.kwargs = method, args, kwargs
        self.done = threading.Event()
        self.result = None
        self.error = None


class ThreadConfinedIbkrClient:
    """Drop-in replacement for IbkrOrderClient that owns its real client on a
    single dedicated thread and bounds every call with a timeout.

    Presents the same 7-method interface paper_trader consumes, so nothing
    upstream changes.
    """

    def __init__(self, factory=None, timeouts: dict | None = None, **client_kwargs):
        # timeouts: {"read":s, "place":s, "default":s} — overridable so tests can
        # exercise the degradation paths without waiting the production budgets.
        t = timeouts or {}
        self._t_read = float(t.get("read", READ_TIMEOUT_S))
        self._t_place = float(t.get("place", PLACE_TIMEOUT_S))
        self._t_default = float(t.get("default", DEFAULT_CALL_TIMEOUT_S))
        self._factory = factory
        self._client_kwargs = client_kwargs
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._init_error: str | None = None
        self._inner = None
        self._thread = threading.Thread(
            target=self._run, name="ibkr-client", daemon=True)
        self._thread.start()
        # Bounded wait for the worker to build its client (connect happens here).
        if not self._ready.wait(timeout=self._t_default + 10):
            self._init_error = "IBKR worker did not become ready in time"
            logger.error("ibkr-thread: %s", self._init_error)

    # ── worker thread ────────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            if self._factory is not None:
                self._inner = self._factory()
            else:
                from src.data.ibkr_orders import IbkrOrderClient
                self._inner = IbkrOrderClient(**self._client_kwargs)
        except Exception as exc:  # noqa: BLE001
            self._init_error = f"{type(exc).__name__}: {exc}"
            logger.error("ibkr-thread: client construction failed: %s", self._init_error)
        finally:
            self._ready.set()

        while True:
            job = self._q.get()
            if job is None:                       # shutdown sentinel
                return
            try:
                if self._inner is None:
                    raise RuntimeError("IBKR client unavailable (init failed)")
                job.result = getattr(self._inner, job.method)(*job.args, **job.kwargs)
            except Exception as exc:              # noqa: BLE001
                job.error = exc
            finally:
                job.done.set()

    # ── marshalling ──────────────────────────────────────────────────────────
    def _call(self, method: str, *args, timeout: float, **kwargs) -> Any:
        """Run `method` on the worker thread; raise IbkrCallTimeout on overrun.

        A timed-out job is ABANDONED, not cancelled — we cannot interrupt a
        blocked socket call. It stays on the worker (which may still be wedged);
        subsequent calls will also time out, and each caller returns promptly
        with the safe degraded value instead of freezing. That is the whole
        point: the engine keeps its cycle even when IBKR does not answer.
        """
        if self._inner is None and self._init_error:
            raise RuntimeError(f"IBKR client unavailable: {self._init_error}")
        job = _Job(method, args, kwargs)
        self._q.put(job)
        if not job.done.wait(timeout=timeout):
            logger.error("ibkr-thread: %s() exceeded %.0fs — abandoning call "
                         "(caller returns a degraded value, engine keeps cycling)",
                         method, timeout)
            raise IbkrCallTimeout(f"{method} exceeded {timeout:.0f}s")
        if job.error is not None:
            raise job.error
        return job.result

    # ── the interface paper_trader consumes ──────────────────────────────────
    def available(self) -> bool:
        try:
            return bool(self._call("available", timeout=self._t_read))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ibkr-thread: available() degraded to False (%s)", exc)
            return False

    def connection_state(self) -> str:
        """'connected' | 'disconnected' | 'unresponsive'.

        available() folds two different failures into False. They need different
        handling: a DISCONNECTED client should be reconnected; an UNRESPONSIVE
        worker (a wedged socket call) must not be — a reconnect would only queue
        behind the stuck job."""
        if self._inner is None:
            return "unresponsive"
        try:
            return "connected" if self._call("available", timeout=self._t_read) else "disconnected"
        except Exception:  # noqa: BLE001
            return "unresponsive"

    def reconnect(self) -> bool:
        """Marshal reconnect() onto the worker thread (ib_async binds its event
        loop to the creating thread — calling from a cycle thread deadlocks)."""
        try:
            return bool(self._call("reconnect", timeout=self._t_default + 25))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ibkr-thread: reconnect failed/timed out (%s)", exc)
            return False

    def place_option_order(self, *a, **k) -> dict | None:
        return self._place("place_option_order", *a, **k)

    def place_spread_order(self, *a, **k) -> dict | None:
        return self._place("place_spread_order", *a, **k)

    def _place(self, method: str, *a, **k) -> dict | None:
        try:
            return self._call(method, *a, timeout=self._t_place, **k)
        except IbkrCallTimeout as exc:
            # AMBIGUOUS, never a reject: the order may be live at IBKR.
            logger.error("ibkr-thread: %s TIMED OUT — reporting AMBIGUOUS so the "
                         "intent stays unbound for resolve_intents", method)
            return {"error": f"call_timeout: {exc}", "transport_error": True}
        except Exception as exc:  # noqa: BLE001
            logger.error("ibkr-thread: %s failed (%s) — AMBIGUOUS", method, exc)
            return {"error": f"call_failed: {exc}", "transport_error": True}

    def get_orders(self):
        return self._read("get_orders")

    def get_positions(self):
        return self._read("get_positions")

    def get_order(self, order_id):
        return self._read("get_order", order_id)

    def _read(self, method: str, *a):
        try:
            return self._call(method, *a, timeout=self._t_read)
        except Exception as exc:  # noqa: BLE001
            # None = read FAILED (broker view unknown). NEVER [] — a phantom
            # empty is what makes recovery drop live intents (R2).
            logger.error("ibkr-thread: %s failed/timed out (%s) — returning None "
                         "(unknown), never a silent empty", method, exc)
            return None

    def cancel_order(self, order_id):
        try:
            return self._call("cancel_order", order_id, timeout=self._t_default)
        except Exception as exc:  # noqa: BLE001
            logger.error("ibkr-thread: cancel_order failed/timed out (%s)", exc)
            return None

    def disconnect(self) -> None:
        try:
            self._call("disconnect", timeout=self._t_read)
        except Exception:  # noqa: BLE001
            pass

    # passthrough for diagnostics (e.g. _account) without marshalling
    def __getattr__(self, name):
        inner = object.__getattribute__(self, "__dict__").get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)
