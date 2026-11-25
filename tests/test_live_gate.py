"""Live-mode gate tests (Phase L step 2, 2026-07-02).

The charter invariant under test: live mode is IMPOSSIBLE unless every gate
condition holds — the agent refuses (raises), it does not warn. Plus the
kill switch: engaged => no new orders anywhere, exits never blocked.

Run: .venv/bin/python -m unittest tests.test_live_gate -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.live_gate as lg

OBS_CFG = {"agent": {"mode": "observation"}}
LIVE_CFG = {"agent": {"mode": "live"}}

PASS = (True, "ok")
FAIL = (False, "nope")


def _gate_patches(validated=PASS, forward=PASS, creds=PASS):
    return (patch.object(lg, "_check_validated_strategies", return_value=validated),
            patch.object(lg, "_check_forward_acceptance", return_value=forward),
            patch.object(lg, "_check_live_credentials", return_value=creds))


class TmpSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = lg.KILL_SWITCH_FILE
        lg.KILL_SWITCH_FILE = Path(self.tmp.name) / "KILL_SWITCH"

    def tearDown(self):
        lg.KILL_SWITCH_FILE = self._orig
        self.tmp.cleanup()


class ResolveModeTest(TmpSwitchTest):
    def test_observation_default_no_checks(self):
        out = lg.resolve_mode(OBS_CFG)
        self.assertEqual(out["mode"], "observation")
        out = lg.resolve_mode({})  # missing agent.mode entirely
        self.assertEqual(out["mode"], "observation")

    def test_unknown_mode_refused(self):
        with self.assertRaises(lg.LiveModeRefused):
            lg.resolve_mode({"agent": {"mode": "yolo"}})

    def test_live_refused_without_validated_strategy(self):
        p1, p2, p3 = _gate_patches(validated=FAIL)
        with p1, p2, p3, self.assertRaises(lg.LiveModeRefused) as ctx:
            lg.resolve_mode(LIVE_CFG)
        self.assertIn("validated_strategy", str(ctx.exception))

    def test_live_refused_without_forward_acceptance(self):
        p1, p2, p3 = _gate_patches(forward=FAIL)
        with p1, p2, p3, self.assertRaises(lg.LiveModeRefused) as ctx:
            lg.resolve_mode(LIVE_CFG)
        self.assertIn("forward_acceptance", str(ctx.exception))

    def test_live_refused_without_credentials(self):
        p1, p2, p3 = _gate_patches(creds=FAIL)
        with p1, p2, p3, self.assertRaises(lg.LiveModeRefused) as ctx:
            lg.resolve_mode(LIVE_CFG)
        self.assertIn("live_credentials", str(ctx.exception))

    def test_live_refused_with_kill_switch(self):
        lg.engage_kill_switch("test stop")
        p1, p2, p3 = _gate_patches()
        with p1, p2, p3, self.assertRaises(lg.LiveModeRefused) as ctx:
            lg.resolve_mode(LIVE_CFG)
        self.assertIn("kill_switch", str(ctx.exception))

    def test_live_refused_because_execution_layer_unbuilt(self):
        """Even with every other condition met, live is structurally
        impossible until recommend-and-confirm ships."""
        p1, p2, p3 = _gate_patches()
        with p1, p2, p3, self.assertRaises(lg.LiveModeRefused) as ctx:
            lg.resolve_mode(LIVE_CFG)
        self.assertIn("live_execution_layer", str(ctx.exception))
        self.assertFalse(lg.LIVE_EXECUTION_BUILT)

    def test_live_allowed_only_when_everything_holds(self):
        p1, p2, p3 = _gate_patches()
        with p1, p2, p3, patch.object(lg, "LIVE_EXECUTION_BUILT", True):
            out = lg.resolve_mode(LIVE_CFG)
        self.assertEqual(out["mode"], "live")
        self.assertTrue(all(ok for _, ok, _ in out["checks"]))

    def test_nonstrict_falls_back_to_observation(self):
        """Mid-run config flip: non-strict resolution stays observation with
        the refusal attached instead of raising."""
        p1, p2, p3 = _gate_patches(validated=FAIL)
        with p1, p2, p3:
            out = lg.resolve_mode(LIVE_CFG, strict=False)
        self.assertEqual(out["mode"], "observation")
        self.assertIn("validated_strategy", out["refused"])

    def test_real_checks_fail_closed_today(self):
        """No mocks: against the real repo state (no validated strategies,
        forward not met, no live creds) live MUST refuse."""
        with self.assertRaises(lg.LiveModeRefused):
            lg.resolve_mode(LIVE_CFG)


class KillSwitchTest(TmpSwitchTest):
    def test_engage_status_release(self):
        self.assertIsNone(lg.kill_switch_engaged())
        lg.engage_kill_switch("testing")
        self.assertIn("testing", lg.kill_switch_engaged())
        self.assertTrue(lg.release_kill_switch())
        self.assertIsNone(lg.kill_switch_engaged())
        self.assertFalse(lg.release_kill_switch())  # idempotent

    def test_paper_test_path_blocked(self):
        from src.engine.pipeline import _paper_execution_test
        lg.engage_kill_switch("halt")
        called = []
        _paper_execution_test(
            signals={}, applicability={}, cycle_count=1,
            cfg={"agent": {"paper_test_mode": True}}, record={},
            submit_paper_order=lambda *a, **k: called.append(1),
            has_open_exposure=lambda *a: None)
        self.assertEqual(called, [])  # no order path reached


if __name__ == "__main__":
    unittest.main()
