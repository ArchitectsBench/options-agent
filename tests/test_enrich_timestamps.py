"""Truth-ruler timestamp handling (2026-08-10).

The enricher's ET conversion keyed the aware-UTC branch on a literal 'Z'
suffix. IBKR stamps '+00:00' — aware UTC without the Z — so every IBKR
entry timestamp fell into the naive-PT branch (+3h on top of UTC) and landed
after the close. Result: 57 fabricated truth scores (+$4,411.50 phantom P&L,
75% of the IBKR era) consumed by the frozen gates, flattering the SCALP
candidate's profit factor from a failing 1.27 to a passing 1.55.

The three timestamp forms in the ledger, pinned so no format is ever again
routed by string suffix:
  - naive local PT   (engine writes)      '2026-08-04T06:45:24'
  - aware UTC with Z (older broker rows)  '2026-08-10T14:00:10Z'
  - aware UTC +00:00 (IBKR ib_async)      '2026-08-10T14:00:10.305590+00:00'
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from enrich_trades_thetadata import pt_to_et_minute  # noqa: E402


class EtConversionCoversEveryLedgerForm(unittest.TestCase):
    def test_naive_pt(self):
        self.assertEqual(pt_to_et_minute("2026-08-04T06:45:24"), "09:45")

    def test_aware_utc_z_suffix(self):
        self.assertEqual(pt_to_et_minute("2026-08-10T14:00:10Z"), "10:00")

    def test_aware_utc_offset_form_the_ibkr_defect(self):
        """THE defect: '+00:00' is aware UTC and must convert like 'Z' —
        not get +3h stacked on top of UTC (17:00, after the close)."""
        self.assertEqual(
            pt_to_et_minute("2026-08-10T14:00:10.305590+00:00"), "10:00")

    def test_aware_nonzero_offset(self):
        """Any aware offset must route through the tz database."""
        self.assertEqual(pt_to_et_minute("2026-08-10T07:00:10-07:00"), "10:00")

    def test_dst_winter_utc(self):
        """R10: EST is UTC-5; a hardcoded -4 would say 10:00."""
        self.assertEqual(pt_to_et_minute("2026-12-10T14:00:00Z"), "09:00")


class FailClosedGuardExists(unittest.TestCase):
    def test_enricher_refuses_empty_windows(self):
        """A truth ruler must refuse loudly when it cannot measure. The
        corrupt rows carried status='ok' with n_path_minutes=0; the guard
        writes status='window_error' instead, which the gates exclude
        (any non-'ok' status is fail-closed). Assert the guard is present
        and precedes the status='ok' write."""
        src = (ROOT / "scripts" / "enrich_trades_thetadata.py").read_text()
        self.assertIn('"window_error"', src)
        self.assertLess(src.index('"window_error"'), src.index('"status": "ok"'),
                        "the refusal guard must run before a row can be scored ok")


if __name__ == "__main__":
    unittest.main()
