"""Tests for the LIVE (Track B / IBKR paper) integrity invariants INV9-INV12
and the run_live() aggregator added to scripts/learning/integrity.py.

Plain dict fixtures only — NO broker connection anywhere, mirrors the shapes
in scratchpad/IBKR_MIGRATION_CONTRACT.md:
  - engine_positions ~ data/paper_positions.json rows (legs + action) OR a
    pre-flattened {"symbol","quantity"} shape
  - broker_positions ~ PaperOrderClient.get_positions() ({"symbol",
    "quantity" signed, "cost_basis"})
  - fills            ~ logs/observation/fills.jsonl rows (event, rec_id,
    order_id, ...)
  - pending_orders   ~ data/paper_orders.json intents (rec_id, order_id, ...)
  - orders           ~ PaperOrderClient.get_orders() ({"id","status","tag"})
  - broker_executions ~ normalized ib_async execution records ({"order_id"})

Covers, per invariant: one OK case and one ALARM case, plus the manual
-position protection required by the contract (HON/GOOGL/DELL/PLTR must never
trip inv9, matching config.yaml's real agent.adopt_foreign=false default).

Run: .venv/bin/python -m pytest tests/test_integrity_live.py -x -q
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "learning"))
integrity = importlib.import_module("integrity")

# Realistic OCC symbols: ROOT + YYMMDD + C/P + 8-digit strike (15-char suffix).
NVDA_C = "NVDA260717C00130000"
SPY_P = "SPY260717P00450000"
HON_C = "HON260717C00220000"      # user's MANUAL position (foreign, sacred)
XYZ_C = "XYZ260717C00050000"      # generic non-manual foreign/crash-orphan ticker


# ─────────────────────────── INV9: position reconcile ───────────────────────
class TestInv9PositionReconcile(unittest.TestCase):
    def test_ok_when_engine_and_broker_agree(self):
        engine = [{"rec_id": "2026-07-24-001", "ticker": "NVDA",
                   "legs": [{"symbol": NVDA_C, "action": "buy_to_open"}], "quantity": 5}]
        broker = [{"symbol": NVDA_C, "quantity": 5.0, "cost_basis": 650.0}]
        r = integrity.inv9_position_reconcile(engine, broker)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["severity"], "critical")
        self.assertEqual(r["detail"]["n_alarms"], 0)

    def test_alarm_on_quantity_mismatch(self):
        engine = [{"rec_id": "2026-07-24-001", "ticker": "NVDA",
                   "legs": [{"symbol": NVDA_C, "action": "buy_to_open"}], "quantity": 5}]
        broker = [{"symbol": NVDA_C, "quantity": 3.0, "cost_basis": 390.0}]  # partial-fill drift
        r = integrity.inv9_position_reconcile(engine, broker)
        self.assertEqual(r["status"], "ALARM")
        self.assertEqual(r["detail"]["n_qty_mismatch"], 1)
        self.assertEqual(r["detail"]["qty_mismatch"][0]["symbol"], NVDA_C)

    def test_alarm_on_missing_at_broker(self):
        """L20 case: engine's book claims a leg the broker no longer has."""
        engine = [{"rec_id": "2026-07-24-001", "ticker": "NVDA",
                   "legs": [{"symbol": NVDA_C, "action": "buy_to_open"}], "quantity": 5}]
        broker = []
        r = integrity.inv9_position_reconcile(engine, broker)
        self.assertEqual(r["status"], "ALARM")
        self.assertEqual(r["detail"]["n_missing_at_broker"], 1)
        self.assertEqual(r["detail"]["n_qty_mismatch"], 0)

    def test_signed_short_leg_matches_negative_broker_qty(self):
        engine = [{"rec_id": "2026-07-24-002", "ticker": "SPY",
                   "legs": [{"symbol": SPY_P, "action": "sell_to_open"}], "quantity": 2}]
        broker = [{"symbol": SPY_P, "quantity": -2.0, "cost_basis": -140.0}]
        r = integrity.inv9_position_reconcile(engine, broker)
        self.assertEqual(r["status"], "OK")

    def test_manual_position_never_alarms_with_default_adopt_foreign_false(self):
        """Contract: manual positions are sacred. adopt_foreign=False (the
        real config.yaml default, and inv9's default) must never alarm on the
        user's own HON/GOOGL/DELL/PLTR trades sharing the paper sandbox."""
        engine = []  # engine has no idea HON exists — correct, it's not ours
        broker = [{"symbol": HON_C, "quantity": 10.0, "cost_basis": 2200.0}]
        r = integrity.inv9_position_reconcile(engine, broker)  # adopt_foreign defaults False
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["detail"]["n_ignored_foreign"], 1)
        self.assertEqual(r["detail"]["ignored_foreign"][0]["reason"], "manual_position")

    def test_generic_foreign_position_ignored_when_adopt_foreign_false(self):
        """Even a non-manual (e.g. crash-orphan) foreign broker position must
        not alarm while adopt_foreign=False — that flag intentionally leaves
        it ungoverned (paper_trader.reconcile_with_broker), it is not drift."""
        engine = []
        broker = [{"symbol": XYZ_C, "quantity": 1.0, "cost_basis": 50.0}]
        r = integrity.inv9_position_reconcile(engine, broker, adopt_foreign=False)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["detail"]["ignored_foreign"][0]["reason"], "adopt_foreign_false")

    def test_manual_position_still_ignored_even_if_adopt_foreign_true(self):
        """Defense in depth: even if adopt_foreign were flipped True, the
        named manual tickers must still never alarm."""
        engine = []
        broker = [{"symbol": HON_C, "quantity": 10.0, "cost_basis": 2200.0}]
        r = integrity.inv9_position_reconcile(engine, broker, adopt_foreign=True)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["detail"]["ignored_foreign"][0]["reason"], "manual_position")

    def test_nonmanual_foreign_alarms_when_adopt_foreign_true_and_not_adopted(self):
        """adopt_foreign=True means non-manual foreign positions SHOULD have
        been adopted into the book; if one still shows up un-adopted here,
        that's a genuine reconciliation-adoption gap."""
        engine = []
        broker = [{"symbol": XYZ_C, "quantity": 1.0, "cost_basis": 50.0}]
        r = integrity.inv9_position_reconcile(engine, broker, adopt_foreign=True)
        self.assertEqual(r["status"], "ALARM")
        self.assertEqual(r["detail"]["n_missing_at_engine"], 1)

    def test_flat_preflattened_engine_shape_also_supported(self):
        """engine_positions may also arrive pre-flattened ({'symbol','quantity'
        signed}) rather than the legs shape — both must be handled."""
        engine = [{"symbol": NVDA_C, "quantity": 5.0}]
        broker = [{"symbol": NVDA_C, "quantity": 5.0, "cost_basis": 650.0}]
        r = integrity.inv9_position_reconcile(engine, broker)
        self.assertEqual(r["status"], "OK")

    def test_empty_both_sides_is_ok(self):
        r = integrity.inv9_position_reconcile([], [])
        self.assertEqual(r["status"], "OK")


# ─────────────────────────── INV10: order reject rate ───────────────────────
class TestInv10OrderRejectRate(unittest.TestCase):
    TAG = "UNVALIDATED-PAPER-ONLY"

    def _ours_orders(self, n_total, n_rejected, tagged=True):
        orders = []
        for i in range(n_total):
            status = "rejected" if i < n_rejected else "filled"
            orders.append({"id": i, "status": status, "tag": self.TAG if tagged else None})
        return orders

    def test_ok_at_boundary_threshold(self):
        # 10 orders, 1 rejected -> rate exactly 0.10, threshold default 0.10 -> not > threshold
        orders = self._ours_orders(10, 1)
        fills = [{"order_id": i, "event": "paper_fill", "rec_id": "r"} for i in range(1, 10)]
        r = integrity.inv10_order_reject_rate(fills, orders)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["detail"]["total_attributed"], 10)
        self.assertEqual(r["detail"]["rejected"], 1)

    def test_alarm_critical_when_reject_rate_high(self):
        orders = self._ours_orders(10, 3)  # 30% reject rate
        fills = [{"order_id": i, "event": "paper_fill", "rec_id": "r"} for i in range(3, 10)]
        r = integrity.inv10_order_reject_rate(fills, orders)
        self.assertEqual(r["status"], "ALARM")
        self.assertEqual(r["severity"], "critical")  # 0.30 > 2x default threshold (0.20)

    def test_alarm_warning_when_reject_rate_mildly_elevated(self):
        orders = self._ours_orders(20, 3)  # 15% reject rate: > 0.10, <= 0.20 critical bar
        fills = [{"order_id": i, "event": "paper_fill", "rec_id": "r"} for i in range(3, 20)]
        r = integrity.inv10_order_reject_rate(fills, orders)
        self.assertEqual(r["status"], "ALARM")
        self.assertEqual(r["severity"], "warning")

    def test_untagged_manual_orders_excluded_from_denominator(self):
        """A rejected order from the user's manual trading (untagged, never
        in our fills) must not pollute the engine's own reject-rate metric."""
        ours = self._ours_orders(10, 1)  # 10% — under threshold on its own
        foreign_rejects = [{"id": 100 + i, "status": "rejected", "tag": None} for i in range(5)]
        fills = [{"order_id": i, "event": "paper_fill", "rec_id": "r"} for i in range(1, 10)]
        r = integrity.inv10_order_reject_rate(fills, ours + foreign_rejects)
        self.assertEqual(r["detail"]["total_attributed"], 10)  # foreign orders excluded
        self.assertEqual(r["status"], "OK")

    def test_small_sample_never_alarms_below_min_orders(self):
        orders = self._ours_orders(2, 1)  # 50% reject rate but only 2 orders
        r = integrity.inv10_order_reject_rate([], orders)
        self.assertEqual(r["status"], "OK")

    def test_configurable_threshold_from_cfg_style_kwarg(self):
        orders = self._ours_orders(10, 2)  # 20% reject rate
        r = integrity.inv10_order_reject_rate([], orders, threshold=0.5)
        self.assertEqual(r["status"], "OK")  # generous threshold, not exceeded


# ─────────────────────────── INV11: fill has execution ──────────────────────
class TestInv11FillHasExecution(unittest.TestCase):
    def test_ok_when_every_fill_has_a_matching_execution(self):
        fills = [
            {"event": "paper_fill", "order_id": 111, "rec_id": "2026-07-24-001"},
            {"event": "paper_exit", "order_id": 222, "rec_id": "2026-07-24-001"},
        ]
        broker_executions = [{"order_id": 111}, {"order_id": 222}]
        r = integrity.inv11_fill_has_execution(fills, broker_executions)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["detail"]["n_checked"], 2)

    def test_alarm_on_phantom_fill_with_no_broker_execution(self):
        fills = [
            {"event": "paper_fill", "order_id": 111, "rec_id": "2026-07-24-001"},
            {"event": "paper_exit", "order_id": 222, "rec_id": "2026-07-24-001"},
        ]
        broker_executions = [{"order_id": 111}]  # 222 never actually executed at the broker
        r = integrity.inv11_fill_has_execution(fills, broker_executions)
        self.assertEqual(r["status"], "ALARM")
        self.assertEqual(r["severity"], "critical")
        self.assertEqual(r["detail"]["n_missing"], 1)
        self.assertEqual(r["detail"]["missing"][0]["order_id"], 222)

    def test_non_booked_events_are_not_checked(self):
        fills = [{"event": "entry_expired", "order_id": 999, "rec_id": "2026-07-24-002"}]
        r = integrity.inv11_fill_has_execution(fills, [])
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["detail"]["n_checked"], 0)


# ─────────────────────────── INV12: order/fill has rec_id ───────────────────
class TestInv12OrderHasRecId(unittest.TestCase):
    def test_ok_when_everything_is_stamped(self):
        pending = [{"rec_id": "2026-07-24-001", "order_id": 1, "intent_seq": "a"}]
        fills = [{"event": "paper_fill", "order_id": 1, "rec_id": "2026-07-24-001"}]
        r = integrity.inv12_order_has_rec_id(pending, fills)
        self.assertEqual(r["status"], "OK")

    def test_alarm_on_pending_order_missing_rec_id(self):
        pending = [{"rec_id": None, "order_id": 2, "intent_seq": "b"}]
        r = integrity.inv12_order_has_rec_id(pending, [])
        self.assertEqual(r["status"], "ALARM")
        self.assertEqual(r["detail"]["n_missing"], 1)
        self.assertEqual(r["detail"]["missing"][0]["source"], "pending_order")

    def test_alarm_on_fill_event_missing_rec_id(self):
        fills = [{"event": "paper_fill", "order_id": 3, "rec_id": ""}]
        r = integrity.inv12_order_has_rec_id([], fills)
        self.assertEqual(r["status"], "ALARM")
        self.assertEqual(r["detail"]["missing"][0]["source"], "fill_event")

    def test_orphan_events_are_exempt_by_design(self):
        """reconciled_orphan/reconciled_orphan_order are emitted for
        broker-side state the engine never placed (crash orphans / the user's
        manual trades) and legitimately carry no rec_id — must NOT alarm."""
        fills = [
            {"event": "reconciled_orphan_order", "order_id": 9, "symbol": HON_C,
             "action": "canceled"},
            {"event": "reconciled_orphan", "action": "adopted", "ticker": "HON"},
        ]
        r = integrity.inv12_order_has_rec_id([], fills)
        self.assertEqual(r["status"], "OK")


# ─────────────────────────── run_live aggregator ─────────────────────────────
class TestRunLive(unittest.TestCase):
    def _all_ok_kwargs(self):
        return dict(
            engine_positions=[{"rec_id": "2026-07-24-001", "ticker": "NVDA",
                               "legs": [{"symbol": NVDA_C, "action": "buy_to_open"}], "quantity": 5}],
            broker_positions=[{"symbol": NVDA_C, "quantity": 5.0, "cost_basis": 650.0}],
            fills=[{"event": "paper_fill", "order_id": 111, "rec_id": "2026-07-24-001"}],
            pending_orders=[],
            broker_executions=[{"order_id": 111}],
            orders=[{"id": 111, "status": "filled", "tag": "UNVALIDATED-PAPER-ONLY"}],
            cfg={},
        )

    def test_overall_ok_and_not_critical_when_everything_clean(self):
        out = integrity.run_live(**self._all_ok_kwargs())
        self.assertEqual(out["overall"], "OK")
        self.assertFalse(out["critical"])
        self.assertEqual(len(out["results"]), 4)
        names = [r["invariant"] for r in out["results"]]
        self.assertEqual(names, ["INV9_position_reconcile", "INV10_order_reject_rate",
                                  "INV11_fill_has_execution", "INV12_order_has_rec_id"])

    def test_overall_alarm_and_critical_on_phantom_fill(self):
        kwargs = self._all_ok_kwargs()
        kwargs["broker_executions"] = []  # the one fill now has no matching execution
        out = integrity.run_live(**kwargs)
        self.assertEqual(out["overall"], "ALARM")
        self.assertTrue(out["critical"])
        inv11 = next(r for r in out["results"] if r["invariant"] == "INV11_fill_has_execution")
        self.assertEqual(inv11["status"], "ALARM")

    def test_manual_position_does_not_flip_run_live_to_alarm(self):
        kwargs = self._all_ok_kwargs()
        kwargs["broker_positions"] = kwargs["broker_positions"] + [
            {"symbol": HON_C, "quantity": 10.0, "cost_basis": 2200.0}
        ]
        out = integrity.run_live(**kwargs)
        self.assertEqual(out["overall"], "OK")

    def test_runs_with_empty_cfg_using_builtin_defaults(self):
        """'Add thresholds under cfg integrity with defaults so it runs
        without config changes' — cfg={} must not KeyError anywhere."""
        out = integrity.run_live(**self._all_ok_kwargs())  # cfg={} baked into helper
        self.assertIn(out["overall"], ("OK", "ALARM"))

    def test_run_live_not_folded_into_run_all(self):
        """run_all's signature/behavior must be untouched — offline pipeline
        has no broker state and must not require any of the new inputs."""
        import inspect
        params = set(inspect.signature(integrity.run_all).parameters)
        self.assertNotIn("engine_positions", params)
        self.assertNotIn("broker_positions", params)


if __name__ == "__main__":
    unittest.main()
