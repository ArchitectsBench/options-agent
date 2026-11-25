"""Persistent-cloud-lock escalation tests.

The nightly cloud leg excludes a Drive-locked path and continues (2026-10-20),
which is right for a TRANSIENT lock but silently unbounded: on 2026-10-21..25
`data/trendbook/state.json` was absent from the offsite copy five nights running
while every receipt read `PARTIAL(1 locked)`. These pin the streak counter that
names such a path.

Every fixture is synthetic text in a tmp dir — no production `.err` file is read
and nothing under logs/ is written (CLAUDE.md destructive-probe rule).

Run: .venv/bin/python -m unittest tests.test_persistent_cloud_locks -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ops_checks as oc  # noqa: E402

STUCK = "data/trendbook/state.json"
# The exact pair of lines the real openrsync run writes (124 B, 10-21..10-25).
REAL_ERR = ("rsync(36993): error: data/trendbook/state.json: mmap: Resource "
            "deadlock avoided\nrsync(36992): error: unexpected end of file\n")


class PersistentCloudLockTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def night(self, date, text=""):
        (self.d / f"backup_cloud-{date}.err").write_text(text)

    def test_the_real_five_night_incident_is_named(self):
        """The 10-21..10-25 reading, verbatim, is what this exists to catch."""
        self.night("2026-10-20")                 # 0 B, the clean night before
        for day in range(21, 26):
            self.night(f"2026-10-{day}", REAL_ERR)
        r = oc.persistent_cloud_locks(self.d)
        self.assertTrue(r["alert"])
        self.assertEqual(len(r["persistent"]), 1)
        hit = r["persistent"][0]
        self.assertEqual(hit["path"], STUCK)
        self.assertEqual(hit["nights"], 5)
        self.assertEqual(hit["since"], "2026-10-21")
        self.assertEqual(hit["latest"], "2026-10-25")

    def test_one_night_blip_does_not_alert(self):
        self.night("2026-10-24")
        self.night("2026-10-25", REAL_ERR)
        r = oc.persistent_cloud_locks(self.d)
        self.assertFalse(r["alert"])
        self.assertEqual(r["persistent"], [])

    def test_two_nights_is_below_the_pre_stated_threshold(self):
        self.night("2026-10-24", REAL_ERR)
        self.night("2026-10-25", REAL_ERR)
        self.assertFalse(oc.persistent_cloud_locks(self.d)["alert"])

    def test_threshold_is_three_nights(self):
        for day in (23, 24, 25):
            self.night(f"2026-10-{day}", REAL_ERR)
        r = oc.persistent_cloud_locks(self.d)
        self.assertTrue(r["alert"])
        self.assertEqual(r["nights_threshold"], 3)
        self.assertEqual(r["persistent"][0]["nights"], 3)

    def test_a_released_lock_ends_the_streak(self):
        """Locked for four nights, then a clean newest night: nothing to chase."""
        for day in (20, 21, 22, 23):
            self.night(f"2026-10-{day}", REAL_ERR)
        self.night("2026-10-24")
        r = oc.persistent_cloud_locks(self.d)
        self.assertFalse(r["alert"])

    def test_streak_is_trailing_not_cumulative(self):
        """An old 3-night run that released must not be re-reported forever."""
        for day in (18, 19, 20):
            self.night(f"2026-10-{day}", REAL_ERR)
        for day in (21, 22, 23):
            self.night(f"2026-10-{day}")
        self.assertFalse(oc.persistent_cloud_locks(self.d)["alert"])

    def test_distinct_paths_are_counted_separately(self):
        other = ("rsync(1): error: data/other/thing.json: mmap: Resource "
                 "deadlock avoided\n")
        for day in (21, 22, 23):
            self.night(f"2026-10-{day}", REAL_ERR + other)
        self.night("2026-10-24", REAL_ERR)       # only the trendbook path today
        r = oc.persistent_cloud_locks(self.d)
        by_path = {p["path"]: p["nights"] for p in r["persistent"]}
        self.assertEqual(by_path, {STUCK: 4})    # other/thing.json streak = 0

    def test_unrelated_rsync_errors_are_not_locks(self):
        for day in (21, 22, 23):
            self.night(f"2026-10-{day}",
                       "rsync(9): error: unexpected end of file\n"
                       "rsync(9): error: some/path: Permission denied\n")
        self.assertFalse(oc.persistent_cloud_locks(self.d)["alert"])

    def test_no_err_files_at_all_is_quiet_not_an_alert(self):
        r = oc.persistent_cloud_locks(self.d)
        self.assertFalse(r["alert"])
        self.assertEqual(r["nights_seen"], 0)

    def test_missing_directory_is_quiet(self):
        r = oc.persistent_cloud_locks(self.d / "nope")
        self.assertFalse(r["alert"])
        self.assertEqual(r["nights_seen"], 0)

    def test_undated_filenames_are_ignored(self):
        (self.d / "backup_cloud-latest.err").write_text(REAL_ERR)
        (self.d / "backup_cloud.err").write_text(REAL_ERR)
        r = oc.persistent_cloud_locks(self.d)
        self.assertEqual(r["nights_seen"], 0)
        self.assertFalse(r["alert"])

    def test_nights_threshold_is_configurable(self):
        for day in (24, 25):
            self.night(f"2026-10-{day}", REAL_ERR)
        self.assertTrue(oc.persistent_cloud_locks(self.d, nights=2)["alert"])

    def test_path_with_spaces_is_captured(self):
        self.night("2026-10-23")
        for day in (24, 25, 26):
            self.night(f"2026-10-{day}",
                       "rsync(7): error: data/My Drive/state.json: mmap: "
                       "Resource deadlock avoided\n")
        r = oc.persistent_cloud_locks(self.d)
        self.assertEqual(r["persistent"][0]["path"], "data/My Drive/state.json")

    def test_reporting_only_no_gate_change(self):
        """The receipt gains a key; `stale` stays the staging-only alarm."""
        for day in (21, 22, 23):
            self.night(f"2026-10-{day}", REAL_ERR)
        r = oc.persistent_cloud_locks(self.d)
        # `offsite_holes` added 2026-11-07 with the sidecar branch; the two
        # pre-existing keys and the `alert` boolean are unchanged in shape.
        self.assertEqual(set(r), {"nights_threshold", "nights_seen",
                                  "persistent", "offsite_holes", "alert"})
        self.assertNotIn("stale", r)


class SidecarBranch(unittest.TestCase):
    """2026-11-07: `backup_research_record.sh` copies each Drive-locked path to
    `<path>.sidecar`, so "locked N nights running" stopped meaning "missing
    offsite" — the first rsync fails every night forever. Semantics agreed
    before this code existed: a streak alerts only if the LATEST night lacks a
    sidecar-ok, and a sidecar FAILED alerts immediately."""

    LOCK = ("rsync(7): error: data/trendbook/state.json: mmap: "
            "Resource deadlock avoided\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def night(self, date, text=""):
        (self.d / f"backup_cloud-{date}.err").write_text(text)

    def streak(self, extra_on_latest=""):
        for day in (21, 22):
            self.night(f"2026-10-{day}", self.LOCK)
        self.night("2026-10-23", self.LOCK + extra_on_latest)
        return oc.persistent_cloud_locks(self.d)

    def test_locked_streak_with_sidecar_ok_is_quiet(self):
        r = self.streak("sidecar: data/trendbook/state.json -> "
                        "data/trendbook/state.json.sidecar ok\n")
        self.assertFalse(r["alert"])
        self.assertEqual(r["offsite_holes"], [])
        self.assertEqual(r["persistent"][0]["nights"], 3)
        self.assertEqual(r["persistent"][0]["sidecar_latest"], "ok")

    def test_dated_fallback_sidecar_name_also_counts_as_ok(self):
        r = self.streak("sidecar: data/trendbook/state.json -> "
                        "data/trendbook/state.json.sidecar-2026-10-23 ok\n")
        self.assertFalse(r["alert"])
        self.assertEqual(r["persistent"][0]["sidecar_latest"], "ok")

    def test_locked_streak_with_sidecar_failed_alerts(self):
        r = self.streak("sidecar: data/trendbook/state.json FAILED\n")
        self.assertTrue(r["alert"])
        self.assertEqual([h["path"] for h in r["offsite_holes"]],
                         ["data/trendbook/state.json"])
        self.assertEqual(r["persistent"][0]["sidecar_latest"], "failed")

    def test_locked_streak_with_absent_sidecar_still_alerts(self):
        """The 14 pre-sidecar tests' semantics must survive untouched."""
        r = self.streak()
        self.assertTrue(r["alert"])
        self.assertEqual(r["offsite_holes"][0]["sidecar_latest"], "absent")

    def test_subthreshold_lock_with_failed_sidecar_alerts_at_once(self):
        """The OR branch: affirmative evidence of a hole does not wait 3 nights."""
        self.night("2026-10-22")
        self.night("2026-10-23",
                   self.LOCK + "sidecar: data/trendbook/state.json FAILED\n")
        r = oc.persistent_cloud_locks(self.d)
        self.assertTrue(r["alert"])
        self.assertEqual(r["persistent"], [])  # streak is 1, below threshold
        self.assertEqual(r["offsite_holes"][0]["nights"], 1)

    def test_subthreshold_lock_with_absent_sidecar_is_quiet(self):
        """Deliberate deviation from the literal PASS wording: an ABSENT sidecar
        line is the normal reading when the stuck path is not in staging, so it
        keeps the pre-stated 3-night threshold rather than alerting on night 1."""
        self.night("2026-10-22")
        self.night("2026-10-23", self.LOCK)
        r = oc.persistent_cloud_locks(self.d)
        self.assertFalse(r["alert"])
        self.assertEqual(r["offsite_holes"], [])

    def test_only_the_latest_night_sidecar_decides(self):
        """ok on the latest night is quiet even if an earlier night FAILED."""
        self.night("2026-10-21", self.LOCK)
        self.night("2026-10-22",
                   self.LOCK + "sidecar: data/trendbook/state.json FAILED\n")
        self.night("2026-10-23",
                   self.LOCK + "sidecar: data/trendbook/state.json -> "
                   "data/trendbook/state.json.sidecar ok\n")
        r = oc.persistent_cloud_locks(self.d)
        self.assertFalse(r["alert"])
        self.assertEqual(r["persistent"][0]["nights"], 3)

    def test_sidecar_for_a_different_path_does_not_cover_the_locked_one(self):
        r = self.streak("sidecar: data/other/thing.json -> "
                        "data/other/thing.json.sidecar ok\n")
        self.assertTrue(r["alert"])
        self.assertEqual(r["offsite_holes"][0]["path"],
                         "data/trendbook/state.json")


if __name__ == "__main__":
    unittest.main()
