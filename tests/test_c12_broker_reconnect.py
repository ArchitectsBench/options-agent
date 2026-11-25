"""C-12.2 (operator "go", 2026-09-28) — the engine re-connects to the broker IN SESSION.

On 09-28 IB Gateway was not logged in at 06:20. The engine connects once at
startup; that attempt failed, nothing re-tried, and SCALP-v3's first session ran
78 cycles with no broker although the gateway came up during the day.

Pinned here:
  * a DISCONNECTED client is reconnected; an UNRESPONSIVE one is left alone
    (a reconnect would queue behind the wedged call)
  * reconnect runs on the worker thread (ib_async's loop is bound to it)
  * after a reconnect the book is reconciled BEFORE anything trades
  * the operator is paged once when the link is found down, once when it returns
  * flag off / sandbox client -> byte-identical old behaviour
  * O-26: a 78-cycle day with the link down for 20 cycles and then restored
Hermetic under the O-20 seal. UNVALIDATED — PAPER ONLY.
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import src.engine.paper_trader as pt
from src.data.ibkr_orders import IbkrOrderClient
from src.data.ibkr_thread import ThreadConfinedIbkrClient

CFG_ON = {"risk": {"broker_reconnect_in_session": True}}


class _Link:
    """Stub with the two methods ensure_broker needs."""
    def __init__(self, states, reconnect_ok=False):
        self.states = list(states); self.reconnect_ok = reconnect_ok; self.reconnect_calls = 0
    def connection_state(self):
        return self.states[0] if len(self.states) == 1 else self.states.pop(0)
    def reconnect(self):
        self.reconnect_calls += 1
        return self.reconnect_ok
    def available(self): return False


class EnsureBroker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); base = Path(self.tmp.name)
        self.pages = []
        self._p = [patch.object(pt, "FILLS_LOG", base / "fills.jsonl"),
                   patch.object(pt, "_notify_drift", side_effect=lambda *a: self.pages.append(a)),
                   patch.object(pt, "reconcile_with_broker", return_value=[{"event": "reconcile_alarm"}]),
                   patch.object(pt, "_BROKER_DOWN_CYCLES", 0)]
        for p in self._p: p.start()

    def tearDown(self):
        for p in reversed(self._p): p.stop()
        self.tmp.cleanup()

    def _events(self):
        p = pt.FILLS_LOG
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []

    def test_connected_is_a_no_op(self):
        c = _Link(["connected"])
        self.assertIsNone(pt.ensure_broker(CFG_ON, c))
        self.assertEqual(c.reconnect_calls, 0)

    def test_disconnected_reconnects_and_reconciles_before_trading(self):
        c = _Link(["disconnected"], reconnect_ok=True)
        with patch.object(pt, "reconcile_with_broker", return_value=[{"event": "reconcile_alarm"}]) as rec:
            ev = pt.ensure_broker(CFG_ON, c)
            rec.assert_called_once()
        self.assertEqual(ev["event"], "broker_reconnected")
        self.assertEqual(ev["reconcile"], ["reconcile_alarm"])
        self.assertEqual([e["event"] for e in self._events()], ["broker_reconnected"])
        self.assertEqual(len(self.pages), 1)

    def test_a_failed_reconnect_pages_once_then_retries_quietly(self):
        c = _Link(["disconnected"], reconnect_ok=False)
        first = pt.ensure_broker(CFG_ON, c)
        self.assertEqual(first["event"], "broker_down_in_session")
        for _ in range(4):
            self.assertIsNone(pt.ensure_broker(CFG_ON, c))
        self.assertEqual(c.reconnect_calls, 5, "it must keep trying every cycle")
        self.assertEqual(len(self.pages), 1, "one page, not one per cycle")
        self.assertEqual(self.pages[0][1], "urgent")

    def test_down_then_up_reports_how_long_it_was_down(self):
        c = _Link(["disconnected"], reconnect_ok=False)
        for _ in range(3):
            pt.ensure_broker(CFG_ON, c)
        c.reconnect_ok = True
        ev = pt.ensure_broker(CFG_ON, c)
        self.assertEqual((ev["event"], ev["down_cycles"]), ("broker_reconnected", 4))

    def test_unresponsive_worker_is_left_alone(self):
        c = _Link(["unresponsive"], reconnect_ok=True)
        self.assertIsNone(pt.ensure_broker(CFG_ON, c))
        self.assertEqual(c.reconnect_calls, 0, "a reconnect would queue behind the wedged call")

    def test_flag_off_is_the_old_behaviour(self):
        c = _Link(["disconnected"], reconnect_ok=True)
        self.assertIsNone(pt.ensure_broker({"risk": {"broker_reconnect_in_session": False}}, c))
        self.assertIsNone(pt.ensure_broker({}, c))
        self.assertEqual(c.reconnect_calls, 0)

    def test_a_client_without_reconnect_is_a_no_op(self):
        class Sandbox:
            def available(self): return False
        self.assertIsNone(pt.ensure_broker(CFG_ON, Sandbox()))

    def test_a_raising_client_never_kills_the_cycle(self):
        class Boom:
            def connection_state(self): raise RuntimeError("socket exploded")
            def reconnect(self): return True
        self.assertIsNone(pt.ensure_broker(CFG_ON, Boom()))

    def test_pending_intents_do_not_block_a_reconnect(self):
        """Refusing to reconnect because work is outstanding is the 09-12 restart
        deadlock again: the book could not clear because the broker was dead."""
        c = _Link(["disconnected"], reconnect_ok=True)
        with patch.object(pt, "_load", return_value=[{"intent": "close", "rec_id": "r", "order_id": 9}]):
            ev = pt.ensure_broker(CFG_ON, c)
        self.assertEqual(ev["event"], "broker_reconnected")


class AdapterReconnect(unittest.TestCase):
    def _client(self):
        return IbkrOrderClient(connect=False)

    def test_reconnect_reuses_connect_and_reports_the_result(self):
        c = self._client()
        def fake_connect():
            c._connected, c._account = True, "DU123"
        with patch.object(c, "_connect", side_effect=fake_connect) as m:
            self.assertTrue(c.reconnect())
            m.assert_called_once()
        self.assertTrue(c.available())

    def test_reconnect_is_rate_limited(self):
        c = self._client()
        with patch.object(c, "_connect") as m:
            self.assertFalse(c.reconnect())
            self.assertFalse(c.reconnect())
            self.assertEqual(m.call_count, 1, "one attempt per cycle, not one per call")

    def test_already_connected_is_a_no_op(self):
        c = self._client(); c._connected, c._account = True, "DU123"
        with patch.object(c, "_connect") as m:
            self.assertTrue(c.reconnect())
            m.assert_not_called()

    def test_a_raising_connect_is_a_failed_reconnect_not_a_crash(self):
        c = self._client()
        with patch.object(c, "_connect", side_effect=OSError("refused")):
            self.assertFalse(c.reconnect())


class _Inner:
    def __init__(self): self.up = False; self.thread = None; self.block = None
    def available(self):
        if self.block is not None:
            self.block.wait(5)
        return self.up
    def reconnect(self):
        self.thread = threading.current_thread().name; self.up = True; return True


class ProxyStates(unittest.TestCase):
    def test_states_and_reconnect_on_the_worker_thread(self):
        inner = _Inner()
        p = ThreadConfinedIbkrClient(factory=lambda: inner, timeouts={"read": 0.5, "place": 0.5, "default": 0.5})
        self.assertEqual(p.connection_state(), "disconnected")
        self.assertTrue(p.reconnect())
        self.assertEqual(inner.thread, "ibkr-client", "reconnect must run on the thread that owns the client")
        self.assertEqual(p.connection_state(), "connected")

    def test_a_wedged_worker_reads_as_unresponsive_not_disconnected(self):
        inner = _Inner(); inner.block = threading.Event()
        p = ThreadConfinedIbkrClient(factory=lambda: inner, timeouts={"read": 0.2, "place": 0.2, "default": 0.2})
        self.assertEqual(p.connection_state(), "unresponsive")
        inner.block.set(); time.sleep(0.05)


class LinkDownThenRestored(unittest.TestCase):
    """O-26: the 09-28 shape, mid-session. Link down at cycle 20, gateway back at
    cycle 40; the client must reconnect itself and trading must resume."""

    def _run(self, **kw):
        from tests.sim.session_sim import SimBroker
        from tests.test_v2_session_sim import run
        b = SimBroker(combo_ioc_rejected=False, **kw)
        s = run(b, fill_mode="marketable_day")
        return s, s.report(), b

    def test_reconnects_once_reconciles_and_resumes(self):
        s, r, b = self._run(disconnect_at_cycle=20, reconnect_at_cycle=40)
        self.assertEqual(r["exceptions"], [])
        self.assertEqual(r["cycles"], 78)
        self.assertEqual(b.reconnects, 1)
        self.assertEqual(r["broker_reconnected"], 1)
        self.assertEqual(r["broker_down_events"], 1, "paged once when found down")
        t20, t40 = s.cycles[20]["ts"], s.cycles[40]["ts"]
        during = [o for o in b.orders.values() if t20 <= o["create_date"] < t40]
        self.assertEqual(during, [], "nothing can be placed while the link is down")
        after = [o for o in b.orders.values() if o["create_date"] >= t40]
        self.assertGreater(len(after), 0, "trading must RESUME after the reconnect")
        self.assertEqual(r["open_positions"], 0, "flat at the close")
        self.assertEqual(r["pending_intents"], 0)

    def test_link_that_never_returns_behaves_as_before(self):
        s, r, b = self._run(disconnect_at_cycle=25)
        self.assertEqual(r["exceptions"], [])
        self.assertEqual(r["cycles"], 78)
        self.assertEqual((b.reconnects, r["broker_reconnected"]), (0, 0))
        self.assertEqual(r["broker_down_events"], 1)
        late = [o for o in b.orders.values() if o["create_date"] >= s.cycles[25]["ts"]]
        self.assertEqual(late, [])

    def test_production_config_has_the_flag_on(self):
        import yaml
        cfg = yaml.safe_load((Path(pt.__file__).resolve().parents[2] / "config" / "config.yaml").read_text())
        self.assertTrue(cfg["risk"]["broker_reconnect_in_session"])
