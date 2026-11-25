"""B3 — entitlement-probe GUARD BEHAVIOUR.

Standing rule (D1): test what the guard DOES when tripped, never that a flag
exists. Each test names the guard's obligation and proves it is met.

The probe is a GATING control for the migration, and it had no tests at all
(blocker B3 in docs/ibkr_verification_matrix.md).
"""
import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def _probe():
    spec = importlib.util.spec_from_file_location(
        "probe_mod", ROOT / "scripts" / "ibkr_entitlement_check.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Tick:
    def __init__(self, bid=None, ask=None, last=None, close=None):
        self.bid, self.ask, self.last, self.close = bid, ask, last, close


class LivenessGuard(unittest.TestCase):
    """GUARD: only a two-sided quote or a trade print counts as a live feed.
    OBLIGATION: a `close`-only quote must NOT be reported live — that misread
    produced a false OPRA pass during manual verification on 2026-08-04."""

    def setUp(self):
        self.p = _probe()

    def test_close_only_is_not_live(self):
        q = self.p._quote(_Tick(close=79.3))
        self.assertFalse(self.p._is_live(q),
                         "a static prior close is not evidence of a live feed")

    def test_ibkr_minus_one_sentinel_is_not_a_price(self):
        q = self.p._quote(_Tick(bid=-1, ask=-1, close=79.3))
        self.assertIsNone(q["bid"]); self.assertIsNone(q["ask"])
        self.assertFalse(self.p._is_live(q))

    def test_two_sided_quote_is_live(self):
        self.assertTrue(self.p._is_live(self.p._quote(_Tick(bid=0.80, ask=0.81))))

    def test_trade_print_alone_is_live(self):
        self.assertTrue(self.p._is_live(self.p._quote(_Tick(last=0.58))))


class SessionGuard(unittest.TestCase):
    """GUARD: liveness is unverifiable outside the session.
    OBLIGATION: report a labelled TENTATIVE pass, never a false green."""

    def setUp(self):
        self.p = _probe()

    def test_weekend_is_not_an_open_session(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        sat = dt.datetime(2026, 8, 8, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(self.p._market_open_et(sat))

    def test_before_open_and_after_close_are_closed(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        self.assertFalse(self.p._market_open_et(dt.datetime(2026, 8, 5, 9, 0, tzinfo=ET)))
        self.assertFalse(self.p._market_open_et(dt.datetime(2026, 8, 5, 16, 30, tzinfo=ET)))
        self.assertTrue(self.p._market_open_et(dt.datetime(2026, 8, 5, 11, 0, tzinfo=ET)))


class AlertingGuard(unittest.TestCase):
    """GUARD: alert severity must track DECISION VALUE, not event frequency.
    OBLIGATIONS: the FIRST green is URGENT and fires exactly once (it starts the
    cutover clock); a FAIL carries its error code where it can be read; a repeat
    green is quiet. This is the 07-27..08-03 silent-week lesson."""

    def setUp(self):
        self.p = _probe()
        self.sent = []
        self.p._notify = lambda t, b, priority="default", tags="": \
            self.sent.append((t, priority))
        self.td = TemporaryDirectory()
        self.p.STATE_FILE = Path(self.td.name) / "state.json"

    def tearDown(self):
        self.td.cleanup()

    def _run(self, code, msg):
        self.sent.clear()
        self.p.check = lambda port: (code, msg)
        sys.argv = ["probe", "--notify"]
        try:
            return self.p.main()
        except SystemExit as e:
            return e.code

    def test_first_green_is_urgent_and_starts_the_clock(self):
        rc = self._run(0, "PASS ✅ BOTH FEEDS REAL-TIME — acct DUQ000000")
        self.assertEqual(rc, 0)
        title, priority = self.sent[0]
        self.assertEqual(priority, "urgent",
                         "the cutover-clock message must not arrive quietly")
        self.assertIn("GREEN", title)

    def test_second_green_is_quiet_not_repeated_urgency(self):
        self._run(0, "PASS ✅ BOTH FEEDS REAL-TIME")
        self._run(0, "PASS ✅ BOTH FEEDS REAL-TIME")
        self.assertEqual(self.sent[0][1], "low",
                         "a latched green must not re-alarm every day")

    def test_fail_carries_the_error_code_where_it_is_visible(self):
        self._run(1, "FAIL (delayed-only, [10089]) — acct DUQ000000")
        title, priority = self.sent[0]
        self.assertIn("10089", title, "the code must be readable without opening")
        self.assertEqual(priority, "high")

    def test_every_fail_path_exits_non_zero(self):
        self.assertEqual(self._run(1, "FAIL (competing live session, 10197)"), 1)
        self.assertEqual(self._run(2, "GATEWAY DOWN — port 4002 closed"), 2)


if __name__ == "__main__":
    unittest.main()


class _FakeTicker:
    def __init__(self, seq):
        self._seq, self._i = seq, 0
        self.bid = self.ask = self.last = self.close = None
        self._apply()

    def _apply(self):
        q = self._seq[min(self._i, len(self._seq) - 1)]
        self.bid, self.ask, self.last, self.close = (
            q.get("bid"), q.get("ask"), q.get("last"), q.get("close"))

    def advance(self):
        self._i += 1; self._apply()


class _FakeIB:
    """Minimal ib_async stand-in: no socket, scripted quotes."""
    def __init__(self, stock_seq, opt_quote, chain=True):
        self._stock = _FakeTicker(stock_seq)
        self._opt = _FakeTicker([opt_quote])
        self._chain = chain
        self.errorEvent = _Ev()

    def connect(self, *a, **k): pass
    def disconnect(self): pass
    def managedAccounts(self): return ["DUQ000000"]
    def reqMarketDataType(self, n): pass
    def qualifyContracts(self, c): c.conId = 1; return [c]
    def sleep(self, n): self._stock.advance()
    def reqMktData(self, contract, *a, **k):
        return self._opt if getattr(contract, "secType", "") == "OPT" else self._stock
    def reqSecDefOptParams(self, *a, **k):
        if not self._chain:
            return []
        p = type("P", (), {})(); p.exchange = "SMART"
        # Deliberately carries an ALREADY-EXPIRED date first, exactly as
        # reqSecDefOptParams does for a while after an expiry passes. The probe
        # must skip it and take the future one; picking the stale date is what
        # produced the phantom check-(a) failures on 2026-10-02 and 2026-10-09.
        p.expirations = ["20260805", "20991231"]
        p.strikes = [775.0, 776.0, 777.0]
        return [p]


class _Ev:
    def __iadd__(self, fn): return self


# check() sleeps twice before taking its two samples, so a genuinely TICKING
# feed needs three distinct quotes; the frozen case reuses one.
_TICKING = [{"bid": 775.0, "ask": 775.1, "last": 775.05},
            {"bid": 775.2, "ask": 775.3, "last": 775.25},
            {"bid": 775.4, "ask": 775.5, "last": 775.45}]


def _install_fake_ib(monkey_seq, opt_quote, chain=True):
    """Put a fake ib_async in sys.modules so check() imports it."""
    mod = types.ModuleType("ib_async")
    fake = _FakeIB(monkey_seq, opt_quote, chain)
    mod.IB = lambda: fake
    class _C:
        def __init__(self, *a, **k):
            self.secType = "OPT" if a and a[0] == "OPT" else "STK"
            self.conId = None
    mod.Stock = lambda *a, **k: type("S", (), {"secType": "STK", "conId": None})()
    mod.Option = lambda *a, **k: type("O", (), {"secType": "OPT", "conId": None})()
    sys.modules["ib_async"] = mod
    return fake


class BothFeedsGuard(unittest.TestCase):
    """GUARD: PASS requires BOTH stock and OPRA real-time.
    OBLIGATION: equities alone must NOT produce a green — that would start the
    cutover clock while the options data the book trades on is still delayed."""

    def setUp(self):
        self.p = _probe()
        self.p._port_open = lambda port: True
        self.p._market_open_et = lambda now=None: True
        self._orig = sys.modules.get("ib_async")

    def tearDown(self):
        if self._orig is not None:
            sys.modules["ib_async"] = self._orig
        else:
            sys.modules.pop("ib_async", None)

    def test_live_stock_but_DEAD_opra_fails(self):
        _install_fake_ib(_TICKING, {"close": 0.01})   # option: close only = not live
        code, msg = self.p.check(4002)
        self.assertEqual(code, 1, "equities-only must not pass")
        self.assertIn("OPRA", msg)

    def test_both_feeds_live_passes(self):
        _install_fake_ib(_TICKING, {"bid": 2.30, "ask": 2.32})
        code, msg = self.p.check(4002)
        self.assertEqual(code, 0)
        self.assertIn("BOTH FEEDS REAL-TIME", msg)

    def test_frozen_stock_quote_fails_even_with_no_entitlement_error(self):
        """GUARD: a feed that does not tick is not real-time.
        OBLIGATION: FAIL, not a green, when bid/ask/last are identical across
        two polls of the most liquid US equity during the regular session."""
        frozen = {"bid": 775.0, "ask": 775.1, "last": 775.05}
        _install_fake_ib([frozen, frozen], {"bid": 2.30, "ask": 2.32})
        code, msg = self.p.check(4002)
        self.assertEqual(code, 1)
        self.assertIn("FROZEN", msg)

    def test_crossed_opra_quote_fails(self):
        _install_fake_ib(_TICKING, {"bid": 2.40, "ask": 2.30})     # bid > ask
        code, msg = self.p.check(4002)
        self.assertEqual(code, 1)
        self.assertIn("crossed", msg.lower())

    def test_gateway_down_is_inconclusive_not_a_fail_verdict(self):
        self.p._port_open = lambda port: False
        code, msg = self.p.check(4002)
        self.assertEqual(code, 2, "gateway down is inconclusive (2), not FAIL (1)")


class ExpiryBoundaryGuard(unittest.TestCase):
    """GUARD: the probed OPRA contract is never an expiry in the past.

    INCIDENT: `reqSecDefOptParams` keeps just-expired dates in its list, and the
    probe took `sorted(expirations)[0]`. An expired option quotes bid/ask/last =
    None, which is indistinguishable from a missing OPRA entitlement, so check
    (a) reported a phantom entitlement failure: 2026-10-02 probed `20261001`
    (expired the day before) and 2026-10-09 probed `20261008` with the gateway
    healthy and the data line free.
    OBLIGATION: pick the nearest expiry >= today; same-day expiries stay
    eligible (they are live through the close), past ones never are."""

    def setUp(self):
        self.p = _probe()
        # Pin the session clock, exactly as BothFeedsGuard does. The end-to-end
        # test below asserts the market-OPEN verdict, so reading the real clock
        # made it red at every checkpoint except mid_session (09:14 ET pre_open,
        # 16:20 ET post_close, 22:10 ET evening and weekends are all closed).
        self.p._market_open_et = lambda now=None: True
        self._orig = sys.modules.get("ib_async")

    def tearDown(self):
        # The end-to-end tests put a fake ib_async in sys.modules; without this
        # it leaks to every test collected after this class.
        if self._orig is not None:
            sys.modules["ib_async"] = self._orig
        else:
            sys.modules.pop("ib_async", None)

    def test_expiry_dated_today_is_still_eligible(self):
        # 2026-10-08 probing 20261008 was CORRECT — an expiry is live through
        # its own close, so the filter must be >= today, not > today.
        self.assertEqual(
            self.p._nearest_expiry(["20261008", "20261009"], "20261008"),
            "20261008")

    def test_yesterdays_expiry_is_skipped_for_the_next_one(self):
        # The 2026-10-09 failure, pinned: today is the day AFTER 20261008.
        self.assertEqual(
            self.p._nearest_expiry(["20261008", "20261009", "20261010"], "20261009"),
            "20261009")

    def test_the_10_02_failure_exactly(self):
        self.assertEqual(
            self.p._nearest_expiry(["20261001", "20261006"], "20261002"),
            "20261006")

    def test_all_expiries_in_the_past_returns_none_not_a_stale_date(self):
        self.assertIsNone(
            self.p._nearest_expiry(["20261001", "20261008"], "20261009"))

    def test_unsorted_input_still_yields_the_nearest(self):
        self.assertEqual(
            self.p._nearest_expiry(["20261120", "20261012", "20261030"], "20261009"),
            "20261012")

    def test_today_is_an_exchange_date_string(self):
        t = self.p._today_et()
        self.assertRegex(t, r"^\d{8}$")

    def test_probe_never_names_a_past_expiry_end_to_end(self):
        # The fake chain offers an expired 20260805 and a future 20991231; the
        # contract the probe actually reaches for must be the future one.
        _install_fake_ib(_TICKING, {"bid": None, "ask": None, "last": None})
        self.p._port_open = lambda port: True
        code, msg = self.p.check(4002)
        self.assertNotIn("20260805", msg,
                         "probe reached for an already-expired contract")
        self.assertIn("20991231", msg)
        self.assertEqual(code, 1, "a dead option quote is still a FAIL")

    def test_probe_never_names_a_past_expiry_outside_the_session(self):
        # The market-CLOSED branch, covered ON PURPOSE instead of by accident of
        # the wall clock: outside the session check() returns a labelled
        # tentative PASS (code 0), and the contract it names must STILL be the
        # unexpired one. The expiry filter is clock-independent; the verdict is
        # the only thing the session changes.
        _install_fake_ib(_TICKING, {"bid": None, "ask": None, "last": None})
        self.p._market_open_et = lambda now=None: False
        self.p._port_open = lambda port: True
        code, msg = self.p.check(4002)
        self.assertNotIn("20260805", msg,
                         "probe reached for an already-expired contract")
        self.assertIn("20991231", msg)
        self.assertEqual(code, 0, "liveness is unverifiable outside the session")
        self.assertIn("market CLOSED", msg)


class _StallIB:
    """ib_async stand-in that ACCEPTS the connection and then hangs forever.

    This is the 2026-10-05 fault reproduced hermetically: the port accepted and
    the first POST-CONNECT call never returned, so the probe hung ~15 h 21 m
    with no verdict. An absent gateway (ECONNREFUSED) fails fast at _port_open
    and does NOT reproduce it, which is why the stall has to be injected here.

    `managedAccounts()` is the hang point because it is the first post-connect
    call `check()` makes; a bound placed around `connect` alone would not catch it.
    """
    def __init__(self, released: threading.Event):
        self.errorEvent = _Ev()
        self._released = released
        self.reached = threading.Event()

    def connect(self, *a, **k): pass
    def disconnect(self): pass

    def managedAccounts(self):
        self.reached.set()
        self._released.wait()          # never set by the test: hangs on purpose
        return ["DUQ000000"]


class StallGuard(unittest.TestCase):
    """GUARD: a probe that hangs past its bound still produces a verdict, and
    that verdict is INCONCLUSIVE (2) — never a PASS.

    Why 2 and not 0 matters more than the bound itself: exit 0 latches
    `first_pass_at` in the state file and fires the one-shot urgent
    CUTOVER-CLOCK page. A stalled gateway must not be able to start the
    migration clock.

    `check_bounded` is tested directly and `main()` never is: main() calls
    os._exit on the timeout path, which would kill the pytest process and take
    the whole suite with it.
    """
    def setUp(self):
        self.p = _probe()
        self._saved = sys.modules.get("ib_async")
        self._released = threading.Event()
        mod = types.ModuleType("ib_async")
        self.fake = _StallIB(self._released)
        mod.IB = lambda: self.fake
        mod.Stock = lambda *a, **k: type("S", (), {"secType": "STK", "conId": None})()
        mod.Option = lambda *a, **k: type("O", (), {"secType": "OPT", "conId": None})()
        sys.modules["ib_async"] = mod
        self.p._port_open = lambda port: True

    def tearDown(self):
        # Release the abandoned daemon thread so it can unwind instead of
        # lingering for the rest of the suite.
        self._released.set()
        if self._saved is None:
            sys.modules.pop("ib_async", None)
        else:
            sys.modules["ib_async"] = self._saved

    def test_stalled_probe_returns_inconclusive_not_pass(self):
        code, msg, timed_out = self.p.check_bounded(4002, timeout_s=0.5)
        self.assertTrue(timed_out, "the bound did not fire on a hung probe")
        self.assertEqual(code, 2,
                         "a stall must be INCONCLUSIVE — 0 would latch the cutover clock")
        self.assertIn("STALLED", msg)
        self.assertIn("NOT a pass", msg)
        self.assertTrue(self.fake.reached.wait(2),
                        "the probe never reached the post-connect call, so this "
                        "test did not exercise the stall path")

    def test_bound_is_honoured_not_merely_declared(self):
        import time
        t0 = time.monotonic()
        self.p.check_bounded(4002, timeout_s=0.5)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0,
                        f"check_bounded blocked {elapsed:.1f}s past a 0.5s bound")

    def test_a_completing_probe_is_not_marked_timed_out(self):
        # The bound must not change the fast path: release the hang up front so
        # check() runs to its own (gateway-ish) verdict and timed_out is False.
        self._released.set()
        code, msg, timed_out = self.p.check_bounded(4002, timeout_s=10.0)
        self.assertFalse(timed_out, "a returning probe was reported as a timeout")
        self.assertNotIn("STALLED", msg)

    def test_a_raising_probe_is_inconclusive_not_silent(self):
        def _boom(*a, **k):
            raise RuntimeError("socket exploded")
        self.fake.managedAccounts = _boom
        code, msg, timed_out = self.p.check_bounded(4002, timeout_s=10.0)
        self.assertFalse(timed_out)
        self.assertEqual(code, 2, "an exception inside the thread must not read as PASS")
        self.assertIn("CHECK RAISED", msg)
