"""TRACK A STAGE 2, ENCODED: entry and exit rules must not contradict.

Stage 2 requires "entry and exit rules are unambiguous and non-contradictory".
That was checked on paper for candidate strategies but never enforced on the
LIVE engine — and on 2026-08-06 the live rules contradicted directly:

  exit  : close a long when vwap_dist_atr < 0 (short when > 0), no buffer
  entry : no VWAP requirement at all

10 of 21 entries were opened ALREADY INSIDE the exit condition (longs to -2.38
ATR below VWAP). Result: 18 round trips, 0 winners, median hold one cycle,
-$1,651 paid in spread.

THE INVARIANT: for every side, an entry the gate ACCEPTS must survive an
immediate exit evaluation with price unchanged. If opening a position instantly
satisfies its own exit, the ruleset is inconsistent.
"""
import unittest

from datetime import datetime
from zoneinfo import ZoneInfo

from src.engine.exit_manager import DEFAULT_CFG, evaluate_exit

# mid-session ET so the 15:45 flat-time rule is not what fires
MID_SESSION = datetime(2026, 8, 6, 11, 0, tzinfo=ZoneInfo("America/New_York"))
from src.engine.pipeline import _vwap_entry_ok, entry_vwap_margin_atr

MARGIN = entry_vwap_margin_atr()


def _position(direction, entry_price=1.00):
    return {"rec_id": "T", "ticker": "SPY", "direction": direction,
            "entry_price": entry_price, "quantity": 1, "closing": False,
            "structure": "long_call", "strategy": "0dte_directional",
            "legs": [{"symbol": "SPY260810C00770000", "action": "buy_to_open"}],
            "mark_high": entry_price, "mark_low": entry_price}


class RuleConsistencyInvariant(unittest.TestCase):
    """For each side: an accepted entry, evaluated for exit at UNCHANGED price,
    must NOT fire an exit."""

    def _accepted_vwap(self, direction):
        # the least-favourable VWAP distance the gate still accepts
        return MARGIN if direction == "long" else -MARGIN

    def test_accepted_long_does_not_instantly_exit(self):
        vd = self._accepted_vwap("long")
        self.assertTrue(_vwap_entry_ok("SPY", {"vwap_dist_atr": vd}, "SCALP",
                                       "0dte_directional", "long"))
        reason = evaluate_exit(_position("long"), value=1.00,
                               signals={"vwap_dist_atr": vd},
                               cfg=DEFAULT_CFG, now_et=MID_SESSION)
        self.assertIsNone(reason,
                          f"a long accepted at vwap_dist={vd} instantly exits: {reason}")

    def test_accepted_short_does_not_instantly_exit(self):
        vd = self._accepted_vwap("short")
        self.assertTrue(_vwap_entry_ok("SPY", {"vwap_dist_atr": vd}, "SCALP",
                                       "0dte_directional", "short"))
        reason = evaluate_exit(_position("short"), value=1.00,
                               signals={"vwap_dist_atr": vd},
                               cfg=DEFAULT_CFG, now_et=MID_SESSION)
        self.assertIsNone(reason,
                          f"a short accepted at vwap_dist={vd} instantly exits: {reason}")


class TodaysRealEntriesAreRefused(unittest.TestCase):
    """The gate must refuse the actual 08-06 entries that were born invalid —
    and those same entries must FAIL the consistency invariant on the OLD rules
    (no entry gate), which is what made them losers."""

    REAL_BAD = [("long", -2.38), ("long", -2.15), ("long", -1.83),
                ("long", -1.80), ("long", -1.26), ("long", -1.09),
                ("short", 0.82), ("short", 0.39), ("short", 0.13)]

    def test_gate_refuses_every_born_invalid_entry(self):
        for direction, vd in self.REAL_BAD:
            self.assertFalse(
                _vwap_entry_ok("SPY", {"vwap_dist_atr": vd}, "SCALP",
                               "0dte_directional", direction),
                f"{direction} at vwap_dist={vd} must be REFUSED (it opens inside "
                "the exit condition)")

    def test_those_entries_DO_instantly_exit_proving_the_old_rules_contradicted(self):
        """Pre-fix behaviour: with no entry gate these positions fire an exit
        immediately at unchanged price. This is the defect, demonstrated."""
        for direction, vd in self.REAL_BAD:
            reason = evaluate_exit(_position(direction), value=1.00,
                                   signals={"vwap_dist_atr": vd},
                                   cfg=DEFAULT_CFG, now_et=MID_SESSION)
            self.assertIsNotNone(
                reason,
                f"{direction} at {vd} should instantly exit under the exit rule")
            self.assertIn("invalidation", reason)

    def test_gate_accepts_entries_on_the_correct_side(self):
        for direction, vd in [("long", 2.19), ("long", 1.13), ("long", 0.95),
                              ("short", -5.61), ("short", -4.77), ("short", -0.50)]:
            self.assertTrue(
                _vwap_entry_ok("SPY", {"vwap_dist_atr": vd}, "SCALP",
                               "0dte_directional", direction),
                f"{direction} at vwap_dist={vd} is on the correct side and "
                "should be allowed")

    def test_unknown_vwap_is_not_blocked(self):
        self.assertTrue(_vwap_entry_ok("SPY", {}, "SCALP", "0dte", "long"))


if __name__ == "__main__":
    unittest.main()
