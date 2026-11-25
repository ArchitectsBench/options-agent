"""Mock ib_async layer for the IBKR paper adapter tests.

Lets a test drive every order state — filled, partial, reject, buying-power
reject, cancel, open/resting — and any position book WITHOUT opening a socket.
NO test using this ever touches a real gateway.

Design notes
------------
- The IBKR *data* classes (Option, Contract, LimitOrder, ComboLeg) are inert
  dataclasses that construct fine offline, so the adapter uses the real ones;
  only the live `IB` object is mocked here.
- `MockIB.placeOrder` builds a mock Trade from a *scenario* dict. Scenarios are
  consumed FIFO from `ib.scenarios` (falling back to `ib.default_scenario`,
  a resting/open order). `ib.transition(order_id, scenario)` mutates a live
  Trade to simulate the broker progressing an order (open -> filled/partial/…),
  which is how poll-after-place is tested.
- Rejects also emit `ib.errorEvent(orderId, code, message, contract)` so the
  adapter's real error-capture path is exercised.
"""

from __future__ import annotations

from datetime import datetime

from ib_async import Option

from src.data.ibkr_orders import occ_from_fields, parse_occ


# ── scenario builders ────────────────────────────────────────────────────────

def scen_open(status: str = "Submitted") -> dict:
    """A resting order: nothing filled yet."""
    return {"status": status, "filled": 0.0}


def scen_filled(avg_fill_price: float, filled: float, fills=None) -> dict:
    return {"status": "Filled", "filled": float(filled), "remaining": 0.0,
            "avg_fill_price": float(avg_fill_price), "fills": fills or []}


def scen_partial(filled: float, remaining: float, avg_fill_price: float,
                 fills=None) -> dict:
    return {"status": "Submitted", "filled": float(filled),
            "remaining": float(remaining), "avg_fill_price": float(avg_fill_price),
            "fills": fills or []}


def scen_rejected(message: str, code: int = 201, status: str = "Cancelled") -> dict:
    return {"status": status, "filled": 0.0, "remaining": None,
            "reject": {"message": message, "code": code, "status": status}}


def scen_canceled() -> dict:
    return {"status": "Cancelled", "filled": 0.0}


def leg_fill(symbol: str, shares: float, price: float, side: str = "BOT") -> dict:
    """A per-leg execution for a multileg scenario. side: BOT (buy) / SLD (sell)."""
    return {"symbol": symbol, "shares": float(shares), "price": float(price), "side": side}


# ── mock ib_async objects ────────────────────────────────────────────────────

class MockEvent:
    """Minimal eventkit.Event stand-in supporting `+=` and emit()."""

    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def emit(self, *args, **kwargs):
        for h in list(self._handlers):
            h(*args, **kwargs)


class MockOrderStatus:
    def __init__(self, status="Submitted", filled=0.0, remaining=0.0, avgFillPrice=0.0):
        self.status = status
        self.filled = filled
        self.remaining = remaining
        self.avgFillPrice = avgFillPrice


class MockExecution:
    def __init__(self, conId=None, shares=0.0, price=0.0, side="BOT", time=None):
        self.conId = conId
        self.shares = shares
        self.price = price
        self.side = side
        self.time = time or datetime.now()


class MockCommissionReport:
    def __init__(self, commission=0.65):
        self.commission = commission


class MockContract:
    def __init__(self, secType="OPT", conId=None, localSymbol=None):
        self.secType = secType
        self.conId = conId
        self.localSymbol = localSymbol


class MockFill:
    def __init__(self, contract, execution, commissionReport, time=None):
        self.contract = contract
        self.execution = execution
        self.commissionReport = commissionReport
        self.time = time or execution.time


class MockLogEntry:
    def __init__(self, status="", message="", errorCode=0, time=None):
        self.status = status
        self.message = message
        self.errorCode = errorCode
        self.time = time or datetime.now()


class MockTrade:
    _TERMINAL = ("Filled", "Cancelled", "ApiCancelled", "Inactive")

    def __init__(self, contract, order, orderStatus, fills, log):
        self.contract = contract
        self.order = order
        self.orderStatus = orderStatus
        self.fills = fills
        self.log = log

    def isDone(self):
        return self.orderStatus.status in self._TERMINAL


class MockPosition:
    def __init__(self, account, contract, position, avgCost):
        self.account = account
        self.contract = contract
        self.position = position
        self.avgCost = avgCost


class MockAccountValue:
    def __init__(self, tag, value):
        self.tag = tag
        self.value = value


class MockIB:
    """Stand-in for ib_async.IB — no sockets, fully scriptable."""

    def __init__(self, accounts=None):
        self.errorEvent = MockEvent()
        self._accounts = list(accounts) if accounts is not None else ["DU1234567"]
        self._positions: list = []
        self._trades: dict[int, MockTrade] = {}
        self._next_order_id = 1000
        self._next_con_id = 500000
        self._conid_by_occ: dict[str, int] = {}
        self.scenarios: list[dict] = []
        self.default_scenario: dict = scen_open()
        self.connected = False

    # connection surface (used only by the factory test's real-ish path)
    def connect(self, host, port, clientId=0, timeout=20):
        self.connected = True  # NO socket — records the intent only
        return self

    def disconnect(self):
        self.connected = False

    # ---- broker-side order book (B1): survives client teardown, so a FRESH
    # client instance can recover orders it never placed itself ----
    _BROKER_BOOK: dict = {}          # class-level: shared across instances
    fail_order_query: bool = False   # flip to simulate a transport failure

    def reqAllOpenOrders(self):
        if self.fail_order_query:
            raise ConnectionError("gateway dropped during order query")
        return [t for t in MockIB._BROKER_BOOK.values()
                if getattr(t.orderStatus, "status", "") not in ("Filled", "Cancelled")]

    def reqCompletedOrders(self, apiOnly=False):
        if self.fail_order_query:
            raise ConnectionError("gateway dropped during completed-order query")
        return [t for t in MockIB._BROKER_BOOK.values()
                if getattr(t.orderStatus, "status", "") in ("Filled", "Cancelled")]

    def managedAccounts(self):
        return list(self._accounts)

    def accountSummary(self):
        return [MockAccountValue("NetLiquidation", "100000"),
                MockAccountValue("BuyingPower", "200000")]

    def sleep(self, _t=0):
        return None

    # contract qualification
    def qualifyContracts(self, *contracts):
        for c in contracts:
            if getattr(c, "secType", "") == "BAG":
                if not getattr(c, "conId", None):
                    c.conId = 0
                continue
            occ = occ_from_fields(c.symbol, c.lastTradeDateOrContractMonth,
                                  c.right, c.strike)
            cid = self._conid_by_occ.get(occ)
            if cid is None:
                cid = self._next_con_id
                self._next_con_id += 1
                self._conid_by_occ[occ] = cid
            c.conId = cid
            c.localSymbol = occ
        return contracts

    # order placement / lifecycle
    def placeOrder(self, contract, order):
        oid = self._next_order_id
        self._next_order_id += 1
        order.orderId = oid
        sc = self.scenarios.pop(0) if self.scenarios else dict(self.default_scenario)
        trade = self._build_trade(contract, order, sc)
        self._trades[oid] = trade
        self._emit_reject(oid, sc, contract)
        MockIB._BROKER_BOOK[int(trade.order.orderId)] = trade
        return trade

    def cancelOrder(self, order):
        tr = self._trades.get(int(order.orderId))
        if tr is not None and tr.orderStatus.status != "Filled":
            tr.orderStatus.status = "Cancelled"
        return tr

    def positions(self):
        return list(self._positions)

    # ── test-driving helpers ────────────────────────────────────────────────
    def transition(self, order_id, scenario: dict):
        """Progress a live order to a new state (broker moved the order)."""
        tr = self._trades[int(order_id)]
        filled = float(scenario.get("filled", 0.0))
        remaining = scenario.get("remaining", None)
        if remaining is None:
            remaining = float(tr.order.totalQuantity) - filled
        tr.orderStatus = MockOrderStatus(
            status=scenario.get("status", "Submitted"), filled=filled,
            remaining=float(remaining), avgFillPrice=float(scenario.get("avg_fill_price", 0.0)))
        tr.fills = self._build_fills(scenario.get("fills", []))
        reject = scenario.get("reject")
        tr.log = [MockLogEntry(status=reject.get("status", "Cancelled"),
                               message=reject["message"], errorCode=reject.get("code", 201))] \
            if reject else []
        self._emit_reject(int(order_id), scenario, tr.contract)
        return tr

    def add_option_position(self, occ: str, position: float, avg_cost: float,
                            account: str | None = None):
        root, yyyymmdd, right, strike = parse_occ(occ)
        contract = Option(root, yyyymmdd, strike, right, "SMART", currency="USD")
        contract.localSymbol = occ
        self._positions.append(MockPosition(account or self._accounts[0], contract,
                                            position, avg_cost))

    def add_raw_position(self, contract, position: float, avg_cost: float):
        """For non-option positions (stock/future) — must be ignored by the adapter."""
        self._positions.append(MockPosition(self._accounts[0], contract, position, avg_cost))

    # ── internals ────────────────────────────────────────────────────────────
    def _build_trade(self, contract, order, sc) -> MockTrade:
        filled = float(sc.get("filled", 0.0))
        remaining = sc.get("remaining", None)
        if remaining is None:
            remaining = float(order.totalQuantity) - filled
        ostatus = MockOrderStatus(status=sc.get("status", "Submitted"), filled=filled,
                                  remaining=float(remaining),
                                  avgFillPrice=float(sc.get("avg_fill_price", 0.0)))
        fills = self._build_fills(sc.get("fills", []))
        reject = sc.get("reject")
        log = [MockLogEntry(status=reject.get("status", "Cancelled"),
                            message=reject["message"], errorCode=reject.get("code", 201))] \
            if reject else []
        return MockTrade(contract=contract, order=order, orderStatus=ostatus,
                         fills=fills, log=log)

    def _build_fills(self, fill_defs):
        fills = []
        for fd in fill_defs or []:
            cid = self._conid_by_occ.get(fd["symbol"])
            fc = MockContract(secType="OPT", conId=cid, localSymbol=fd["symbol"])
            ex = MockExecution(conId=cid, shares=float(fd["shares"]),
                               price=float(fd["price"]), side=fd.get("side", "BOT"))
            cr = MockCommissionReport(commission=float(fd.get("commission", 0.65)))
            fills.append(MockFill(contract=fc, execution=ex, commissionReport=cr))
        return fills

    def _emit_reject(self, oid, scenario, contract):
        reject = scenario.get("reject")
        if reject:
            self.errorEvent.emit(oid, reject.get("code", 201), reject["message"], contract)
