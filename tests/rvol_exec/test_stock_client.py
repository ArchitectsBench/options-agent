"""IbkrStockClient against the FakeIB — spec §13 contract, §8 construction,
§16.2 `test_stock_client.py` matrix. Hermetic: no socket, no API.

UNVALIDATED — PAPER ONLY.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from ib_async import Order, OrderStatus, Stock, Trade

from src.data import ibkr_stock_orders as iso
from src.data.ibkr_stock_orders import (IbkrStockClient, fifo_positions,
                                        is_short_unavailable_reason, role_of)
from src.data.order_client import (AMBIGUOUS, DEFINITIVE_REJECT, OK,  # test-side only
                                   classify_post_result)
from src.rvol_exec.constants import (CLIENT_ID, ET, OPTIONS_ENGINE_TAG, RECONNECT_ATTEMPTS,
                                     RECONNECT_WAIT_S, SHORT_LOCATE_WAIT_S, TAG)
from tests.rvol_exec.fake_ib import (CANCEL_NOTICE, LOCATE_HELD_MSG, PRICE_INCREMENT_MSG,
                                     SHORT_REJECT_MSG, FakeIB)

SYM = "ABCD"
DAY = datetime(2026, 9, 14, tzinfo=ET)
T0 = DAY.replace(hour=9, minute=40)
GTD = DAY.replace(hour=10, minute=25)
OCA = f"RVX-20260914-{SYM}"


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start

    def now_et(self):
        return self.t.astimezone(ET)

    def now_utc(self):
        return self.t.astimezone(timezone.utc)

    def sleep(self, secs: float):
        self.t += timedelta(seconds=secs)

    def set(self, dt: datetime):
        self.t = dt


def _fake(**kw) -> tuple[FakeIB, FakeClock]:
    clock = FakeClock(T0)
    fake = FakeIB(clock=clock, **kw)
    fake.set_mark(SYM, 50.00)
    return fake, clock


def _client(fake, clock, **kw) -> IbkrStockClient:
    return IbkrStockClient(ib=fake, clock=clock, **kw)


def _entry(client, side="long", qty=10, level=50.10, limit=50.13, stop=49.50, tif="GTD"):
    return client.place_stop_limit_entry_with_stop_child(
        SYM, side, qty, level, limit, stop, OCA, tif=tif, gtd_et=GTD)


# ── rails ────────────────────────────────────────────────────────────────────

def test_paper_d_account_activates():
    fake, clock = _fake()
    c = _client(fake, clock)
    assert c.available() is True
    assert c.account == "DUQ000000"
    assert fake.connect_calls == [CLIENT_ID]


def test_non_d_account_never_activates():
    fake, clock = _fake(accounts=["U1234567"])
    c = _client(fake, clock)
    assert c.available() is False
    assert _entry(c) is None
    assert c.get_own_orders() is None            # UNKNOWN, never []
    assert c.get_own_executions() is None
    assert c.get_own_positions() is None
    assert c.foreign_stk_symbols() is None
    assert fake.placed == []


def test_under_pytest_without_injected_ib_never_opens_socket(monkeypatch):
    monkeypatch.delenv("ALLOW_LIVE_GATEWAY_TESTS", raising=False)

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("a real IB() was constructed under pytest")

    with patch.object(iso, "IB", Boom):
        c = IbkrStockClient()                      # connect=True by default
    assert c.available() is False
    assert c.get_own_orders() is None


def test_connect_uses_only_clientid_61_no_fallback():
    fake, clock = _fake()
    fake.in_use = {CLIENT_ID}
    c = _client(fake, clock)
    assert c.available() is False
    c.connect()
    c.connect()
    assert fake.connect_calls == [CLIENT_ID, CLIENT_ID, CLIENT_ID]
    assert set(fake.connect_calls) == {CLIENT_ID}, "a fallback clientId family is forbidden (F4)"
    fake.in_use = set()
    assert c.connect() is True


def test_reconnect_is_bounded_and_waits_on_the_same_id():
    fake, clock = _fake()
    c = _client(fake, clock)
    fake.down = True
    fake.disconnect()
    assert c.available() is False
    t_before = clock.t
    assert c.reconnect() is False
    attempts = fake.connect_calls[1:]
    assert len(attempts) == RECONNECT_ATTEMPTS and set(attempts) == {CLIENT_ID}
    assert (clock.t - t_before).total_seconds() == (RECONNECT_ATTEMPTS - 1) * RECONNECT_WAIT_S
    fake.down = False
    assert c.reconnect() is True
    assert c.available() is True


def test_disconnected_event_flips_available():
    fake, clock = _fake()
    c = _client(fake, clock)
    fake.disconnect()
    assert c.available() is False
    assert c.gateway_lost is True
    assert c.reconnect() is True
    assert c.gateway_lost is False


def test_scheduled_disconnect_then_recovery():
    fake, clock = _fake()
    c = _client(fake, clock)
    fake.disconnect_at(T0 + timedelta(minutes=5), for_s=120)
    fake.advance(T0 + timedelta(minutes=5))
    assert c.available() is False
    assert c.connect() is False                   # still down
    clock.t = T0 + timedelta(minutes=8)
    fake.advance(clock.t)
    assert c.connect() is True


def test_logger_is_lane_local_not_trader():
    assert iso.logger.name == "rvol_exec"
    assert TAG != OPTIONS_ENGINE_TAG


# ── construction (§8.1) ──────────────────────────────────────────────────────

def test_entry_parent_and_child_exact_construction():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c)
    assert "order" in res, res
    o = res["order"]
    assert o["id"] == o["parent_id"] and o["child_id"] != o["parent_id"]
    assert classify_post_result(res)["status"] == OK
    parent, child = fake.placed[0], fake.placed[1]
    p, ch = parent.order, child.order
    assert p.orderType == "STP LMT" and p.action == "BUY"
    assert p.auxPrice == 50.10 and p.lmtPrice == 50.13 and p.totalQuantity == 10
    assert p.tif == "GTD" and p.goodTillDate == "20260914 10:25:00 US/Eastern"
    assert p.orderRef == TAG and p.account == "DUQ000000"
    assert p.outsideRth is False and p.triggerMethod == 2 and p.transmit is False
    assert ch.orderType == "STP" and ch.action == "SELL" and ch.auxPrice == 49.50
    assert ch.totalQuantity == 10 and ch.parentId == p.orderId
    assert ch.ocaGroup == OCA and ch.ocaType == 1
    assert ch.tif == "DAY" and ch.orderRef == TAG and ch.account == "DUQ000000"
    assert ch.outsideRth is False and ch.triggerMethod == 2 and ch.transmit is True
    assert parent.contract.secType == "STK" and parent.contract.exchange == "SMART"
    assert parent.contract.currency == "USD" and parent.contract.conId > 0
    assert o["status"] == "pending"                # PreSubmitted (stop held at IB)


def test_short_entry_mirrors_actions():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c, side="short", level=49.90, limit=49.87, stop=50.50)
    assert "order" in res
    p, ch = fake.placed[0].order, fake.placed[1].order
    assert p.action == "SELL" and ch.action == "BUY"
    assert p.auxPrice == 49.90 and p.lmtPrice == 49.87 and ch.auxPrice == 50.50


def test_tif_day_has_no_gtd():
    fake, clock = _fake()
    c = _client(fake, clock)
    _entry(c, tif="DAY")
    p = fake.placed[0].order
    assert p.tif == "DAY" and p.goodTillDate == ""


def test_gtd_without_gtd_et_is_a_definitive_error():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = c.place_stop_limit_entry_with_stop_child(SYM, "long", 10, 50.1, 50.13, 49.5, OCA,
                                                   tif="GTD", gtd_et=None)
    assert classify_post_result(res)["status"] == DEFINITIVE_REJECT
    assert fake.placed == []


def test_qty_below_one_and_unknown_symbol_are_definitive():
    fake, clock = _fake()
    fake.unknown_symbols.add("ZZZZ")
    c = _client(fake, clock)
    assert classify_post_result(_entry(c, qty=0))["status"] == DEFINITIVE_REJECT
    res = c.place_stop_limit_entry_with_stop_child("ZZZZ", "long", 1, 5.1, 5.13, 4.9, "g",
                                                   tif="DAY")
    assert res["reason"] == "qualify"
    assert classify_post_result(res)["status"] == DEFINITIVE_REJECT
    assert c.qualify("ZZZZ") is None
    assert fake.placed == []


def test_every_placed_order_is_tagged():
    fake, clock = _fake()
    c = _client(fake, clock)
    _entry(c)
    fake.advance(T0, {SYM: [50.11]})
    c.flatten_market(SYM, 10, "long", OCA)
    assert len(fake.placed) == 3
    assert all(t.order.orderRef == TAG for t in fake.placed)
    assert all(t.order.outsideRth is False for t in fake.placed)


# ── lifecycle: fills, stop child, OCA ────────────────────────────────────────

def test_fill_activates_child_and_positions_come_from_executions():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c)
    pid, cid = res["order"]["parent_id"], res["order"]["child_id"]
    fake.advance(T0 + timedelta(seconds=30), {SYM: [50.00, 50.05, 50.11]})
    orders = {d["id"]: d for d in c.get_own_orders()}
    assert orders[pid]["status"] == "filled" and orders[pid]["avg_fill_price"] == 50.11
    assert orders[cid]["status"] == "open" and orders[cid]["remaining"] == 10
    assert orders[cid]["role"] == "stop" and orders[pid]["role"] == "entry"
    pos = c.get_own_positions()
    assert pos[SYM]["qty"] == 10 and pos[SYM]["avg_px"] == 50.11
    ex = c.get_own_executions()
    assert len(ex) == 1 and ex[0]["side"] == "BOT" and ex[0]["role"] == "entry"
    assert ex[0]["commission"] == 1.0 and ex[0]["tag"] == TAG
    # stop touched -> child fills at print - slip; net flat; FIFO realized
    fake.advance(T0 + timedelta(minutes=2), {SYM: [49.80, 49.40]})
    pos = c.get_own_positions()
    assert pos[SYM]["qty"] == 0
    assert pos[SYM]["realized"] == pytest.approx((49.39 - 50.11) * 10)
    assert pos[SYM]["commissions"] == 2.0
    assert pos[SYM]["realized_net"] == pytest.approx((49.39 - 50.11) * 10 - 2.0)
    assert c.get_own_orders(open_only=True) == []
    assert FakeIB.net_by_symbol(TAG) == {SYM: 0.0}


def test_trigger_print_beyond_limit_leaves_parent_live_unfilled():
    fake, clock = _fake()
    c = _client(fake, clock)
    pid = _entry(c)["order"]["parent_id"]
    fake.advance(T0, {SYM: [50.30]})               # gap through the guardrail
    st = c.order_status(pid)
    assert st["status"] == "open" and st["filled"] == 0
    assert FakeIB.executions(TAG) == []
    fake.advance(T0 + timedelta(seconds=10), {SYM: [50.12]})  # retrace within the limit
    assert c.order_status(pid)["status"] == "filled"           # the fill the lane must prevent


def test_cancelled_parent_never_fills_and_cancels_child_202_not_reject():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c)
    pid, cid = res["order"]["parent_id"], res["order"]["child_id"]
    out = c.cancel(pid)
    assert out["order"]["status"] == "canceled" and "error" not in out
    assert c.order_status(cid)["status"] == "canceled"
    assert c._reject_reason(c._find_trade(pid)) is None    # 202 empty reason is NOT a reject
    assert c.errors[pid][0] == 202 and c.errors[pid][1] == CANCEL_NOTICE
    fake.advance(T0, {SYM: [50.12]})
    assert FakeIB.executions(TAG) == []


def test_partial_fill_then_cancel_child_quantity_by_knob():
    for knob, expect in (({}, 4), ({"child_stays_full_qty": True}, 10),
                         ({"drop_child_on_cancel": True}, "canceled")):
        FakeIB.reset_broker()
        fake, clock = _fake()
        for k, v in knob.items():
            setattr(fake, k, v)
        fake.partial(SYM, 4)
        c = _client(fake, clock)
        res = _entry(c)
        pid, cid = res["order"]["parent_id"], res["order"]["child_id"]
        fake.advance(T0, {SYM: [50.11, 50.12]})
        st = c.order_status(pid)
        assert st["status"] == "partially_filled" and st["filled"] == 4 and st["remaining"] == 6
        assert c.get_own_positions()[SYM]["qty"] == 4
        c.cancel(pid)
        assert c.order_status(pid)["status"] == "canceled"
        child = c.order_status(cid)
        if expect == "canceled":
            assert child["status"] == "canceled", knob
        else:
            assert child["status"] == "open" and child["qty"] == expect, knob


def test_amend_stop_modifies_same_order_and_rolls_back_on_refusal():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c)
    cid = res["order"]["child_id"]
    fake.advance(T0, {SYM: [50.11]})
    n_placed = len(fake.placed)
    out = c.amend_stop(cid, 49.61)
    assert out["order"]["id"] == cid and out["order"]["aux_price"] == 49.61
    assert len(fake.placed) == n_placed                       # a modify is not a new order
    assert c.order_status(cid)["aux_price"] == 49.61
    out = c.amend_stop(cid, 49.70, new_qty=4)
    assert out["order"]["qty"] == 4 and c.order_status(cid)["qty"] == 4
    fake.amend_rejected(SYM)
    out = c.amend_stop(cid, 49.90)
    assert "error" in out and out["code"] == 201
    assert c.order_status(cid)["aux_price"] == 49.70            # rolled back
    fake._amend_rejected.clear()
    fake.advance(T0 + timedelta(minutes=1), {SYM: [49.60]})   # stop hit
    out = c.amend_stop(cid, 49.00)
    assert out["error"] == "not_modifiable"


def test_amend_and_cancel_raise_are_ambiguous():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c)
    pid, cid = res["order"]["parent_id"], res["order"]["child_id"]
    with patch.object(fake, "placeOrder", side_effect=TimeoutError("write timeout")):
        out = c.amend_stop(cid, 49.60)
    assert out["transport_error"] is True and classify_post_result(out)["status"] == AMBIGUOUS
    with patch.object(fake, "cancelOrder", side_effect=TimeoutError("write timeout")):
        out = c.cancel(pid)
    assert out["transport_error"] is True


def test_flatten_joins_oca_and_fill_cancels_stop():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c)
    cid = res["order"]["child_id"]
    fake.advance(T0, {SYM: [50.11]})
    out = c.flatten_market(SYM, 10, "long", OCA)
    assert out["mode"] == "oca" and out["child_cancel_required"] is False
    mkt = fake.placed[-1].order
    assert mkt.orderType == "MKT" and mkt.action == "SELL" and mkt.totalQuantity == 10
    assert mkt.ocaGroup == OCA and mkt.ocaType == 1 and mkt.tif == "DAY"
    assert mkt.orderRef == TAG and mkt.outsideRth is False and mkt.transmit is True
    assert c.order_status(cid)["status"] == "open"            # stop still live until the fill
    fake.tick(SYM, 50.20)
    assert c.order_status(out["order"]["id"])["status"] == "filled"
    assert c.order_status(cid)["status"] == "canceled"        # OCA cancelled the stop
    assert c.get_own_positions()[SYM]["qty"] == 0
    assert c.get_own_orders(open_only=True) == []


def test_flatten_short_position_buys():
    fake, clock = _fake()
    c = _client(fake, clock)
    _entry(c, side="short", level=49.90, limit=49.87, stop=50.50)
    fake.advance(T0, {SYM: [49.89]})
    assert c.get_own_positions()[SYM]["qty"] == -10
    out = c.flatten_market(SYM, 10, "short", OCA)
    assert fake.placed[-1].order.action == "BUY"
    fake.tick(SYM, 49.80)
    assert c.get_own_positions()[SYM]["qty"] == 0
    assert c.get_own_positions()[SYM]["realized"] == pytest.approx((49.89 - 49.81) * 10)


def test_flatten_oca_join_rejected_falls_back_to_plain_mkt():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c)
    cid = res["order"]["child_id"]
    fake.advance(T0, {SYM: [50.11]})
    fake.oca_join_rejected = True
    out = c.flatten_market(SYM, 10, "long", OCA)
    assert out["mode"] == "plain" and out["child_cancel_required"] is True
    assert "oca_join_reject" in out
    plain = fake.placed[-1].order
    assert plain.orderType == "MKT" and plain.ocaGroup == "" and plain.orderRef == TAG
    fake.tick(SYM, 50.20)
    assert c.order_status(out["order"]["id"])["status"] == "filled"
    assert c.order_status(cid)["status"] == "open"            # the lane must cancel it now
    assert c.cancel(cid)["order"]["status"] == "canceled"


def test_orphan_stop_after_flatten_and_reversed_net_race():
    fake, clock = _fake()
    fake.orphan_stop_after_flatten = True
    c = _client(fake, clock)
    cid = _entry(c)["order"]["child_id"]
    fake.advance(T0, {SYM: [50.11]})
    c.flatten_market(SYM, 10, "long", OCA)
    fake.tick(SYM, 50.20)
    assert c.order_status(cid)["status"] == "open"            # IB "forgot" -> lane force-cancels
    assert c.cancel(cid)["order"]["status"] == "canceled"

    FakeIB.reset_broker()
    fake, clock = _fake()
    fake.stop_and_mkt_both_fill = True
    c = _client(fake, clock)
    _entry(c)
    fake.advance(T0, {SYM: [50.11]})
    c.flatten_market(SYM, 10, "long", OCA)
    fake.advance(T0 + timedelta(seconds=5), {SYM: [49.40]})   # stop AND mkt both execute
    assert c.get_own_positions()[SYM]["qty"] == -10           # F27: reversed own net


def test_gtd_expiry_cancels_parent_and_child_as_cancel_not_reject():
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c)
    pid, cid = res["order"]["parent_id"], res["order"]["child_id"]
    fake.advance(GTD - timedelta(seconds=1))
    assert c.order_status(pid)["status"] == "pending"
    fake.advance(GTD)
    assert c.order_status(pid)["status"] == "canceled"
    assert c.order_status(cid)["status"] == "canceled"
    assert c.order_status(pid)["reject_reason"] is None


def test_day_child_expires_at_1600():
    fake, clock = _fake()
    c = _client(fake, clock)
    cid = _entry(c, tif="DAY")["order"]["child_id"]
    fake.advance(T0, {SYM: [50.11]})
    fake.advance(DAY.replace(hour=16, minute=0))
    assert c.order_status(cid)["status"] == "canceled"


# ── rejects / short / 404 / 110 ──────────────────────────────────────────────

def test_reject_201_is_definitive_and_not_short():
    fake, clock = _fake()
    fake.reject(SYM, 201, "Order rejected - reason: insufficient buying power")
    c = _client(fake, clock)
    res = _entry(c)
    assert res["order"]["status"] == "rejected" and res["code"] == 201
    assert "buying power" in res["error"]
    assert res.get("short_unavailable") is not True
    assert classify_post_result(res)["status"] == DEFINITIVE_REJECT
    assert c.order_status(res["order"]["child_id"])["status"] == "canceled"


def test_short_unavailable_201_wording_flagged():
    fake, clock = _fake()
    fake.short_unavailable(SYM, "201")
    c = _client(fake, clock)
    res = _entry(c, side="short", level=49.90, limit=49.87, stop=50.50)
    assert res["short_unavailable"] is True and res["error"] == SHORT_REJECT_MSG
    assert classify_post_result(res)["status"] == DEFINITIVE_REJECT
    # a LONG on the same name is unaffected
    FakeIB.reset_broker()
    fake, clock = _fake()
    fake.short_unavailable(SYM, "201")
    c = _client(fake, clock)
    assert "order" in _entry(c) and _entry(c).get("short_unavailable") is not True


def test_short_regex_three_reason_shapes():
    assert is_short_unavailable_reason(201, SHORT_REJECT_MSG)
    assert is_short_unavailable_reason(10147, "Order rejected: no shortable shares to locate")
    assert is_short_unavailable_reason(404, LOCATE_HELD_MSG)
    assert is_short_unavailable_reason(404, "")
    assert not is_short_unavailable_reason(201, "Order rejected - reason: insufficient buying power")
    assert not is_short_unavailable_reason(202, CANCEL_NOTICE)
    assert not is_short_unavailable_reason(None, None)


def test_404_held_wait_then_cancel_or_release():
    fake, clock = _fake()
    fake.short_unavailable(SYM, "404")
    c = _client(fake, clock)
    res = _entry(c, side="short", level=49.90, limit=49.87, stop=50.50)
    pid, cid = res["order"]["parent_id"], res["order"]["child_id"]
    assert res["order"]["held"] is True and pid in c.held and c.errors[pid][0] == 404
    assert c.order_status(pid)["status"] == "pending" and c.order_status(pid)["why_held"] == "locate"
    assert c.locate_expired(pid) is False
    fake.advance(T0, {SYM: [49.80]})                # would trigger, but it is HELD
    assert FakeIB.executions(TAG) == []
    clock.sleep(SHORT_LOCATE_WAIT_S - 1)
    assert c.locate_expired(pid) is False
    clock.sleep(2)
    assert c.locate_expired(pid) is True
    out = c.cancel(pid)
    assert out["order"]["status"] == "canceled" and c.order_status(cid)["status"] == "canceled"
    assert pid not in c.held

    FakeIB.reset_broker()
    fake, clock = _fake()
    fake.short_unavailable(SYM, "404", release_after_s=10)
    c = _client(fake, clock)
    pid = _entry(c, side="short", level=49.90, limit=49.87, stop=50.50)["order"]["parent_id"]
    assert pid in c.held
    clock.sleep(10)
    fake.advance(clock.t, {SYM: [49.89]})           # released, triggers, fills within the limit
    assert pid not in c.held
    assert c.order_status(pid)["status"] == "filled"
    assert c.get_own_positions()[SYM]["qty"] == -10


def test_error_110_rerounds_and_retries_exactly_once():
    fake, clock = _fake()
    fake.reject(SYM, 110, PRICE_INCREMENT_MSG)
    c = _client(fake, clock)
    res = _entry(c, level=50.1000001, limit=50.13, stop=49.5)
    assert "order" in res and res["order"]["status"] == "pending"
    assert len(fake.placed) == 4                     # rejected pair + retried pair
    FakeIB.reset_broker()
    fake, clock = _fake()
    fake.reject(SYM, 110, PRICE_INCREMENT_MSG).reject(SYM, 110, PRICE_INCREMENT_MSG)
    c = _client(fake, clock)
    res = _entry(c)
    assert res["order"]["status"] == "rejected" and res["code"] == 110
    assert len(fake.placed) == 4                     # never a third attempt


def test_transport_error_before_and_after_booking_is_ambiguous():
    fake, clock = _fake()
    fake.transport_error_on_place(SYM, live=False)
    c = _client(fake, clock)
    res = _entry(c)
    assert res["transport_error"] is True
    assert classify_post_result(res)["status"] == AMBIGUOUS
    assert FakeIB.book() == []                       # nothing live
    fake.transport_error_on_place(SYM, live=True)
    res = _entry(c)
    assert classify_post_result(res)["status"] == AMBIGUOUS
    live = c.get_own_orders()                        # broker truth finds the live parent
    assert [d["role"] for d in live] == ["entry"]
    assert live[0]["raw_status"] == "PendingSubmit"  # untransmitted (child never sent)


def test_phantom_filled_with_zero_quantity_is_canceled():
    fake, clock = _fake()
    c = _client(fake, clock)
    o = Order(orderId=7, permId=99, action="BUY", totalQuantity=5, orderType="STP LMT",
              orderRef=TAG)
    t = Trade(contract=Stock(SYM, "SMART", "USD"), order=o,
              orderStatus=OrderStatus(orderId=7, status="Filled", filled=0.0, remaining=0.0),
              fills=[], log=[])
    assert c._status_of(t) == "canceled"


# ── ownership filters (§0) ───────────────────────────────────────────────────

def _seed_foreign(fake):
    fake.foreign_orders([
        {"symbol": "SPY", "secType": "OPT", "orderRef": OPTIONS_ENGINE_TAG, "clientId": 42,
         "action": "BUY", "orderType": "LMT", "qty": 1, "lmtPrice": 1.25},
        {"symbol": "QQQ", "secType": "BAG", "orderRef": OPTIONS_ENGINE_TAG, "clientId": 42,
         "action": "BUY", "orderType": "LMT", "qty": 2, "lmtPrice": 0.80},
        {"symbol": "HON", "secType": "STK", "orderRef": "", "clientId": 0,
         "action": "SELL", "orderType": "LMT", "qty": 100, "lmtPrice": 210.0},
        {"symbol": "IEF", "secType": "STK", "orderRef": "TRENDBOOK", "clientId": 23,
         "action": "BUY", "orderType": "MKT", "qty": 50},
        {"symbol": "MES", "secType": "FUT", "orderRef": "TRENDBOOK", "clientId": 23,
         "action": "BUY", "orderType": "MKT", "qty": 1},
    ])
    # HON's position comes from today's manual execution below; the rest are held from before
    fake.foreign_positions([("GOOGL", "STK", 20, 180.0),
                            ("DELL", "STK", 30, 120.0), ("PLTR", "STK", 200, 25.0),
                            ("GLD", "STK", 10, 220.0),
                            {"symbol": "SPY", "secType": "OPT", "qty": 1, "avgCost": 125.0,
                             "localSymbol": "SPY   260918C00650000"},
                            ("MES", "FUT", 1, 6400.0)])
    fake.foreign_execution("HON", "BOT", 100, 205.0, orderRef="")
    fake.foreign_execution("SPY", "BOT", 1, 1.25, orderRef=OPTIONS_ENGINE_TAG, secType="OPT")
    fake.foreign_execution("IEF", "BOT", 50, 95.0, orderRef="TRENDBOOK")
    fake.foreign_execution("DELL", "BOT", 30, 120.0, orderRef="SOMEONE-ELSE")


def test_own_order_filter_drops_opt_bag_other_tag_and_no_tag():
    fake, clock = _fake()
    _seed_foreign(fake)
    c = _client(fake, clock)
    assert c.get_own_orders() == []                  # genuinely empty: [] not None
    res = _entry(c)
    own = c.get_own_orders()
    assert sorted(d["role"] for d in own) == ["entry", "stop"]
    assert all(d["tag"] == TAG and d["sec_type"] == "STK" for d in own)
    assert {d["symbol"] for d in own} == {SYM}
    assert len(c.foreign_seen) == 5
    assert fake.cancel_calls == []                   # foreign rows never cancelled
    assert c.cancel(res["order"]["parent_id"])["order"]["status"] == "canceled"
    assert fake.cancel_calls == [res["order"]["parent_id"]]


def test_own_executions_and_positions_ignore_foreign_rows():
    fake, clock = _fake()
    _seed_foreign(fake)
    c = _client(fake, clock)
    assert c.get_own_executions() == [] and c.get_own_positions() == {}
    _entry(c)
    fake.advance(T0, {SYM: [50.11]})
    ex = c.get_own_executions()
    assert len(ex) == 1 and ex[0]["symbol"] == SYM and ex[0]["tag"] == TAG
    assert set(c.get_own_positions()) == {SYM}
    assert c.foreign_stk_symbols() == {"HON", "GOOGL", "DELL", "PLTR", "IEF", "GLD", SYM}
    bound = c.positions_bound()
    assert bound["HON"] == 100 and bound[SYM] == 10 and "SPY" not in bound and "MES" not in bound


def test_reads_return_none_on_failure_never_empty():
    fake, clock = _fake()
    c = _client(fake, clock)
    assert c.get_own_orders() == [] and c.get_own_executions() == []
    assert c.get_own_positions() == {} and c.foreign_stk_symbols() == set()
    fake.fail_reads("reqAllOpenOrders")
    assert c.get_own_orders() is None
    fake.clear_fail_reads().fail_reads("reqCompletedOrders")
    assert c.get_own_orders() is None
    fake.clear_fail_reads().fail_reads("fills")
    assert c.get_own_executions() is None and c.get_own_positions() is None
    fake.clear_fail_reads()
    fake.positions_read_fails = True
    assert c.foreign_stk_symbols() is None and c.positions_bound() is None
    fake.positions_read_fails = False
    fake.disconnect()
    assert c.get_own_orders() is None and c.get_own_executions() is None
    assert c.match_own_order(SYM, "entry") is None


def test_reqexecutions_repull_when_fills_empty():
    fake, clock = _fake()
    c = _client(fake, clock)
    _entry(c)
    fake.advance(T0, {SYM: [50.11]})
    with patch.object(fake, "fills", return_value=[]):
        ex = c.get_own_executions()
    assert len(ex) == 1 and ex[0]["symbol"] == SYM


# ── identity across restarts (§10) ───────────────────────────────────────────

def test_permid_keying_survives_restart_and_orderid_collision():
    fake, clock = _fake()
    a = _client(fake, clock)
    res = _entry(a)
    pid, cid, perm = res["order"]["parent_id"], res["order"]["child_id"], res["order"]["perm_id"]
    # a foreign order on another clientId sharing OUR orderId (F4 / B5 drill)
    fake.foreign_orders([{"symbol": "IEF", "clientId": 23, "orderId": pid, "orderRef": "TRENDBOOK",
                          "action": "BUY", "orderType": "LMT", "qty": 5, "lmtPrice": 95.0}])
    a.disconnect()
    fake2 = FakeIB(clock=clock)                      # same class-level book
    fake2.set_mark(SYM, 50.0)
    b = _client(fake2, clock)
    own = {d["role"]: d for d in b.get_own_orders()}
    assert own["entry"]["id"] == pid and own["entry"]["perm_id"] == perm
    assert own["stop"]["id"] == cid and own["stop"]["parent_id"] == pid
    assert len(own) == 2
    assert b.cancel(pid)["order"]["status"] == "canceled"     # same clientId -> cancellable
    assert FakeIB.trade_by_order_id(23, pid).orderStatus.status == "Submitted"  # foreign untouched


def test_completed_record_from_previous_session_keyed_by_permid():
    fake, clock = _fake()
    a = _client(fake, clock)
    res = _entry(a)
    pid, perm = res["order"]["parent_id"], res["order"]["perm_id"]
    fake.advance(T0, {SYM: [50.11]})
    a.disconnect()
    fake2 = FakeIB(clock=clock)
    b = _client(fake2, clock)
    completed = [t for t in fake2.reqCompletedOrders(False) if t.order.permId == perm]
    assert completed and completed[0].order.orderId == 0   # the gateway's shape
    own = {d["perm_id"]: d for d in b.get_own_orders()}
    assert own[perm]["status"] == "filled" and own[perm]["filled"] == 10
    assert b.get_own_positions()[SYM]["qty"] == 10           # from executions, not the record
    assert b.match_own_order(SYM, "stop", parent_id=pid)["status"] == "open"
    ex = b.get_own_executions()
    assert len(ex) == 1 and ex[0]["role"] == "entry"         # role via permId, not orderId=0
    assert ex[0]["perm_id"] == perm


def test_404_held_is_rebuilt_from_broker_truth_after_restart():
    fake, clock = _fake()
    fake.short_unavailable(SYM, "404")
    a = _client(fake, clock)
    pid = _entry(a, side="short", level=49.90, limit=49.87, stop=50.50)["order"]["parent_id"]
    assert pid in a.held
    a.disconnect()
    b = _client(FakeIB(clock=clock), clock)
    assert b.get_own_orders() is not None
    assert pid in b.held and b.locate_expired(pid) is False
    clock.sleep(SHORT_LOCATE_WAIT_S)
    assert b.locate_expired(pid) is True
    assert b.cancel(pid)["order"]["status"] == "canceled"
    assert pid not in b.held


def test_cancel_from_another_clientid_is_10147_and_rereads():
    fake, clock = _fake()
    a = _client(fake, clock)
    pid = _entry(a)["order"]["parent_id"]
    fake2 = FakeIB(clock=clock)
    b = IbkrStockClient(ib=fake2, clock=clock, client_id=71)
    out = b.cancel(pid)
    assert out["error"] == "cancel_target_not_found" and out["code"] == 10147
    assert out["reread"] is True
    assert a.order_status(pid)["status"] == "pending"        # no state change at the broker


def test_cancel_after_fill_is_10148_and_cancel_race_books_the_fill():
    fake, clock = _fake()
    c = _client(fake, clock)
    pid = _entry(c)["order"]["parent_id"]
    fake.advance(T0, {SYM: [50.11]})
    out = c.cancel(pid)
    assert out["code"] == 10148 and out["order"]["status"] == "filled" and "error" not in out
    FakeIB.reset_broker()
    fake, clock = _fake()
    fake.cancel_races_fill(SYM)
    c = _client(fake, clock)
    pid = _entry(c)["order"]["parent_id"]
    out = c.cancel(pid)
    assert out["code"] == 10148 and out["order"]["status"] == "filled"
    assert c.get_own_positions()[SYM]["qty"] == 10


def test_execution_only_visible_after_restart():
    fake, clock = _fake()
    fake.execution_only_visible_after_restart(SYM)
    a = _client(fake, clock)
    _entry(a)
    fake.advance(T0, {SYM: [50.11]})
    assert a.get_own_executions() == []              # landed while "down"
    a.disconnect()
    b = _client(FakeIB(clock=clock), clock)
    assert len(b.get_own_executions()) == 1 and b.get_own_positions()[SYM]["qty"] == 10


def test_role_of_and_match_own_order_keys():
    assert role_of("STP LMT", 0) == "entry" and role_of("STP LMT", 5) == "other"
    assert role_of("STP", 5) == "stop" and role_of("MKT", 0) == "flatten" and role_of("LMT") == "other"
    fake, clock = _fake()
    c = _client(fake, clock)
    res = _entry(c)
    pid, cid = res["order"]["parent_id"], res["order"]["child_id"]
    assert c.match_own_order(SYM, "entry", action="BUY", qty=10, lmt_price=50.13,
                             aux_price=50.10)["id"] == pid
    assert c.match_own_order(SYM, "stop", parent_id=pid)["id"] == cid
    assert c.match_own_order(SYM, "stop", oca_group=OCA)["id"] == cid
    assert c.match_own_order(SYM, "stop", oca_group="other") is None
    assert c.match_own_order(SYM, "entry", exclude_ids=(pid,)) is None
    assert c.match_own_order("HON", "entry") is None


# ── quotes, commissions, FIFO ────────────────────────────────────────────────

def test_quotes_last_and_no_quote_and_delayed_source():
    fake, clock = _fake()
    fake.set_mark("NVDA", 120.0).no_quote("IBM")
    c = _client(fake, clock)
    assert c.subscribe_quotes([SYM, "NVDA", "IBM"]) == {SYM, "NVDA", "IBM"}
    q = c.last(SYM)
    assert q["last"] == 50.0 and q["bid"] == 49.995 and q["ask"] == 50.005
    assert q["mark"] == 50.0 and q["source"] == "ibkr_live" and q["age_s"] == 0
    assert c.last("IBM") is None                     # NaN quote = no mark
    assert c.last("NOPE") is None
    c.unsubscribe_all()
    assert c.last(SYM) is None
    c.subscribe_quotes([SYM], market_data_type=3)
    assert fake.market_data_type == 3 and c.last(SYM)["source"] == "ibkr_delayed"


def test_delayed_commission_reports_are_nullable_then_known():
    fake, clock = _fake()
    fake.delayed_commission_reports = True
    c = _client(fake, clock)
    _entry(c)
    fake.advance(T0, {SYM: [50.11]})
    assert c.get_own_executions()[0]["commission"] is None
    assert c.get_own_positions()[SYM]["commissions"] == 0.0
    assert fake.deliver_commissions() == 1
    assert c.get_own_executions()[0]["commission"] == 1.0
    assert c.get_own_positions()[SYM]["commissions"] == 1.0


def test_fifo_positions_known_answers():
    def e(sym, side, sh, px, t, comm=1.0):
        return {"symbol": sym, "side": side, "shares": sh, "price": px, "time_et": t,
                "exec_id": f"{sym}{t}", "commission": comm}
    execs = [e("L", "BOT", 10, 50.0, "1"), e("L", "SLD", 4, 51.0, "2"), e("L", "SLD", 6, 49.0, "3"),
             e("S", "SLD", 5, 20.0, "1"), e("S", "BOT", 5, 19.0, "2"),
             e("O", "BOT", 3, 10.0, "1", comm=None)]
    p = fifo_positions(execs)
    assert p["L"]["qty"] == 0 and p["L"]["realized"] == pytest.approx(4 * 1.0 - 6 * 1.0)
    assert p["L"]["commissions"] == 3.0 and p["L"]["realized_net"] == pytest.approx(-2.0 - 3.0)
    assert p["S"]["qty"] == 0 and p["S"]["realized"] == pytest.approx(5.0)
    assert p["O"]["qty"] == 3 and p["O"]["avg_px"] == 10.0 and p["O"]["commissions"] == 0.0
    assert p["O"]["n_exec"] == 1
    assert fifo_positions([]) == {}
