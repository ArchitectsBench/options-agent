"""B4 — read-only rehearsal GUARD BEHAVIOUR.

Standing rule (D1): prove what the guard DOES when tripped, not that a flag or
a wrapper exists. The rehearsal's whole safety claim is "it cannot place an
order"; before this file that claim was only checked at runtime, so a refactor
could have removed it silently (blocker B4 in the verification matrix).
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _reh():
    spec = importlib.util.spec_from_file_location(
        "reh_mod", ROOT / "scripts" / "ibkr_readonly_rehearsal.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Inner:
    """A client that WOULD happily place an order if it were ever reached."""
    def __init__(self):
        self.placed = []
        self.cancelled = []

    def available(self): return True
    def get_orders(self): return [{"id": 1}]
    def get_positions(self): return []
    def place_option_order(self, *a, **k):
        self.placed.append(a); return {"order": {"id": 1, "status": "open"}}
    def place_spread_order(self, *a, **k):
        self.placed.append(a); return {"order": {"id": 2, "status": "open"}}
    def cancel_order(self, oid):
        self.cancelled.append(oid); return {"ok": True}


class WriteGuardBehaviour(unittest.TestCase):
    """GUARD: the rehearsal must not mutate anything at the broker.
    OBLIGATION: every write method RAISES and the underlying client is never
    reached — not 'we simply don't call it'."""

    def setUp(self):
        self.m = _reh()
        self.inner = _Inner()
        self.c = self.m._ReadOnlyClient(self.inner)

    def test_place_option_order_raises_and_never_reaches_the_broker(self):
        with self.assertRaises(self.m.ReadOnlyViolation):
            self.c.place_option_order("SPY", "SPY260807C00770000", "buy_to_open", 1, 0.5)
        self.assertEqual(self.inner.placed, [],
                         "the underlying client must never see a write")

    def test_place_spread_order_raises_and_never_reaches_the_broker(self):
        with self.assertRaises(self.m.ReadOnlyViolation):
            self.c.place_spread_order("SPY", [], 1.0, "debit")
        self.assertEqual(self.inner.placed, [])

    def test_cancel_order_raises_and_never_reaches_the_broker(self):
        with self.assertRaises(self.m.ReadOnlyViolation):
            self.c.cancel_order(123)
        self.assertEqual(self.inner.cancelled, [],
                         "cancelling is a mutation too — a foreign order must "
                         "never be cancelled by a read-only rehearsal")

    def test_reads_still_pass_through(self):
        """OBLIGATION: the guard blocks writes WITHOUT breaking observation."""
        self.assertEqual(self.c.get_orders(), [{"id": 1}])
        self.assertEqual(self.c.get_positions(), [])
        self.assertTrue(self.c.available())


class PollBehaviour(unittest.TestCase):
    """GUARD: a failed read is UNKNOWN, never 'nothing there' (R2)."""

    def setUp(self):
        self.m = _reh()

    def test_none_read_is_recorded_as_error_not_empty(self):
        class _C(_Inner):
            def get_orders(self): return None
        row = self.m.poll_once(self.m._ReadOnlyClient(_C()))
        self.assertEqual(row["orders"]["outcome"], "error_none",
                         "a failed read must be distinguishable from empty")

    def test_empty_read_is_recorded_as_empty(self):
        class _C(_Inner):
            def get_orders(self): return []
        row = self.m.poll_once(self.m._ReadOnlyClient(_C()))
        self.assertEqual(row["orders"]["outcome"], "empty")

    def test_raising_read_is_recorded_not_swallowed(self):
        class _C(_Inner):
            def get_positions(self): raise ConnectionError("gateway dropped")
        row = self.m.poll_once(self.m._ReadOnlyClient(_C()))
        self.assertEqual(row["positions"]["outcome"], "exception")
        self.assertIn("gateway dropped", row["positions"]["error"])


class ReportHonestyBehaviour(unittest.TestCase):
    """GUARD: the EOD report must not let a quiet session read as proof.
    OBLIGATIONS: state zero orders placed, carry the B1 annotation, and say
    explicitly that a clean session does NOT satisfy criterion 5."""

    def setUp(self):
        self.m = _reh()
        self.rows = [{"ts": "2026-08-05T07:00:00", "connected": True,
                      "orders": {"outcome": "empty", "count": 0, "latency_ms": 5},
                      "positions": {"outcome": "empty", "count": 0, "latency_ms": 5}}]

    def test_report_states_zero_orders_placed(self):
        self.assertIn("ORDERS PLACED: 0", self.m.summarize(self.rows))

    def test_report_carries_the_b1_annotation(self):
        r = self.m.summarize(self.rows)
        self.assertIn("PROCESS-LOCAL", r)
        self.assertIn("POSITIONS and EXECUTIONS", r,
                      "the report must name what IS valid criterion-5 evidence")

    def test_clean_session_is_not_claimed_as_criterion_5_satisfied(self):
        self.assertIn("does NOT satisfy criterion 5", self.m.summarize(self.rows))


if __name__ == "__main__":
    unittest.main()
