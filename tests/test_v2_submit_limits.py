"""SCALP-v2 submit path: v2_entry_limit edge cases, intent stamps, the pre-submit
guards under v2, the three POST outcomes, and the Tradier client's tif contract.

Every test is HERMETIC: the broker is a capturing stub, the books and the
fills ledger live in a per-test tmp dir (PENDING_FILE / POSITIONS_FILE /
FILLS_LOG patched on top of the conftest O-20 seal). Behaviour is asserted
through the engine's own readers (_load(PENDING_FILE), rec_id_already_live,
today_v2_attempt_count) — never by re-parsing files by hand.

Tests marked @unittest.expectedFailure document a VERIFIED src defect: the
docstring states the correct behaviour, the assertion encodes it, and the
test fails today. Fixing the defect turns them into an 'unexpected success',
which is the prompt to remove the marker.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import src.engine.paper_trader as pt
from src.data.tradier_orders import PaperOrderClient

TODAY = f"{datetime.now():%Y%m%d}"
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
V2_CFG = {"enabled": True, "read_failed": False, "fill_mode": "marketable_ioc",
          "aggressiveness": 0.5, "min_net_debit": 1.0, "max_per_day": 10,
          "max_attempts_per_day": 30}


def _single(**leg_over):
    leg = {"symbol": "SPY990101C00500000", "action": "buy_to_open",
           "bid": 1.45, "ask": 1.55, "mid": 1.50, "spread": 0.10}
    leg.update(leg_over)
    for k in [k for k, v in leg.items() if v is _ABSENT]:
        del leg[k]
    return {"structure": "0DTE long call", "strategy": "0dte_directional", "direction": "long",
            "expiration": "2099-01-01", "net_debit": leg.get("mid", 1.50),
            "max_loss": 1.50, "max_profit": None, "legs": [leg]}


_ABSENT = object()


def _spread(legs, net_debit=1.00):
    return {"structure": "call debit spread", "strategy": "debit_spread", "direction": "long",
            "expiration": "2099-01-01", "net_debit": net_debit, "max_loss": net_debit,
            "max_profit": 1.00, "legs": legs}


def _rec(trade, n=1, ticker="SPY"):
    return {"rec_id": f"{TODAY}-SCALP-{n}-{ticker}-x", "ticker": ticker, "trade": trade}


class _Client:
    """Capturing broker stub. `result` is what place_*_order returns."""
    def __init__(self, result=None):
        self.calls = []
        self.result = {"order": {"id": 7, "status": "open"}} if result is None else result

    def available(self):
        return True

    def place_option_order(self, *a, **k):
        self.calls.append(("single", a, k))
        return self.result

    def place_spread_order(self, *a, **k):
        self.calls.append(("spread", a, k))
        return self.result


# ───────────────────────────────── v2_entry_limit ─────────────────────────────
class V2EntryLimitEdges(unittest.TestCase):
    """Prevents a mis-priced IOC (too low -> never fills; too high -> pays more
    than the natural) when a leg summary arrives with an unusual quote shape."""

    def test_missing_spread_uses_ask_minus_bid(self):
        """Prevents a leg without a `spread` key being sent at the MID as an IOC
        (which never fills) — bid/ask must reconstruct the spread."""
        t = _single(spread=_ABSENT)                         # bid 1.45 / ask 1.55
        self.assertAlmostEqual(pt.v2_entry_limit(t, 0.5), 1.55, places=4)

    def test_missing_spread_and_quotes_refuses_instead_of_pricing_at_mid(self):
        """RESOLVED 2026-09-12 (two writers disagreed): a leg with no spread AND
        no bid/ask cannot be priced marketably. Falling back to the mid would
        send an IOC at v1's price with none of v1's patience — a guaranteed
        miss that burns an attempt. Fail closed: refuse."""
        t = _single(); t["legs"] = [{"symbol": "SPY990101C00500000", "action": "buy_to_open", "mid": 1.50}]
        with self.assertRaises((ValueError, KeyError)):
            pt.v2_entry_limit(t, 0.5)

    def test_explicit_zero_spread_reconstructs_from_quotes(self):
        """Prevents a stale `spread: 0` on a leg silently turning a marketable
        order into a resting-mid one when bid/ask say the market is wide."""
        t = _single(spread=0.0)                             # ask-bid = 0.10
        self.assertAlmostEqual(pt.v2_entry_limit(t, 0.5), 1.55, places=4)

    def test_floor_at_one_cent(self):
        """Prevents a zero/sub-penny limit (broker reject) on a near-worthless
        contract: the floor is $0.01, never below."""
        t = _single(mid=0.004, bid=0.0, ask=0.008, spread=0.008)
        self.assertEqual(pt.v2_entry_limit(t, 0.0), 0.01)
        self.assertEqual(pt.v2_entry_limit(t, 1.0), 0.01)   # 0.012 -> rounds to 0.01
        self.assertEqual(pt.v2_entry_limit(_single(mid=0.0, spread=0.0, bid=0.0, ask=0.0), 0.5), 0.01)

    def test_spread_legs_sum_all_leg_spreads_regardless_of_side(self):
        """Prevents a debit spread being priced off the long leg only: the combo
        fills at the NET NBBO, so the sell leg's spread widens the net too."""
        legs = [{"symbol": "SPY990101C00500000", "action": "buy_to_open", "mid": 2.00, "spread": 0.10},
                {"symbol": "SPY990101C00502000", "action": "sell_to_open", "mid": 1.00,
                 "bid": 0.97, "ask": 1.03}]                 # no spread key -> 0.06 from quotes
        self.assertAlmostEqual(pt.v2_entry_limit(_spread(legs), 0.5), 1.08, places=4)
        self.assertAlmostEqual(pt.v2_entry_limit(_spread(legs), 0.0), 1.00, places=4)
        self.assertAlmostEqual(pt.v2_entry_limit(_spread(legs), 1.0), 1.16, places=4)

    def test_four_leg_condor_sums_every_leg(self):
        """Prevents a condor's two untouched wings being ignored in the net
        spread (each leg contributes to the net NBBO)."""
        legs = [{"symbol": f"SPY990101{cp}0050{i}000", "action": act, "mid": 1.0, "spread": 0.02}
                for i, (cp, act) in enumerate([("P", "buy_to_open"), ("P", "sell_to_open"),
                                               ("C", "sell_to_open"), ("C", "buy_to_open")])]
        self.assertAlmostEqual(pt.v2_entry_limit(_spread(legs, net_debit=0.50), 0.5), 0.54, places=4)

    def test_rounds_to_cents(self):
        """Prevents a sub-penny limit (e.g. 1.5505) reaching the broker — IBKR
        rejects options limits that are not on the penny/nickel grid."""
        t = _single(mid=1.501, spread=0.10)                 # 1.501 + 0.05 = 1.551
        v = pt.v2_entry_limit(t, 0.5)
        self.assertEqual(v, round(v, 2))
        self.assertAlmostEqual(v, 1.55, places=4)
        t2 = _single(mid=1.234, spread=0.011)               # 1.2395 -> 1.24
        self.assertEqual(pt.v2_entry_limit(t2, 0.5), 1.24)

    def test_aggressiveness_zero_is_mid_and_one_is_full_spread_above_mid(self):
        """Prevents the aggressiveness dial being silently clamped: 0 == v1's
        mid, 1 == mid + the whole spread (half a spread ABOVE the ask)."""
        t = _single()                                       # mid 1.50, spread 0.10
        self.assertAlmostEqual(pt.v2_entry_limit(t, 0.0), 1.50, places=4)
        self.assertAlmostEqual(pt.v2_entry_limit(t, 1.0), 1.60, places=4)
        self.assertGreater(pt.v2_entry_limit(t, 1.0), t["legs"][0]["ask"])


# ───────────────────────────── submit_paper_order ─────────────────────────────
class _SubmitCase(unittest.TestCase):
    """Books + ledger in tmp; broker stubbed; v2 config injected."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        d = Path(self._td.name)
        self._patches = [
            patch.object(pt, "PENDING_FILE", d / "orders.json"),
            patch.object(pt, "POSITIONS_FILE", d / "positions.json"),
            patch.object(pt, "FILLS_LOG", d / "fills.jsonl"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._td.cleanup()

    def _submit(self, rec, client, v2cfg=V2_CFG, **kw):
        with patch.object(pt, "get_order_client", return_value=client), \
                patch.object(pt, "scalp_v2_config", return_value=dict(v2cfg)):
            return pt.submit_paper_order(rec, quantity=1, **kw)

    def _pending(self):
        return pt._load(pt.PENDING_FILE)


class IntentStamps(_SubmitCase):
    """Prevents a fill whose mechanism cannot be told from its intent record —
    the v1/v2 evidence split lives on these three keys."""

    def test_v1_intent_is_resting_mid_day(self):
        """Prevents v1 (the CLOSED_FAILED record) drifting when v2 is off."""
        c = _Client()
        out = self._submit(_rec(_single()), c)
        self.assertTrue(out["submitted"])
        (intent,) = self._pending()
        self.assertEqual(intent["fill_mode"], "resting_mid")
        self.assertEqual(intent["tif"], "DAY")
        self.assertEqual(intent["limit_sent"], round(1.50, 4))
        self.assertEqual(intent["order_id"], 7)
        kind, a, k = c.calls[0]
        self.assertEqual(k["tif"], "DAY")
        self.assertAlmostEqual(a[4], 1.50, places=4)

    def test_v2_intent_is_ioc_at_the_marketable_limit(self):
        """Prevents a v2 order being stamped v2 but sent at v1's price."""
        c = _Client()
        out = self._submit(_rec(_single()), c, fill_mode="marketable_ioc")
        self.assertTrue(out["submitted"])
        (intent,) = self._pending()
        self.assertEqual(intent["fill_mode"], "marketable_ioc")
        self.assertEqual(intent["tif"], "IOC")
        self.assertEqual(intent["limit_sent"], pt.v2_entry_limit(_single(), 0.5))
        self.assertEqual(intent["order_id"], 7)
        kind, a, k = c.calls[0]
        self.assertEqual(k["tif"], "IOC")
        self.assertAlmostEqual(a[4], 1.55, places=4)

    def test_v2_spread_intent_sends_ioc_with_net_limit(self):
        """Prevents a v2 combo silently resting as a DAY order."""
        c = _Client()
        legs = [{"symbol": "SPY990101C00500000", "action": "buy_to_open", "mid": 2.00, "spread": 0.10},
                {"symbol": "SPY990101C00502000", "action": "sell_to_open", "mid": 1.00, "spread": 0.06}]
        self._submit(_rec(_spread(legs)), c, fill_mode="marketable_ioc")
        kind, a, k = c.calls[0]
        self.assertEqual(kind, "spread")
        self.assertEqual(k["tif"], "IOC")
        self.assertAlmostEqual(a[2], 1.08, places=4)
        (intent,) = self._pending()
        self.assertEqual(intent["tif"], "IOC")
        self.assertAlmostEqual(intent["limit_sent"], 1.08, places=4)

    def test_configured_aggressiveness_reaches_the_broker(self):
        """Prevents the operator's aggressiveness dial being ignored at submit
        time: 0 sends the mid, 1 sends mid + the full spread."""
        for agg, want in ((0.0, 1.50), (1.0, 1.60)):
            c = _Client()
            cfg = dict(V2_CFG, aggressiveness=agg)
            self._submit(_rec(_single(), n=int(agg * 10) + 100), c, v2cfg=cfg, fill_mode="marketable_ioc")
            kind, a, k = c.calls[0]
            self.assertAlmostEqual(a[4], want, places=4, msg=f"aggressiveness={agg}")
            self.assertEqual(k["tif"], "IOC")

    def test_intent_is_on_disk_before_the_broker_post(self):
        """Prevents an invisible live order after a crash mid-POST: the
        write-ahead intent must exist (unbound) when the broker is called."""
        seen = {}

        class _Peek(_Client):
            def place_option_order(self, *a, **k):
                seen["pending_at_post"] = pt._load(pt.PENDING_FILE)
                return super().place_option_order(*a, **k)

        self._submit(_rec(_single()), _Peek(), fill_mode="marketable_ioc")
        (at_post,) = seen["pending_at_post"]
        self.assertIsNone(at_post["order_id"])
        self.assertEqual(at_post["fill_mode"], "marketable_ioc")
        self.assertEqual(at_post["tif"], "IOC")


class GuardsStillRefuseUnderV2(_SubmitCase):
    """The pre-submit guards run BEFORE the fill_mode branch; v2 must not be a
    door around them."""

    def test_expired_contract_is_refused_before_any_broker_call(self):
        """Prevents a v2 IOC into a lapsed contract (cannot fill, cannot be
        managed) — seen live 2026-08-07 as a month-old expiry on a card."""
        c = _Client()
        t = _single()
        t["expiration"] = YESTERDAY
        with self.assertLogs("trader", level="ERROR") as cm:
            out = self._submit(_rec(t), c, fill_mode="marketable_ioc")
        self.assertEqual(out["refused"], "expiry_in_past")
        self.assertFalse(out["submitted"])
        self.assertEqual(c.calls, [])
        self.assertEqual(self._pending(), [])
        self.assertTrue(any("REFUSING ORDER" in m for m in cm.output))
        # the refusal is on the ledger, so the day's audit can see it
        rows = [json.loads(l) for l in pt.FILLS_LOG.read_text().splitlines()]
        self.assertEqual([r["event"] for r in rows], ["order_refused_expired"])
        # ... and it is NOT an IOC attempt
        self.assertEqual(pt.today_v2_attempt_count(), 0)
    def test_refused_expiry_does_not_consume_a_filled_entry_slot(self):
        """DOCUMENTED DEFECT (hunter: 'max_per_day counts every ledger row with
        today's SCALP rec_id'): prevents a refusal that never reached the
        broker eating one of the ten FILLED-entry slots. The expiry guard
        writes an order_refused_expired row carrying the rec_id; the reader
        the pipeline gates on (today_test_entry_count) counts that rec_id as a
        placed entry. Correct behaviour: a refused order is 0 fills."""
        t = _single()
        t["expiration"] = YESTERDAY
        with self.assertLogs("trader", level="ERROR"):
            out = self._submit(_rec(t, n=8), _Client(), fill_mode="marketable_ioc")
        self.assertEqual(out["refused"], "expiry_in_past")
        self.assertEqual(pt.today_test_entry_count(), 0)

    def test_duplicate_rec_id_with_pending_order_is_refused(self):
        """Prevents two IOCs for one decision: the second submit for a rec_id
        that already has a pending order is refused, no broker call."""
        c = _Client()
        rec = _rec(_single())
        self.assertTrue(self._submit(rec, c, fill_mode="marketable_ioc")["submitted"])
        out = self._submit(rec, c, fill_mode="marketable_ioc")
        self.assertEqual(out, {"submitted": False, "refused": "duplicate_rec_id"})
        self.assertEqual(len(c.calls), 1)
        self.assertEqual(len(self._pending()), 1)

    def test_duplicate_rec_id_with_open_position_is_refused(self):
        """Prevents re-entering a rec_id that already fills (the 'same card
        repeatedly' bug) even when the pending book is empty."""
        rec = _rec(_single(), n=9)
        pt._save(pt.POSITIONS_FILE, [{"rec_id": rec["rec_id"], "ticker": "SPY",
                                      "strategy": "0dte_directional", "direction": "long",
                                      "structure": "0DTE long call", "quantity": 1}])
        c = _Client()
        out = self._submit(rec, c, fill_mode="marketable_ioc")
        self.assertEqual(out["refused"], "duplicate_rec_id")
        self.assertEqual(c.calls, [])
        self.assertEqual(self._pending(), [])

    def test_unavailable_client_places_nothing_and_leaves_no_intent(self):
        """Prevents an orphan write-ahead intent when the broker is offline."""
        class _Down(_Client):
            def available(self):
                return False
        c = _Down()
        self.assertIsNone(self._submit(_rec(_single()), c, fill_mode="marketable_ioc"))
        self.assertEqual(c.calls, [])
        self.assertEqual(self._pending(), [])


class DefinitiveRejectDropsTheIntent(_SubmitCase):
    """Prevents a dead intent lingering in the pending book (blocking
    has_open_exposure and the restart gate) after the broker said 'no order'."""

    def _assert_dropped(self, result, fill_mode="marketable_ioc"):
        c = _Client(result=result)
        rec = _rec(_single(), n=3)
        with self.assertLogs("trader", level="WARNING") as cm:
            out = self._submit(rec, c, fill_mode=fill_mode)
        self.assertEqual(len(c.calls), 1)
        self.assertFalse(out["submitted"])
        self.assertNotIn("ambiguous", out)
        self.assertEqual(self._pending(), [])
        self.assertFalse(pt.rec_id_already_live(rec["rec_id"]))
        self.assertTrue(any("paper entry rejected" in m for m in cm.output))
        return out

    def test_order_status_rejected_drops(self):
        self._assert_dropped({"order": {"id": 5, "status": "rejected"}, "reason": "Invalid TIF"})

    def test_http_error_envelope_drops(self):
        self._assert_dropped({"error": "400 Bad Request", "status": 400})

    def test_v1_reject_drops_too(self):
        self._assert_dropped({"order": {"id": 5, "status": "rejected"}}, fill_mode="resting_mid")
    def test_ioc_cancel_at_post_time_still_counts_as_an_attempt(self):
        """DOCUMENTED DEFECT (hunter: 'unfilled IOC classified rejected'):
        prevents an all-day IOC machine-gun — when the exchange cancel of an
        unfilled v2 IOC lands inside the broker POST (IB error 202 'Order
        Canceled - reason:' during _settle), place_*_order returns a reject
        envelope, submit_paper_order drops the intent and writes NOTHING to the
        ledger. today_v2_attempt_count() stays 0, so max_attempts_per_day can
        never bind and the same setup re-fires every 5-minute cycle. The
        correct behaviour: a v2 IOC that went to the broker and came back
        unfilled is an ATTEMPT, whatever word the adapter used. Only the
        counter is asserted here — HOW the fix records it (drop-then-log, or
        leave the intent for poll_fills) is deliberately unconstrained."""
        c = _Client(result={"order": {"id": 5, "status": "rejected"},
                            "reason": "Order Canceled - reason:"})
        self._submit(_rec(_single(), n=3), c, fill_mode="marketable_ioc")
        self.assertEqual(len(c.calls), 1)
        self.assertEqual(pt.today_v2_attempt_count(), 1)


class AmbiguousLeavesTheIntentUnbound(_SubmitCase):
    """Prevents a live order being hidden (or blindly resubmitted) after a POST
    that got no broker answer: the intent stays, unbound, for resolve_intents."""

    def _assert_unbound(self, result):
        c = _Client(result=result)
        rec = _rec(_single(), n=4)
        with self.assertLogs("trader", level="WARNING") as cm:
            out = self._submit(rec, c, fill_mode="marketable_ioc")
        self.assertEqual(len(c.calls), 1)
        self.assertEqual(out["submitted"], False)
        self.assertTrue(out["ambiguous"])
        (intent,) = self._pending()
        self.assertIsNone(intent["order_id"])
        self.assertEqual(intent["rec_id"], rec["rec_id"])
        self.assertEqual(intent["fill_mode"], "marketable_ioc")
        self.assertEqual(intent["tif"], "IOC")
        self.assertTrue(pt.rec_id_already_live(rec["rec_id"]))
        self.assertTrue(any("AMBIGUOUS" in m for m in cm.output))
        # a second submit for the same decision is refused while it is unresolved
        c2 = _Client()
        self.assertEqual(self._submit(rec, c2, fill_mode="marketable_ioc")["refused"], "duplicate_rec_id")
        self.assertEqual(c2.calls, [])

    def test_transport_error_is_ambiguous(self):
        self._assert_unbound({"error": "timed out", "transport_error": True})

    def test_non_dict_response_is_ambiguous(self):
        self._assert_unbound("<html>502 Bad Gateway</html>")

    def test_success_shape_without_order_id_is_ambiguous(self):
        self._assert_unbound({"order": {"status": "open"}})


class V2ConfigMustBeTrustedAtSubmit(_SubmitCase):
    """submit_paper_order re-reads agent.scalp_v2 from DISK for aggressiveness;
    that read must be honoured, not defaulted."""
    def test_torn_config_read_refuses_rather_than_defaulting_aggressiveness(self):
        """DOCUMENTED DEFECT (hunter: 'read_failed has zero consumers'):
        prevents a torn/unparseable config.yaml mid-session quietly sending a
        v2 IOC at the DEFAULT aggressiveness (0.5) instead of the operator's.
        scalp_v2_config() flags read_failed=True but submit_paper_order never
        looks at it. Correct behaviour: refuse (no broker call, no intent)."""
        c = _Client()
        with patch.object(pt, "get_order_client", return_value=c), \
                patch.object(pt.yaml, "safe_load", side_effect=RuntimeError("torn yaml")):
            out = pt.submit_paper_order(_rec(_single(), n=5), quantity=1, fill_mode="marketable_ioc")
        self.assertEqual(c.calls, [], "must not POST on a failed config read")
        self.assertEqual(self._pending(), [])
        self.assertFalse((out or {}).get("submitted"))
    def test_unknown_fill_mode_is_refused_not_downgraded_to_day(self):
        """DOCUMENTED DEFECT (hunter: 'fill_mode is an unvalidated free string'):
        prevents a fill_mode typo ('ioc', 'marketable-ioc') running v1 resting
        DAY orders at mid under a v2-labelled intent — every such fill is then
        wrong_fill_mode to the tracker and invisible to the attempt cap.
        Correct behaviour: an unrecognised fill_mode is refused with no broker
        call and no intent; the stamped mode may never disagree with the
        mechanism sent."""
        c = _Client()
        out = self._submit(_rec(_single(), n=6), c, fill_mode="ioc")
        self.assertEqual(c.calls, [], "must not send a DAY order under an unknown mode")
        self.assertEqual(self._pending(), [])
        self.assertFalse((out or {}).get("submitted"))

    def test_unknown_fill_mode_never_stamps_a_mechanism_it_did_not_send(self):
        """Prevents the intent lying about its mechanism: whatever fill_mode
        string arrives, the stamped tif must match what went to the broker."""
        c = _Client()
        self._submit(_rec(_single(), n=6), c, fill_mode="ioc")
        if c.calls:                       # current behaviour: falls back to DAY
            kind, a, k = c.calls[0]
            (intent,) = self._pending()
            self.assertEqual(intent["tif"], k["tif"])
            self.assertAlmostEqual(intent["limit_sent"], a[4], places=4)


# ───────────────────────────── Tradier client tif ─────────────────────────────
class TradierClientAcceptsTif(unittest.TestCase):
    """Prevents a TypeError the first time the v2 path (tif=...) reaches the
    Tradier adapter — and pins that Tradier still gets duration=day (IOC is not
    modelled there; the tif contract is IBKR's)."""

    def setUp(self):
        self._env = patch.dict(os.environ, {"TRADIER_SANDBOX_TOKEN": "t-test",
                                            "TRADIER_SANDBOX_ACCOUNT_ID": "VA000000"})
        self._env.start()
        self.sent = []

        def _capture(_self, method, path, data=None):
            self.sent.append((method, path, data))
            return {"order": {"id": 42, "status": "ok"}}
        self._req = patch.object(PaperOrderClient, "_request", _capture)
        self._req.start()

    def tearDown(self):
        self._req.stop()
        self._env.stop()

    def test_single_leg_refuses_ioc_and_sends_day_only(self):
        """RESOLVED 2026-09-12: the sandbox has no IOC. Sending a DAY order under
        v2's name would trade v1 rules behind a v2 label, so the client REFUSES
        tif=IOC (a definitive reject, nothing rests) and only DAY goes out."""
        c = PaperOrderClient()
        self.assertTrue(c.available())
        out = c.place_option_order("SPY", "SPY990101C00500000", "buy_to_open", 1, 1.55, tif="IOC")
        self.assertIn("error", out); self.assertNotIn("order", out)
        self.assertEqual(self.sent, [], "an IOC must never be POSTed to Tradier as a DAY order")
        out = c.place_option_order("SPY", "SPY990101C00500000", "buy_to_open", 1, 1.55, tif="DAY")
        self.assertEqual(out["order"]["id"], 42)
        (method, path, data), = self.sent
        self.assertEqual(method, "POST"); self.assertIn("/orders", path)
        self.assertEqual(data["duration"], "day"); self.assertEqual(data["price"], "1.55")
        self.assertEqual(data["type"], "limit"); self.assertNotIn("tif", data)
    def test_spread_refuses_ioc_and_sends_day_only(self):
        """Same contract for the multileg path."""
        c = PaperOrderClient()
        legs = [{"option_symbol": "SPY990101C00500000", "side": "buy_to_open", "quantity": 1},
                {"option_symbol": "SPY990101C00502000", "side": "sell_to_open", "quantity": 1}]
        out = c.place_spread_order("SPY", legs, 1.08, order_type="debit", tif="IOC")
        self.assertIn("error", out); self.assertNotIn("order", out)
        self.assertEqual(self.sent, [])
        out = c.place_spread_order("SPY", legs, 1.08, order_type="debit", tif="DAY")
        self.assertEqual(out["order"]["id"], 42)
        (method, path, data), = self.sent
        self.assertEqual(method, "POST"); self.assertEqual(data["duration"], "day")
        self.assertEqual(data["price"], "1.08"); self.assertNotIn("tif", data)
    def test_default_tif_unchanged(self):
        """The v1 call shape (no tif kwarg) still works byte-identically."""
        c = PaperOrderClient()
        c.place_option_order("SPY", "SPY990101C00500000", "buy_to_open", 1, 1.50)
        (_, _, data), = self.sent
        self.assertEqual(data["duration"], "day")


if __name__ == "__main__":
    unittest.main()
