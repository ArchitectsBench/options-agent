"""Tests for the pure position-reconciliation drift classifier
(src/engine/reconcile_drift.py). Plain dict fixtures — NO broker connection.

Covers every drift class:
  - in_sync
  - broker_lost               (the L20 case)
  - orphan_at_broker          (manual-position protection when adopt_foreign=False)
  - orphan_at_broker + adopt  (adopt_foreign=True)
  - quantity_mismatch         (partial-fill / manual-adjust)
  - empty-both                (no positions either side)
  - multi-leg spread with a mixed classification
  - signed short-leg handling

Run: .venv/bin/python -m pytest tests/test_reconcile_drift.py -x -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.engine.reconcile_drift import reconcile_drift, underlying_of  # noqa: E402

# Realistic OCC symbols (ROOT + YYMMDD + C/P + 8-digit strike, 15-char suffix).
NVDA_C = "NVDA260717C00130000"   # engine-tracked long call
SPY_P = "SPY260717P00450000"     # engine-tracked short put leg
HON_C = "HON260717C00220000"     # user's MANUAL position (foreign)
GOOGL_C = "GOOGL260717C00180000"  # user's MANUAL position (foreign)


def _engine_pos(rec_id, ticker, legs, quantity=1, **extra):
    p = {"rec_id": rec_id, "ticker": ticker, "quantity": quantity, "legs": legs}
    p.update(extra)
    return p


def _broker_pos(symbol, quantity, cost_basis=0.0):
    return {"symbol": symbol, "quantity": quantity, "cost_basis": cost_basis}


# --------------------------------------------------------------------------- #
# in_sync
# --------------------------------------------------------------------------- #
def test_in_sync_matches_and_agrees():
    engine = [_engine_pos("2026-07-17-001", "NVDA",
                          [{"symbol": NVDA_C, "action": "buy_to_open"}], quantity=10)]
    broker = [_broker_pos(NVDA_C, 10.0, cost_basis=1300.0)]

    diff = reconcile_drift(engine, broker)

    assert diff["drift"] == []
    assert diff["actions"] == []
    assert len(diff["matched"]) == 1
    m = diff["matched"][0]
    assert m["class"] == "in_sync"
    assert m["symbol"] == NVDA_C
    assert m["engine_qty"] == 10.0
    assert m["broker_qty"] == 10.0
    assert m["rec_id"] == "2026-07-17-001"


# --------------------------------------------------------------------------- #
# broker_lost (L20)
# --------------------------------------------------------------------------- #
def test_broker_lost_when_broker_absent():
    engine = [_engine_pos("2026-07-13-003", "SMCI",
                          [{"symbol": NVDA_C, "action": "buy_to_open"}], quantity=10)]
    broker = []  # broker no longer holds it

    diff = reconcile_drift(engine, broker)

    assert diff["matched"] == []
    assert len(diff["drift"]) == 1
    d = diff["drift"][0]
    assert d["class"] == "broker_lost"
    assert d["action"] == "flag_broker_lost"
    assert d["rec_id"] == "2026-07-13-003"
    assert d["engine_qty"] == 10.0
    assert d["broker_qty"] == 0.0
    assert d["detail"]["lesson"] == "L20"
    assert len(diff["actions"]) == 1
    assert diff["actions"][0].startswith("[broker_lost]")
    assert "stop close-retries" in diff["actions"][0]


def test_broker_lost_when_broker_qty_zero():
    # A broker read that returns the symbol flat (qty 0) is still "no longer has".
    engine = [_engine_pos("2026-07-13-004", "IWM",
                          [{"symbol": NVDA_C, "action": "buy_to_open"}], quantity=5)]
    broker = [_broker_pos(NVDA_C, 0.0)]

    diff = reconcile_drift(engine, broker)

    assert [d["class"] for d in diff["drift"]] == ["broker_lost"]
    assert diff["matched"] == []


# --------------------------------------------------------------------------- #
# orphan_at_broker — manual-position protection
# --------------------------------------------------------------------------- #
def test_orphan_left_untouched_when_adopt_foreign_false():
    # Broker holds the user's manual HON position; engine tracks nothing.
    engine = []
    broker = [_broker_pos(HON_C, 3.0, cost_basis=6600.0)]

    diff = reconcile_drift(engine, broker, adopt_foreign=False)

    assert diff["matched"] == []
    assert len(diff["drift"]) == 1
    d = diff["drift"][0]
    assert d["class"] == "orphan_at_broker"
    assert d["action"] == "leave_untouched"        # never flagged, never closed
    assert d["rec_id"] is None                      # engine doesn't track it
    assert d["ticker"] == "HON"
    assert d["symbol"] == HON_C
    assert d["engine_qty"] == 0.0
    assert d["broker_qty"] == 3.0
    assert "manual position" in d["detail"]["protection"]
    assert "left untouched" in diff["actions"][0]


def test_orphan_adopted_when_adopt_foreign_true():
    engine = []
    broker = [_broker_pos(HON_C, 3.0, cost_basis=6600.0)]

    diff = reconcile_drift(engine, broker, adopt_foreign=True)

    d = diff["drift"][0]
    assert d["class"] == "orphan_at_broker"
    assert d["action"] == "adopt"
    assert d["detail"]["adopt_foreign"] is True
    assert "adopting into book" in diff["actions"][0]


def test_manual_positions_never_flagged_or_closed_default():
    # Multiple manual tickers + one legit engine position that IS broker_lost.
    # Assert the manual legs are only ever leave_untouched, never flagged/closed.
    engine = [_engine_pos("2026-07-13-003", "SMCI",
                          [{"symbol": NVDA_C, "action": "buy_to_open"}], quantity=10)]
    broker = [
        _broker_pos(HON_C, 3.0),      # manual
        _broker_pos(GOOGL_C, 1.0),    # manual
        # NVDA (engine) absent -> broker_lost
    ]

    diff = reconcile_drift(engine, broker, adopt_foreign=False)

    orphans = [d for d in diff["drift"] if d["class"] == "orphan_at_broker"]
    assert {o["ticker"] for o in orphans} == {"HON", "GOOGL"}
    # The invariant: no orphan ever receives a flag/close action.
    for o in orphans:
        assert o["action"] == "leave_untouched"
        assert o["rec_id"] is None
    # And the only flag_broker_lost is the engine-tracked SMCI leg.
    flagged = [d for d in diff["drift"] if d["action"] == "flag_broker_lost"]
    assert len(flagged) == 1
    assert flagged[0]["symbol"] == NVDA_C


# --------------------------------------------------------------------------- #
# quantity_mismatch
# --------------------------------------------------------------------------- #
def test_quantity_mismatch_partial_fill():
    engine = [_engine_pos("2026-07-17-002", "NVDA",
                          [{"symbol": NVDA_C, "action": "buy_to_open"}], quantity=10)]
    broker = [_broker_pos(NVDA_C, 5.0)]  # only 5 filled / manually reduced

    diff = reconcile_drift(engine, broker)

    assert diff["matched"] == []
    assert len(diff["drift"]) == 1
    d = diff["drift"][0]
    assert d["class"] == "quantity_mismatch"
    assert d["action"] == "reconcile_quantity"
    assert d["engine_qty"] == 10.0
    assert d["broker_qty"] == 5.0
    assert d["detail"]["delta"] == -5.0
    assert "!=" in diff["actions"][0]


# --------------------------------------------------------------------------- #
# empty-both
# --------------------------------------------------------------------------- #
def test_empty_both():
    diff = reconcile_drift([], [])
    assert diff == {"matched": [], "drift": [], "actions": []}


# --------------------------------------------------------------------------- #
# signed short-leg handling
# --------------------------------------------------------------------------- #
def test_short_leg_sign_in_sync():
    # sell_to_open qty 4 -> engine_qty -4; broker short -4 -> in_sync.
    engine = [_engine_pos("2026-07-17-003", "SPY",
                          [{"symbol": SPY_P, "action": "sell_to_open"}], quantity=4)]
    broker = [_broker_pos(SPY_P, -4.0)]

    diff = reconcile_drift(engine, broker)

    assert len(diff["matched"]) == 1
    assert diff["matched"][0]["engine_qty"] == -4.0
    assert diff["drift"] == []


def test_short_leg_sign_flip_is_mismatch():
    # Engine short -4 but broker shows +4 (sign flip) -> quantity_mismatch.
    engine = [_engine_pos("2026-07-17-004", "SPY",
                          [{"symbol": SPY_P, "action": "sell_to_open"}], quantity=4)]
    broker = [_broker_pos(SPY_P, 4.0)]

    diff = reconcile_drift(engine, broker)

    assert [d["class"] for d in diff["drift"]] == ["quantity_mismatch"]
    d = diff["drift"][0]
    assert d["engine_qty"] == -4.0
    assert d["broker_qty"] == 4.0


# --------------------------------------------------------------------------- #
# multi-leg spread with mixed classification
# --------------------------------------------------------------------------- #
def test_multileg_spread_one_leg_lost_one_in_sync():
    # A vertical: long NVDA_C in_sync, short SPY_P broker_lost.
    engine = [_engine_pos("2026-07-17-005", "NVDA", [
        {"symbol": NVDA_C, "action": "buy_to_open"},
        {"symbol": SPY_P, "action": "sell_to_open"},
    ], quantity=2)]
    broker = [_broker_pos(NVDA_C, 2.0)]  # only the long leg remains

    diff = reconcile_drift(engine, broker)

    classes = {m["symbol"]: m["class"] for m in diff["matched"]}
    classes.update({d["symbol"]: d["class"] for d in diff["drift"]})
    assert classes[NVDA_C] == "in_sync"
    assert classes[SPY_P] == "broker_lost"
    # one drift entry -> one action line
    assert len(diff["actions"]) == len(diff["drift"]) == 1


# --------------------------------------------------------------------------- #
# structural invariants
# --------------------------------------------------------------------------- #
def test_actions_parallel_to_drift():
    engine = [
        _engine_pos("a", "NVDA", [{"symbol": NVDA_C, "action": "buy_to_open"}], quantity=10),
        _engine_pos("b", "SPY", [{"symbol": SPY_P, "action": "sell_to_open"}], quantity=3),
    ]
    broker = [
        _broker_pos(NVDA_C, 5.0),   # quantity_mismatch
        _broker_pos(HON_C, 1.0),    # orphan
        # SPY_P absent -> broker_lost
    ]
    diff = reconcile_drift(engine, broker, adopt_foreign=False)
    # exactly one action string per drift record
    assert len(diff["actions"]) == len(diff["drift"])
    seen = {d["class"] for d in diff["drift"]}
    assert seen == {"quantity_mismatch", "broker_lost", "orphan_at_broker"}


def test_underlying_of_helper():
    assert underlying_of(NVDA_C) == "NVDA"
    assert underlying_of(GOOGL_C) == "GOOGL"
    assert underlying_of("SHORT") == "SHORT"  # non-OCC fallback
