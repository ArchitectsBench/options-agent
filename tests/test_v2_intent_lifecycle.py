"""SCALP-v2 INTENT LIFECYCLE through poll_fills (2026-09-12, activation night).

The v2 mechanism is a marketable IOC: the order either executes (fully or
partially) or the broker cancels it within milliseconds. Every one of those
three outcomes has to leave the RIGHT trace in the engine's own readers —
today_v2_attempt_count(), today_test_entry_count(), the lane book — because
the frozen forward gate (config/scalp_v2_forward_criteria.yaml) and the daily
caps read those, not agent.log.

Activation-night lesson (commit 60ff608 -> d89fe95): a test that reads the
YAML directly can pass while the engine disagrees. Every assertion here goes
through the engine's readers, never by re-parsing files the engine wrote.

HERMETIC: the broker is a stub; FILLS_LOG / PENDING_FILE / POSITIONS_FILE are
patched to tmp on top of the conftest O-20 seal; helpers that reach for a
quote feed or a parquet are patched out.

Run: .venv/bin/python -m pytest -q tests/test_v2_intent_lifecycle.py
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.engine.paper_trader as pt

ROOT = Path(pt.__file__).resolve().parents[2]

SINGLE_LEG = [{"symbol": "SPY990101C00500000", "action": "buy_to_open",
               "bid": 1.45, "ask": 1.55, "mid": 1.50, "spread": 0.10}]
LIMIT_SENT = 1.55          # v2 marketable limit at aggressiveness 0.5 == the ask
ORDER_ID = 7


def _today_scalp_rec_id(n: int = 1, ticker: str = "SPY") -> str:
    """A rec_id BOTH counters recognise as today's SCALP-lane entry."""
    return f"{datetime.now():%Y%m%d}-SCALP-{n}-{ticker}-0dte_directional"


def _order(status: str, filled: float = 0.0, **extra) -> dict:
    """Normalized order dict in the shape IbkrOrderClient._normalize_order
    emits for a SINGLE-LEG order (no `leg` array; signed by `side`)."""
    o = {"id": ORDER_ID, "status": status, "filled_quantity": float(filled),
         "avg_fill_price": LIMIT_SENT if filled else 0.0,
         "option_symbol": SINGLE_LEG[0]["symbol"], "side": "buy_to_open",
         "create_date": "2026-09-14T13:35:00Z",
         "transaction_date": "2026-09-14T13:35:01Z",
         "tag": "UNVALIDATED-PAPER-ONLY", "symbol": "SPY"}
    o.update(extra)
    return o


class V2LifecycleTest(unittest.TestCase):
    """Tmp book + stub broker. Every subclass test drives pt.poll_fills()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._patches = [
            patch.object(pt, "PENDING_FILE", base / "orders.json"),
            patch.object(pt, "POSITIONS_FILE", base / "positions.json"),
            patch.object(pt, "FILLS_LOG", base / "fills.jsonl"),
            # helpers that reach outside the process: quote feed + parquet
            patch.object(pt, "_mid_at_fill", return_value=None),
            patch.object(pt, "regime_tags", return_value={}),
            # re-reads config.yaml per call; freeze at the documented default
            patch.object(pt, "partial_fill_remainder_cancel_seconds", return_value=60),
        ]
        for p in self._patches:
            p.start()
        self.client = MagicMock(name="broker")
        self.client.available.return_value = True
        self.client.cancel_order.return_value = {"order": {"id": ORDER_ID, "status": "canceled"}}
        self._client_patch = patch.object(pt, "get_order_client", return_value=self.client)
        self._client_patch.start()

    def tearDown(self):
        self._client_patch.stop()
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────────
    def _fills(self, event: str | None = None) -> list[dict]:
        if not pt.FILLS_LOG.exists():
            return []
        rows = [json.loads(l) for l in pt.FILLS_LOG.read_text().splitlines() if l.strip()]
        return [r for r in rows if event is None or r.get("event") == event]

    def _intent(self, **over) -> dict:
        """A v2 open intent exactly as submit_paper_order writes it (stamped
        fill_mode / limit_sent / tif), bound to a broker order id."""
        rec = {
            "intent": "open", "intent_seq": "seq-v2-1", "order_id": ORDER_ID,
            "rec_id": _today_scalp_rec_id(), "ticker": "SPY",
            "structure": "0DTE long call", "strategy": "0dte_directional",
            "direction": "long", "legs": SINGLE_LEG,
            "rec_price": 1.50, "max_profit": None, "max_loss": 1.50,
            "expiration": (datetime.now() + timedelta(days=7)).date().isoformat(),
            "quantity": 1,
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
            "tag": "UNVALIDATED-PAPER-ONLY",
            "fill_mode": "marketable_ioc", "limit_sent": LIMIT_SENT, "tif": "IOC",
        }
        rec.update(over)
        return rec

    def _seed(self, *intents: dict) -> None:
        pt._save(pt.PENDING_FILE, list(intents))

    def _broker_says(self, order: dict | None) -> None:
        self.client.get_order.side_effect = lambda oid: order if int(oid) == ORDER_ID else None


# ═══════════════════════════════════════════════════════════════════════════
#  1. UNFILLED IOC — the design outcome, not a failure
# ═══════════════════════════════════════════════════════════════════════════
class UnfilledIoc(V2LifecycleTest):

    def test_unfilled_ioc_is_one_attempt_and_the_intent_is_gone(self):
        """Prevents: Monday 06:35, the first IOC finds no liquidity and the
        engine either loses the attempt (cap + fill-rate denominator blind) or
        leaves a dead intent resting so the ticker is blocked for 30 minutes
        and later 'expires' an order the broker killed in milliseconds."""
        self._seed(self._intent())
        self._broker_says(_order("canceled", filled=0))

        events = pt.poll_fills()

        # exactly one attempt, read through the ENGINE'S counter
        self.assertEqual(pt.today_v2_attempt_count(), 1)
        rows = self._fills("entry_ioc_unfilled")
        self.assertEqual(len(rows), 1, "exactly one entry_ioc_unfilled row")
        self.assertEqual(rows[0]["rec_id"], _today_scalp_rec_id())
        self.assertEqual(rows[0]["order_id"], ORDER_ID)
        self.assertAlmostEqual(rows[0]["limit_sent"], LIMIT_SENT, places=4)
        self.assertEqual(rows[0]["fill_mode"], "marketable_ioc")
        # the intent is removed: nothing is resting, nothing blocks the name
        self.assertEqual(pt._load(pt.PENDING_FILE), [])
        self.assertEqual(pt.load_positions(), [])
        self.assertIsNone(pt.has_open_exposure("SPY", "0dte_directional", "long", lane="scalp"))
        self.assertEqual(pt.open_paper_count("scalp"), 0)
        # NOT an expiry: no entry_expired row from the poll ...
        self.assertEqual(self._fills("entry_expired"), [])
        self.assertFalse(any(e.get("event") == "entry_expired" for e in events))
        # ... and the stale-entry sweep finds nothing to cancel afterwards
        self.assertEqual(pt.cancel_stale_entries(timeout_min=0), [])
        self.client.cancel_order.assert_not_called()
        self.assertEqual(self._fills("entry_expired"), [])

    def test_unfilled_ioc_does_not_consume_a_filled_entry_slot(self):
        """Prevents: ten unfilled IOCs on a thin morning exhausting the
        max_per_day=10 FILLED-entries cap so the engine 'is done for today'
        with zero positions, while max_attempts_per_day=30 never binds.

        config.yaml documents max_per_day as FILLED entries and says an
        unfilled IOC consumes no fill slot; the pipeline gates on
        today_test_entry_count(). Written to the DOCUMENTED semantics."""
        self._seed(self._intent())
        self._broker_says(_order("canceled", filled=0))

        pt.poll_fills()

        self.assertEqual(pt.today_v2_attempt_count(), 1, "the attempt is counted")
        self.assertEqual(pt.today_test_entry_count(), 0,
                         "an unfilled IOC must not occupy a FILLED-entry slot "
                         "(config: max_per_day counts FILLED entries)")

    def test_ioc_cancel_reported_as_rejected_with_202_is_still_an_attempt(self):
        """Prevents: an all-day IOC machine-gun when IBKR reports the cancel
        with 'Error 202, reqId N: Order Canceled - reason:' — the adapter then
        classifies the ending as 'rejected', and if the engine only counts the
        word 'canceled' the attempt vanishes from today_v2_attempt_count() (so
        max_attempts_per_day can never bind) and from the fill-rate denominator
        (so min_fill_rate reads 100%).

        This is the engine-level requirement: a ZERO-fill terminal v2 open
        intent is an attempt regardless of the status word. An adapter-only
        fix would not satisfy it — deliberately."""
        self._seed(self._intent())
        self._broker_says(_order("rejected", filled=0,
                                 reason="Order Canceled - reason:",
                                 error="Order Canceled - reason:"))

        pt.poll_fills()

        self.assertEqual(pt._load(pt.PENDING_FILE), [], "intent is dropped either way")
        self.assertEqual(pt.load_positions(), [])
        self.assertEqual(pt.today_v2_attempt_count(), 1,
                         "a zero-fill terminal IOC is an ATTEMPT even when the "
                         "broker's cancel arrives dressed as a reject (error 202)")
        self.assertEqual(len(self._fills("entry_ioc_unfilled")), 1)

    def test_replayed_unfilled_ioc_is_not_double_counted(self):
        """Prevents: a restart (deadman / crash) between _log_event and the
        pending checkpoint replaying the same canceled IOC and writing a second
        entry_ioc_unfilled row — double-charging max_attempts_per_day and
        inflating the fill-rate denominator."""
        intent = self._intent()
        # the crash: the row was written, the checkpoint never happened
        pt._log_event({"ts": datetime.now().isoformat(timespec="seconds"),
                       "event": "entry_ioc_unfilled", "rec_id": intent["rec_id"],
                       "ticker": "SPY", "order_id": ORDER_ID,
                       "limit_sent": LIMIT_SENT, "fill_mode": "marketable_ioc",
                       "stamp": "UNVALIDATED — PAPER ONLY"})
        self._seed(intent)
        self._broker_says(_order("canceled", filled=0))

        pt.poll_fills()   # the replay

        self.assertEqual(pt._load(pt.PENDING_FILE), [])
        rows = [r for r in self._fills("entry_ioc_unfilled") if r["rec_id"] == intent["rec_id"]]
        self.assertEqual(len(rows), 1, "replay must be idempotent: one row per rec_id")
        self.assertEqual(pt.today_v2_attempt_count(), 1)


# ═══════════════════════════════════════════════════════════════════════════
#  2. PARTIAL IOC — booked at executed qty, remainder dead, position managed
# ═══════════════════════════════════════════════════════════════════════════
class PartialIoc(V2LifecycleTest):

    def _partial(self, requested: int = 5, executed: float = 2.0):
        self._seed(self._intent(
            quantity=requested,
            submitted_at=(datetime.now() - timedelta(seconds=120)).isoformat(timespec="seconds")))
        self._broker_says(_order("partially_filled", filled=executed))
        return pt.poll_fills()

    def test_partial_ioc_books_executed_qty_cancels_remainder_and_is_managed(self):
        """Prevents: a 5-lot IOC that sweeps 2 contracts (IBKR cancels the
        other 3) being booked at the REQUESTED size (the 08-05 GOOGL qty
        phantom), left pending forever (the adapter reports
        'partially_filled' for a cancelled-after-partial order for good), or
        never entering exit management at all."""
        events = self._partial(requested=5, executed=2.0)

        # booked at BROKER truth, not the request
        positions = pt.load_positions()
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos["rec_id"], _today_scalp_rec_id())
        self.assertEqual(pos["quantity"], 2)
        self.assertTrue(pos.get("partial"))
        self.assertAlmostEqual(pos["entry_price"], LIMIT_SENT, places=4)
        self.assertFalse(pos.get("closing"))
        for k in ("legs", "expiration", "max_loss", "entry_order_id", "opened_at"):
            self.assertIn(k, pos, f"exit manager needs {k}")
        booked = self._fills("partial_fill_booked")
        self.assertEqual(len(booked), 1)
        self.assertEqual((booked[0]["quantity"], booked[0]["requested"]), (2, 5))
        # remainder: already dead at the broker, but the engine must issue the
        # cancel, log it, and DROP the record — no wedge
        self.client.cancel_order.assert_called_once_with(ORDER_ID)
        cancelled = self._fills("partial_remainder_cancelled")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual((cancelled[0]["filled"], cancelled[0]["requested"]), (2, 5))
        self.assertEqual(pt._load(pt.PENDING_FILE), [], "no pending record survives")
        self.assertEqual({e["event"] for e in events},
                         {"partial_fill_booked", "partial_remainder_cancelled"})
        # the position is MANAGED: the lane sees it, exposure is held
        self.assertEqual(pt.open_paper_count("scalp"), 1)
        self.assertIn(pos["rec_id"],
                      pt.has_open_exposure("SPY", "0dte_directional", "long", lane="scalp") or "")
        self.assertTrue(pt.rec_id_already_live(pos["rec_id"]))
        # a second poll on the now-empty pending book changes nothing
        self.assertEqual(pt.poll_fills(), [])
        self.assertEqual(pt.load_positions()[0]["quantity"], 2)

    def test_partial_ioc_is_v2_evidence(self):
        """Prevents: a real v2 partial fill (the COMMON IOC outcome for
        multi-lot sizing) vanishing from the attempt cap and from the frozen
        gate's sample — no paper_fill is written, the booking event carries no
        fill_mode, so today_v2_attempt_count() ignores it and
        scalp_forward_check --v2 later files the round trip as wrong_fill_mode
        and ioc_fill_rate counts it as neither filled nor unfilled."""
        self._partial(requested=5, executed=2.0)

        self.assertEqual(pt.today_v2_attempt_count(), 1,
                         "a partially-filled IOC is one attempt (and one fill)")
        booked = self._fills("partial_fill_booked")[0]
        self.assertEqual(booked.get("fill_mode"), "marketable_ioc",
                         "the booking event must carry the v2 scope key")

    def test_partial_ioc_counts_in_the_frozen_fill_rate_numerator(self):
        """Prevents: the frozen min_fill_rate criterion being computed on the
        all-or-nothing subsample only — a 2-of-5 fill must be a FILL in
        scripts/scalp_forward_check.ioc_fill_rate, read off the ledger the
        engine actually wrote."""
        sys.path.insert(0, str(ROOT / "scripts"))
        import importlib
        sfc = importlib.import_module("scalp_forward_check")
        self._partial(requested=5, executed=2.0)
        crit = {"scope": {"fill_mode": "marketable_ioc", "ledger": str(pt.FILLS_LOG)},
                "window_start": None, "min_fill_rate": 0.25}
        rate = sfc.ioc_fill_rate(crit, [])
        self.assertEqual((rate["filled"], rate["unfilled"]), (1, 0),
                         "partial fill must be in the numerator, not invisible")


# ═══════════════════════════════════════════════════════════════════════════
#  3. FULL FILL — paper_fill carries the v2 scope keys
# ═══════════════════════════════════════════════════════════════════════════
class FullIocFill(V2LifecycleTest):

    def test_full_fill_paper_fill_carries_fill_mode_limit_sent_and_tif(self):
        """Prevents: a v2 fill landing in the ledger without fill_mode /
        limit_sent / tif, so scalp_forward_check --v2 throws it out as
        wrong_fill_mode and the attempt counter never sees it — a session of
        real v2 executions yielding zero v2 evidence."""
        self._seed(self._intent())
        self._broker_says(_order("filled", filled=1.0))

        events = pt.poll_fills()

        fills = self._fills("paper_fill")
        self.assertEqual(len(fills), 1)
        ev = fills[0]
        self.assertEqual(ev["rec_id"], _today_scalp_rec_id())
        self.assertEqual(ev["fill_mode"], "marketable_ioc")
        self.assertAlmostEqual(ev["limit_sent"], LIMIT_SENT, places=4)
        self.assertEqual(ev["tif"], "IOC")
        self.assertEqual(ev["quantity"], 1)
        self.assertAlmostEqual(ev["fill_price"], LIMIT_SENT, places=4)
        self.assertEqual([e["event"] for e in events], ["paper_fill"])
        # both caps see it, through the engine's readers
        self.assertEqual(pt.today_v2_attempt_count(), 1)
        self.assertEqual(pt.today_test_entry_count(), 1)
        # booked and managed; intent gone; nothing v2-unfilled was written
        self.assertEqual(pt._load(pt.PENDING_FILE), [])
        pos = pt.load_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["quantity"], 1)
        self.assertFalse(pos[0].get("partial"))
        self.assertEqual(self._fills("entry_ioc_unfilled"), [])
        self.assertEqual(pt.open_paper_count("scalp"), 1)

    def test_replayed_full_fill_does_not_duplicate_position_or_attempt(self):
        """Prevents: a crash-replay of a booked IOC fill creating a second
        position or a second paper_fill row (double-charging both caps)."""
        self._seed(self._intent())
        self._broker_says(_order("filled", filled=1.0))
        pt.poll_fills()
        self._seed(self._intent())            # the replay: same intent back in pending
        pt.poll_fills()
        self.assertEqual(len(pt.load_positions()), 1)
        self.assertEqual(len(self._fills("paper_fill")), 1)
        self.assertEqual(pt.today_v2_attempt_count(), 1)
        self.assertEqual(pt._load(pt.PENDING_FILE), [])


# ═══════════════════════════════════════════════════════════════════════════
#  4. v1 (resting DAY) intent — the guarantee that v2 events stay v2-only
# ═══════════════════════════════════════════════════════════════════════════
class V1RestingIntentCanceled(V2LifecycleTest):

    def test_v1_canceled_intent_emits_no_entry_ioc_unfilled(self):
        """Prevents: a v1 resting-mid DAY order that gets cancelled (signal
        expiry, EOD) being logged as an IOC non-fill — polluting the frozen
        v2 fill-rate denominator and the attempt cap with v1 traffic."""
        self._seed(self._intent(fill_mode="resting_mid", tif="DAY", limit_sent=1.50))
        self._broker_says(_order("canceled", filled=0))

        pt.poll_fills()

        self.assertEqual(self._fills("entry_ioc_unfilled"), [])
        self.assertEqual(pt.today_v2_attempt_count(), 0)
        self.assertEqual(pt._load(pt.PENDING_FILE), [])
        self.assertEqual(pt.load_positions(), [])

    def test_legacy_intent_without_fill_mode_key_is_v1(self):
        """Prevents: a pre-activation intent (written before fill_mode existed,
        still pending across the restart) being mis-filed as a v2 attempt."""
        legacy = self._intent()
        for k in ("fill_mode", "limit_sent", "tif"):
            legacy.pop(k)
        self._seed(legacy)
        self._broker_says(_order("canceled", filled=0))

        pt.poll_fills()

        self.assertEqual(self._fills("entry_ioc_unfilled"), [])
        self.assertEqual(pt.today_v2_attempt_count(), 0)
        self.assertEqual(pt._load(pt.PENDING_FILE), [])

    def test_v1_full_fill_is_stamped_resting_mid_not_ioc(self):
        """Prevents: a v1 fill (fill_mode absent on the intent) defaulting into
        the v2 scope — paper_fill must carry resting_mid / DAY so the v2 gate
        excludes it as wrong_fill_mode, exactly as the frozen scope demands."""
        legacy = self._intent()
        for k in ("fill_mode", "limit_sent", "tif"):
            legacy.pop(k)
        self._seed(legacy)
        self._broker_says(_order("filled", filled=1.0))

        pt.poll_fills()

        ev = self._fills("paper_fill")[0]
        self.assertEqual(ev["fill_mode"], "resting_mid")
        self.assertEqual(ev["tif"], "DAY")
        self.assertIsNone(ev["limit_sent"])
        self.assertEqual(pt.today_v2_attempt_count(), 0)
        self.assertEqual(pt.today_test_entry_count(), 1)


# ═══════════════════════════════════════════════════════════════════════════
#  marketable_day (2026-09-16) — the same lifecycle, but the broker kills it
# ═══════════════════════════════════════════════════════════════════════════
class MarketableDayAtTheBoundary(V2LifecycleTest):
    """marketable_day rests a DAY limit at the natural and the ENGINE cancels
    it at entry_timeout_min. But the broker can still terminate it first (a
    reject, or IB cancelling it). That path is the silent-accounting trap: it
    was gated on fill_mode == "marketable_ioc", so a dead marketable_day order
    dropped its intent and logged NOTHING — the attempt vanished from the cap
    and from the fill-rate denominator."""

    def _day_intent(self, **over):
        return self._intent(fill_mode="marketable_day", tif="DAY", **over)

    def test_broker_cancel_of_a_day_entry_is_still_one_attempt(self):
        self._seed(self._day_intent())
        self._broker_says(_order("canceled", filled=0))

        pt.poll_fills()

        self.assertEqual(pt.today_v2_attempt_count(), 1)
        rows = self._fills("entry_ioc_unfilled")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fill_mode"], "marketable_day",
                         "the row must carry the mode, so reports never blend the two")
        self.assertEqual(pt._load(pt.PENDING_FILE), [], "no dead intent left resting")
        self.assertIsNone(pt.has_open_exposure("SPY", "0dte_directional", "long", lane="scalp"))

    def test_broker_reject_of_a_day_entry_is_still_one_attempt(self):
        self._seed(self._day_intent())
        self._broker_says(_order("rejected", filled=0))

        pt.poll_fills()

        self.assertEqual(pt.today_v2_attempt_count(), 1)
        self.assertEqual(len(self._fills("entry_ioc_unfilled")), 1)

    def test_a_filled_day_entry_books_and_occupies_a_fill_slot(self):
        self._seed(self._day_intent())
        self._broker_says(_order("filled", filled=1.0))

        pt.poll_fills()

        ev = self._fills("paper_fill")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["fill_mode"], "marketable_day")
        self.assertEqual(pt.today_v2_attempt_count(), 1, "a fill is also an attempt")
        self.assertEqual(pt.today_test_entry_count(), 1, "and it DOES take a fill slot")
        self.assertEqual(len(pt.load_positions()), 1)


if __name__ == "__main__":
    unittest.main()
