"""Veto-persistence safety tests (post-session fix #1, 2026-06-11).

The invariant under test: local confidence >= threshold REQUIRES Claude
approval. A veto blocks the ticker+strategy for the full cooldown; a
cooldown without a usable verdict holds the candidate. Only status
"approved" may produce an order.

Run: .venv/bin/python -m unittest tests.test_veto_persistence -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.llm.claude as claude
from src.llm.claude import BudgetGuard, check_gate, follow_on_ok

CFG = {"max_calls_per_day": 20, "ticker_cooldown_minutes": 90}
SNAPSHOT = {"direction": "short", "spot": 81.95, "atr": 0.80, "regime_bias": "bearish"}


def order_would_submit(result: dict | None) -> bool:
    """Mirror of the pipeline's submit condition: orders flow ONLY on approved."""
    return result is not None and result.get("status") == "approved"


class VetoPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_budget = claude.BUDGET_FILE
        claude.BUDGET_FILE = Path(self.tmp.name) / "claude_budget.json"

    def tearDown(self):
        claude.BUDGET_FILE = self._orig_budget
        self.tmp.cleanup()

    def _guard(self) -> BudgetGuard:
        return BudgetGuard(CFG["max_calls_per_day"], CFG["ticker_cooldown_minutes"])

    # ── THE 6/11 BUG: veto -> candidate during cooldown -> must NOT order ──
    def test_veto_blocks_candidate_for_full_cooldown(self):
        guard = self._guard()
        guard.record_call("NFLX")  # the veto call starts the cooldown
        guard.record_verdict("NFLX", "debit_spread", "vetoed", SNAPSHOT,
                             {"approve": False, "veto_reason": "stale quotes"})

        result = check_gate("NFLX", "debit_spread", SNAPSHOT, CFG)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "veto_active")
        self.assertIn("claude_veto_active", result["reason"])
        self.assertRegex(result["reason"], r"\(\d+m remaining\)")
        self.assertFalse(order_would_submit(result), "VETOED candidate must never order")

    def test_six_repeat_candidates_all_blocked(self):
        """6 NFLX orders slipped through on 6/11 — every repeat must now block."""
        guard = self._guard()
        guard.record_call("NFLX")
        guard.record_verdict("NFLX", "debit_spread", "vetoed", SNAPSHOT, {"approve": False})
        for i in range(6):
            result = check_gate("NFLX", "debit_spread", SNAPSHOT, CFG)
            self.assertEqual(result["status"], "veto_active", f"candidate {i + 1} leaked")
            self.assertFalse(order_would_submit(result))

    # ── 1b: cooldown with NO verdict must hold, never local-only approve ──
    def test_cooldown_without_verdict_holds(self):
        guard = self._guard()
        guard.record_call("QQQ")  # cooldown running, but no verdict stored for this strategy
        result = check_gate("QQQ", "debit_spread", SNAPSHOT, CFG)
        self.assertEqual(result["status"], "held")
        self.assertIn("candidate_held", result["reason"])
        self.assertFalse(order_would_submit(result))

    def test_budget_exhausted_holds(self):
        guard = self._guard()
        guard.state["calls"] = CFG["max_calls_per_day"]
        guard._save()
        result = check_gate("SPY", "debit_spread", SNAPSHOT, CFG)
        self.assertEqual(result["status"], "held")
        self.assertFalse(order_would_submit(result))

    # ── 1c: follow-on approvals — identical structure, conditions intact ──
    def test_follow_on_allowed_when_conditions_intact(self):
        guard = self._guard()
        guard.record_call("JPM")
        guard.record_verdict("JPM", "debit_spread", "approved", SNAPSHOT,
                             {"approve": True, "narrative": "ok"})
        current = {**SNAPSHOT, "spot": SNAPSHOT["spot"] + 0.2}  # drift 0.2 < 0.5*0.8
        result = check_gate("JPM", "debit_spread", current, CFG)
        self.assertEqual(result["status"], "approved")
        self.assertTrue(result.get("follow_on"))
        self.assertTrue(order_would_submit(result))

    def test_follow_on_held_on_price_drift(self):
        guard = self._guard()
        guard.record_call("JPM")
        guard.record_verdict("JPM", "debit_spread", "approved", SNAPSHOT, {"approve": True})
        current = {**SNAPSHOT, "spot": SNAPSHOT["spot"] + 0.5}  # 0.5 >= 0.5*ATR(0.8)=0.4
        result = check_gate("JPM", "debit_spread", current, CFG)
        self.assertEqual(result["status"], "held")
        self.assertFalse(order_would_submit(result))

    def test_follow_on_held_on_direction_or_regime_change(self):
        for changed in ({"direction": "long"}, {"regime_bias": "neutral"}):
            guard = self._guard()
            guard.record_call("JPM")
            guard.record_verdict("JPM", "debit_spread", "approved", SNAPSHOT, {"approve": True})
            result = check_gate("JPM", "debit_spread", {**SNAPSHOT, **changed}, CFG)
            self.assertEqual(result["status"], "held", f"changed={changed}")
            self.assertFalse(order_would_submit(result))
            claude.BUDGET_FILE.unlink()  # reset state between sub-cases

    # ── scoping and expiry of the veto ──
    def test_veto_scoped_to_ticker_and_strategy(self):
        guard = self._guard()
        guard.record_verdict("NFLX", "debit_spread", "vetoed", SNAPSHOT, {"approve": False})
        # different strategy, no ticker cooldown (no call recorded) -> may proceed to Claude
        self.assertIsNone(check_gate("NFLX", "0dte_directional", SNAPSHOT, CFG))
        # different ticker entirely -> may proceed
        self.assertIsNone(check_gate("SPY", "debit_spread", SNAPSHOT, CFG))

    def test_veto_expires_after_cooldown(self):
        guard = self._guard()
        guard.record_verdict("NFLX", "debit_spread", "vetoed", SNAPSHOT, {"approve": False})
        # age the verdict past the cooldown window
        state = json.loads(claude.BUDGET_FILE.read_text())
        state["verdicts"]["NFLX:debit_spread"]["ts"] = "2026-06-11T00:00:00"
        claude.BUDGET_FILE.write_text(json.dumps(state))
        self.assertIsNone(check_gate("NFLX", "debit_spread", SNAPSHOT, CFG))

    def test_follow_on_requires_atr(self):
        ok, why = follow_on_ok(SNAPSHOT, {**SNAPSHOT, "atr": None})
        self.assertFalse(ok)
        self.assertIn("insufficient data", why)


if __name__ == "__main__":
    unittest.main()
