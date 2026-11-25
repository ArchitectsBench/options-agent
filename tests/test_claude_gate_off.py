"""O-33 (operator, 2026-09-29): "Keep everything except claude veto turning off."

The Claude validator is OFF. Pinned here:
  * llm.claude.enabled: false -> every gated candidate is HELD, before any
    cooldown/follow-on logic (a stored approval inside its cooldown would
    otherwise pass a follow-on with no API call)
  * resolve_validation never constructs an API client when off
  * "off" means HOLD, never local-only approval: the pipeline's submit
    condition (approved only) stays unchanged
  * batch_reviews (the other Anthropic consumer) is dead when the master
    switch is off, whatever its own flag says
  * production config has the switch OFF (and batch_reviews off)
Hermetic under the O-20 seal: the budget file is swapped to tmp.
UNVALIDATED — PAPER ONLY.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import src.llm.claude as claude
from src.llm.claude import BudgetGuard, DISABLED_REASON, check_gate, resolve_validation
from tests.test_veto_persistence import order_would_submit

ROOT = Path(claude.__file__).resolve().parents[2]
CFG_OFF = {"enabled": False, "max_calls_per_day": 20, "ticker_cooldown_minutes": 90}
CFG_ON = {**CFG_OFF, "enabled": True}
SNAPSHOT = {"direction": "long", "spot": 100.0, "atr": 1.0, "regime_bias": "bullish"}


class GateOff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = patch.object(claude, "BUDGET_FILE", Path(self.tmp.name) / "claude_budget.json")
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def test_off_holds_a_fresh_candidate(self):
        r = check_gate("NVDA", "call_debit_spread", SNAPSHOT, CFG_OFF)
        self.assertEqual(r["status"], "held")
        self.assertEqual(r["reason"], DISABLED_REASON)
        self.assertFalse(order_would_submit(r))

    def test_off_beats_a_stored_approval_inside_its_cooldown(self):
        """The follow-on path returns 'approved' from a prior verdict with no
        API call — with the validator off that must be a HOLD too."""
        g = BudgetGuard(20, 90)
        g.record_call("NVDA")
        g.record_verdict("NVDA", "call_debit_spread", "approved", SNAPSHOT, {"approve": True})
        on = check_gate("NVDA", "call_debit_spread", SNAPSHOT, CFG_ON)
        self.assertEqual((on["status"], on.get("follow_on")), ("approved", True),
                         "sanity: with the validator ON this is a follow-on approval")
        off = check_gate("NVDA", "call_debit_spread", SNAPSHOT, CFG_OFF)
        self.assertEqual(off["status"], "held")
        self.assertFalse(order_would_submit(off))

    def test_off_makes_no_api_call_even_with_a_key(self):
        class Boom:
            def __init__(self, *a, **k):
                raise AssertionError("an Anthropic client was constructed with the validator OFF")
        fake = type(sys)("anthropic"); fake.Anthropic = Boom
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-not-real"}), \
             patch.dict(sys.modules, {"anthropic": fake}):
            r = resolve_validation({"ctx": 1}, "NVDA", "call_debit_spread", SNAPSHOT, CFG_OFF)
        self.assertEqual(r["status"], "held")
        self.assertEqual(r["reason"], DISABLED_REASON)
        self.assertEqual(BudgetGuard(20, 90).used, "0/20", "no call recorded against the budget")

    def test_default_is_on_so_older_configs_are_unchanged(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            r = check_gate("NVDA", "call_debit_spread", SNAPSHOT,
                           {"max_calls_per_day": 20, "ticker_cooldown_minutes": 90})
        self.assertEqual(r["reason"], "candidate_held (no ANTHROPIC_API_KEY)",
                         "without the key the gate reaches the old key check, i.e. enabled defaulted to True")


class BatchReviewsOff(unittest.TestCase):
    def _cfg_with(self, master: bool, own: bool) -> dict:
        import src.research.batch_reviews as br
        doc = {"llm": {"claude": {"enabled": master, "batch_reviews": {"enabled": own}}}}
        with patch.object(br.yaml, "safe_load", return_value=doc):
            return br._cfg()

    def test_master_off_kills_batch_reviews_whatever_their_flag_says(self):
        self.assertFalse(self._cfg_with(master=False, own=True)["enabled"])
        self.assertFalse(self._cfg_with(master=False, own=False)["enabled"])

    def test_master_on_leaves_the_own_flag_in_charge(self):
        self.assertTrue(self._cfg_with(master=True, own=True)["enabled"])
        self.assertFalse(self._cfg_with(master=True, own=False)["enabled"])

    def test_disabled_main_never_submits(self):
        import src.research.batch_reviews as br
        with patch.object(br, "_cfg", return_value={"enabled": False}), \
             patch.object(br, "submit", side_effect=AssertionError("submit called")), \
             patch.object(br, "collect", side_effect=AssertionError("collect called")), \
             patch.object(sys, "argv", ["batch_reviews.py", "--wait", "0"]):
            br.main()


class ProductionConfig(unittest.TestCase):
    def test_production_config_has_the_validator_off(self):
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
        c = cfg["llm"]["claude"]
        self.assertIs(c["enabled"], False)
        self.assertIs(c["batch_reviews"]["enabled"], False)

    def test_feed_reports_the_switch(self):
        from src.api.feed import claude_section
        s = claude_section()
        self.assertIn("enabled", s)
        self.assertFalse(s["enabled"])


if __name__ == "__main__":
    unittest.main()
