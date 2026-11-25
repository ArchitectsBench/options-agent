"""Go/no-go check (h) must FAIL when the broker cannot be read.

2026-10-12: the gateway was dead all session (port 4002 closed). The 06:34
go/no-go printed `[PASS] (h) book matches broker (no divergence)` while the
same night's checks JSON reported `position_reconcile FAILED — broker position
read FAILED (not an empty account)`. Cause: check_h called the RAW
audit_book_vs_broker(), which returns [] both for a clean book and for an
unreadable broker; the detect-only engine wants that, a GATE must not have it.
The hazard day is the one the gateway comes back half-open with a stale book.

These tests drive the real check function and read its emitted line.
"""
from __future__ import annotations

import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops" / "ibkr_go_nogo_checks.py"


def _load_checks():
    spec = importlib.util.spec_from_file_location("ibkr_go_nogo_checks", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Unavailable:
    def available(self):
        return False


class _ReadFails:
    def available(self):
        return True

    def get_positions(self):
        return None            # read FAILED — not an empty account


class _CleanEmpty:
    def available(self):
        return True

    def get_positions(self):
        return []              # genuinely flat


class GoNoGoCheckH(unittest.TestCase):
    def _run_h(self, client):
        mod = _load_checks()
        buf = io.StringIO()
        with patch("src.engine.paper_trader.get_order_client", return_value=client):
            with redirect_stdout(buf):
                mod.check_h_book_vs_broker()
        line = buf.getvalue().strip()
        self.assertTrue(line.startswith("h|"), line)
        _, status, detail = line.split("|", 2)
        return status, detail

    def test_broker_unavailable_fails_not_passes(self):
        status, detail = self._run_h(_Unavailable())
        self.assertEqual(status, "FAIL", f"gateway down must NOT pass (h): {detail}")
        self.assertIn("CANNOT VERIFY", detail)

    def test_position_read_failure_fails_not_passes(self):
        """The exact 2026-10-12 shape: client up, position read errors."""
        status, detail = self._run_h(_ReadFails())
        self.assertEqual(status, "FAIL", f"unreadable positions must NOT pass (h): {detail}")
        self.assertIn("CANNOT VERIFY", detail)

    def test_genuinely_flat_still_passes(self):
        """The fix must not turn a real clean reading into a false alarm."""
        with patch("src.engine.paper_trader.load_positions", return_value=[]):
            status, detail = self._run_h(_CleanEmpty())
        self.assertEqual(status, "PASS", detail)

    def test_check_h_does_not_use_the_raw_audit(self):
        """Source guard: the raw [] -on-failure helper must not come back here."""
        src = SCRIPT.read_text()
        start = src.index("def check_h_book_vs_broker")
        end = src.index("\ndef ", start + 10)
        body = src[start:end]
        self.assertIn("audit_book_vs_broker_checked", body)
        self.assertNotIn("import audit_book_vs_broker\n", body)
        self.assertNotIn("= audit_book_vs_broker()", body)


if __name__ == "__main__":
    unittest.main()
