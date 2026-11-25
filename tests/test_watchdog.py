"""Risk watchdog tests (2026-07-03).

Covers: streamed-quote position valuation, tick evaluation (stop/target/
neither, single-leg no-target), the close path (lock respected, re-read under
lock, no double-submit after the cycle closes it, closed-by-me dedup),
mark-persist throttling, symbol-set tracking, and quote-line parsing.

Run: .venv/bin/python -m unittest tests.test_watchdog -v
"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.engine.paper_trader as pt
import src.engine.watchdog as wd

LEGS = [{"symbol": "SPY260710C00740000", "action": "buy_to_open"},
        {"symbol": "SPY260710C00747000", "action": "sell_to_open"}]

POSITION = {"rec_id": "R1", "ticker": "SPY", "structure": "call_debit_spread",
            "direction": "long", "legs": LEGS, "entry_price": 3.6, "quantity": 1,
            "max_profit": 3.4, "max_loss": 3.6, "expiration": "2026-07-10",
            "closing": False}

CFG = {"risk": {"paper_exits": {"stop_loss_pct": 0.50, "profit_target_pct": 0.50}},
       "agent": {"watchdog_enabled": True}}

Q = lambda a, b, c, d: {LEGS[0]["symbol"]: (a, b), LEGS[1]["symbol"]: (c, d)}  # noqa: E731


class ValueAndEvalTest(unittest.TestCase):
    def test_value_from_stream_quotes(self):
        self.assertEqual(wd.position_value_from(POSITION, Q(6.0, 6.2, 1.6, 1.8)), 4.4)
        self.assertIsNone(wd.position_value_from(POSITION, {LEGS[0]["symbol"]: (6.0, 6.2)}))
        self.assertIsNone(wd.position_value_from(POSITION, Q(6.0, None, 1.6, 1.8)))

    def test_evaluate_tick(self):
        cfg = CFG["risk"]["paper_exits"]
        self.assertIn("stop_loss", wd.evaluate_tick(POSITION, 1.7, cfg))
        self.assertIn("profit_target", wd.evaluate_tick(POSITION, 5.4, cfg))
        self.assertIsNone(wd.evaluate_tick(POSITION, 3.9, cfg))
        # single-leg target (gap closed 2026-07-06): +50% of debit
        single = {**POSITION, "max_profit": None}
        cfg_s = {**cfg, "single_leg_target_gain_pct": 0.50}
        self.assertIn("single-leg", wd.evaluate_tick(single, 5.4, cfg_s))
        self.assertIsNone(wd.evaluate_tick(single, 5.39, cfg_s))


class WatchdogBookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._orig = (pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG)
        pt.PENDING_FILE = base / "paper_orders.json"
        pt.POSITIONS_FILE = base / "paper_positions.json"
        pt.FILLS_LOG = base / "fills.jsonl"
        pt._save(pt.POSITIONS_FILE, [dict(POSITION)])
        self.dog = wd.RiskWatchdog(threading.Lock(), CFG)

    def tearDown(self):
        pt.PENDING_FILE, pt.POSITIONS_FILE, pt.FILLS_LOG = self._orig
        self.tmp.cleanup()

    def test_stop_breach_submits_close_once(self):
        self.dog.quotes = Q(2.0, 2.1, 0.35, 0.45)  # value 1.65 <= 1.8 stop line
        with patch.object(wd, "submit_close_order",
                          return_value={"submitted": True}) as sub, \
                patch("src.engine.notify.send"):
            self.dog._check_positions()
            self.dog._check_positions()  # second tick: dedup via _closed_by_me
        self.assertEqual(sub.call_count, 1)
        reason = sub.call_args[0][2]
        self.assertIn("stop_loss (watchdog", reason)
        self.assertEqual(pt.exit_reason_code(reason), "stop_50")

    def test_no_submit_when_cycle_already_closing(self):
        pt._save(pt.POSITIONS_FILE, [{**POSITION, "closing": True}])
        self.dog.quotes = Q(2.0, 2.1, 0.35, 0.45)
        with patch.object(wd, "submit_close_order") as sub:
            self.dog._check_positions()
        sub.assert_not_called()

    def test_lock_reread_catches_race(self):
        """Position open at first read, but closing by the time the lock is
        held -> no submit."""
        self.dog.quotes = Q(2.0, 2.1, 0.35, 0.45)
        real_load = pt.load_positions
        calls = {"n": 0}

        def racy_load():
            calls["n"] += 1
            if calls["n"] >= 2:  # the under-lock re-read sees it closing
                return [{**POSITION, "closing": True}]
            return real_load()

        with patch.object(wd, "load_positions", side_effect=racy_load), \
                patch.object(wd, "submit_close_order") as sub:
            self.dog._check_positions()
        sub.assert_not_called()

    def test_target_breach(self):
        self.dog.quotes = Q(6.8, 7.0, 1.4, 1.6)  # value 5.4 >= entry+1.7 target
        with patch.object(wd, "submit_close_order",
                          return_value={"submitted": True}) as sub, \
                patch("src.engine.notify.send"):
            self.dog._check_positions()
        self.assertIn("profit_target (watchdog", sub.call_args[0][2])

    def test_marks_throttled(self):
        self.dog.quotes = Q(6.0, 6.2, 1.6, 1.8)  # value 4.4, no exit
        with patch.object(wd, "update_position_marks") as marks:
            self.dog._check_positions()
            self.dog._check_positions()  # within throttle window
        self.assertEqual(marks.call_count, 1)

    def test_symbols_and_idle(self):
        self.assertEqual(self.dog._symbols(self.dog._open_positions()),
                         {l["symbol"] for l in LEGS})
        pt._save(pt.POSITIONS_FILE, [])
        self.assertEqual(self.dog._open_positions(), [])

    def test_quote_line_parsing(self):
        line = json.dumps({"type": "quote", "symbol": LEGS[0]["symbol"],
                           "bid": 6.05, "ask": 6.15})
        ev = json.loads(line)
        self.assertEqual(ev["type"], "quote")  # shape the consumer expects

    def test_disabled_by_config(self):
        out = wd.start_watchdog(threading.Lock(),
                                {"agent": {"watchdog_enabled": False}})
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()


class BookReadFailsLoud(unittest.TestCase):
    """2026-09-12: _open_positions() returned [] on any exception, so a failing
    book read made the watchdog silently report "nothing to watch" and stop
    being a reflex arc — on a layer whose docstring promises FAILS LOUD,
    DEGRADES SAFE. Degrading safe is correct; doing it silently is not."""

    def _wd(self):
        from unittest.mock import patch
        from src.engine.watchdog import RiskWatchdog
        w = RiskWatchdog.__new__(RiskWatchdog)
        w._closed_by_me = set()
        return w

    def test_book_read_failure_is_logged_not_swallowed(self):
        from unittest.mock import patch
        from src.engine import watchdog as wd
        w = self._wd()
        with patch.object(wd, "load_positions", side_effect=RuntimeError("boom")), \
                patch.object(wd, "send", create=True), \
                self.assertLogs("trader", level="ERROR") as cm:
            out = w._open_positions()
        self.assertEqual(out, [], "must still degrade safe — 5-min loop is the backstop")
        self.assertTrue(any("book read FAILED" in m for m in cm.output),
                        "the failure must be visible in the log")

    def test_consecutive_failures_are_counted(self):
        from unittest.mock import patch
        from src.engine import watchdog as wd
        w = self._wd()
        with patch.object(wd, "load_positions", side_effect=RuntimeError("boom")), \
                patch.object(wd, "send", create=True), \
                self.assertLogs("trader", level="ERROR"):
            w._open_positions(); w._open_positions()
        self.assertEqual(w._book_read_fails, 2)

    def test_counter_resets_on_a_good_read(self):
        from unittest.mock import patch
        from src.engine import watchdog as wd
        w = self._wd()
        w._book_read_fails = 5
        with patch.object(wd, "load_positions", return_value=[]):
            w._open_positions()
        self.assertEqual(w._book_read_fails, 0)
