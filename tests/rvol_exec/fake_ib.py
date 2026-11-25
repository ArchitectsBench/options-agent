"""FakeIB — a scriptable stand-in for `ib_async.IB` for the RVOL-EXEC lane
(spec §16.1). NO socket is ever opened; every test using this is hermetic.

Modelled on `tests/mock_ib.py` (class-level `_BROKER_BOOK`) but it goes
further, because the lane must survive restarts, partials, OCA races and a
shared account:

* The broker BOOK and the execution TAPE are CLASS-level: they survive client
  instances, so a restart test builds a second `IbkrStockClient` on the same
  broker and recovers what the first one placed.
* Orders remember the clientId that placed them; orderIds are PER clientId
  (as at IB), so a foreign order can share our orderId — only permId is
  unique. `cancelOrder` from another clientId -> error 10147, no state change
  (F4, the B5 drill).
* `placeOrder` with an existing orderId is a MODIFY (auxPrice / lmtPrice /
  totalQuantity). `transmit=False` -> `PendingSubmit` until a child with
  `parentId == orderId` and `transmit=True` arrives.
* Price engine: `advance(ts_et, {sym: [prints...]})` walks each print; a
  STP-LMT parent triggers on `last >= stopPrice` (BUY) / `<=` (SELL) and fills
  at the print only when it is within the limit — a trigger print beyond the
  limit leaves the order live-unfilled (the breach the lane must cancel);
  attached STP children activate at the parent's first execution; OCA
  (ocaType 1) — a member's fill cancels the others; MKT fills at the next
  print +/- `mkt_slip`; GTD expires at `goodTillDate`; DAY expires at 16:00.
* Foreign rows (the human's manual stock, the options engine's OPT/BAG orders
  tagged UNVALIDATED-PAPER-ONLY, the trend book's ETFs and a future) can be
  seeded in the book, the tape and the position list so ownership filters are
  EXERCISED, not assumed.

Scenario knobs (each documented on its method): reject, short_unavailable,
partial, transport_error_on_place, fail_reads, disconnect_at, halted,
child_stays_full_qty, drop_child_on_cancel, orphan_stop_after_flatten,
oca_join_rejected, stop_and_mkt_both_fill, delayed_commission_reports,
execution_only_visible_after_restart, foreign_positions, foreign_orders,
foreign_execution, positions_read_fails, lost_position, account, no_quote,
marks, gtd_rejected, amend_rejected, cancel_races_fill.

ExecutionFilter.time: ib_async passes the string verbatim; the TWS docs give
"yyyymmdd-hh:mm:ss" (older docs: space-separated). The fake accepts both;
which one the gateway honours is a Monday item (spec §23 item 4).
"""

from __future__ import annotations

import copy
import math
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from eventkit import Event
from ib_async import (CommissionReport, Contract, Execution, ExecutionFilter, Fill,
                      Order, OrderStatus, Position, Stock, Ticker, Trade, TradeLogEntry)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

CANCEL_NOTICE = "Order Canceled - reason:"           # IB's plain cancel (202), EMPTY reason
SHORT_REJECT_MSG = "Order rejected - reason: The contract is not available for short sale"
LOCATE_HELD_MSG = "Order held while securities are located."
NOT_FOUND_MSG = "OrderId {oid} that needs to be cancelled is not found."
CANNOT_CANCEL_MSG = "OrderId {oid} that needs to be cancelled cannot be cancelled, state: {state}."
PRICE_INCREMENT_MSG = ("The price does not conform to the minimum price variation "
                       "for this contract.")

_TERMINAL = OrderStatus.DoneStates


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=ET)


def make_contract(symbol: str, sec_type: str = "STK", **kw) -> Contract:
    """A contract for seeding foreign rows (STK/OPT/BAG/FUT)."""
    if sec_type == "STK":
        c = Stock(symbol, "SMART", "USD")
    else:
        c = Contract(secType=sec_type, symbol=symbol, currency="USD",
                     exchange=kw.pop("exchange", "SMART"))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class FakeIB:
    """Stand-in for ib_async.IB — no sockets, fully scriptable."""

    # ── class-level broker state (survives client instances; reset per test) ──
    _BROKER_BOOK: dict[int, Trade] = {}          # permId -> Trade (all clients, all secTypes)
    _META: dict[int, dict] = {}                  # permId -> fake-side bookkeeping
    _BROKER_FILLS: list[Fill] = []               # visible execution tape (account-wide)
    _HIDDEN_FILLS: list[Fill] = []               # execution_only_visible_after_restart
    _SEED_POSITIONS: list[Position] = []         # foreign positions seeded by tests
    _NEXT_ORDER_ID: dict[int, int] = {}          # clientId -> next orderId (per client, as at IB)
    _NEXT_PERM: list[int] = [1838451540]
    _NEXT_EXEC: list[int] = [1]
    _NEXT_CONID: list[int] = [265598]
    _CONIDS: dict[str, int] = {}
    _SESSION_NO: list[int] = [0]                 # bumps on every connect()

    RequestTimeout: float = 0

    @classmethod
    def reset_broker(cls) -> None:
        cls._BROKER_BOOK = {}
        cls._META = {}
        cls._BROKER_FILLS = []
        cls._HIDDEN_FILLS = []
        cls._SEED_POSITIONS = []
        cls._NEXT_ORDER_ID = {}
        cls._NEXT_PERM = [1838451540]
        cls._NEXT_EXEC = [1]
        cls._NEXT_CONID = [265598]
        cls._CONIDS = {}
        cls._SESSION_NO = [0]

    def __init__(self, accounts=None, clock=None, now: datetime | None = None):
        # events (ib_async signatures)
        self.errorEvent = Event("errorEvent")                 # (reqId, code, msg, contract)
        self.orderStatusEvent = Event("orderStatusEvent")     # (trade)
        self.execDetailsEvent = Event("execDetailsEvent")     # (trade, fill)
        self.commissionReportEvent = Event("commissionReportEvent")  # (trade, fill, report)
        self.disconnectedEvent = Event("disconnectedEvent")   # ()
        self.connectedEvent = Event("connectedEvent")         # ()
        self.openOrderEvent = Event("openOrderEvent")
        self.updateEvent = Event("updateEvent")

        self._accounts = list(accounts) if accounts is not None else ["DUQ000000"]
        self.clock = clock
        self._now = _aware(now) if now else datetime.now(ET)
        self.connected = False
        self.clientId: int | None = None
        self.connect_calls: list[int] = []       # every clientId ever attempted
        self.placed: list[Trade] = []            # every placeOrder on THIS instance, in order
        self.cancel_calls: list[int] = []
        self.market_data_type: int | None = None
        self._tickers: dict[str, Ticker] = {}

        # market
        self.marks: dict[str, float] = {}
        self.half_spread = 0.005
        self.mkt_slip = 0.01
        self.mkt_fill_immediately = False
        self.exchanges: dict[str, str] = {"IBM": "NYSE", "HON": "NASDAQ", "SPY": "ARCA"}

        # knobs
        self.down = False
        self.down_until: datetime | None = None
        self.in_use: set[int] = set()
        self.unknown_symbols: set[str] = set()
        self._rejects: dict[str, list[tuple[int, str]]] = {}
        self._short: dict[str, dict] = {}
        self._partials: dict[str, dict] = {}
        self._stop_partials: dict[str, float] = {}
        self._transport: dict[str, str] = {}      # sym -> "before" | "after"
        self._fail_reads: set[str] = set()
        self._halted: set[str] = set()
        self._no_quote: set[str] = set()
        self._lost_positions: set[str] = set()
        self._hidden_syms: set[str] = set()
        self._amend_rejected: set[str] = set()
        self._cancel_races_fill: set[str] = set()
        self._disconnect_schedule: list[tuple[datetime, float]] = []
        self.child_stays_full_qty = False
        self.drop_child_on_cancel = False
        self.orphan_stop_after_flatten = False
        self.oca_join_rejected = False
        self.stop_and_mkt_both_fill = False
        self.delayed_commission_reports = False
        self.positions_read_fails = False
        self.gtd_rejected = False
        self._pending_reports: list[tuple[Trade, Fill, CommissionReport]] = []

    # ── time ─────────────────────────────────────────────────────────────────
    def now(self) -> datetime:
        if self.clock is not None:
            return _aware(self.clock.now_et())
        return self._now

    def _now_utc(self) -> datetime:
        return self.now().astimezone(UTC)

    # ── connection surface ───────────────────────────────────────────────────
    def connect(self, host, port, clientId=0, timeout=20, **_kw):
        self.connect_calls.append(int(clientId))
        if self.down or (self.down_until is not None and self.now() < self.down_until):
            raise ConnectionRefusedError(f"[Errno 61] Connection refused ({host}:{port})")
        if int(clientId) in self.in_use:
            self.errorEvent.emit(-1, 326, "Unable to connect as the client id is already "
                                 "in use. Retry with a unique client id.", None)
            raise ConnectionError("Error 326: client id already in use")
        self.connected = True
        self.clientId = int(clientId)
        FakeIB._SESSION_NO[0] += 1
        self._session = FakeIB._SESSION_NO[0]
        # executions that landed while no client was connected become visible now
        if FakeIB._HIDDEN_FILLS:
            FakeIB._BROKER_FILLS.extend(FakeIB._HIDDEN_FILLS)
            FakeIB._HIDDEN_FILLS = []
            self._hidden_syms = set()
        self.connectedEvent.emit()
        return self

    def disconnect(self):
        was = self.connected
        self.connected = False
        if was:
            self.disconnectedEvent.emit()
        return None

    def isConnected(self) -> bool:
        return bool(self.connected)

    def managedAccounts(self):
        self._require_connected()
        return list(self._accounts)

    def sleep(self, secs: float = 0.02) -> bool:
        if secs and self.clock is not None and hasattr(self.clock, "sleep"):
            self.clock.sleep(secs)
        return True

    def _require_connected(self):
        if not self.connected:
            raise ConnectionError("Not connected")

    def _read(self, name: str):
        self._require_connected()
        if name in self._fail_reads:
            raise TimeoutError(f"{name} timed out (fake)")

    # ── contract qualification ───────────────────────────────────────────────
    def qualifyContracts(self, *contracts):
        self._require_connected()
        out = []
        for c in contracts:
            sym = getattr(c, "symbol", "")
            if getattr(c, "secType", "") != "STK" or sym in self.unknown_symbols:
                self.errorEvent.emit(-1, 200, "No security definition has been found "
                                     "for the request", c)
                continue
            c.conId = self.conid(sym)
            c.primaryExchange = self.exchanges.get(sym, "NASDAQ")
            c.localSymbol = sym
            c.tradingClass = sym
            out.append(c)
        return out

    @classmethod
    def conid(cls, sym: str) -> int:
        cid = cls._CONIDS.get(sym)
        if cid is None:
            cid = cls._NEXT_CONID[0]
            cls._NEXT_CONID[0] += 1
            cls._CONIDS[sym] = cid
        return cid

    # ── order placement / modify ─────────────────────────────────────────────
    def placeOrder(self, contract, order: Order) -> Trade:
        self._require_connected()
        sym = getattr(contract, "symbol", "")
        mode = self._transport.pop(sym, None)
        if mode == "before":
            raise TimeoutError("socket timeout before transmit (fake)")
        existing = self._find(self.clientId, int(getattr(order, "orderId", 0) or 0))
        if existing is not None:
            trade = self._modify(existing, order)
        else:
            trade = self._book_new(contract, order)
        if mode == "after":
            raise TimeoutError("socket timeout after transmit — order is LIVE (fake)")
        return trade

    def _book_new(self, contract, order: Order) -> Trade:
        cid = self.clientId
        oid = self._next_order_id(cid)
        order.orderId = oid
        order.clientId = cid
        order.permId = self._next_perm()
        sym = contract.symbol
        qty = float(order.totalQuantity)
        status = "Submitted"
        log: list[TradeLogEntry] = []
        meta = {"clientId": cid, "session": self._session, "triggered": False,
                "child_active": False, "parent_perm": None, "sym": sym,
                "partial_done": False, "held_release_at": None, "placed_at": self.now(),
                "accepted": (order.auxPrice, order.lmtPrice, order.totalQuantity)}

        is_entry_short = (order.action == "SELL" and order.orderType == "STP LMT"
                          and not order.parentId)
        reject = self._pop_reject(sym, order)
        if reject is None and is_entry_short and sym in self._short:
            spec = self._short[sym]
            if spec["mode"] == "201":
                reject = (201, SHORT_REJECT_MSG)
        if reject is not None:
            code, msg = reject
            status = "Cancelled"
            log.append(TradeLogEntry(self.now(), "Cancelled", msg, code))
        elif is_entry_short and sym in self._short and self._short[sym]["mode"] == "404":
            status = "PreSubmitted"
            rel = self._short[sym].get("release_after_s")
            meta["held"] = True
            meta["held_release_at"] = (self.now() + timedelta(seconds=rel)) if rel else None
        elif not order.transmit:
            status = "PendingSubmit"
        elif order.parentId:
            status = "PreSubmitted"
        elif order.orderType in ("STP", "STP LMT"):
            status = "PreSubmitted"

        why = "locate" if meta.get("held") else ""
        ost = OrderStatus(orderId=oid, status=status, filled=0.0, remaining=qty,
                          avgFillPrice=0.0, permId=order.permId, parentId=order.parentId,
                          clientId=cid, whyHeld=why)
        trade = Trade(contract=contract, order=order, orderStatus=ost, fills=[], log=log)
        FakeIB._BROKER_BOOK[order.permId] = trade
        FakeIB._META[order.permId] = meta
        self.placed.append(trade)

        if order.parentId:
            parent = self._find(cid, int(order.parentId))
            if parent is not None:
                meta["parent_perm"] = parent.order.permId
                if parent.orderStatus.status in _TERMINAL and parent.orderStatus.filled <= 0:
                    # a child attached to an already-rejected/cancelled parent is cancelled
                    ost.status = "Cancelled"
                    log.append(TradeLogEntry(self.now(), "Cancelled", CANCEL_NOTICE, 202))
                    status = "Cancelled"
                elif parent.orderStatus.status == "PendingSubmit" and order.transmit:
                    parent.orderStatus.status = (
                        "PreSubmitted" if parent.order.orderType in ("STP", "STP LMT")
                        else "Submitted")
                    self.orderStatusEvent.emit(parent)
                if parent.orderStatus.filled > 0 and status not in _TERMINAL:
                    self._activate_child(trade, parent)
        self.orderStatusEvent.emit(trade)
        if reject is not None:
            self.errorEvent.emit(oid, reject[0], reject[1], contract)
        elif meta.get("held"):
            self.errorEvent.emit(oid, 404, LOCATE_HELD_MSG, contract)
        elif (order.orderType == "MKT" and self.mkt_fill_immediately
              and sym in self.marks and sym not in self._halted):
            self._execute(trade, self._mkt_px(order, self.marks[sym]))
        return trade

    def _pop_reject(self, sym: str, order: Order):
        if order.parentId:
            return None                      # rejects target parents; a child follows its parent
        q = self._rejects.get(sym)
        if q:
            return q.pop(0)
        if self.gtd_rejected and order.tif == "GTD":
            return (201, "Order rejected - reason: Time in force GTD is not supported "
                         "for this order type on this venue")
        if self.oca_join_rejected and order.orderType == "MKT" and order.ocaGroup:
            self.oca_join_rejected = False
            return (201, f"Order rejected - reason: cannot join OCA group {order.ocaGroup}")
        return None

    def _modify(self, trade: Trade, order: Order) -> Trade:
        """Re-placing an existing orderId = MODIFY. The caller mutates and
        re-sends the broker's own Order object (as with ib_async), so a refused
        modify must ROLL BACK to the last accepted values."""
        sym = trade.contract.symbol
        meta = FakeIB._META[trade.order.permId]
        if trade.orderStatus.status in _TERMINAL:
            self._restore_accepted(trade)
            self.errorEvent.emit(trade.order.orderId, 104,
                                 "Cannot modify a filled order.", trade.contract)
            return trade
        if sym in self._amend_rejected:
            self._restore_accepted(trade)
            self.errorEvent.emit(trade.order.orderId, 201,
                                 "Order rejected - reason: modify refused (fake)", trade.contract)
            return trade
        trade.order.auxPrice = order.auxPrice
        trade.order.lmtPrice = order.lmtPrice
        trade.order.totalQuantity = order.totalQuantity
        trade.orderStatus.remaining = max(0.0, float(order.totalQuantity)
                                          - float(trade.orderStatus.filled))
        meta["accepted"] = (order.auxPrice, order.lmtPrice, order.totalQuantity)
        self.orderStatusEvent.emit(trade)
        return trade

    @staticmethod
    def _restore_accepted(trade: Trade) -> None:
        acc = FakeIB._META[trade.order.permId].get("accepted")
        if acc:
            trade.order.auxPrice, trade.order.lmtPrice, trade.order.totalQuantity = acc

    def cancelOrder(self, order: Order, manualCancelOrderTime: str = ""):
        self._require_connected()
        oid = int(getattr(order, "orderId", 0) or 0)
        self.cancel_calls.append(oid)
        owner = int(getattr(order, "clientId", 0) or 0) or self.clientId
        trade = self._find(owner, oid)
        if trade is None or owner != self.clientId:
            # F4: another clientId (or an unknown id) — IB says not found, nothing changes
            self.errorEvent.emit(oid, 10147, NOT_FOUND_MSG.format(oid=oid), None)
            return None
        st = trade.orderStatus.status
        if st in _TERMINAL:
            self.errorEvent.emit(oid, 10148, CANNOT_CANCEL_MSG.format(oid=oid, state=st),
                                 trade.contract)
            return trade
        sym = trade.contract.symbol
        if sym in self._cancel_races_fill:
            self._cancel_races_fill.discard(sym)
            px = self.marks.get(sym, trade.order.lmtPrice or trade.order.auxPrice)
            self._execute(trade, float(px))
            self.errorEvent.emit(oid, 10148, CANNOT_CANCEL_MSG.format(oid=oid, state="Filled"),
                                 trade.contract)
            return trade
        self._cancel(trade)
        return trade

    def _cancel(self, trade: Trade, notice: bool = True) -> None:
        if trade.orderStatus.status in _TERMINAL:
            return
        trade.orderStatus.status = "Cancelled"
        trade.orderStatus.whyHeld = ""
        FakeIB._META[trade.order.permId]["held"] = False
        trade.log.append(TradeLogEntry(self.now(), "Cancelled", CANCEL_NOTICE, 202))
        self.orderStatusEvent.emit(trade)
        if notice:
            self.errorEvent.emit(trade.order.orderId, 202, CANCEL_NOTICE, trade.contract)
        # attached children of a cancelled parent
        for child in self._children_of(trade):
            if child.orderStatus.status in _TERMINAL:
                continue
            filled = float(trade.orderStatus.filled)
            if filled <= 0 or self.drop_child_on_cancel:
                self._cancel(child)
            elif not self.child_stays_full_qty:
                child.order.totalQuantity = filled
                child.orderStatus.remaining = filled - float(child.orderStatus.filled)
                self._note_accepted(child)
                self.orderStatusEvent.emit(child)

    # ── execution engine ─────────────────────────────────────────────────────
    def _execute(self, trade: Trade, price: float, qty: float | None = None) -> None:
        ost = trade.orderStatus
        remaining = float(ost.remaining)
        q = remaining if qty is None else min(float(qty), remaining)
        if q <= 0:
            return
        order = trade.order
        sym = trade.contract.symbol
        FakeIB._NEXT_EXEC[0] += 1
        exec_id = f"0000e0d5.{FakeIB._NEXT_EXEC[0]:08x}.01.01"
        t_utc = self._now_utc()
        new_filled = float(ost.filled) + q
        avg = (float(ost.avgFillPrice) * float(ost.filled) + price * q) / new_filled
        ex = Execution(execId=exec_id, time=t_utc, acctNumber=self._accounts[0],
                       exchange="SMART", side="BOT" if order.action == "BUY" else "SLD",
                       shares=q, price=round(price, 4), permId=order.permId,
                       clientId=int(order.clientId or 0), orderId=int(order.orderId),
                       cumQty=new_filled, avgPrice=round(avg, 4),
                       orderRef=order.orderRef or "")
        commission = max(1.0, 0.005 * q)
        report = CommissionReport(execId=exec_id,
                                  commission=0.0 if self.delayed_commission_reports else commission,
                                  currency="USD")
        fill = Fill(trade.contract, ex, report, t_utc)
        trade.fills.append(fill)
        ost.filled = new_filled
        ost.remaining = remaining - q
        ost.avgFillPrice = round(avg, 4)
        ost.lastFillPrice = price
        if ost.remaining <= 0:
            ost.status = "Filled"
        elif ost.status in ("PreSubmitted", "PendingSubmit"):
            ost.status = "Submitted"
        hidden = sym in self._hidden_syms
        if hidden:
            FakeIB._HIDDEN_FILLS.append(fill)
        else:
            FakeIB._BROKER_FILLS.append(fill)
            self.execDetailsEvent.emit(trade, fill)
            self.orderStatusEvent.emit(trade)
            if self.delayed_commission_reports:
                self._pending_reports.append((trade, fill, CommissionReport(
                    execId=exec_id, commission=commission, currency="USD")))
            else:
                self.commissionReportEvent.emit(trade, fill, report)
        # attached children follow the parent's executions
        for child in self._children_of(trade):
            if child.orderStatus.status in _TERMINAL:
                continue
            if not FakeIB._META[child.order.permId]["child_active"]:
                self._activate_child(child, trade)
            elif not self.child_stays_full_qty:
                child.order.totalQuantity = new_filled
                child.orderStatus.remaining = new_filled - float(child.orderStatus.filled)
                self._note_accepted(child)
        # OCA (ocaType 1): a member's fill cancels the rest of the group
        if order.ocaGroup and int(order.ocaType or 0) == 1:
            self._oca_cancel_siblings(trade)

    def _activate_child(self, child: Trade, parent: Trade) -> None:
        FakeIB._META[child.order.permId]["child_active"] = True
        if not self.child_stays_full_qty:
            child.order.totalQuantity = float(parent.orderStatus.filled)
            child.orderStatus.remaining = float(parent.orderStatus.filled)
            self._note_accepted(child)
        child.orderStatus.status = "Submitted"
        self.orderStatusEvent.emit(child)

    @staticmethod
    def _note_accepted(trade: Trade) -> None:
        o = trade.order
        FakeIB._META[o.permId]["accepted"] = (o.auxPrice, o.lmtPrice, o.totalQuantity)

    def _oca_cancel_siblings(self, filled: Trade) -> None:
        if self.stop_and_mkt_both_fill:
            self.stop_and_mkt_both_fill = False       # one race, then normal OCA
            return
        for t in list(FakeIB._BROKER_BOOK.values()):
            if t is filled or t.orderStatus.status in _TERMINAL:
                continue
            if t.order.ocaGroup != filled.order.ocaGroup:
                continue
            if self.orphan_stop_after_flatten and filled.order.orderType == "MKT" \
                    and t.order.orderType == "STP":
                continue                                  # IB "forgot" to cancel the stop
            self._cancel(t)

    def deliver_commissions(self) -> int:
        """`delayed_commission_reports`: deliver the reports that were withheld."""
        n = 0
        for trade, fill, report in self._pending_reports:
            fill.commissionReport.commission = report.commission
            self.commissionReportEvent.emit(trade, fill, report)
            n += 1
        self._pending_reports = []
        return n

    # ── price engine ─────────────────────────────────────────────────────────
    def set_mark(self, sym: str, last: float) -> "FakeIB":
        self.marks[sym] = float(last)
        tk = self._tickers.get(sym)
        if tk is not None:
            self._fill_ticker(tk, sym)
        return self

    def advance(self, ts_et: datetime, prints: dict[str, list[float]] | None = None) -> None:
        """Move the fake clock to `ts_et` and walk each symbol's print path.
        An injected clock that exposes `set(dt)` is moved too (the simulator's
        fake Clock does), so broker time and lane time never diverge."""
        self._now = _aware(ts_et)
        if self.clock is not None and hasattr(self.clock, "set"):
            self.clock.set(self._now)
        self._process_schedule()
        for sym, path in (prints or {}).items():
            for p in path:
                self.set_mark(sym, p)
                self._match(sym, float(p))      # matching happens at the broker, connected or not
        self._expire()

    def tick(self, sym: str, price: float) -> None:
        """One print on one symbol at the current fake time."""
        self.advance(self.now(), {sym: [price]})

    def _match(self, sym: str, p: float) -> None:
        if sym in self._halted:
            return
        active = [t for t in FakeIB._BROKER_BOOK.values()
                  if t.contract.symbol == sym and getattr(t.contract, "secType", "") == "STK"
                  and t.orderStatus.status not in _TERMINAL]
        for t in sorted(active, key=lambda t: t.order.permId):
            ost, o = t.orderStatus, t.order
            meta = FakeIB._META[o.permId]
            if ost.status == "PendingSubmit" or meta.get("held"):
                continue
            if o.parentId and not meta["child_active"]:
                continue
            if ost.status in _TERMINAL:
                continue
            buy = o.action == "BUY"
            typ = o.orderType
            if typ == "STP LMT":
                if not meta["triggered"]:
                    if (buy and p >= o.auxPrice) or (not buy and p <= o.auxPrice):
                        meta["triggered"] = True
                        ost.status = "Submitted"
                if meta["triggered"] and ((buy and p <= o.lmtPrice) or (not buy and p >= o.lmtPrice)):
                    self._fill_with_partial_knob(t, p)
            elif typ == "STP":
                if (buy and p >= o.auxPrice) or (not buy and p <= o.auxPrice):
                    px = p + self.mkt_slip if buy else p - self.mkt_slip
                    sp = self._stop_partials.pop(sym, None)
                    self._execute(t, px, sp)
            elif typ == "MKT":
                self._execute(t, self._mkt_px(o, p))
            elif typ == "LMT":
                if (buy and p <= o.lmtPrice) or (not buy and p >= o.lmtPrice):
                    self._execute(t, p)

    def _fill_with_partial_knob(self, t: Trade, p: float) -> None:
        sym = t.contract.symbol
        meta = FakeIB._META[t.order.permId]
        spec = self._partials.get(sym)
        if spec is not None and not meta["partial_done"]:
            meta["partial_done"] = True
            self._execute(t, p, spec["qty"])
            return
        if spec is not None and meta["partial_done"] and not spec.get("rest_fills"):
            return                              # no more liquidity for the remainder
        self._execute(t, p)

    def _mkt_px(self, o: Order, p: float) -> float:
        return p + self.mkt_slip if o.action == "BUY" else p - self.mkt_slip

    def _expire(self) -> None:
        now = self.now()
        for t in list(FakeIB._BROKER_BOOK.values()):
            if t.orderStatus.status in _TERMINAL:
                continue
            o = t.order
            if o.tif == "GTD" and o.goodTillDate:
                try:
                    d, hms = o.goodTillDate.split()[:2]
                    gtd = datetime.strptime(f"{d} {hms}", "%Y%m%d %H:%M:%S").replace(tzinfo=ET)
                except Exception:  # noqa: BLE001
                    gtd = None
                if gtd is not None and now >= gtd:
                    self._cancel(t)
                    continue
            if o.tif == "DAY" and now.time() >= time(16, 0) and \
                    FakeIB._META[o.permId]["placed_at"].date() <= now.date():
                self._cancel(t)

    def _process_schedule(self) -> None:
        now = self.now()
        for when, for_s in list(self._disconnect_schedule):
            if now >= when:
                self._disconnect_schedule.remove((when, for_s))
                self.down_until = when + timedelta(seconds=for_s)
                if self.connected:
                    self.connected = False
                    self.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost.", None)
                    self.disconnectedEvent.emit()
        for t in list(FakeIB._BROKER_BOOK.values()):
            meta = FakeIB._META[t.order.permId]
            rel = meta.get("held_release_at")
            if meta.get("held") and rel is not None and now >= rel \
                    and t.orderStatus.status not in _TERMINAL:
                meta["held"] = False
                t.orderStatus.whyHeld = ""
                t.orderStatus.status = "PreSubmitted"
                self.orderStatusEvent.emit(t)

    # ── reads ────────────────────────────────────────────────────────────────
    def openTrades(self):
        self._read("openTrades")
        return [t for t in FakeIB._BROKER_BOOK.values() if t.orderStatus.status not in _TERMINAL]

    def reqAllOpenOrders(self):
        self._read("reqAllOpenOrders")
        return [t for t in FakeIB._BROKER_BOOK.values() if t.orderStatus.status not in _TERMINAL]

    def reqCompletedOrders(self, apiOnly: bool = False):
        """Terminal orders. Records from a PREVIOUS session come back with
        orderId=0 (the real gateway does this) — only permId identifies them."""
        self._read("reqCompletedOrders")
        out = []
        for t in FakeIB._BROKER_BOOK.values():
            if t.orderStatus.status not in _TERMINAL:
                continue
            if FakeIB._META[t.order.permId]["session"] != self._session:
                o = copy.copy(t.order)
                o.orderId = 0
                st = copy.copy(t.orderStatus)
                st.orderId = 0
                out.append(Trade(contract=t.contract, order=o, orderStatus=st,
                                 fills=t.fills, log=t.log))
            else:
                out.append(t)
        return out

    def trades(self):
        self._read("trades")
        return list(FakeIB._BROKER_BOOK.values())

    def fills(self):
        self._read("fills")
        return list(FakeIB._BROKER_FILLS)

    def reqExecutions(self, execFilter: ExecutionFilter | None = None):
        self._read("reqExecutions")
        f = execFilter or ExecutionFilter()
        out = []
        since = None
        if f.time:
            s = f.time.replace("-", " ", 1) if "-" in f.time else f.time
            try:
                since = datetime.strptime(s, "%Y%m%d %H:%M:%S").replace(tzinfo=ET)
            except ValueError:
                since = None
        for fl in FakeIB._BROKER_FILLS:
            if f.secType and getattr(fl.contract, "secType", "") != f.secType:
                continue
            if f.symbol and fl.contract.symbol != f.symbol:
                continue
            if f.clientId and int(fl.execution.clientId) != int(f.clientId):
                continue
            if since is not None and fl.execution.time < since:
                continue
            out.append(fl)
        return out

    def positions(self, account: str = ""):
        self._read("positions")
        if self.positions_read_fails:
            raise TimeoutError("positions() timed out (fake)")
        net: dict[tuple, list] = {}
        for p in FakeIB._SEED_POSITIONS:
            key = (getattr(p.contract, "secType", ""), p.contract.symbol,
                   getattr(p.contract, "localSymbol", ""))
            net.setdefault(key, [p.contract, 0.0, 0.0])
            net[key][1] += float(p.position)
            net[key][2] += float(p.avgCost) * float(p.position)
        for fl in FakeIB._BROKER_FILLS:
            c = fl.contract
            key = (getattr(c, "secType", ""), c.symbol, getattr(c, "localSymbol", ""))
            net.setdefault(key, [c, 0.0, 0.0])
            sgn = 1.0 if fl.execution.side == "BOT" else -1.0
            net[key][1] += sgn * float(fl.execution.shares)
            net[key][2] += sgn * float(fl.execution.shares) * float(fl.execution.price)
        out = []
        for (sec, sym, _ls), (c, qty, cost) in net.items():
            if sym in self._lost_positions:
                continue
            if abs(qty) < 1e-9:
                continue
            out.append(Position(self._accounts[0], c, qty, abs(cost / qty) if qty else 0.0))
        return out

    # ── market data ──────────────────────────────────────────────────────────
    def reqMarketDataType(self, marketDataType: int):
        self.market_data_type = int(marketDataType)

    def reqMktData(self, contract, genericTickList="", snapshot=False,
                   regulatorySnapshot=False, mktDataOptions=None):
        self._require_connected()
        sym = contract.symbol
        tk = Ticker(contract=contract)
        self._tickers[sym] = tk
        if sym in self._no_quote:
            self.errorEvent.emit(-1, 10167, "Requested market data is not subscribed. "
                                 "Displaying delayed market data.", contract)
        self._fill_ticker(tk, sym)
        return tk

    def _fill_ticker(self, tk: Ticker, sym: str) -> None:
        if sym in self._no_quote or sym not in self.marks:
            tk.last = math.nan
            tk.bid = math.nan
            tk.ask = math.nan
            return
        last = self.marks[sym]
        tk.last = last
        tk.bid = round(last - self.half_spread, 4)
        tk.ask = round(last + self.half_spread, 4)
        tk.time = self._now_utc()

    def ticker(self, contract):
        return self._tickers.get(contract.symbol)

    def cancelMktData(self, contract):
        return self._tickers.pop(contract.symbol, None) is not None

    # ── seeding / scenario knobs ─────────────────────────────────────────────
    def account(self, acct: str) -> "FakeIB":
        self._accounts = [acct]
        return self

    def marks_set(self, **marks) -> "FakeIB":
        for s, v in marks.items():
            self.set_mark(s, v)
        return self

    def no_quote(self, *syms) -> "FakeIB":
        self._no_quote.update(syms)
        return self

    def halted(self, *syms) -> "FakeIB":
        self._halted.update(syms)
        return self

    def reject(self, sym: str, code: int, msg: str) -> "FakeIB":
        """The NEXT order on `sym` is rejected (status Cancelled + error `code`)."""
        self._rejects.setdefault(sym, []).append((int(code), msg))
        return self

    def short_unavailable(self, sym: str, mode: str = "201",
                          release_after_s: float | None = None) -> "FakeIB":
        """Entry SELL STP-LMT on `sym`: mode '201' -> reject with IB's short-sale
        wording; '404' -> held while securities are located (whyHeld=locate),
        released after `release_after_s` fake seconds or never."""
        self._short[sym] = {"mode": mode, "release_after_s": release_after_s}
        return self

    def partial(self, sym: str, qty: float, rest_fills: bool = False) -> "FakeIB":
        """The first execution on `sym`'s entry fills only `qty`; the remainder
        stays working and never fills unless `rest_fills`."""
        self._partials[sym] = {"qty": float(qty), "rest_fills": rest_fills}
        return self

    def stop_partial(self, sym: str, qty: float) -> "FakeIB":
        self._stop_partials[sym] = float(qty)
        return self

    def transport_error_on_place(self, sym: str, live: bool = True) -> "FakeIB":
        """The next placeOrder on `sym` raises. live=True: the order IS booked
        first (ambiguous, live at the broker); False: raised before booking."""
        self._transport[sym] = "after" if live else "before"
        return self

    def fail_reads(self, *names: str) -> "FakeIB":
        self._fail_reads.update(names)
        return self

    def clear_fail_reads(self) -> "FakeIB":
        self._fail_reads = set()
        return self

    def disconnect_at(self, when: datetime, for_s: float) -> "FakeIB":
        self._disconnect_schedule.append((_aware(when), float(for_s)))
        return self

    def execution_only_visible_after_restart(self, *syms) -> "FakeIB":
        self._hidden_syms.update(syms)
        return self

    def amend_rejected(self, *syms) -> "FakeIB":
        self._amend_rejected.update(syms)
        return self

    def cancel_races_fill(self, *syms) -> "FakeIB":
        self._cancel_races_fill.update(syms)
        return self

    def lost_position(self, *syms) -> "FakeIB":
        self._lost_positions.update(syms)
        return self

    def foreign_positions(self, specs) -> "FakeIB":
        """specs: (symbol, secType, qty, avgCost) tuples or dicts with those keys
        (+ optional contract kwargs, e.g. localSymbol for an OPT)."""
        for s in specs:
            if isinstance(s, dict):
                s = dict(s)
                sym, sec = s.pop("symbol"), s.pop("secType", "STK")
                qty, cost = float(s.pop("qty")), float(s.pop("avgCost", 0.0))
                c = make_contract(sym, sec, **s)
            else:
                sym, sec, qty, cost = s
                c = make_contract(sym, sec)
            FakeIB._SEED_POSITIONS.append(Position(self._accounts[0], c, qty, cost))
        return self

    def foreign_orders(self, specs) -> list[Trade]:
        """Book orders placed by SOMEONE ELSE (another clientId / tag / secType).
        spec keys: symbol, secType='STK', action='BUY', orderType='LMT',
        qty=1, lmtPrice=0, auxPrice=0, orderRef='', clientId=0, status='Submitted',
        orderId=None (default: that client's next id)."""
        out = []
        for s in specs:
            s = dict(s)
            c = make_contract(s.pop("symbol"), s.pop("secType", "STK"))
            cid = int(s.pop("clientId", 0))
            o = Order(action=s.pop("action", "BUY"), totalQuantity=float(s.pop("qty", 1)),
                      orderType=s.pop("orderType", "LMT"), lmtPrice=float(s.pop("lmtPrice", 0.0)),
                      auxPrice=float(s.pop("auxPrice", 0.0)), orderRef=s.pop("orderRef", ""),
                      tif=s.pop("tif", "DAY"), ocaGroup=s.pop("ocaGroup", ""),
                      parentId=int(s.pop("parentId", 0)))
            status = s.pop("status", "Submitted")
            oid = s.pop("orderId", None)
            o.orderId = int(oid) if oid is not None else self._next_order_id(cid)
            o.clientId = cid
            o.permId = self._next_perm()
            o.transmit = True
            ost = OrderStatus(orderId=o.orderId, status=status, filled=0.0,
                              remaining=o.totalQuantity, permId=o.permId, clientId=cid)
            t = Trade(contract=c, order=o, orderStatus=ost, fills=[], log=[])
            FakeIB._BROKER_BOOK[o.permId] = t
            FakeIB._META[o.permId] = {"clientId": cid, "session": -1, "triggered": False,
                                      "child_active": True, "parent_perm": None,
                                      "sym": c.symbol, "partial_done": False,
                                      "held_release_at": None, "placed_at": self.now()}
            out.append(t)
        return out

    def foreign_execution(self, symbol: str, side: str, shares: float, price: float,
                          orderRef: str = "", secType: str = "STK", clientId: int = 0,
                          **contract_kw) -> Fill:
        """An execution on the tape that is NOT ours (manual stock, options engine)."""
        c = make_contract(symbol, secType, **contract_kw)
        FakeIB._NEXT_EXEC[0] += 1
        ex = Execution(execId=f"0000f0f0.{FakeIB._NEXT_EXEC[0]:08x}.01.01", time=self._now_utc(),
                       acctNumber=self._accounts[0], exchange="SMART", side=side,
                       shares=float(shares), price=float(price), permId=self._next_perm(),
                       clientId=clientId, orderId=0, cumQty=float(shares),
                       avgPrice=float(price), orderRef=orderRef)
        fl = Fill(c, ex, CommissionReport(execId=ex.execId, commission=1.0, currency="USD"),
                  self._now_utc())
        FakeIB._BROKER_FILLS.append(fl)
        return fl

    # ── test tallies ─────────────────────────────────────────────────────────
    @classmethod
    def executions(cls, order_ref: str | None = None, sec_type: str | None = "STK") -> list[Fill]:
        out = []
        for fl in cls._BROKER_FILLS:
            if order_ref is not None and fl.execution.orderRef != order_ref:
                continue
            if sec_type is not None and getattr(fl.contract, "secType", "") != sec_type:
                continue
            out.append(fl)
        return out

    @classmethod
    def net_by_symbol(cls, order_ref: str | None = None) -> dict[str, float]:
        net: dict[str, float] = {}
        for fl in cls.executions(order_ref):
            sgn = 1.0 if fl.execution.side == "BOT" else -1.0
            net[fl.contract.symbol] = net.get(fl.contract.symbol, 0.0) + sgn * fl.execution.shares
        return net

    @classmethod
    def book(cls) -> list[Trade]:
        return list(cls._BROKER_BOOK.values())

    @classmethod
    def trade_by_order_id(cls, client_id: int, order_id: int) -> Trade | None:
        return cls._find_cls(client_id, order_id)

    # ── internals ────────────────────────────────────────────────────────────
    @classmethod
    def _find_cls(cls, client_id: int | None, order_id: int) -> Trade | None:
        if not order_id:
            return None
        for t in cls._BROKER_BOOK.values():
            if int(t.order.orderId) == int(order_id) and \
                    int(cls._META[t.order.permId]["clientId"]) == int(client_id or 0):
                return t
        return None

    def _find(self, client_id, order_id):
        return self._find_cls(client_id, order_id)

    def _children_of(self, parent: Trade) -> list[Trade]:
        pp = parent.order.permId
        return [t for t in FakeIB._BROKER_BOOK.values()
                if FakeIB._META[t.order.permId].get("parent_perm") == pp]

    @classmethod
    def _next_order_id(cls, client_id: int) -> int:
        n = cls._NEXT_ORDER_ID.get(int(client_id), 1)
        cls._NEXT_ORDER_ID[int(client_id)] = n + 1
        return n

    @classmethod
    def _next_perm(cls) -> int:
        p = cls._NEXT_PERM[0]
        cls._NEXT_PERM[0] += 1
        return p
