"""Hermetic tests for scripts/ops/ephemeral_port_check.py.

No network, no netstat/sysctl subprocess, no production file. Every fixture is
synthetic text, and the two anchor cases are the REAL measurements from the
incident this tool exists to catch (2026-10-23: 17,793 pinned sockets against a
16,384-port range) and from the cleared state after the 2026-10-24 reboot
(21 sockets). A tool that scores those two inputs the same way is useless, so
they are asserted explicitly rather than smoke-tested.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops" / "ephemeral_port_check.py"

_spec = importlib.util.spec_from_file_location("ephemeral_port_check", SCRIPT)
epc = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(epc)

SYSCTL_REAL = (
    "net.inet.ip.portrange.first: 49152\n"
    "net.inet.ip.portrange.last: 65535\n"
    "net.inet.tcp.msl: 15000\n"
)


def netstat_text(n_timewait: int, port_start: int = 49152, state: str = "TIME_WAIT") -> str:
    """Synthesize a macOS `netstat -an -p tcp` table with n pinned sockets."""
    head = (
        "Active Internet connections (including servers)\n"
        "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)\n"
        "tcp4       0      0  127.0.0.1.8421         *.*                    LISTEN\n"
        "tcp4       0      0  192.168.1.20.52001     17.253.1.1.443         ESTABLISHED\n"
    )
    rows = []
    for i in range(n_timewait):
        port = port_start + (i % 16384)
        rows.append(
            f"tcp4       0      0  192.168.1.20.{port}     52.84.1.{i % 250}.443"
            f"         {state}"
        )
    return head + "\n".join(rows) + "\n"


class ParseRange(unittest.TestCase):
    def test_reads_real_sysctl(self):
        first, last, msl, from_sysctl = epc.parse_range(SYSCTL_REAL)
        self.assertEqual((first, last, msl), (49152, 65535, 15000))
        self.assertTrue(from_sysctl)

    def test_range_size_is_16384_the_incident_denominator(self):
        first, last, _, _ = epc.parse_range(SYSCTL_REAL)
        self.assertEqual(last - first + 1, 16384)

    def test_falls_back_and_says_so_when_sysctl_empty(self):
        first, last, msl, from_sysctl = epc.parse_range("")
        self.assertEqual((first, last), (epc.FALLBACK_FIRST, epc.FALLBACK_LAST))
        self.assertIsNone(msl)
        self.assertFalse(from_sysctl, "a fallback must never be reported as measured")

    def test_falls_back_on_inverted_range(self):
        txt = "net.inet.ip.portrange.first: 65535\nnet.inet.ip.portrange.last: 49152\n"
        _, _, _, from_sysctl = epc.parse_range(txt)
        self.assertFalse(from_sysctl)

    def test_falls_back_on_garbage_values(self):
        txt = "net.inet.ip.portrange.first: nope\nnet.inet.ip.portrange.last: 65535\n"
        first, last, _, from_sysctl = epc.parse_range(txt)
        self.assertFalse(from_sysctl)
        self.assertEqual((first, last), (epc.FALLBACK_FIRST, epc.FALLBACK_LAST))

    def test_honours_a_widened_range(self):
        """The operator's fix is to widen the range — the denominator must follow it."""
        txt = (
            "net.inet.ip.portrange.first: 16384\n"
            "net.inet.ip.portrange.last: 65535\n"
        )
        first, last, _, from_sysctl = epc.parse_range(txt)
        self.assertTrue(from_sysctl)
        self.assertEqual(last - first + 1, 49152)


class CountPinned(unittest.TestCase):
    def test_counts_time_wait_in_range(self):
        in_range, total = epc.count_pinned(netstat_text(500), 49152, 65535)
        self.assertEqual(in_range, 500)
        self.assertEqual(total, 500)

    def test_ignores_established_and_listen(self):
        """The header rows carry an ESTABLISHED and a LISTEN; neither pins a port."""
        in_range, total = epc.count_pinned(netstat_text(0), 49152, 65535)
        self.assertEqual((in_range, total), (0, 0))

    def test_excludes_pinned_sockets_outside_the_range(self):
        """A TIME_WAIT on a service port is not ephemeral consumption."""
        txt = (
            "tcp4       0      0  127.0.0.1.8421         127.0.0.1.50001        TIME_WAIT\n"
            "tcp4       0      0  192.168.1.20.49200     1.2.3.4.443            TIME_WAIT\n"
        )
        in_range, total = epc.count_pinned(txt, 49152, 65535)
        self.assertEqual(in_range, 1, "only the 49200 socket is in range")
        self.assertEqual(total, 2, "but both are reported in the total")

    def test_counts_last_ack_and_fin_wait_too(self):
        """The incident had 277 LAST_ACK + 170 FIN_WAIT_1 on top of TIME_WAIT."""
        txt = netstat_text(10, state="LAST_ACK") + netstat_text(5, state="FIN_WAIT_1")
        in_range, _ = epc.count_pinned(txt, 49152, 65535)
        self.assertEqual(in_range, 15)

    def test_ipv6_rows_do_not_crash_the_parser(self):
        txt = (
            "tcp6       0      0  fe80::1%lo0.49300      fe80::2.443            TIME_WAIT\n"
        )
        in_range, total = epc.count_pinned(txt, 49152, 65535)
        self.assertEqual(total, 1)
        self.assertEqual(in_range, 1)


class Measure(unittest.TestCase):
    def test_the_2026_10_23_incident_breaches(self):
        """17,793 pinned against 16,384 ports = 108% — the fault, caught."""
        r = epc.measure(netstat_text(17793), SYSCTL_REAL, 80.0)
        self.assertEqual(r["pinned_in_range"], 17793)
        self.assertEqual(r["range_size"], 16384)
        self.assertGreater(r["pct_consumed"], 100.0)
        self.assertTrue(r["breach"])

    def test_the_2026_10_24_post_reboot_state_is_clean(self):
        """21 pinned sockets = 0.13%; the cleared host must not alert."""
        r = epc.measure(netstat_text(21), SYSCTL_REAL, 80.0)
        self.assertEqual(r["pinned_in_range"], 21)
        self.assertLess(r["pct_consumed"], 1.0)
        self.assertFalse(r["breach"])

    def test_flags_with_headroom_left_which_is_the_whole_point(self):
        """At 80% there is still room to act; at 100% the box cannot even page."""
        r = epc.measure(netstat_text(13200), SYSCTL_REAL, 80.0)
        self.assertTrue(r["breach"])
        self.assertLess(r["pct_consumed"], 100.0)

    def test_threshold_is_inclusive_at_the_boundary(self):
        r = epc.measure(netstat_text(13108), SYSCTL_REAL, 80.0)  # 13107.2 = 80%
        self.assertTrue(r["breach"])
        self.assertFalse(epc.measure(netstat_text(13106), SYSCTL_REAL, 80.0)["breach"])

    def test_threshold_is_configurable(self):
        txt = netstat_text(8200)  # ~50%
        self.assertFalse(epc.measure(txt, SYSCTL_REAL, 80.0)["breach"])
        self.assertTrue(epc.measure(txt, SYSCTL_REAL, 40.0)["breach"])

    def test_msl_is_reported_for_the_wedged_table_argument(self):
        """2*MSL=30s expiry + a static table is what proves 'wedged, not flooded'."""
        self.assertEqual(epc.measure(netstat_text(1), SYSCTL_REAL, 80.0)["msl_ms"], 15000)

    def test_breach_summary_names_the_errno_and_disclaims_the_fix(self):
        s = epc.summarize(epc.measure(netstat_text(17793), SYSCTL_REAL, 80.0))
        self.assertTrue(s.startswith("BREACH"))
        self.assertIn("Errno 49", s)
        self.assertIn("reboot", s)

    def test_ok_summary_does_not_say_breach(self):
        s = epc.summarize(epc.measure(netstat_text(21), SYSCTL_REAL, 80.0))
        self.assertTrue(s.startswith("OK"))
        self.assertNotIn("BREACH", s)

    def test_fallback_range_is_disclosed_in_the_summary(self):
        s = epc.summarize(epc.measure(netstat_text(21), "", 80.0))
        self.assertIn("compiled fallback", s)


class CliExitCodes(unittest.TestCase):
    """Exit codes are the deadman's contract — a wrong one disables the layer."""

    def _run(self, n, extra=(), sysctl=SYSCTL_REAL, tmp=None):
        ns = tmp / "netstat.txt"
        ns.write_text(netstat_text(n))
        sc = tmp / "sysctl.txt"
        sc.write_text(sysctl)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--netstat-file", str(ns),
             "--sysctl-file", str(sc), *extra],
            capture_output=True, text=True, timeout=120,
        )

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_host_exits_zero(self):
        p = self._run(21, tmp=self.tmp)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("OK", p.stdout)

    def test_breach_exits_three_not_one(self):
        """3 must be distinct from 1: 'cannot measure' is never 'all clear'."""
        p = self._run(17793, tmp=self.tmp)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("BREACH", p.stdout)

    def test_unmeasurable_netstat_exits_one(self):
        ns = self.tmp / "empty.txt"
        ns.write_text("")
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--netstat-file", str(ns)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(p.returncode, 1)
        self.assertIn("UNMEASURED", p.stderr)

    def test_json_output_is_parseable_and_carries_the_denominator(self):
        p = self._run(17793, extra=("--json",), tmp=self.tmp)
        d = json.loads(p.stdout)
        self.assertEqual(d["range_size"], 16384)
        self.assertEqual(d["pinned_in_range"], 17793)
        self.assertTrue(d["breach"])

    def test_tool_writes_nothing(self):
        """Read-only by construction — the run must not create any file."""
        before = sorted(p.name for p in self.tmp.iterdir())
        self._run(21, tmp=self.tmp)
        after = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(before or after, after)
        self.assertEqual(set(after), {"netstat.txt", "sysctl.txt"})


class LiveReadersAreInjectable(unittest.TestCase):
    """The live path must be stubbable, so no test ever shells out to netstat."""

    def test_read_netstat_uses_the_injected_runner(self):
        seen = []

        def fake(cmd):
            seen.append(cmd)
            return "stub"

        self.assertEqual(epc.read_netstat(fake), "stub")
        self.assertEqual(seen[0][0], "netstat")

    def test_read_sysctl_requests_the_three_keys(self):
        seen = []

        def fake(cmd):
            seen.append(cmd)
            return "stub"

        epc.read_sysctl(fake)
        self.assertIn("net.inet.ip.portrange.first", seen[0])
        self.assertIn("net.inet.ip.portrange.last", seen[0])
        self.assertIn("net.inet.tcp.msl", seen[0])


if __name__ == "__main__":
    unittest.main()
