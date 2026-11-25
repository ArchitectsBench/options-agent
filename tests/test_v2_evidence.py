"""SCALP-v2 / v1 EVIDENCE tests — scripts/scalp_forward_check.py under a
synthetic ledger.

Model for what these hunt (activation night, 2026-09-12): the YAML said v2 was
ON, a test that re-read the YAML agreed, and the ENGINE disagreed. So every
assertion here goes through the tracker's own readers (qualifying,
ioc_fill_rate, evaluate) on a ledger the test wrote — never by re-parsing the
ledger or the criteria files by hand.

Hermetic: the ledger is an ABSOLUTE tmp path passed through scope.ledger;
mode_map / load_enrichment / adverse_regime / load_criteria are patched so no
production log, parquet or config is consulted (the conftest O-20 seal blocks
writes; these patches remove the READS too, so nothing here depends on the
state of logs/ or data/).

Tests whose assertion message starts with 'SRC DEFECT:' document CORRECT
behaviour the script does not yet implement. They are meant to FAIL until the
script is fixed — deliberately NOT xfail, because a hidden failure is exactly
the activation-night pattern.

Run: .venv/bin/python -m pytest -q tests/test_v2_evidence.py
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scalp_forward_check as sfc  # noqa: E402

V2_START = "2026-09-12T22:55:27-07:00"          # the activation stamp (window_start of v2, window_end of v1)
V2_MODE = "marketable_ioc"


# --------------------------------------------------------------------------- helpers
def _fill(rec_id, fill_mode=V2_MODE, ts="2026-09-14T06:35:00", price=1.55, **extra):
    """A paper_fill row that passes the v1 artifact_fill_filter band."""
    row = {"ts": ts, "event": "paper_fill", "rec_id": rec_id,
           "ticker": rec_id.split("-")[-2], "fill_price": price,
           "mid_at_fill": price, "spread_at_fill": 0.04, "quantity": 1}
    if fill_mode is not None:
        row["fill_mode"] = fill_mode
        row["limit_sent"] = price
        row["tif"] = "IOC" if fill_mode == V2_MODE else "DAY"
    row.update(extra)
    return row


def _exit(rec_id, entered, pnl, ts=None):
    ts = ts or "2026-09-14T08:00:00"
    return {"ts": ts, "event": "paper_exit", "rec_id": rec_id,
            "ticker": rec_id.split("-")[-2], "pnl_total": pnl,
            "round_trip": {"entry_filled_at": entered}}


def _unfilled(rec_id, ts):
    return {"ts": ts, "event": "entry_ioc_unfilled", "rec_id": rec_id,
            "ticker": rec_id.split("-")[-2], "order_id": 7, "limit_sent": 1.60,
            "fill_mode": V2_MODE, "stamp": "UNVALIDATED — PAPER ONLY"}


def _v2_criteria(ledger: Path, window_start=V2_START, scoring=None) -> dict:
    return {"status": "FROZEN", "window_start": window_start, "scoring": scoring,
            "scope": {"ledger": str(ledger), "rec_id_tag": "-SCALP-",
                      "fill_mode": V2_MODE, "claude_approved_only": False,
                      "include_fallback": True},
            "min_trades": 100, "min_trading_days": 60, "adverse_regime_required": True,
            "min_fill_rate": 0.25, "min_forward_sharpe_ann": 0.8,
            "min_profit_factor": 1.3, "max_drawdown_usd": 700,
            "per_structure_non_negative": True, "concentration_check": True}


class _LedgerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "fills.jsonl"     # ABSOLUTE path
        # No production reads: observation logs, parquets, enrichment.
        self._patches = [
            patch.object(sfc, "mode_map", return_value={}),
            patch.object(sfc.fac, "adverse_regime",
                         return_value={"met": False, "adverse_weeks": 0}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def write(self, rows):
        self.ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


# =========================================================================== qualifying
class QualifyingOnSyntheticLedger(_LedgerBase):
    """The v2 gate reads whatever scope.ledger names — including an absolute
    path — and counts ONLY entries filled under the frozen fill mode."""

    def test_absolute_ledger_path_is_honoured(self):
        """Prevents the tracker silently reading the PRODUCTION ledger (ROOT /
        relative) when a caller points scope.ledger somewhere else — the
        forward check would then report evidence from the wrong file."""
        self.write([
            _fill("20260914-SCALP-3-SPY-debit_spread"),
            _exit("20260914-SCALP-3-SPY-debit_spread", "2026-09-14T06:35:00", 40.0),
        ])
        trades, excluded = sfc.qualifying(_v2_criteria(self.ledger))
        self.assertEqual([t["rec_id"] for t in trades], ["20260914-SCALP-3-SPY-debit_spread"])
        self.assertEqual(trades[0]["pnl"], 40.0)
        self.assertEqual(trades[0]["pnl_booked"], 40.0)
        self.assertEqual(trades[0]["structure"], "debit_spread")
        self.assertTrue(all(len(v) == 0 for v in excluded.values()), excluded)

    def test_wrong_fill_mode_is_excluded_and_reported(self):
        """Prevents a v1 resting-mid fill (no fill_mode / fill_mode=resting_mid)
        that carries the same -SCALP- marker from being blended into v2
        evidence — and prevents it being dropped SILENTLY."""
        self.write([
            _fill("20260914-SCALP-1-SPY-0dte_directional"),                        # v2
            _exit("20260914-SCALP-1-SPY-0dte_directional", "2026-09-14T06:35:00", 25.0),
            _fill("20260914-SCALP-2-QQQ-debit_spread", fill_mode=None),            # v1: no fill_mode key
            _exit("20260914-SCALP-2-QQQ-debit_spread", "2026-09-14T06:40:00", 70.0),
            _fill("20260914-SCALP-4-IWM-debit_spread", fill_mode="resting_mid"),   # v1: explicit
            _exit("20260914-SCALP-4-IWM-debit_spread", "2026-09-14T06:45:00", -15.0),
        ])
        trades, excluded = sfc.qualifying(_v2_criteria(self.ledger))
        self.assertEqual([t["rec_id"] for t in trades], ["20260914-SCALP-1-SPY-0dte_directional"])
        self.assertEqual(sorted(r["rec_id"] for r in excluded["wrong_fill_mode"]),
                         ["20260914-SCALP-2-QQQ-debit_spread", "20260914-SCALP-4-IWM-debit_spread"])
        # The evaluate() report must carry the exclusion, with its uncounted P&L.
        with patch.object(sfc, "load_criteria", return_value=_v2_criteria(self.ledger)):
            r = sfc.evaluate(v2=True)
        self.assertEqual(r["excluded"]["wrong_fill_mode"]["count"], 2)
        self.assertEqual(r["excluded"]["wrong_fill_mode"]["pnl_not_counted"], 55)
        self.assertEqual(r["n_trades"], 1)

    def test_funnel_and_legacy_test_marker_scoping(self):
        """Prevents a funnel round trip (no SCALP marker) counting as v2 evidence,
        while the legacy '-TEST-' era marker still routes through is_scalp."""
        self.write([
            _fill("2026-09-14-002"),                                                 # funnel id
            _exit("2026-09-14-002", "2026-09-14T06:35:00", 500.0),
            _fill("20260914-TEST-9-SPY-debit_spread"),                              # legacy marker, v2 mode
            _exit("20260914-TEST-9-SPY-debit_spread", "2026-09-14T06:36:00", 5.0),
        ])
        trades, _ = sfc.qualifying(_v2_criteria(self.ledger))
        self.assertEqual([t["rec_id"] for t in trades], ["20260914-TEST-9-SPY-debit_spread"])


# =========================================================================== window_start
class WindowStartCut(_LedgerBase):
    """window_start = 2026-09-12T22:55:27-07:00. Entries are compared tz-aware:
    naive stamps are America/Los_Angeles; aware UTC stamps are converted."""

    ROWS = [
        # naive PT, 5 minutes BEFORE the window opens
        ("20260912-SCALP-1-SPY-debit_spread", "2026-09-12T22:50:00", False),
        # naive PT, 15 minutes AFTER
        ("20260912-SCALP-2-QQQ-debit_spread", "2026-09-12T23:10:00", True),
        # aware UTC: 05:55:00Z == 22:55:00 PT, 27 s BEFORE the window
        ("20260912-SCALP-3-IWM-debit_spread", "2026-09-13T05:55:00+00:00", False),
        # aware UTC: 06:00:00Z == 23:00 PT, AFTER
        ("20260912-SCALP-4-TLT-debit_spread", "2026-09-13T06:00:00+00:00", True),
    ]

    def _ledger(self):
        rows = []
        for rid, entered, _ in self.ROWS:
            rows.append(_fill(rid, ts=entered))
            rows.append(_exit(rid, entered, 10.0, ts="2026-09-13T08:00:00"))
        self.write(rows)

    def test_qualifying_cuts_on_window_start_naive_and_aware(self):
        """Prevents the 2026-07-09 freeze-day bug recurring for v2: a UTC stamp
        that is BEFORE the -07:00 window opening but sorts AFTER it as a string
        must not leak pre-activation round trips into frozen evidence."""
        self._ledger()
        trades, _ = sfc.qualifying(_v2_criteria(self.ledger))
        self.assertEqual(sorted(t["rec_id"] for t in trades),
                         sorted(rid for rid, _, inside in self.ROWS if inside))

    def test_no_window_means_everything_counts(self):
        """Prevents a DRAFT (window_start null) run from silently dropping
        rows — dry-run mode must show the whole motivating sample."""
        self._ledger()
        trades, _ = sfc.qualifying(_v2_criteria(self.ledger, window_start=None))
        self.assertEqual(len(trades), len(self.ROWS))


# =========================================================================== ioc_fill_rate
class IocFillRate(_LedgerBase):
    """fills / (fills + entry_ioc_unfilled) — the denominator IS the criterion."""

    def test_shape_filled_unfilled_rate_need(self):
        """Prevents a fill rate computed on fills alone (100% by construction):
        every entry_ioc_unfilled row inside the window must be in the
        denominator, and v1 fills must never be in the numerator."""
        self.write([
            _fill("20260914-SCALP-1-SPY-debit_spread", ts="2026-09-14T06:35:00"),
            _fill("20260914-SCALP-2-QQQ-debit_spread", fill_mode=None, ts="2026-09-14T06:36:00"),  # v1 fill: not counted
            _unfilled("20260914-SCALP-3-IWM-debit_spread", "2026-09-14T06:40:00"),
            _unfilled("20260914-SCALP-4-SPY-debit_spread", "2026-09-14T06:45:00"),
            _unfilled("20260914-SCALP-5-QQQ-debit_spread", "2026-09-14T06:50:00"),
            _unfilled("20260912-SCALP-0-SPY-debit_spread", "2026-09-12T22:00:00"),  # pre-window: not counted
        ])
        fr = sfc.ioc_fill_rate(_v2_criteria(self.ledger), [])
        # broker_rejects / market_rate are DISCLOSURE fields (2026-09-15); the
        # frozen computation is still filled / (filled + unfilled)
        self.assertEqual(set(fr), {"filled", "unfilled", "rate", "need", "broker_rejects", "market_rate"})
        self.assertEqual((fr["filled"], fr["unfilled"]), (1, 3))
        self.assertAlmostEqual(fr["rate"], 0.25)
        self.assertEqual(fr["need"], 0.25)

    def test_zero_attempts_is_not_a_pass(self):
        """Prevents a day with NO IOC attempts (Monday the mechanism never fires)
        reading as a 100% fill rate: rate must be None and the check must FAIL,
        not pass, not crash."""
        self.write([{"ts": "2026-09-14T06:30:00", "event": "cycle_noise"}])
        fr = sfc.ioc_fill_rate(_v2_criteria(self.ledger), [])
        self.assertEqual(fr, {"filled": 0, "unfilled": 0, "rate": None, "need": 0.25,
                              "broker_rejects": 0, "market_rate": None})
        with patch.object(sfc, "load_criteria", return_value=_v2_criteria(self.ledger)):
            r = sfc.evaluate(v2=True)
        chk = r["checks"]["ioc_fill_rate"]
        self.assertFalse(chk["pass"])
        self.assertEqual(chk["value"], "no IOC attempts yet")
        self.assertFalse(r["all_pass"])

    def test_all_unfilled_fails_the_frozen_floor(self):
        """Prevents 'nothing fills all day' (the hypothesis failing) from being
        reported as anything but a failed criterion."""
        self.write([_unfilled(f"20260914-SCALP-{i}-SPY-debit_spread", f"2026-09-14T06:{30+i:02d}:00")
                    for i in range(5)])
        fr = sfc.ioc_fill_rate(_v2_criteria(self.ledger), [])
        self.assertEqual((fr["filled"], fr["unfilled"], fr["rate"]), (0, 5, 0.0))
        with patch.object(sfc, "load_criteria", return_value=_v2_criteria(self.ledger)):
            r = sfc.evaluate(v2=True)
        self.assertFalse(r["checks"]["ioc_fill_rate"]["pass"])
        self.assertEqual(r["checks"]["ioc_fill_rate"]["value"], "0% (0/5)")

    def test_v1_scope_has_no_fill_rate(self):
        """Prevents the v1 (resting-mid) record acquiring an IOC criterion it was
        never frozen with: scope without fill_mode -> None, no check key."""
        self.write([_unfilled("20260914-SCALP-1-SPY-debit_spread", "2026-09-14T06:40:00")])
        crit = _v2_criteria(self.ledger)
        del crit["scope"]["fill_mode"]
        self.assertIsNone(sfc.ioc_fill_rate(crit, []))
        with patch.object(sfc, "load_criteria", return_value=crit):
            r = sfc.evaluate(v2=False)
        self.assertNotIn("ioc_fill_rate", r["checks"])

    def test_window_start_is_tz_aware_for_the_denominator(self):
        """SRC DEFECT (expected to fail today): prevents the 7-hour naive/aware
        window bug recurring in the fill-rate denominator. The tracker's own
        docstring says the ledger mixes UTC and local stamps; qualifying() is
        tz-aware but ioc_fill_rate() compares strings against
        window_start[:19]. A paper_fill stamped 2026-09-13T05:30:00+00:00
        (= 22:30 PT on 09-12, BEFORE the 22:55 PT window) must NOT count."""
        self.write([
            _fill("20260912-SCALP-1-SPY-debit_spread", ts="2026-09-13T05:30:00+00:00"),  # pre-window (UTC)
            _unfilled("20260912-SCALP-2-QQQ-debit_spread", "2026-09-12T23:00:00"),      # in-window (naive PT)
        ])
        fr = sfc.ioc_fill_rate(_v2_criteria(self.ledger), [])
        self.assertEqual((fr["filled"], fr["unfilled"]), (0, 1),
                         "SRC DEFECT: ioc_fill_rate window cut is a naive string compare; "
                         "an aware-UTC pre-window fill leaks into the numerator")


# =========================================================================== v1 CLOSED_FAILED
class V1ClosedRecord(_LedgerBase):
    """The v1 candidate was CLOSED_FAILED at 2026-09-12T22:55:27-07:00. The
    tracker must SAY so, must not call anything after it evidence, and must not
    keep accruing v2 trades onto the closed record."""

    def _real_v1(self, extra_rows=()):
        crit = copy.deepcopy(sfc.load_criteria(v2=False))     # read-only; config/ is never written
        crit["scope"]["ledger"] = str(self.ledger)
        rows = [
            _fill("20260910-SCALP-1-SPY-debit_spread", fill_mode=None, ts="2026-09-10T07:00:00"),
            _exit("20260910-SCALP-1-SPY-debit_spread", "2026-09-10T07:00:00", 30.0, ts="2026-09-10T08:00:00"),
            *extra_rows,
        ]
        self.write(rows)
        enrichment = {r["rec_id"]: {"rec_id": r["rec_id"], "status": "ok", "true_pnl_mid": 12.0}
                      for r in rows if r["event"] == "paper_exit"}
        return crit, enrichment

    def test_status_closed_failed_is_surfaced_and_not_evidence(self):
        """Prevents the nightly report presenting the retired v1 record as a
        FROZEN, evidence-accruing candidate: status must read CLOSED_FAILED
        and evidence must be False, read through evaluate()."""
        crit, enrichment = self._real_v1()
        self.assertEqual(crit["status"], "CLOSED_FAILED")        # the criteria file as loaded by the tracker
        self.assertTrue(crit.get("window_end"))
        with patch.object(sfc, "load_criteria", return_value=crit), \
             patch.object(sfc.fac, "load_enrichment", return_value=enrichment):
            r = sfc.evaluate(v2=False)
        self.assertEqual(r["status"], "CLOSED_FAILED")
        self.assertFalse(r["evidence"])
        self.assertEqual(r["n_trades"], 1)                        # the pre-close trade IS the v1 record
        self.assertEqual(r["window_start"], crit["window_start"])

    def test_v1_scores_market_truth_not_booked(self):
        """Prevents the L21 regression: v1 is scored market_mid_v1, so the
        row's pnl must be the enrichment truth (12) while pnl_booked keeps
        the simulator's 30 as a diagnostic."""
        crit, enrichment = self._real_v1()
        with patch.object(sfc.fac, "load_enrichment", return_value=enrichment):
            trades, excluded = sfc.qualifying(crit)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["pnl"], 12.0)
        self.assertEqual(trades[0]["pnl_booked"], 30.0)
        # and an unenriched trade is excluded fail-closed, never silently
        with patch.object(sfc.fac, "load_enrichment", return_value={}):
            trades, excluded = sfc.qualifying(crit)
        self.assertEqual(trades, [])
        self.assertEqual([r["rec_id"] for r in excluded["pending_enrichment"]],
                         ["20260910-SCALP-1-SPY-debit_spread"])

    def test_no_trades_after_window_end_are_counted(self):
        """SRC DEFECT (expected to fail today): prevents the closed v1 record
        silently accruing v2 trades after 2026-09-12T22:55:27-07:00. A v2 IOC
        round trip entered Monday 2026-09-14 is in the -SCALP- lane and the v1
        scope has no fill_mode, so ONLY window_end can keep it out — and the
        tracker never reads window_end."""
        post = [
            _fill("20260914-SCALP-7-SPY-debit_spread", ts="2026-09-14T06:35:00"),           # v2 IOC entry
            _exit("20260914-SCALP-7-SPY-debit_spread", "2026-09-14T06:35:00", 45.0, ts="2026-09-14T07:00:00"),
        ]
        crit, enrichment = self._real_v1(post)
        with patch.object(sfc.fac, "load_enrichment", return_value=enrichment):
            trades, excluded = sfc.qualifying(crit)
        self.assertEqual([t["rec_id"] for t in trades], ["20260910-SCALP-1-SPY-debit_spread"],
                         "SRC DEFECT: qualifying() ignores window_end; a post-close v2 trade "
                         "accrues onto the CLOSED_FAILED v1 record")
        # Not a silent drop: the post-window trade must appear under SOME exclusion.
        all_excluded = [r["rec_id"] for v in excluded.values() for r in v]
        self.assertIn("20260914-SCALP-7-SPY-debit_spread", all_excluded,
                      "SRC DEFECT: post-window_end trade must be reported as excluded, not vanish")

    def test_ioc_fill_rate_respects_window_end(self):
        """SRC DEFECT (expected to fail today): prevents a closed window's fill
        rate drifting after close — attempts after window_end must not enter
        numerator or denominator."""
        end = "2026-09-14T13:00:00-07:00"
        crit = _v2_criteria(self.ledger)
        crit["window_end"] = end
        crit["status"] = "CLOSED_FAILED"
        self.write([
            _fill("20260914-SCALP-1-SPY-debit_spread", ts="2026-09-14T06:35:00"),        # inside
            _unfilled("20260914-SCALP-2-QQQ-debit_spread", "2026-09-14T06:40:00"),       # inside
            _unfilled("20260915-SCALP-3-IWM-debit_spread", "2026-09-15T06:40:00"),       # after window_end
            _fill("20260915-SCALP-4-SPY-debit_spread", ts="2026-09-15T06:45:00"),        # after window_end
        ])
        fr = sfc.ioc_fill_rate(crit, [])
        self.assertEqual((fr["filled"], fr["unfilled"]), (1, 1),
                         "SRC DEFECT: ioc_fill_rate() never reads window_end")


# =========================================================================== partial IOC
class PartialIocIsV2Evidence(_LedgerBase):
    """An IOC that fills 2 of 5 and is exchange-cancelled is the CANONICAL v2
    outcome. Its round trip is real v2 evidence and its attempt is a fill."""

    def _partial_ledger(self):
        rid = "20260914-SCALP-5-NVDA-debit_spread"
        self.write([
            {"ts": "2026-09-14T06:35:10", "event": "partial_fill_booked", "rec_id": rid,
             "ticker": "NVDA", "filled": 2, "quantity": 5, "fill_price": 1.55,
             "mid_at_fill": 1.55, "spread_at_fill": 0.04,
             "fill_mode": V2_MODE, "limit_sent": 1.57, "tif": "IOC"},       # the stamp the engine SHOULD write
            {"ts": "2026-09-14T06:40:10", "event": "partial_remainder_cancelled", "rec_id": rid,
             "ticker": "NVDA", "remaining": 3},
            _exit(rid, "2026-09-14T06:35:10", 80.0, ts="2026-09-14T07:30:00"),
        ])
        return rid

    def test_partial_round_trip_qualifies_not_wrong_fill_mode(self):
        """SRC DEFECT (expected to fail today): prevents a genuine v2 partial
        fill being thrown out of the frozen v2 gate as 'wrong_fill_mode' —
        qualifying() builds entry_fills from paper_fill rows ONLY, so a
        partial_fill_booked entry defaults to resting_mid and is excluded."""
        rid = self._partial_ledger()
        trades, excluded = sfc.qualifying(_v2_criteria(self.ledger))
        self.assertEqual([t["rec_id"] for t in trades], [rid],
                         "SRC DEFECT: partial_fill_booked entry is not recognised as a v2 fill; "
                         f"excluded={ {k: [r['rec_id'] for r in v] for k, v in excluded.items() if v} }")
        self.assertEqual(excluded["wrong_fill_mode"], [])

    def test_partial_counts_as_filled_in_fill_rate(self):
        """SRC DEFECT (expected to fail today): prevents a partially-filled IOC
        counting as NEITHER filled nor unfilled — the fill-rate criterion must
        see the attempt, and it filled."""
        self._partial_ledger()
        fr = sfc.ioc_fill_rate(_v2_criteria(self.ledger), [])
        self.assertEqual((fr["filled"], fr["unfilled"]), (1, 0),
                         "SRC DEFECT: ioc_fill_rate() counts only paper_fill rows; "
                         "a partial IOC fill is invisible to the criterion")


# =========================================================================== robustness
class NewEventsDoNotBreakTheTracker(_LedgerBase):
    def test_v2_event_rows_without_rec_id_are_ignored_and_round_trip_counted_once(self):
        """Prevents a new v2 ledger row (entry_ioc_unfilled, price-floor block —
        some with no rec_id, no fill_price) from crashing the nightly tracker
        or double-counting the round trip it sits next to."""
        rid = "20260914-SCALP-1-SPY-debit_spread"
        self.write([
            {"ts": "2026-09-14T06:30:05", "event": "entry_blocked_v2_price_floor",
             "ticker": "QQQ", "net_debit": 0.45, "floor": 1.0},                 # no rec_id
            _unfilled("20260914-SCALP-0-IWM-debit_spread", "2026-09-14T06:30:10"),
            _fill(rid),
            {"ts": "2026-09-14T06:35:30", "event": "position_managed", "rec_id": rid},
            _exit(rid, "2026-09-14T06:35:00", 20.0),
            {"ts": "2026-09-14T08:00:05", "event": "reconcile_alarm", "kind": "x"},  # no rec_id
        ])
        crit = _v2_criteria(self.ledger)
        trades, excluded = sfc.qualifying(crit)
        self.assertEqual([t["rec_id"] for t in trades], [rid])
        fr = sfc.ioc_fill_rate(crit, trades)
        self.assertEqual((fr["filled"], fr["unfilled"]), (1, 1))
        with patch.object(sfc, "load_criteria", return_value=crit):
            r = sfc.evaluate(v2=True)
        self.assertEqual(r["n_trades"], 1)
        self.assertEqual(r["total_pnl"], 20)
        self.assertEqual(r["checks"]["ioc_fill_rate"]["value"], "50% (1/2)")
        self.assertTrue(r["checks"]["ioc_fill_rate"]["pass"])
        self.assertFalse(r["all_pass"])          # 1 trade << 100: accruing, not met

    def test_main_v2_prints_without_raising(self):
        """Prevents the operator's `scalp_forward_check.py --v2` (the Monday
        07:30 ops check) 500-ing on a ledger containing only attempts."""
        self.write([_unfilled("20260914-SCALP-0-IWM-debit_spread", "2026-09-14T06:30:10")])
        with patch.object(sfc, "load_criteria", return_value=_v2_criteria(self.ledger)), \
             patch.object(sys, "argv", ["scalp_forward_check.py", "--v2"]), \
             patch("builtins.print") as out:
            self.assertEqual(sfc.main(), 0)
        printed = "\n".join(str(c.args[0]) if c.args else "" for c in out.call_args_list)
        self.assertIn("SCALP-v2 FORWARD TRACKER", printed)
        self.assertIn("ioc_fill_rate", printed)
        self.assertIn("0% (0/1)", printed)


# =========================================================================== nightly wiring
class NightlyRunsTheV2Tracker(unittest.TestCase):
    def test_ops_checks_evaluates_v2(self):
        """SRC DEFECT (expected to fail today): prevents the live v2 candidate
        accruing NOTHING visible while the nightly checks keep reporting the
        CLOSED_FAILED v1 record — ops_checks.scalp_forward() must run
        evaluate(v2=True) (the only caller of --v2 today is the test suite)."""
        import ops_checks
        fake = {"status": "FROZEN", "window_start": V2_START, "n_trades": 0,
                "total_pnl": 0, "excluded": {}, "checks": {}, "all_pass": False}
        with patch.object(sfc, "evaluate", return_value=fake) as ev:
            ops_checks.scalp_forward()
        self.assertTrue(any(c.kwargs.get("v2") is True or (c.args and c.args[0] is True)
                            for c in ev.call_args_list),
                        f"SRC DEFECT: nightly never calls evaluate(v2=True); calls={ev.call_args_list}")


# =========================================================================== report visibility
class TrackerSurfacesBookedBesideTruth(_LedgerBase):
    def test_evaluate_reports_booked_aggregate(self):
        """SRC DEFECT (expected to fail today; report-only, NOT a gate change):
        prevents a mid-scored PASS hiding a negative booked P&L. v2 pays half
        the spread by design, the prereg's 'truth P&L exceeds round-trip spread
        cost' clause is checked nowhere, and pnl_booked is carried per row but
        never aggregated — a sleeve earning +$8 at mids while paying $10 to
        cross would pass every frozen check."""
        rows, enrichment = [], {}
        for i, (booked, truth) in enumerate([(-30.0, 12.0), (-25.0, 10.0), (-20.0, 8.0)]):
            rid = f"20260914-SCALP-{i}-SPY-debit_spread"
            rows += [_fill(rid, ts=f"2026-09-14T06:{35+i}:00"),
                     _exit(rid, f"2026-09-14T06:{35+i}:00", booked, ts=f"2026-09-14T07:{i:02d}:00")]
            enrichment[rid] = {"rec_id": rid, "status": "ok", "true_pnl_mid": truth}
        self.write(rows)
        crit = _v2_criteria(self.ledger, scoring="market_mid_v1")
        with patch.object(sfc, "load_criteria", return_value=crit), \
             patch.object(sfc.fac, "load_enrichment", return_value=enrichment):
            r = sfc.evaluate(v2=True)
        self.assertEqual(r["total_pnl"], 30)                      # truth, as frozen
        self.assertIn("total_pnl_booked", r,
                      "SRC DEFECT: evaluate() does not surface the booked (crossing-cost-inclusive) aggregate")
        self.assertEqual(r["total_pnl_booked"], -75)


if __name__ == "__main__":
    unittest.main()
