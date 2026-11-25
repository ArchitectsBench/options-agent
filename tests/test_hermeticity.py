"""ITEM 8 — test hermeticity. The suite must never resolve a live broker.

Origin: leaving `broker: ibkr_paper` in config.yaml made the TEST SUITE resolve
to IBKR and open real gateway sockets — 21 failures with genuine IBKR Error 200s
against live contracts. Tests must depend on their mocks, not on the operator's
live runtime config.
"""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.data.ibkr_orders import IbkrOrderClient


class HermeticityTest(unittest.TestCase):
    def test_client_refuses_live_gateway_under_pytest(self):
        """Default path: no socket, adapter simply unavailable."""
        self.assertNotEqual(os.environ.get("ALLOW_LIVE_GATEWAY_TESTS"), "1",
                            "this test asserts the DEFAULT (opt-out) behaviour")
        c = IbkrOrderClient()                       # would connect in production
        self.assertFalse(c.available(),
                         "under pytest the adapter must not open a real socket")

    def test_suite_config_with_ibkr_paper_does_not_reach_a_gateway(self):
        """THE 21-FAILURE CASE: config says ibkr_paper, no gateway running.
        get_order_client() must still yield something inert, never a live socket."""
        from src.data import order_client as oc
        with TemporaryDirectory() as td:
            cfg = Path(td) / "config.yaml"
            cfg.write_text("agent:\n  broker: ibkr_paper\n")
            self.assertEqual(oc._resolve_broker(cfg), "ibkr_paper")
            oc.reset_order_client()
            try:
                client = oc.get_order_client(config_path=cfg)
                self.assertFalse(client.available(),
                                 "no gateway must be contacted under pytest")
            finally:
                oc.reset_order_client()

    def test_explicit_optin_is_required_and_is_off(self):
        self.assertTrue(IbkrOrderClient._under_pytest(),
                        "pytest detection must work, or the guard is inert")


if __name__ == "__main__":
    unittest.main()
