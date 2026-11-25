"""Risk kernel tests (Phase L step 1, 2026-07-02).

Covers: position sizing (budget, unaffordable, ceiling, undefined max_loss),
daily-loss circuit breaker (trip, alert-once, quantity-aware P&L), funnel
caps (max open / max per day, TEST exclusion), gate_new_entry decisions, and
quantity flowing through the order lifecycle into pnl_total.

Run: .venv/bin/python -m unittest tests.test_risk_kernel -v
"""

import json
import sys
import tempfile
import unittest
from datetime import date as _date, timedelta as _td

# 2026-08-07: submit_paper_order now REFUSES a lapsed expiry. These fixtures were
# written when 2026-07-10 was in the future; make them roll so the guard under
# test is the expiry rule itself, not the calendar.
_FUTURE_EXP = (_date.today() + _td(days=7)).isoformat()
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.paper_trader as pt
import src.engine.risk as risk

RCFG = {"account_size": 50000, "max_risk_per_trade_pct": 2.0,
        "max_open_recommendations": 3, "max_recommendations_per_day": 6,
        "max_daily_loss_pct": 3.0, "max_contracts": 10}
CFG = {"risk": RCFG}


class TmpRiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._orig_pt = (pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG)
        pt.PENDING_FILE = base / "paper_orders.json"
        pt.POSITIONS_FILE = base / "paper_positions.json"
        pt.FILLS_LOG = base / "fills.jsonl"
        self._orig_risk = (risk.FILLS_LOG, risk.RISK_STATE)
        risk.FILLS_LOG = base / "fills.jsonl"
        risk.RISK_STATE = base / "risk_state.json"

    def tearDown(self):
        pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG = self._orig_pt
        risk.FILLS_LOG, risk.RISK_STATE = self._orig_risk
        self.tmp.cleanup()

    def _write_exit(self, pnl_per_contract, quantity=None, ts=None):
        event = {"ts": ts or datetime.now().isoformat(timespec="seconds"),
                 "event": "paper_exit", "pnl_per_contract": pnl_per_contract}
        if quantity is not None:
            event["quantity"] = quantity
            event["pnl_total"] = round(pnl_per_contract * quantity, 2)
        with open(risk.FILLS_LOG, "a") as fh:
            fh.write(json.dumps(event) + "\n")


class SizingTest(unittest.TestCase):
    def test_normal_sizing(self):
        # $1,000 budget / $360 per contract -> 2 contracts
        out = risk.size_position({"max_loss": 3.6}, RCFG)
        self.assertEqual(out["quantity"], 2)
        self.assertEqual(out["max_loss_per_contract"], 360.0)
        self.assertEqual(out["max_dollar_risk"], 720.0)

    def test_unaffordable_refused(self):
        out = risk.size_position({"max_loss": 12.0}, RCFG)  # $1,200 > $1,000
        self.assertEqual(out["quantity"], 0)

    def test_ceiling_applies(self):
        out = risk.size_position({"max_loss": 0.5}, RCFG)  # $50 -> 20 raw, cap 10
        self.assertEqual(out["quantity"], 10)

    def test_undefined_max_loss_refused(self):
        self.assertEqual(risk.size_position({"max_loss": None}, RCFG)["quantity"], 0)
        self.assertEqual(risk.size_position({"max_loss": -1}, RCFG)["quantity"], 0)


class BreakerTest(TmpRiskTest):
    def test_trips_on_daily_loss_and_alerts_once(self):
        self._write_exit(-800.0)
        self._write_exit(-40.0, quantity=20)  # pnl_total -800 -> day total -1600
        with patch("src.engine.notify.send") as send:
            out1 = risk.circuit_breaker(RCFG)
            out2 = risk.circuit_breaker(RCFG)
        self.assertTrue(out1["tripped"])
        self.assertEqual(out1["realized_today"], -1600.0)
        self.assertEqual(out1["limit"], 1500.0)
        self.assertTrue(out2["tripped"])
        self.assertEqual(send.call_count, 1)  # alert-once per day

    def test_not_tripped_below_limit(self):
        self._write_exit(-1499.0)
        self.assertFalse(risk.circuit_breaker(RCFG)["tripped"])

    def test_quantity_aware_pnl(self):
        self._write_exit(-10.0, quantity=5)   # -50 total, NOT -10
        self.assertEqual(risk.realized_pnl_today(), -50.0)

    def test_older_days_ignored(self):
        self._write_exit(-9999.0, ts="2026-06-30T10:00:00")
        self.assertFalse(risk.circuit_breaker(RCFG)["tripped"])


class GateTest(TmpRiskTest):
    TRADE = {"max_loss": 3.6}

    def test_allows_and_sizes(self):
        with patch.object(risk, "_funnel_exposure", return_value=(0, 0)):
            out = risk.gate_new_entry(self.TRADE, "funnel", CFG)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["quantity"], 2)

    def test_breaker_blocks_both_kinds(self):
        self._write_exit(-1600.0)
        with patch("src.engine.notify.send"):
            for kind in ("funnel", "test"):
                out = risk.gate_new_entry(self.TRADE, kind, CFG)
                self.assertFalse(out["allowed"])
                self.assertIn("breaker", out["reason"])

    def test_max_open_blocks_funnel_only(self):
        with patch.object(risk, "_funnel_exposure", return_value=(3, 0)):
            self.assertFalse(risk.gate_new_entry(self.TRADE, "funnel", CFG)["allowed"])
        out = risk.gate_new_entry(self.TRADE, "test", CFG)  # test path skips caps
        self.assertTrue(out["allowed"])

    def test_max_per_day_blocks(self):
        with patch.object(risk, "_funnel_exposure", return_value=(0, 6)):
            out = risk.gate_new_entry(self.TRADE, "funnel", CFG)
        self.assertFalse(out["allowed"])
        self.assertIn("max_recommendations_per_day", out["reason"])

    def test_funnel_exposure_excludes_test_positions(self):
        pt._save(pt.POSITIONS_FILE, [
            {"rec_id": "20260702-TEST-1-SPY-debit_spread", "legs": []},
            {"rec_id": "2026-07-02-001", "legs": []},
        ])
        pt._save(pt.PENDING_FILE, [
            {"intent": "open", "rec_id": "20260702-TEST-2-QQQ-0dte", "order_id": 1,
             "submitted_at": "2026-07-02T10:00:00"},
        ])
        open_n, _ = risk._funnel_exposure()
        self.assertEqual(open_n, 1)  # only the curated rec counts


class QuantityLifecycleTest(TmpRiskTest):
    """Quantity flows submit -> fill -> position -> close -> pnl_total."""
    LEGS = [{"symbol": "SPY260710C00740000", "action": "buy_to_open"},
            {"symbol": "SPY260710C00747000", "action": "sell_to_open"}]

    def test_quantity_through_round_trip(self):
        # entry submit with qty 2
        client = MagicMock()
        client.available.return_value = True
        client.place_spread_order.return_value = {"order": {"id": 11, "status": "ok"}}
        rec = {"rec_id": "2026-07-02-777", "ticker": "SPY",
               "trade": {"structure": "call_debit_spread", "strategy": "debit_spread",
                         "direction": "long", "net_debit": 3.6, "max_profit": 3.4,
                         "max_loss": 3.6, "expiration": _FUTURE_EXP,
                         "legs": [{**self.LEGS[0], "mid": 5.3}, {**self.LEGS[1], "mid": 1.7}]}}
        with patch.object(pt, "PaperOrderClient", return_value=client):
            pt.submit_paper_order(rec, quantity=2)
        # broker legs carry qty 2
        _, kwargs_or_args = client.place_spread_order.call_args[0][0], client.place_spread_order.call_args[0][1]
        self.assertTrue(all(l["quantity"] == 2 for l in client.place_spread_order.call_args[0][1]))
        self.assertEqual(pt._load(pt.PENDING_FILE)[0]["quantity"], 2)

        # entry fill -> position qty 2
        entry_order = {"id": 11, "status": "filled", "avg_fill_price": 3.6,
                       "create_date": "x", "transaction_date": "x",
                       "leg": [{"option_symbol": self.LEGS[0]["symbol"], "side": "buy_to_open",
                                "avg_fill_price": 5.27, "exec_quantity": 2},
                               {"option_symbol": self.LEGS[1]["symbol"], "side": "sell_to_open",
                                "avg_fill_price": 1.67, "exec_quantity": 2}]}
        client.get_order.return_value = entry_order
        with patch.object(pt, "PaperOrderClient", return_value=client):
            pt.poll_fills()
        pos = pt.load_positions()[0]
        self.assertEqual(pos["quantity"], 2)

        # close submit uses position qty; exit fill books pnl_total = 2x
        client.place_spread_order.return_value = {"order": {"id": 22, "status": "ok"}}
        with patch.object(pt, "PaperOrderClient", return_value=client):
            pt.submit_close_order(pos, 4.43, "profit_target")
        exit_order = {"id": 22, "status": "filled", "avg_fill_price": -4.43,
                      "create_date": "x", "transaction_date": "x",
                      "leg": [{"option_symbol": self.LEGS[0]["symbol"], "side": "sell_to_close",
                               "avg_fill_price": 6.11, "exec_quantity": 2},
                              {"option_symbol": self.LEGS[1]["symbol"], "side": "buy_to_close",
                               "avg_fill_price": 1.68, "exec_quantity": 2}]}
        client.get_order.return_value = exit_order
        with patch.object(pt, "PaperOrderClient", return_value=client):
            pt.poll_fills()
        exit_ev = [json.loads(l) for l in pt.FILLS_LOG.read_text().splitlines()
                   if json.loads(l)["event"] == "paper_exit"][0]
        self.assertEqual(exit_ev["quantity"], 2)
        self.assertEqual(exit_ev["pnl_per_contract"], 83.0)   # (4.43-3.60)*100
        self.assertEqual(exit_ev["pnl_total"], 166.0)          # x2 contracts
        self.assertEqual(exit_ev["round_trip"]["pnl_total"], 166.0)
        # and the breaker sees the quantity-aware number
        self.assertEqual(risk.realized_pnl_today(), 166.0)


if __name__ == "__main__":
    unittest.main()
