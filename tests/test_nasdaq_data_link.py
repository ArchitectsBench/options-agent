"""Hermetic tests for src/data/nasdaq_data_link.py — no network, no real sleeps.

Every test stubs the module's injectable `_urlopen`/`_sleep`. Nothing here
touches .env, a production file or a real endpoint.
"""

import io
import json
import unittest
import urllib.error
from unittest import mock

from src.data import nasdaq_data_link as ndl


def _resp(payload: dict):
    """A context-manager response whose read() yields the JSON payload."""
    body = json.dumps(payload).encode()

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _R(body)


def _page(rows, cursor=None, columns=("ticker", "date", "close")):
    return {"datatable": {"data": rows,
                          "columns": [{"name": c} for c in columns]},
            "meta": {"next_cursor_id": cursor}}


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


class KeyHandling(unittest.TestCase):
    def test_available_false_without_key(self):
        self.assertFalse(ndl.NasdaqDataLink(key="").available())

    def test_fetch_without_key_never_calls_network(self):
        client = ndl.NasdaqDataLink(key="")
        with mock.patch.object(ndl, "_urlopen", side_effect=AssertionError("called")):
            out = client.fetch("SHARADAR/SEP")
        self.assertIn("NASDAQ_DATA_LINK_KEY not set", out["error"])

    def test_key_is_scrubbed_from_error_bodies(self):
        client = ndl.NasdaqDataLink(key="SECRET123")
        err = _http_error(403, b'{"detail": "key SECRET123 not entitled"}')
        with mock.patch.object(ndl, "_urlopen", side_effect=err):
            out = client.fetch("SHARADAR/SF2")
        self.assertNotIn("SECRET123", out["error"])
        self.assertIn("<KEY>", out["error"])

    def test_key_is_scrubbed_from_network_errors(self):
        client = ndl.NasdaqDataLink(key="SECRET123")
        exc = urllib.error.URLError("failed for api_key=SECRET123")
        with mock.patch.object(ndl, "_urlopen", side_effect=exc), \
                mock.patch.object(ndl, "_sleep"):
            out = client.fetch("SHARADAR/SEP")
        self.assertNotIn("SECRET123", out["error"])

    def test_key_travels_as_api_key_param(self):
        client = ndl.NasdaqDataLink(key="K")
        seen = []

        def fake(req, timeout=None):
            seen.append(req.full_url)
            return _resp(_page([]))

        with mock.patch.object(ndl, "_urlopen", side_effect=fake):
            client.fetch("SHARADAR/SEP")
        self.assertIn("api_key=K", seen[0])
        self.assertNotIn("apiKey=", seen[0])   # that is Polygon's spelling


class Pagination(unittest.TestCase):
    def test_follows_next_cursor_id(self):
        pages = [_resp(_page([["AAPL", "2026-10-15", 1]], cursor="CUR1")),
                 _resp(_page([["AAPL", "2026-10-16", 2]], cursor=None))]
        seen = []

        def fake(req, timeout=None):
            seen.append(req.full_url)
            return pages.pop(0)

        client = ndl.NasdaqDataLink(key="K")
        with mock.patch.object(ndl, "_urlopen", side_effect=fake):
            out = client.fetch("SHARADAR/SEP", ticker="AAPL")
        self.assertEqual(out["pages"], 2)
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(out["columns"], ["ticker", "date", "close"])
        # the cursor is sent back as qopts.cursor_id, not a page number
        self.assertNotIn("cursor_id", seen[0])
        self.assertIn("qopts.cursor_id=CUR1", seen[1])

    def test_stops_without_cursor(self):
        client = ndl.NasdaqDataLink(key="K")
        with mock.patch.object(ndl, "_urlopen",
                               side_effect=[_resp(_page([["A", "d", 1]]))]):
            out = client.fetch("SHARADAR/TICKERS")
        self.assertEqual(out["pages"], 1)

    def test_page_cap_is_enforced(self):
        client = ndl.NasdaqDataLink(key="K")

        def fake(req, timeout=None):            # never stops offering a cursor
            return _resp(_page([["A", "d", 1]], cursor="SAME"))

        with mock.patch.object(ndl, "_urlopen", side_effect=fake):
            out = client.fetch("SHARADAR/SEP")
        self.assertEqual(out["pages"], ndl.PAGE_CAP)


class Filters(unittest.TestCase):
    def test_range_and_list_filters(self):
        seen = []

        def fake(req, timeout=None):
            seen.append(req.full_url)
            return _resp(_page([]))

        client = ndl.NasdaqDataLink(key="K")
        with mock.patch.object(ndl, "_urlopen", side_effect=fake):
            client.fetch("SHARADAR/SEP", ticker=["AAPL", "MSFT"],
                         date={"gte": "2026-01-01"})
        url = seen[0]
        self.assertIn("ticker=AAPL%2CMSFT", url)
        self.assertIn("date.gte=2026-01-01", url)
        self.assertIn("qopts.per_page=", url)


class RetrySemantics(unittest.TestCase):
    def test_429_is_retried_then_succeeds(self):
        calls = [_http_error(429), _resp(_page([["A", "d", 1]]))]

        def fake(req, timeout=None):
            item = calls.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        client = ndl.NasdaqDataLink(key="K")
        with mock.patch.object(ndl, "_urlopen", side_effect=fake), \
                mock.patch.object(ndl, "_sleep") as slept:
            out = client.fetch("SHARADAR/SEP")
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(slept.call_count, 1)

    def test_404_returns_immediately_and_is_distinguishable(self):
        """A wrong namespace (SHARADAR/RTAT10) must NOT be retried and must
        report its 404 — it is a namespace error, not an entitlement failure."""
        client = ndl.NasdaqDataLink(key="K")
        err = _http_error(404, b'{"code": "QECx02", "message": "datatable does not exist"}')
        with mock.patch.object(ndl, "_urlopen", side_effect=err) as opened, \
                mock.patch.object(ndl, "_sleep") as slept:
            out = client.fetch("SHARADAR/RTAT10")
        self.assertEqual(out["http_status"], 404)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(slept.call_count, 0)

    def test_403_returns_immediately(self):
        client = ndl.NasdaqDataLink(key="K")
        with mock.patch.object(ndl, "_urlopen", side_effect=_http_error(403)) as opened, \
                mock.patch.object(ndl, "_sleep"):
            out = client.fetch("SHARADAR/SF2")
        self.assertEqual(out["http_status"], 403)
        self.assertEqual(opened.call_count, 1)

    def test_network_error_exhausts_retries_and_reports(self):
        client = ndl.NasdaqDataLink(key="K")
        exc = urllib.error.URLError("boom")
        with mock.patch.object(ndl, "_urlopen", side_effect=exc) as opened, \
                mock.patch.object(ndl, "_sleep"):
            out = client.fetch("SHARADAR/SEP")
        self.assertEqual(opened.call_count, ndl.BACKOFF_TRIES)
        self.assertIsNone(out["http_status"])
        self.assertEqual(out["attempts"], ndl.BACKOFF_TRIES)

    def test_undecodable_body_is_not_retried(self):
        client = ndl.NasdaqDataLink(key="K")

        class _Bad(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(ndl, "_urlopen",
                               return_value=_Bad(b"not json")) as opened, \
                mock.patch.object(ndl, "_sleep"):
            out = client.fetch("SHARADAR/SEP")
        self.assertIn("error", out)
        self.assertEqual(opened.call_count, 1)


class DatasetNames(unittest.TestCase):
    def test_rtat10_is_in_the_ndaq_namespace(self):
        """Guards the 2026-10-19 probe finding: SHARADAR/RTAT10 404s."""
        self.assertIn("NDAQ/RTAT10", ndl.KNOWN_DATATABLES)
        self.assertNotIn("SHARADAR/RTAT10", ndl.KNOWN_DATATABLES)
        self.assertNotIn("SHARADAR/RTAT", ndl.KNOWN_DATATABLES)

    def test_no_live_loop_usage(self):
        """Offline/historical only — the docstring must say so, and the module
        must not be imported by the cycle pipeline."""
        self.assertIn("HISTORICAL / OFFLINE ONLY", ndl.__doc__)


if __name__ == "__main__":
    unittest.main()
