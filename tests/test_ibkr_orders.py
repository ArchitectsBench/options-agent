"""Unit + integration tests for the IBKR PAPER order adapter and the broker
factory. Everything is mocked — NO test opens a real gateway socket.

Run: .venv/bin/python -m pytest tests/test_ibkr_orders.py -x -q
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import order_client as oc  # noqa: E402
from src.data.ibkr_orders import (IbkrOrderClient, occ_from_fields,  # noqa: E402
                                  parse_occ)
from src.data.tradier_orders import ORDER_TAG, PaperOrderClient  # noqa: E402
from src.engine import paper_trader as pt  # noqa: E402
from tests.mock_ib import (MockContract, MockIB, leg_fill, scen_filled,  # noqa: E402
                           scen_open, scen_partial, scen_rejected)

# a real, well-formed OCC symbol and a matching spread
CALL = "NVDA260717C00120000"       # NVDA 2026-07-17 C 120.0
PUT = "NVDA260717C00125000"        # NVDA 2026-07-17 C 125.0 (short leg of a debit call spread)


def _client(mock=None, **kw):
    mock = mock or MockIB()
    return IbkrOrderClient(ib=mock, **kw), mock


class OccMappingTest(unittest.TestCase):
    def test_parse_and_rebuild_roundtrip(self):
        for occ in ("SMCI260717P00028500", "SPY260630C00450000", CALL):
            root, ymd, right, strike = parse_occ(occ)
            self.assertEqual(occ_from_fields(root, ymd, right, strike), occ)

    def test_parse_fields(self):
        root, ymd, right, strike = parse_occ("SMCI260717P00028500")
        self.assertEqual((root, ymd, right, strike), ("SMCI", "20260717", "P", 28.5))

    def test_bad_symbol_rejected(self):
        with self.assertRaises(ValueError):
            parse_occ("TOOSHORT")


class AvailabilityAndSafetyTest(unittest.TestCase):
    def test_paper_account_is_available(self):
        client, _ = _client(MockIB(accounts=["DU9999999"]))
        self.assertTrue(client.available())

    def test_non_paper_account_refused(self):
        # a live account (no 'D' prefix) must short-circuit availability
        client, _ = _client(MockIB(accounts=["U1234567"]))
        self.assertFalse(client.available())
        self.assertIsNone(client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2))
        # UNKNOWN, not empty (2026-08-22). These asserted [] — which is the
        # claim "the broker authoritatively reports nothing". For a refused
        # LIVE account that is the more dangerous answer of the two: a caller
        # seeing [] could conclude the account holds no orders/positions and
        # reconcile against that phantom-empty view. It must say it does not
        # know. Same fix that stopped ops_checks reporting broker_reachable
        # through three dead sessions.
        self.assertIsNone(client.get_orders())
        self.assertIsNone(client.get_positions())


class SingleLegHappyPathTest(unittest.TestCase):
    def test_place_poll_fill_normalized_shape(self):
        client, mock = _client()
        res = client.place_option_order("NVDA", CALL, "buy_to_open", 2, 1.20)
        self.assertIn("order", res)
        oid = res["order"]["id"]
        self.assertEqual(res["order"]["status"], "open")  # resting

        # broker fills it
        mock.transition(oid, scen_filled(avg_fill_price=1.25, filled=2))
        order = client.get_order(oid)

        self.assertEqual(order["status"], "filled")
        self.assertEqual(order["id"], oid)
        self.assertEqual(order["avg_fill_price"], 1.25)   # UNSIGNED parent
        self.assertNotIn("leg", order)                    # single-leg: no leg array
        self.assertEqual(order["side"], "buy_to_open")
        self.assertEqual(order["symbol"], "NVDA")
        self.assertEqual(order["option_symbol"], CALL)
        self.assertEqual(order["tag"], ORDER_TAG)
        self.assertIsNotNone(order["create_date"])
        self.assertIsNotNone(order["transaction_date"])

        # paper_trader consumes this exact shape:
        detail = pt.order_fill_detail(order)
        self.assertEqual(detail["net_debit"], 1.25)       # buy -> positive debit

    def test_single_leg_sell_to_close_signs_credit(self):
        client, mock = _client()
        res = client.place_option_order("NVDA", CALL, "sell_to_close", 1, 0.90)
        oid = res["order"]["id"]
        mock.transition(oid, scen_filled(avg_fill_price=0.95, filled=1))
        order = client.get_order(oid)
        detail = pt.order_fill_detail(order)
        # sell side -> net_debit negative -> exit credit received
        self.assertEqual(detail["net_debit"], -0.95)


class SpreadHappyPathTest(unittest.TestCase):
    def _legs(self, open_=True):
        if open_:
            return [{"option_symbol": CALL, "side": "buy_to_open", "quantity": 3},
                    {"option_symbol": PUT, "side": "sell_to_open", "quantity": 3}]
        return [{"option_symbol": CALL, "side": "sell_to_close", "quantity": 3},
                {"option_symbol": PUT, "side": "buy_to_close", "quantity": 3}]

    def test_debit_spread_place_poll_fill_legs(self):
        client, mock = _client()
        res = client.place_spread_order("NVDA", self._legs(open_=True), 0.80,
                                        order_type="debit")
        self.assertIn("order", res)
        oid = res["order"]["id"]

        mock.transition(oid, scen_filled(
            avg_fill_price=0.80, filled=3,
            fills=[leg_fill(CALL, shares=3, price=1.20, side="BOT"),
                   leg_fill(PUT, shares=3, price=0.40, side="SLD")]))
        order = client.get_order(oid)

        self.assertEqual(order["status"], "filled")
        self.assertIn("leg", order)
        legs = {l["option_symbol"]: l for l in order["leg"]}
        self.assertEqual(legs[CALL]["side"], "buy_to_open")
        self.assertEqual(legs[CALL]["avg_fill_price"], 1.20)
        self.assertEqual(legs[CALL]["exec_quantity"], 3)
        self.assertEqual(legs[PUT]["avg_fill_price"], 0.40)
        self.assertEqual(legs[PUT]["exec_quantity"], 3)

        detail = pt.order_fill_detail(order)
        self.assertEqual(detail["net_debit"], 0.80)       # 1.20 buy - 0.40 sell

    def test_credit_close_spread_nets_from_legs(self):
        client, mock = _client()
        res = client.place_spread_order("NVDA", self._legs(open_=False), 0.95,
                                        order_type="credit")
        oid = res["order"]["id"]
        mock.transition(oid, scen_filled(
            avg_fill_price=0.95, filled=3,
            fills=[leg_fill(CALL, shares=3, price=1.30, side="SLD"),
                   leg_fill(PUT, shares=3, price=0.35, side="BOT")]))
        order = client.get_order(oid)
        detail = pt.order_fill_detail(order)
        # buy(PUT 0.35) - sell(CALL 1.30) = -0.95 -> exit credit +0.95
        self.assertEqual(detail["net_debit"], -0.95)
        self.assertEqual(round(-detail["net_debit"], 4), 0.95)


class CancelTest(unittest.TestCase):
    def test_cancel_resting_order(self):
        client, mock = _client()
        res = client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        oid = res["order"]["id"]
        out = client.cancel_order(oid)
        self.assertEqual(out["order"]["status"], "canceled")
        self.assertEqual(client.get_order(oid)["status"], "canceled")

    def test_cancel_unknown_order(self):
        client, _ = _client()
        out = client.cancel_order(999999)
        self.assertEqual(out["order"]["status"], "not_found")


class PositionsNormalizationTest(unittest.TestCase):
    def test_long_short_and_nonoption(self):
        mock = MockIB()
        mock.add_option_position("SMCI260717P00028500", position=2, avg_cost=86.0)
        mock.add_option_position(CALL, position=-1, avg_cost=50.0)          # short
        mock.add_raw_position(MockContract(secType="STK", localSymbol="AAPL"), 100, 190.0)
        client, _ = _client(mock)

        positions = client.get_positions()
        by_sym = {p["symbol"]: p for p in positions}
        self.assertEqual(len(positions), 2)                                 # stock skipped
        self.assertEqual(by_sym["SMCI260717P00028500"]["quantity"], 2.0)
        self.assertEqual(by_sym["SMCI260717P00028500"]["cost_basis"], 172.0)  # dollars
        # paper_trader divides cost_basis by 100 to get per-share entry:
        self.assertEqual(round(by_sym["SMCI260717P00028500"]["cost_basis"] / 100, 4), 1.72)
        self.assertEqual(by_sym[CALL]["quantity"], -1.0)
        self.assertEqual(by_sym[CALL]["cost_basis"], -50.0)                  # signed short


class GetOrdersTest(unittest.TestCase):
    def test_lists_session_orders_with_tag(self):
        client, mock = _client()
        r1 = client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        r2 = client.place_option_order("NVDA", PUT, "buy_to_open", 1, 1.1)
        orders = client.get_orders()
        ids = {o["id"] for o in orders}
        self.assertEqual(ids, {r1["order"]["id"], r2["order"]["id"]})
        for o in orders:
            self.assertEqual(o["tag"], ORDER_TAG)
            self.assertIn("status", o)


# ── FAILURE MODE 1: order rejection ──────────────────────────────────────────
class RejectionTest(unittest.TestCase):
    def test_reject_surfaces_error_and_status(self):
        client, mock = _client()
        mock.scenarios = [scen_rejected("Order rejected - reason: invalid contract")]
        with self.assertLogs("trader", level="WARNING") as cm:
            res = client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        self.assertIn("error", res)
        self.assertIn("invalid contract", res["error"])
        oid = res["order"]["id"]
        order = client.get_order(oid)
        self.assertEqual(order["status"], "rejected")
        self.assertIn("invalid contract", order["reason"])
        self.assertTrue(any("REJECTED" in m for m in cm.output))


# ── FAILURE MODE 2: partial fill ─────────────────────────────────────────────
class PartialFillTest(unittest.TestCase):
    def test_partial_status_and_leg_exec_quantity(self):
        client, mock = _client()
        legs = [{"option_symbol": CALL, "side": "buy_to_open", "quantity": 2},
                {"option_symbol": PUT, "side": "sell_to_open", "quantity": 2}]
        res = client.place_spread_order("NVDA", legs, 0.80, order_type="debit")
        oid = res["order"]["id"]
        # 1 of 2 combos filled
        mock.transition(oid, scen_partial(
            filled=1, remaining=1, avg_fill_price=0.80,
            fills=[leg_fill(CALL, shares=1, price=1.20, side="BOT"),
                   leg_fill(PUT, shares=1, price=0.40, side="SLD")]))
        with self.assertLogs("trader", level="WARNING") as cm:
            order = client.get_order(oid)
        self.assertEqual(order["status"], "partially_filled")
        legs_by = {l["option_symbol"]: l for l in order["leg"]}
        self.assertEqual(legs_by[CALL]["exec_quantity"], 1)   # partial reflected
        self.assertEqual(legs_by[PUT]["exec_quantity"], 1)
        self.assertTrue(any("PARTIAL FILL" in m for m in cm.output))

    def test_single_leg_partial(self):
        client, mock = _client()
        res = client.place_option_order("NVDA", CALL, "buy_to_open", 3, 1.2)
        oid = res["order"]["id"]
        mock.transition(oid, scen_partial(filled=1, remaining=2, avg_fill_price=1.2))
        order = client.get_order(oid)
        self.assertEqual(order["status"], "partially_filled")


# ── FAILURE MODE 3: buying-power limit ───────────────────────────────────────
class BuyingPowerRejectTest(unittest.TestCase):
    def test_bp_reject_names_buying_power(self):
        client, mock = _client()
        mock.scenarios = [scen_rejected(
            "Order rejected - reason: insufficient buying power to complete this order")]
        with self.assertLogs("trader", level="WARNING") as cm:
            res = client.place_option_order("NVDA", CALL, "buy_to_open", 10, 5.0)
        self.assertIn("error", res)
        self.assertIn("buying power", res["error"].lower())
        oid = res["order"]["id"]
        self.assertEqual(client.get_order(oid)["status"], "rejected")
        self.assertTrue(any("BUYING POWER" in m for m in cm.output))


# ── FAILURE MODE 4: duplicate-order protection ───────────────────────────────
class DuplicateOrderTest(unittest.TestCase):
    def test_identical_order_blocked_within_window(self):
        client, mock = _client()
        r1 = client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        self.assertIn("order", r1)
        with self.assertLogs("trader", level="WARNING") as cm:
            r2 = client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        self.assertEqual(r2.get("error"), "duplicate_order")
        self.assertEqual(r2["duplicate_of"], r1["order"]["id"])
        self.assertTrue(any("DUPLICATE ORDER" in m for m in cm.output))

    def test_different_signature_not_blocked(self):
        client, mock = _client()
        client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        r2 = client.place_option_order("NVDA", CALL, "buy_to_open", 2, 1.2)   # diff qty
        r3 = client.place_option_order("NVDA", PUT, "buy_to_open", 1, 1.2)    # diff symbol
        self.assertIn("order", r2)
        self.assertIn("order", r3)

    def test_reject_is_retryable_not_deduped(self):
        client, mock = _client()
        mock.scenarios = [scen_rejected("Order rejected - reason: transient"),
                          scen_filled(avg_fill_price=1.2, filled=1)]
        first = client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        self.assertIn("error", first)                    # rejected, NOT recorded for dedup
        second = client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        self.assertIn("order", second)                   # retry allowed

    def test_window_expiry_allows_replacement(self):
        client, mock = _client()
        r1 = client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        # simulate the dedup window elapsing
        for k in list(client._recent):
            oid, _ts = client._recent[k]
            client._recent[k] = (oid, client._recent[k][1] - client.dedup_window_s - 1)
        r2 = client.place_option_order("NVDA", CALL, "buy_to_open", 1, 1.2)
        self.assertIn("order", r2)
        self.assertNotEqual(r1["order"]["id"], r2["order"]["id"])


# ── FACTORY ──────────────────────────────────────────────────────────────────
class FactoryTest(unittest.TestCase):
    def tearDown(self):
        oc.reset_order_client()

    def test_default_returns_paper_order_client(self):
        # config.yaml has no agent.broker key -> tradier_sandbox default
        self.assertIsInstance(oc.get_order_client(), PaperOrderClient)
        self.assertIsInstance(oc.get_order_client(broker="tradier_sandbox"), PaperOrderClient)

    def test_ibkr_paper_returns_ibkr_client(self):
        oc.reset_order_client()
        with patch.object(oc, "IbkrOrderClient",
                          lambda: IbkrOrderClient(ib=MockIB())):
            client = oc.get_order_client(broker="ibkr_paper")
        # 2026-08-05: the factory now returns a ThreadConfinedIbkrClient that
        # OWNS an IbkrOrderClient on a dedicated thread (fix for the event-loop
        # affinity hang). Assert the wrapped client and the behaviour, not the
        # wrapper type.
        from src.data.ibkr_thread import ThreadConfinedIbkrClient
        self.assertIsInstance(client, ThreadConfinedIbkrClient)
        self.assertIsInstance(client._inner, IbkrOrderClient)
        self.assertTrue(client.available())

    def test_ibkr_singleton_reused(self):
        oc.reset_order_client()
        with patch.object(oc, "IbkrOrderClient",
                          lambda: IbkrOrderClient(ib=MockIB())):
            a = oc.get_order_client(broker="ibkr_paper")
            b = oc.get_order_client(broker="ibkr_paper")
        self.assertIs(a, b)

    def test_unknown_broker_falls_back_to_default(self):
        # ITEM 8 hermeticity: pin the config explicitly. This assertion is about
        # the UNKNOWN-BROKER fallback, not about whatever the operator happens to
        # have in config.yaml — reading live config made it flip to IBKR when the
        # engine was set to ibkr_paper.
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as _td:
            _cfg = _P(_td) / "config.yaml"
            _cfg.write_text("agent:\n  broker: tradier_sandbox\n")
            self.assertIsInstance(
                oc.get_order_client(broker="nonsense", config_path=_cfg),
                PaperOrderClient)

    def test_paper_trader_patch_still_intercepts(self):
        # regression guard: existing engine tests patch paper_trader.PaperOrderClient;
        # the tradier default branch must still resolve through that namespace.
        sentinel = object()
        with patch.object(pt, "PaperOrderClient", return_value=sentinel):
            self.assertIs(oc.get_order_client(), sentinel)


if __name__ == "__main__":
    unittest.main()
