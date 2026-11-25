"""Track C trend-book shadow manager tests (2026-07-06).

Covers: state round-trip, first-run + idempotency (second run same date is a
no-op), ledger append shape (daily and skipped records), day-over-day
chaining, and the --verify logic on synthetic caches — including that it
DETECTS a divergence when the manager's math is perturbed.

Run: .venv/bin/python -m unittest tests.test_trendbook -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.research.futures_trend as ft
import src.research.hybrid_trend as ht
import src.research.hybrid_trend_v3 as ht3
import src.trendbook.manager as tb

N_DAYS = 700  # > LOOKBACK(252) + VOL_WIN(20) so signals are active


def make_synth_caches(cache_dir: Path, n=N_DAYS, seed=7):
    """Deterministic random-walk closes for all 15 v3 leg tickers + DFF.
    CPER gets < 10y of history, so the manager's imported CPER rule drops
    the HG leg (14-leg book) — exercising v3's own leg-selection path."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-02", periods=n)
    for i, lg in enumerate(ht.LEGS):
        base = 40.0 + 15.0 * i
        r = rng.normal(0.0002, 0.01, n)
        close = pd.Series(base * np.exp(np.cumsum(r)), index=dates, name="close")
        close.to_frame().to_parquet(
            cache_dir / (lg.ticker.replace("=", "_") + ".parquet"))
    cal = pd.date_range(dates[0], dates[-1])  # DFF is a calendar-day series
    dff = pd.Series(4.33, index=cal, name="dff")
    dff.to_frame().to_parquet(cache_dir / "DFF.parquet")
    return dates


class TrendbookBase(unittest.TestCase):
    """Sandbox: synthetic caches + redirected state/ledger paths."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trendbook_test_"))
        self.dates = make_synth_caches(self.tmp)
        self._saved = (ft.CACHE, ht3.CACHE, ht3.DFF_CACHE,
                       tb.STATE_PATH, tb.LEDGER_PATH)
        ft.CACHE = self.tmp
        ht3.CACHE = self.tmp
        ht3.DFF_CACHE = self.tmp / "DFF.parquet"
        tb.STATE_PATH = self.tmp / "state.json"
        tb.LEDGER_PATH = self.tmp / "ledger.jsonl"

    def tearDown(self):
        (ft.CACHE, ht3.CACHE, ht3.DFF_CACHE,
         tb.STATE_PATH, tb.LEDGER_PATH) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ledger(self):
        p = Path(tb.LEDGER_PATH)
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text().splitlines()]


class TestStateRoundTrip(TrendbookBase):
    def test_round_trip_and_atomic_write(self):
        state = {"mode": "shadow", "equity": 50123.456789012345,
                 "last_processed": "2026-07-06", "cycles": 3,
                 "legs": ["ES", "ZN"], "positions": {"ES": -2.0, "ZN": 185.0}}
        tb.save_state(state)
        self.assertEqual(tb.load_state(), state)
        # full float precision must survive the round trip
        self.assertEqual(tb.load_state()["equity"], state["equity"])
        # no tmp file left behind
        self.assertFalse(Path(str(tb.STATE_PATH) + ".tmp").exists())

    def test_missing_state_is_none(self):
        self.assertIsNone(tb.load_state())


class TestDailyRun(TrendbookBase):
    def test_first_run_idempotency_and_ledger_shape(self):
        last = self.dates[-1]
        rec = tb.run_daily(as_of=last, refresh=False)
        self.assertIsNotNone(rec)

        # ── state shape ──
        st = tb.load_state()
        self.assertEqual(st["mode"], "shadow")
        self.assertIn("OBSERVATION-ONLY", st["stamp"])
        self.assertEqual(st["start_equity"], 50_000.0)
        self.assertEqual(st["last_processed"], str(last.date()))
        self.assertEqual(len(st["legs"]), 14)  # CPER <10y -> HG dropped
        self.assertNotIn("HG", st["legs"])
        self.assertEqual(set(st["positions"]), set(st["legs"]))

        # ── ledger shape (daily record) ──
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        for key in ("record", "mode", "stamp", "date", "info_date", "cycle",
                    "equity", "pnl", "trade_costs", "borrow", "financing",
                    "fin_rate_ann", "gross_etf_notional", "cap_checked",
                    "cap_bind", "legs", "intended_vs_executed_diffs",
                    "fill_convention", "generated_at"):
            self.assertIn(key, r, key)
        self.assertEqual(r["record"], "daily")
        self.assertEqual(r["mode"], "shadow")
        self.assertIn("OBSERVATION-ONLY", r["stamp"])
        self.assertEqual(r["date"], str(last.date()))
        self.assertEqual(r["intended_vs_executed_diffs"], [])  # shadow: zero
        for code, leg in r["legs"].items():
            for key in ("kind", "trade_as", "target", "prev", "traded",
                        "close", "prev_close", "fresh_print"):
                self.assertIn(key, leg, f"{code}.{key}")
            self.assertEqual(leg["prev"], 0.0)  # book started flat
            self.assertEqual(leg["traded"], leg["target"])
        # the book took positions on day one (signals were active)
        self.assertTrue(any(leg["target"] != 0 for leg in r["legs"].values()))
        # financing rate = synthetic DFF 4.33% + 1.50% spread
        self.assertAlmostEqual(r["fin_rate_ann"], 0.0433 + 0.015, places=12)

        # ── idempotency: second run same as_of is a strict no-op ──
        state_before = Path(tb.STATE_PATH).read_text()
        rec2 = tb.run_daily(as_of=last, refresh=False)
        self.assertIsNone(rec2)
        self.assertEqual(Path(tb.STATE_PATH).read_text(), state_before)
        self.assertEqual(len(self.ledger()), 1)

    def test_second_day_appends_and_chains(self):
        tb.run_daily(as_of=self.dates[-1], refresh=False)
        st1 = tb.load_state()

        # a new trading day arrives (append-only, as the refresher would)
        new_day = self.dates[-1] + pd.offsets.BDay(1)
        for lg in ht.LEGS:
            fp = self.tmp / (lg.ticker.replace("=", "_") + ".parquet")
            s = pd.read_parquet(fp)["close"]
            s.index = pd.DatetimeIndex(s.index)
            s2 = pd.concat([s, pd.Series([s.iloc[-1] * 1.001], index=[new_day])])
            s2.rename("close").to_frame().to_parquet(fp)

        rec = tb.run_daily(as_of=new_day, refresh=False)
        rows = self.ledger()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rec["date"], str(new_day.date()))
        self.assertEqual(rec["cycle"], 2)
        st2 = tb.load_state()
        self.assertEqual(st2["last_processed"], str(new_day.date()))
        # day-2 prev positions must chain from day-1 targets
        for code in st1["legs"]:
            self.assertEqual(rows[1]["legs"][code]["prev"],
                             rows[0]["legs"][code]["target"])
        self.assertEqual(st2["positions"][code], rows[1]["legs"][code]["target"])

    def test_missing_day_writes_skipped_record_never_fabricates(self):
        tb.run_daily(as_of=self.dates[-1], refresh=False)
        # next business day: caches have no fresh closes (holiday or failure)
        next_bday = self.dates[-1] + pd.offsets.BDay(1)
        state_before = Path(tb.STATE_PATH).read_text()
        rec = tb.run_daily(as_of=next_bday, refresh=False)
        self.assertIsNone(rec)
        rows = self.ledger()
        self.assertEqual(rows[-1]["record"], "skipped")
        self.assertIn("no fresh closes", rows[-1]["reason"])
        self.assertIn("OBSERVATION-ONLY", rows[-1]["stamp"])
        # state untouched — nothing was fabricated
        self.assertEqual(Path(tb.STATE_PATH).read_text(), state_before)

    def test_weekend_is_quiet_noop(self):
        tb.run_daily(as_of=self.dates[-1], refresh=False)
        n = len(self.ledger())
        sat = self.dates[-1] + pd.Timedelta(days=5 - self.dates[-1].weekday())
        self.assertGreaterEqual(sat.weekday(), 5)
        self.assertIsNone(tb.run_daily(as_of=sat, refresh=False))
        self.assertEqual(len(self.ledger()), n)  # no skipped record spam


class TestVerifyMode(TrendbookBase):
    def test_verify_exact_match_on_synthetic_caches(self):
        res = tb.verify_against_backtest(quiet=True)
        self.assertTrue(res["equity_ok"])
        self.assertEqual(res["stat_mismatches"], {})
        self.assertTrue(res["ok"])
        self.assertEqual(res["date"], str(self.dates[-1].date()))
        self.assertEqual(len(res["targets"]), 14)

    def test_verify_detects_perturbed_manager_math(self):
        # if the manager's sizing constant drifts from the frozen design,
        # verify must fail loudly — this is the whole point of the mode
        saved = tb.PORT_VOL_TARGET
        try:
            tb.PORT_VOL_TARGET = 0.24  # double the vol target in the manager only
            res = tb.verify_against_backtest(quiet=True)
        finally:
            tb.PORT_VOL_TARGET = saved
        self.assertFalse(res["ok"])
        self.assertFalse(res["equity_ok"])
        self.assertIsNotNone(res["first_diff"])

    def test_verify_targets_match_backtest_final_positions(self):
        """The contract: manager targets for the most recent common date ==
        the backtest's positions for that date. Cross-check equity identity
        implies it, but also assert a target reacts to the backtest inputs."""
        legs = tb.active_legs()
        idx, C, _R, sig, vol, _q, _d = ht.build_hybrid_panel(legs)
        dff = ht3.fetch_dff()
        fin_rate = ht3.fin_rate_on(idx, dff)
        eq_ref, _ = ht3.level_b_hybrid_v3(idx, C, sig, vol, legs, fin_rate)
        _eq, _agg, n_final, _rec = tb.replay(idx, C, sig, vol, legs, fin_rate)
        res = tb.verify_against_backtest(quiet=True)
        self.assertEqual(res["targets"],
                         {lg.code: float(v) for lg, v in zip(legs, n_final)})
        self.assertEqual(res["final_equity"], float(eq_ref.values[-1]))


if __name__ == "__main__":
    unittest.main()
