"""The hourly travel-catch-up sweep must not die on a non-replayable RVOL-RANK
session. 2026-09-08..09-12: the Labor Day shadow file returned exit 1 from the
replay, `set -e` aborted the sweep at that line every hour, and the buy-zone
radar (last line of the sweep) never ran — its state froze at 09-07 23:42."""
import re, subprocess, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / "scripts" / "travel_catchup.sh"


class CatchupSweepSurvivesANonReplayableSession(unittest.TestCase):
    def test_replay_line_is_non_fatal_under_set_e(self):
        src = SWEEP.read_text()
        self.assertIn("set -euo pipefail", src)
        line = next(l for l in src.splitlines() if "rvolrank_sleeve.py --date" in l and not l.strip().startswith("#"))
        self.assertRegex(line, r"\|\|\s*echo", "the replay must not abort the sweep (radar runs after it)")

    def test_radar_still_runs_after_a_failing_replay(self):
        # simulate the sweep's control flow: set -e, a failing replay guarded
        # the same way, and a later step that must still execute
        script = "set -euo pipefail\nfalse || echo 'not replayable'\necho RADAR_RAN\n"
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0); self.assertIn("RADAR_RAN", out.stdout)


class PostMortemEnumAcceptsTheEngineStopCode(unittest.TestCase):
    def test_stop_50_in_enum_and_fires_the_stop_rule(self):
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "post_mortem.yaml").read_text())
        self.assertIn("stop_50", cfg["exit_trigger_enum"])
        eng = (ROOT / "scripts" / "post_mortem_engine.py").read_text()
        self.assertRegex(eng, r'exit_reason in \("stop", "stop_50"\)')
        # the engine's canonical mapping that produced the code
        import sys; sys.path.insert(0, str(ROOT))
        from src.engine.paper_trader import exit_reason_code
        self.assertEqual(exit_reason_code("stop_loss (value 0.7 vs entry 1.42)"), "stop_50")
