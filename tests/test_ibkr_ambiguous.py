"""R1 at the IBKR adapter — a timed-out/failed PLACE must classify AMBIGUOUS.

The engine-side 3-state contract (order_client.classify_post_result) was proven
for Tradier; this pins the IBKR adapter's half. The danger it guards:

  placeOrder reaches IBKR, the order goes LIVE, then the confirmation is lost
  (socket drop / gateway timeout). If the adapter reports that as a definitive
  reject, the engine destroys the intent -> the live order becomes invisible,
  and on the CLOSE path a blind resubmit opens an unintended OPPOSITE position.

Everything from placeOrder onward is therefore ambiguous-on-failure; a qualify
failure (order never sent) stays definitive. Fully mocked — no gateway socket.
"""
import unittest
from unittest.mock import patch

from src.data import order_client as oc
from src.data.ibkr_orders import IbkrOrderClient
from tests.mock_ib import MockIB

_SYM = "SPY260710C00740000"
_LEGS = [{"option_symbol": "SPY260710C00740000", "side": "buy_to_open", "quantity": 1},
         {"option_symbol": "SPY260710C00745000", "side": "sell_to_open", "quantity": 1}]


class _BoomIB(MockIB):
    """Gateway that accepts the POST then dies before confirming."""
    def __init__(self, where="place"):
        super().__init__()
        self._where = where

    def placeOrder(self, contract, order):
        if self._where == "place":
            raise TimeoutError("socket timeout after transmit")
        return super().placeOrder(contract, order)

    # NOTE: _settle() swallows its own exceptions by design, so confirmation
    # loss is injected at _status_of (inside the guarded region, and it does
    # propagate) — that is the real "order submitted, state unknown" case.


def _client(mock):
    return IbkrOrderClient(ib=mock)


class IbkrAmbiguousPlaceTest(unittest.TestCase):
    def test_single_leg_place_timeout_is_ambiguous(self):
        c = _client(_BoomIB("place"))
        res = c.place_option_order("SPY", _SYM, "buy_to_open", 1, 1.25)
        self.assertTrue(res.get("transport_error"), res)
        got = oc.classify_post_result(res)
        self.assertEqual(got["status"], oc.AMBIGUOUS)
        self.assertNotEqual(got["status"], oc.DEFINITIVE_REJECT)

    def test_single_leg_confirmation_loss_is_ambiguous(self):
        # placeOrder SUCCEEDED (order is live) but reading its state blew up
        c = _client(MockIB())
        with patch.object(IbkrOrderClient, "_status_of",
                          side_effect=ConnectionResetError("gateway dropped")):
            res = c.place_option_order("SPY", _SYM, "buy_to_open", 1, 1.25)
        self.assertTrue(res.get("transport_error"), res)
        self.assertEqual(oc.classify_post_result(res)["status"], oc.AMBIGUOUS)

    def test_spread_place_timeout_is_ambiguous(self):
        c = _client(_BoomIB("place"))
        res = c.place_spread_order("SPY", _LEGS, 1.00, "debit")
        self.assertTrue(res.get("transport_error"), res)
        self.assertEqual(oc.classify_post_result(res)["status"], oc.AMBIGUOUS)

    def test_spread_confirmation_loss_is_ambiguous(self):
        c = _client(MockIB())
        with patch.object(IbkrOrderClient, "_status_of",
                          side_effect=ConnectionResetError("gateway dropped")):
            res = c.place_spread_order("SPY", _LEGS, 1.00, "debit")
        self.assertTrue(res.get("transport_error"), res)
        self.assertEqual(oc.classify_post_result(res)["status"], oc.AMBIGUOUS)

    def test_qualify_failure_stays_definitive(self):
        """An order that was NEVER SENT must remain a definitive reject —
        otherwise every bad symbol would strand an unbound intent forever."""
        c = _client(MockIB())
        with patch.object(IbkrOrderClient, "_qualify_option",
                          side_effect=ValueError("bad symbol")):
            res = c.place_option_order("SPY", _SYM, "buy_to_open", 1, 1.25)
        self.assertFalse(res.get("transport_error"))
        self.assertEqual(oc.classify_post_result(res)["status"], oc.DEFINITIVE_REJECT)

    def test_happy_path_still_ok(self):
        c = _client(MockIB())
        res = c.place_option_order("SPY", _SYM, "buy_to_open", 1, 1.25)
        got = oc.classify_post_result(res)
        self.assertEqual(got["status"], oc.OK)
        self.assertIsNotNone(got["order_id"])


if __name__ == "__main__":
    unittest.main()
