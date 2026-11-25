"""Hermetic tests for scripts/nasdaq_mirror.py.

No network: the client is a stub. No production file is touched — MIRROR_DIR is
repointed into tmp for every test that writes (CLAUDE.md destructive-probe rule:
a test that proves a writer works writes to a disposable copy, never to the file
whose loss would matter).
"""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import nasdaq_mirror as nm  # noqa: E402


class StubClient:
    """Records every fetch and returns canned pages. Never touches the network."""

    def __init__(self, responses=None, default=None):
        self.responses = responses or {}
        self.default = default or {"columns": [], "rows": []}
        self.calls = []

    def available(self):
        return True

    def fetch(self, datatable, **filters):
        self.calls.append((datatable, filters))
        value = self.responses.get(datatable, self.default)
        return value(filters) if callable(value) else value


class MirrorDirTestCase(unittest.TestCase):
    """Repoints MIRROR_DIR at a tmp dir for the duration of the test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = nm.MIRROR_DIR
        nm.MIRROR_DIR = Path(self._tmp.name) / "nasdaq"

    def tearDown(self):
        nm.MIRROR_DIR = self._saved
        self._tmp.cleanup()


class TableSpecs(unittest.TestCase):
    def test_rtat10_stays_in_the_ndaq_namespace(self):
        # SHARADAR/RTAT10 404s as a namespace error; a "fix" to it would
        # silently mirror nothing. Pin the only correct name.
        self.assertIn("NDAQ/RTAT10", nm.TABLES)
        self.assertNotIn("SHARADAR/RTAT10", nm.TABLES)

    def test_every_table_declares_a_key_and_the_date_column_is_in_no_key(self):
        for name, spec in nm.TABLES.items():
            self.assertTrue(spec["key"], f"{name} has no merge key")
            self.assertIn("date_col", spec)

    def test_tickers_is_the_only_whole_universe_table(self):
        no_date = [n for n, s in nm.TABLES.items() if s["date_col"] is None]
        self.assertEqual(no_date, ["SHARADAR/TICKERS"])

    def test_sf2_filters_on_filingdate_not_date(self):
        # SF2's incremental dimension is the filing date; using "date" would
        # filter on the transaction date and drop late filings.
        self.assertEqual(nm.TABLES["SHARADAR/SF2"]["date_col"], "filingdate")


class Watchlist(unittest.TestCase):
    def test_parses_the_inline_default_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "agent:\n  cycle_minutes: 5\n"
                "watchlist:\n"
                "  default: [AAPL, MSFT, NVDA]\n"
                "  sectors:\n    tech: [AAPL]\n"
                "risk:\n  account_size: 50000\n"
            )
            self.assertEqual(nm.watchlist(path), ["AAPL", "MSFT", "NVDA"])

    def test_dedupes_preserving_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("watchlist:\n  default: [SPY, QQQ, SPY]\n")
            self.assertEqual(nm.watchlist(path), ["SPY", "QQQ"])

    def test_missing_file_is_empty_not_an_exception(self):
        self.assertEqual(nm.watchlist(Path("/nonexistent/config.yaml")), [])

    def test_the_real_config_yields_a_universe(self):
        # Guards against a config reshape silently emptying the mirror.
        self.assertGreaterEqual(len(nm.watchlist()), 5)


class StartDate(unittest.TestCase):
    TODAY = dt.date(2026, 11, 18)

    def test_snapshot_tables_have_no_start_date(self):
        self.assertIsNone(nm.start_date_for([], None, False, self.TODAY))

    def test_first_run_goes_back_the_full_window(self):
        since = nm.start_date_for([], "date", False, self.TODAY)
        self.assertEqual(since, str(self.TODAY - dt.timedelta(days=nm.MIRROR_START_DEFAULT)))

    def test_incremental_run_overlaps_the_tail(self):
        rows = [{"date": "2026-11-10"}, {"date": "2026-11-14"}]
        since = nm.start_date_for(rows, "date", False, self.TODAY)
        # last date 11-14 minus the 7-day overlap
        self.assertEqual(since, "2026-11-07")

    def test_full_flag_ignores_what_is_on_disk(self):
        rows = [{"date": "2026-11-14"}]
        since = nm.start_date_for(rows, "date", True, self.TODAY)
        self.assertEqual(since, str(self.TODAY - dt.timedelta(days=nm.MIRROR_START_DEFAULT)))

    def test_unparseable_stored_date_falls_back_to_the_full_window(self):
        rows = [{"date": "not-a-date"}]
        since = nm.start_date_for(rows, "date", False, self.TODAY)
        self.assertEqual(since, str(self.TODAY - dt.timedelta(days=nm.MIRROR_START_DEFAULT)))


class Merge(unittest.TestCase):
    KEY = ("ticker", "date")

    def test_new_rows_append(self):
        out = nm.merge_rows([{"ticker": "A", "date": "1", "close": 1}],
                            [{"ticker": "A", "date": "2", "close": 2}], self.KEY)
        self.assertEqual(len(out), 2)

    def test_overlap_replaces_rather_than_duplicates(self):
        # This is what makes REFETCH_OVERLAP_DAYS free.
        existing = [{"ticker": "A", "date": "1", "close": 1}]
        fresh = [{"ticker": "A", "date": "1", "close": 99}]
        out = nm.merge_rows(existing, fresh, self.KEY)
        self.assertEqual(out, [{"ticker": "A", "date": "1", "close": 99}])

    def test_rows_differing_only_outside_the_key_still_collapse(self):
        out = nm.merge_rows([{"ticker": "A", "date": "1", "x": 1}],
                            [{"ticker": "A", "date": "1", "x": 2}], self.KEY)
        self.assertEqual(len(out), 1)

    def test_missing_key_field_does_not_raise(self):
        out = nm.merge_rows([], [{"ticker": "A"}], self.KEY)
        self.assertEqual(len(out), 1)


class ReadWriteRoundTrip(MirrorDirTestCase):
    def test_write_then_read_round_trips(self):
        rows = [{"ticker": "AAPL", "date": "2026-11-18", "close": 1.5}]
        nm.write_mirror("SHARADAR/SEP", rows)
        self.assertEqual(nm.read_mirror("SHARADAR/SEP"), rows)

    def test_mirror_path_slugs_the_namespace_separator(self):
        self.assertTrue(str(nm.mirror_path("NDAQ/RTAT10")).endswith("ndaq_rtat10.jsonl"))

    def test_absent_mirror_reads_as_empty(self):
        self.assertEqual(nm.read_mirror("SHARADAR/SEP"), [])

    def test_a_torn_final_line_is_skipped_not_fatal(self):
        # A run killed mid-write (or a cap-death) can leave a partial line.
        path = nm.mirror_path("SHARADAR/SEP")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"ticker": "A", "date": "1"}\n{"ticker": "B", "da\n')
        self.assertEqual(nm.read_mirror("SHARADAR/SEP"), [{"ticker": "A", "date": "1"}])

    def test_write_leaves_no_temp_file_behind(self):
        nm.write_mirror("SHARADAR/SEP", [{"ticker": "A", "date": "1"}])
        self.assertEqual(list(nm.MIRROR_DIR.glob("*.tmp")), [])


class MirrorTable(MirrorDirTestCase):
    TODAY = dt.date(2026, 11, 18)

    def _sep(self, rows):
        return {"columns": ["ticker", "date", "close"], "rows": rows}

    def test_fetches_once_per_ticker_and_writes_the_merged_file(self):
        client = StubClient({"SHARADAR/SEP": self._sep([["AAPL", "2026-11-18", 1.0]])})
        receipt = nm.mirror_table(client, "SHARADAR/SEP", ["AAPL", "MSFT"],
                                  full=False, today=self.TODAY)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(receipt["rows_fetched"], 2)
        self.assertTrue(receipt["ok"])
        self.assertTrue(nm.mirror_path("SHARADAR/SEP").exists())

    def test_snapshot_table_is_fetched_once_with_no_ticker_filter(self):
        client = StubClient({"SHARADAR/TICKERS":
                             {"columns": ["table", "ticker"], "rows": [["SEP", "AAPL"]]}})
        nm.mirror_table(client, "SHARADAR/TICKERS", ["AAPL", "MSFT"],
                        full=False, today=self.TODAY)
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("ticker", client.calls[0][1])

    def test_the_date_filter_is_a_gte_range_on_the_declared_column(self):
        client = StubClient({"SHARADAR/SF2": {"columns": [], "rows": []}})
        nm.mirror_table(client, "SHARADAR/SF2", ["AAPL"], full=False, today=self.TODAY)
        _, filters = client.calls[0]
        self.assertIn("filingdate", filters)
        self.assertEqual(set(filters["filingdate"]), {"gte"})

    def test_one_ticker_failing_does_not_lose_the_others(self):
        def respond(filters):
            if filters.get("ticker") == "BAD":
                return {"error": "HTTP 403: not entitled", "http_status": 403}
            return {"columns": ["ticker", "date"], "rows": [[filters["ticker"], "2026-11-18"]]}

        client = StubClient({"SHARADAR/SEP": respond})
        receipt = nm.mirror_table(client, "SHARADAR/SEP", ["AAPL", "BAD", "MSFT"],
                                  full=False, today=self.TODAY)
        self.assertFalse(receipt["ok"])
        self.assertEqual(len(receipt["errors"]), 1)
        self.assertEqual(receipt["rows_fetched"], 2)      # the good two survived
        self.assertEqual(receipt["rows_total"], 2)        # and were written

    def test_a_failed_table_never_truncates_an_existing_mirror(self):
        nm.write_mirror("SHARADAR/SEP", [{"ticker": "AAPL", "date": "2026-11-01"}])
        client = StubClient({"SHARADAR/SEP": {"error": "HTTP 500", "http_status": 500}})
        receipt = nm.mirror_table(client, "SHARADAR/SEP", ["AAPL"],
                                  full=False, today=self.TODAY)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["rows_total"], 1)
        self.assertEqual(len(nm.read_mirror("SHARADAR/SEP")), 1)

    def test_a_second_run_over_the_same_rows_adds_nothing(self):
        client = StubClient({"SHARADAR/SEP": self._sep([["AAPL", "2026-11-18", 1.0]])})
        nm.mirror_table(client, "SHARADAR/SEP", ["AAPL"], full=False, today=self.TODAY)
        second = nm.mirror_table(client, "SHARADAR/SEP", ["AAPL"],
                                 full=False, today=self.TODAY)
        self.assertEqual(second["rows_added"], 0)
        self.assertEqual(second["rows_total"], 1)


class MainEntryPoint(MirrorDirTestCase):
    def test_dry_run_makes_no_network_call_and_writes_nothing(self):
        saved = nm.NasdaqDataLink
        nm.NasdaqDataLink = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("--dry-run constructed a client"))
        try:
            self.assertEqual(nm.main(["--dry-run"]), 0)
        finally:
            nm.NasdaqDataLink = saved
        self.assertFalse(nm.MIRROR_DIR.exists())

    def test_a_missing_key_skips_with_rc_1_and_writes_nothing(self):
        saved = nm.NasdaqDataLink
        nm.NasdaqDataLink = lambda *a, **k: type(
            "C", (), {"available": lambda self: False})()
        try:
            self.assertEqual(nm.main([]), 1)
        finally:
            nm.NasdaqDataLink = saved
        self.assertFalse(nm.MIRROR_DIR.exists())

    def test_a_successful_run_writes_a_receipt(self):
        saved_client, saved_wl = nm.NasdaqDataLink, nm.watchlist
        nm.NasdaqDataLink = lambda *a, **k: StubClient(
            default={"columns": ["ticker", "date"], "rows": [["AAPL", "2026-11-18"]]})
        nm.watchlist = lambda *a, **k: ["AAPL"]
        try:
            rc = nm.main(["--table", "SHARADAR/SEP"])
        finally:
            nm.NasdaqDataLink, nm.watchlist = saved_client, saved_wl
        self.assertEqual(rc, 0)
        receipt = json.loads((nm.MIRROR_DIR / "mirror_receipt.json").read_text())
        self.assertEqual(len(receipt["tables"]), 1)
        self.assertEqual(receipt["tables"][0]["table"], "SHARADAR/SEP")

    def test_an_empty_watchlist_refuses_rather_than_mirroring_nothing(self):
        saved_client, saved_wl = nm.NasdaqDataLink, nm.watchlist
        nm.NasdaqDataLink = lambda *a, **k: StubClient()
        nm.watchlist = lambda *a, **k: []
        try:
            self.assertEqual(nm.main([]), 1)
        finally:
            nm.NasdaqDataLink, nm.watchlist = saved_client, saved_wl


if __name__ == "__main__":
    unittest.main()


class EmptyTableReporting(unittest.TestCase):
    """A table that mirrors zero rows must report `empty`, not `ok` — and must
    NOT fail the step. Regression guard for the 2026-11-19/20/23 nights where
    SEP/SF2 mirrored nothing while the nightly step read rc=0 / ok=true.
    Probed 2026-11-23: the subscription serves only a 2018 sample window for
    those tables, so zero rows is correct behaviour, incorrectly reported."""

    def test_zero_rows_reports_empty_and_does_not_fail(self):
        from unittest import mock
        client = StubClient({"SHARADAR/SEP": {"columns": ["ticker", "date"], "rows": [], "pages": 1}})
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(nm, "MIRROR_DIR", Path(td)):
                receipt = nm.mirror_table(client, "SHARADAR/SEP", ["AAPL"],
                                          full=False, today=dt.date(2026, 11, 23))
        self.assertEqual(receipt["status"], "empty")
        self.assertFalse(receipt["ok"], "an empty mirror must not report ok")
        self.assertEqual(receipt["errors"], [], "empty is not an error")
        self.assertEqual(receipt["rows_total"], 0)

    def test_empty_table_keeps_exit_code_zero(self):
        receipts = [{"table": "SHARADAR/SEP", "status": "empty", "ok": False,
                     "errors": [], "rows_added": 0, "rows_total": 0, "since": None}]
        self.assertTrue(all(not r["errors"] for r in receipts),
                        "empty must not drive a non-zero exit")
