"""Forward-acceptance evaluator tests (scripts/forward_acceptance_check.py).

Covers: scope filtering (-TEST- fills excluded, non-Claude-approved recs
excluded), profit factor, concentration check (P&L after removing top 2
winners), and the honest zero-qualifying-trades report.

Run: .venv/bin/python -m unittest tests.test_forward_acceptance -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import forward_acceptance_check as fac

FORWARD_ACCEPTANCE_YAML = """\
forward_acceptance:
  scope:
    claude_approved_only: true
    exclude_execution_tests: true
    ledger: logs/observation/fills.jsonl
  min_trades: 100
  min_trading_days: 60
  adverse_regime_required: true
  min_forward_sharpe_ann: 0.8
  min_profit_factor: 1.3
  max_drawdown_pct_of_risk_capital: 10
  per_sleeve_non_negative: true
  no_mid_stream_rule_changes: true
  concentration_check: true
"""

CONFIG_YAML = """\
risk:
  account_size: 50000
  max_risk_per_trade_pct: 2.0
"""


def _rec(rec_id, strategy="debit_spread", status="approved", approve=True):
    return {"rec_id": rec_id, "trade": {"strategy": strategy},
            "validation_chain": {"claude": {
                "status": status,
                "verdict": {"approve": approve} if status == "approved" else None}}}


def _exit(rec_id, ts, pnl, ticker="SPY"):
    return {"ts": ts, "event": "paper_exit", "rec_id": rec_id, "ticker": ticker,
            "pnl_per_contract": pnl, "exit_reason": "target_50",
            "round_trip": {"pnl_per_contract": pnl}}


def _fill(rec_id, ts, ticker="SPY"):
    return {"ts": ts, "event": "paper_fill", "rec_id": rec_id, "ticker": ticker}


class ForwardAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "logs" / "observation").mkdir(parents=True)
        (self.root / "data" / "historical").mkdir(parents=True)
        (self.root / "config" / "forward_acceptance.yaml").write_text(
            FORWARD_ACCEPTANCE_YAML)
        (self.root / "config" / "config.yaml").write_text(CONFIG_YAML)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, fills, recs):
        (self.root / "logs" / "observation" / "fills.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in fills))
        (self.root / "logs" / "recommendations.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in recs))

    # ── scope filtering ─────────────────────────────────────────────────

    def test_scope_excludes_test_fills_and_unapproved_recs(self):
        fills = [
            _fill("2026-07-01-001", "2026-07-01T08:00:00"),
            _exit("2026-07-01-001", "2026-07-01T09:00:00", 20.0),        # in
            _exit("20260701-TEST-9-SPY-debit_spread",
                  "2026-07-01T09:05:00", 99.0),                          # TEST
            _exit("2026-07-01-002", "2026-07-01T10:00:00", 50.0),        # skipped
            _exit("2026-07-01-003", "2026-07-01T11:00:00", 50.0),        # vetoed
            _exit("2026-07-01-004", "2026-07-01T12:00:00", 50.0),        # unknown
        ]
        recs = [
            _rec("2026-07-01-001"),
            _rec("2026-07-01-002", status="skipped", approve=False),
            _rec("2026-07-01-003", status="approved", approve=False),  # veto
        ]
        self._write(fills, recs)
        result = fac.evaluate(self.root)
        scope = result["scope"]
        self.assertEqual(scope["exit_events"], 5)
        self.assertEqual(scope["excluded_execution_test"], 1)
        self.assertEqual(scope["excluded_not_claude_approved"], 3)
        self.assertEqual(scope["qualifying"], 1)
        self.assertEqual([t["rec_id"] for t in result["trades"]],
                         ["2026-07-01-001"])
        self.assertEqual(result["total_pnl"], 20.0)

    # ── zero-trades case ────────────────────────────────────────────────

    def test_zero_qualifying_trades_is_honest_not_an_error(self):
        fills = [_exit("20260701-TEST-1-SPY-debit_spread",
                       "2026-07-01T09:00:00", 42.0)]
        self._write(fills, recs=[])
        result = fac.evaluate(self.root)
        self.assertFalse(result["evaluable"])
        self.assertFalse(result["overall"]["pass"])
        self.assertIn("0 qualifying trades", result["overall"]["summary"])
        self.assertEqual(result["progress"]["trades"]["n"], 0)
        self.assertIsNone(result["progress"]["sharpe_ann"]["met"])
        # renders and json-serializes without raising
        text = fac.render(result)
        self.assertIn("0/100", text)
        json.dumps(result, default=str)

    def test_missing_ledger_reports_gracefully(self):
        (self.root / "logs" / "recommendations.jsonl").write_text("")
        result = fac.evaluate(self.root)
        self.assertFalse(result["scope"]["ledger_exists"])
        self.assertEqual(result["scope"]["qualifying"], 0)
        self.assertFalse(result["evaluable"])

    # ── metric math ─────────────────────────────────────────────────────

    def test_profit_factor(self):
        # gross wins 60, gross losses 30 -> PF 2.0
        self.assertEqual(fac.profit_factor([40.0, 20.0, -10.0, -20.0]), 2.0)
        # no losses -> undefined, reported as None (not inf, not crash)
        self.assertIsNone(fac.profit_factor([10.0, 5.0]))

    def test_profit_factor_end_to_end(self):
        fills, recs = [], []
        for i, pnl in enumerate([40.0, 20.0, -10.0, -20.0], start=1):
            rec_id = f"2026-07-01-{i:03d}"
            recs.append(_rec(rec_id))
            fills.append(_exit(rec_id, f"2026-07-01T{9 + i}:00:00", pnl))
        self._write(fills, recs)
        result = fac.evaluate(self.root)
        pf = result["progress"]["profit_factor"]
        self.assertEqual(pf["value"], 2.0)
        self.assertTrue(pf["met"])  # 2.0 >= 1.3

    def test_concentration_check(self):
        # top-2 winners 50+30 = 80; total 65 -> remainder -15 fails
        self.assertEqual(fac.concentration_pnl([50.0, 30.0, 10.0, -25.0]), -15.0)
        # total 90 -> remainder +10 passes
        self.assertEqual(fac.concentration_pnl([50.0, 30.0, 10.0]), 10.0)
        # fewer than two winners: remove what exists
        self.assertEqual(fac.concentration_pnl([-5.0, 7.0]), -5.0)

    def test_concentration_end_to_end_outlier_edge_fails(self):
        # One giant winner masking a bleed: concentration must flag it.
        pnls = [200.0, 5.0, -30.0, -30.0, -30.0]
        fills, recs = [], []
        for i, pnl in enumerate(pnls, start=1):
            rec_id = f"2026-07-01-{i:03d}"
            recs.append(_rec(rec_id))
            fills.append(_exit(rec_id, f"2026-07-01T{8 + i}:00:00", pnl))
        self._write(fills, recs)
        result = fac.evaluate(self.root)
        conc = result["progress"]["concentration"]
        self.assertEqual(conc["pnl_after_top2_winners_removed"], -90.0)
        self.assertFalse(conc["met"])

    def test_sleeve_totals_use_strategy_from_recommendations(self):
        fills = [_exit("2026-07-01-001", "2026-07-01T09:00:00", 10.0),
                 _exit("2026-07-01-002", "2026-07-01T10:00:00", -4.0)]
        recs = [_rec("2026-07-01-001", strategy="debit_spread"),
                _rec("2026-07-01-002", strategy="premium_selling")]
        self._write(fills, recs)
        result = fac.evaluate(self.root)
        sleeve = result["progress"]["per_sleeve"]
        self.assertEqual(sleeve["totals"],
                         {"debit_spread": 10.0, "premium_selling": -4.0})
        self.assertFalse(sleeve["met"])  # premium_selling sleeve negative


if __name__ == "__main__":
    unittest.main()
