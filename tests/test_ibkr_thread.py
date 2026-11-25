"""Thread-confined IBKR client — regression cover for the 2026-08-05 hang.

The gap that let the incident through: every prior IBKR test ran the adapter
OUT OF PROCESS or on a single thread. Nothing exercised the pattern the live
engine actually uses — a long-lived singleton called from DIFFERENT APScheduler
worker threads — which is precisely what deadlocked.

These tests pin both invariants:
  1. THREAD CONFINEMENT — all real client work happens on ONE thread, no matter
     how many caller threads there are.
  2. NO UNBOUNDED WAIT — a blocked IBKR call degrades to a bounded, contract-
     correct value instead of freezing the caller.
"""
import threading
import time
import unittest

from src.data.ibkr_thread import (PLACE_TIMEOUT_S, READ_TIMEOUT_S,
                                  ThreadConfinedIbkrClient)


class _FakeInner:
    """Stands in for IbkrOrderClient; records which thread each call ran on."""

    def __init__(self, block: set[str] | None = None):
        self.block = block or set()
        self.threads: list[str] = []
        self.construct_thread = threading.current_thread().name

    def _record(self, name):
        self.threads.append(threading.current_thread().name)
        if name in self.block:
            time.sleep(60)          # simulate a wedged broker call

    def available(self):
        self._record("available"); return True

    def get_orders(self):
        self._record("get_orders"); return [{"id": 1}]

    def get_positions(self):
        self._record("get_positions"); return []

    def get_order(self, oid):
        self._record("get_order"); return {"id": oid}

    def place_spread_order(self, *a, **k):
        self._record("place_spread_order"); return {"order": {"id": 7, "status": "open"}}

    def place_option_order(self, *a, **k):
        self._record("place_option_order"); return {"order": {"id": 8, "status": "open"}}

    def cancel_order(self, oid):
        self._record("cancel_order"); return {"ok": True}

    def disconnect(self):
        pass


class ThreadConfinementTest(unittest.TestCase):
    def test_all_work_runs_on_one_thread_despite_many_callers(self):
        """THE INCIDENT CASE: the live engine reuses one client across
        APScheduler worker threads. Every call must still execute on the single
        owning thread — that is the condition ib_async requires."""
        inner = _FakeInner()
        c = ThreadConfinedIbkrClient(factory=lambda: inner)

        errors = []

        def caller():
            try:
                c.get_orders()
                c.available()
                c.place_spread_order("SPY", [], 1.0, "debit")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=caller, name=f"worker-{i}")
                   for i in range(5)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=30)

        self.assertEqual(errors, [], f"calls from worker threads failed: {errors}")
        self.assertTrue(inner.threads, "no calls recorded")
        self.assertEqual(set(inner.threads), {"ibkr-client"},
                         f"ALL work must run on the single owning thread, saw "
                         f"{set(inner.threads)}")

    def test_client_is_constructed_on_the_worker_thread(self):
        """The IB object must be BORN on the thread that will use it."""
        holder = {}

        def factory():
            holder["thread"] = threading.current_thread().name
            return _FakeInner()

        ThreadConfinedIbkrClient(factory=factory)
        self.assertEqual(holder["thread"], "ibkr-client")


class TimeoutDegradationTest(unittest.TestCase):
    """A wedged call must not freeze the caller, and must degrade onto the
    broker contracts already in place."""

    FAST = {"read": 1.0, "place": 1.0, "default": 1.0}

    def _blocked(self, method):
        inner = _FakeInner(block={method})
        return ThreadConfinedIbkrClient(factory=lambda: inner, timeouts=self.FAST)

    def test_blocked_place_is_AMBIGUOUS_not_reject(self):
        from src.data import order_client as oc
        c = self._blocked("place_spread_order")
        t0 = time.monotonic()
        res = c.place_spread_order("SPY", [], 1.0, "debit")
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 15, "caller must not hang")
        self.assertTrue(res.get("transport_error"),
                        "a timed-out submit MAY be live -> must be ambiguous")
        self.assertEqual(oc.classify_post_result(res)["status"], oc.AMBIGUOUS)
        self.assertNotEqual(oc.classify_post_result(res)["status"],
                            oc.DEFINITIVE_REJECT,
                            "never a reject: that destroys the intent and hides "
                            "a live order (double-close path)")

    def test_blocked_read_returns_None_not_empty(self):
        c = self._blocked("get_orders")
        t0 = time.monotonic()
        got = c.get_orders()
        self.assertLess(time.monotonic() - t0, 15)
        self.assertIsNone(got, "failed read must be None (unknown), never []")

    def test_blocked_available_degrades_to_False(self):
        c = self._blocked("available")
        self.assertFalse(c.available())

    def test_one_wedged_call_does_not_break_the_contract_for_callers(self):
        """Even while the worker is wedged, later callers return promptly with
        a degraded value rather than piling up unbounded."""
        c = self._blocked("get_orders")
        c.get_orders()                       # wedges the worker
        t0 = time.monotonic()
        second = c.get_positions()           # queued behind the wedged call
        self.assertLess(time.monotonic() - t0, 15,
                        "a second caller must also return bounded")
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
