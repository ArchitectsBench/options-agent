"""IbkrStockClient — the RVOL-EXEC paper lane's OWN IBKR stock-order client.
UNVALIDATED — PAPER ONLY.

Spec: docs/rvol_exec_lane.md §8 (order construction), §13 (contract, error
codes, connection), §0 (isolation). This module is deliberately a SEPARATE
client from `src/data/ibkr_orders.py` (the options engine's) — patterns are
COPIED from it, never imported (spec F8, §0 forbidden-import list).

HARD RAILS
----------
1. PAPER ONLY. `managedAccounts()` must report a 'D'-prefixed account or the
   client never activates (copy of `_activate`). A live account cannot place
   an order through this class.
2. clientId 61, FIXED. There is NO fallback clientId family: IBKR scopes
   orders per clientId (F4), and a lane that must cancel/amend its own resting
   orders after a restart must come back on the SAME id. A stuck 61 is
   retried on 61 and paged — never moved.
3. Every order carries `orderRef == TAG` ("UNVALIDATED-RVOL-PAPER"). An order
   or execution is OURS iff `contract.secType == "STK"` AND
   `orderRef == TAG`. Everything else on the shared account — the options
   engine's `UNVALIDATED-PAPER-ONLY` OPT/BAG orders, the trend book's ETFs and
   futures, the human's manual HON/GOOGL/DELL/PLTR — is invisible: never read
   as ours, never cancelled, never flattened.
4. Own POSITIONS come from own EXECUTIONS only (Σ BOT − Σ SLD per symbol);
   `ib.positions()` is account-level and untagged (F5) and is read for exactly
   two things: the foreign-symbol exclusion set and the consistency bound.
5. Ambiguity boundary (copy of R1): a raise from the first `placeOrder`
   onward is `transport_error` (the order MAY be live). A qualify failure
   before it is definitive.
6. Reads return `None` when the broker view is UNKNOWN (read failed, client
   unavailable) — never a silent `[]`.
7. No socket under pytest unless `ALLOW_LIVE_GATEWAY_TESTS=1`; the ib_async
   layer is injectable (`IbkrStockClient(ib=<FakeIB>)`).
8. Logger `rvol_exec` — never `trader` (whose handler is `logs/agent.log`,
   which the options deadman greps for "Not connected").

ExecutionFilter.time is sent as "yyyymmdd-hh:mm:ss" (ib_async passes it
verbatim; the TWS docs give this form). Which form the gateway honours for a
day-scoped re-pull is spec §23 item 4 (Monday).
"""

from __future__ import annotations

import logging
import math
import os
import re
import sys
from collections import deque
from datetime import datetime, timezone

from src.rvol_exec.constants import (ACCOUNT_PREFIX, CLIENT_ID, CONNECT_TIMEOUT_S, ET, HOST,
                                     PORT, RECONNECT_ATTEMPTS, RECONNECT_WAIT_S,
                                     REQUEST_TIMEOUT_S, SHORT_LOCATE_WAIT_S, STAMP, TAG)

# Guarded like ibkr_orders: the module always imports (available() is just
# False without ib_async), and tests may patch these names.
try:  # pragma: no cover - exercised via the injected fake in tests
    from ib_async import (IB, ExecutionFilter, MarketOrder, Stock, StopLimitOrder,
                          StopOrder)
except Exception:  # pragma: no cover
    IB = None
    ExecutionFilter = None
    MarketOrder = None
    Stock = None
    StopLimitOrder = None
    StopOrder = None

logger = logging.getLogger("rvol_exec")

_RESTING = ("PreSubmitted", "Submitted", "ApiPending", "PendingSubmit", "ApiUpdate",
            "ValidationError")
_TERMINAL = ("Filled", "Cancelled", "ApiCancelled", "Inactive")
_SHORT_RE = re.compile(r"short|shortable|locate|short sale", re.I)
_TICK = 0.01


class _SystemClock:
    """Default clock; the lane injects its own (fake in the simulator)."""

    @staticmethod
    def now_et() -> datetime:
        return datetime.now(ET)

    @staticmethod
    def now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def sleep(secs: float) -> None:
        import time as _t
        _t.sleep(secs)


def is_short_unavailable_reason(code, msg) -> bool:
    """True for the three shapes IB uses when a short cannot be borrowed:
    201 'not available for short sale' wording, 10147 with shortable/locate
    wording, and 404 'held while securities are located' (spec §13.2)."""
    try:
        c = int(code) if code is not None else None
    except (TypeError, ValueError):
        c = None
    if c == 404:
        return True
    return bool(_SHORT_RE.search(str(msg or "")))


def role_of(order_type: str, parent_id: int = 0) -> str:
    """Order role from (orderType, parentId): entry parent / stop child / flatten."""
    ot = (order_type or "").upper()
    if ot == "STP LMT" and not parent_id:
        return "entry"
    if ot == "STP":
        return "stop"
    if ot == "MKT":
        return "flatten"
    return "other"


def fifo_positions(execs: list[dict]) -> dict[str, dict]:
    """Own positions from own executions ONLY (never `ib.positions()`).

    Per symbol: `qty` (signed net), `avg_px` (weighted price of the OPEN lots),
    `realized` (FIFO, gross), `commissions` (Σ known reports), `realized_net`
    (= realized − commissions), `n_exec`, `first_exec_time_et`, `last_exec_time_et`.
    Symbols with any own execution appear even when flat (qty 0) so realized
    P&L on closed names is reported."""
    out: dict[str, dict] = {}
    lots: dict[str, deque] = {}
    for e in sorted(execs, key=lambda x: (x.get("time_et") or "", x.get("exec_id") or "")):
        sym = e["symbol"]
        d = out.setdefault(sym, {"qty": 0.0, "avg_px": 0.0, "realized": 0.0,
                                 "commissions": 0.0, "realized_net": 0.0, "n_exec": 0,
                                 "first_exec_time_et": e.get("time_et"),
                                 "last_exec_time_et": e.get("time_et")})
        q = float(e["shares"]) * (1.0 if e["side"] == "BOT" else -1.0)
        px = float(e["price"])
        d["n_exec"] += 1
        d["last_exec_time_et"] = e.get("time_et")
        if e.get("commission") is not None:
            d["commissions"] += float(e["commission"])
        dq = lots.setdefault(sym, deque())
        while abs(q) > 1e-12 and dq and (dq[0][0] > 0) != (q > 0):
            lot = dq[0]
            close = min(abs(q), abs(lot[0]))
            if lot[0] > 0:                        # selling out of a long lot
                d["realized"] += (px - lot[1]) * close
            else:                                 # covering a short lot
                d["realized"] += (lot[1] - px) * close
            lot[0] -= close if lot[0] > 0 else -close
            q -= close if q > 0 else -close
            if abs(lot[0]) < 1e-12:
                dq.popleft()
        if abs(q) > 1e-12:
            dq.append([q, px])
    for sym, d in out.items():
        dq = lots.get(sym) or deque()
        tot = sum(abs(l[0]) for l in dq)
        d["qty"] = round(sum(l[0] for l in dq), 6)
        d["avg_px"] = round(sum(abs(l[0]) * l[1] for l in dq) / tot, 4) if tot else 0.0
        d["realized"] = round(d["realized"], 4)
        d["commissions"] = round(d["commissions"], 4)
        d["realized_net"] = round(d["realized"] - d["commissions"], 4)
    return out


class IbkrStockClient:
    """STK PAPER orders for the RVOL-EXEC lane, clientId 61, tag TAG."""

    TAG = TAG
    STAMP = STAMP

    def __init__(self, host: str = HOST, port: int = PORT, client_id: int = CLIENT_ID,
                 ib=None, connect: bool = True, clock=None, timeout: int = CONNECT_TIMEOUT_S):
        self.host = host
        self.port = port
        self.client_id = int(client_id)
        self.timeout = timeout
        self._clock = clock or _SystemClock()
        self._injected = ib is not None

        self._ib = ib
        self._connected = False                 # activated: connected AND 'D' account
        self._account: str | None = None
        self._wired = False
        self._trades: dict[int, object] = {}    # identity (permId first) -> Trade, OWN only
        self._by_order_id: dict[int, object] = {}
        self._contracts: dict[str, object] = {}
        self._exec_seen: dict[str, object] = {}  # execId -> Fill (own, from events)
        self._commissions: dict[str, float] = {}
        self.errors: dict[int, tuple] = {}      # order_id -> (code, msg) latest
        self._error_log: list[tuple] = []       # (seq, order_id, code, msg)
        self._err_seq = 0
        self.held: set[int] = set()             # 404 held while securities are located
        self._held_since: dict[int, datetime] = {}
        self.foreign_seen: set[tuple] = set()   # (identity, symbol, orderRef) logged once
        self.gateway_lost = False
        self.last_general_error: tuple | None = None
        self.quote_type: int | None = None
        self._tickers: dict[str, object] = {}
        self.connect_attempts = 0
        self.last_connect_failure: str | None = None   # refused | in_use | non_paper_account | accounts_failed

        if ib is not None:
            try:
                already = bool(ib.isConnected())
            except Exception:  # noqa: BLE001
                already = False
            if already:
                self._activate()
            elif connect:
                self.connect()
        elif connect and IB is not None:
            self.connect()

    # ── logging ──────────────────────────────────────────────────────────────
    @staticmethod
    def _log(msg: str) -> None:
        logger.info("[rvol-ib] %s", msg)

    @staticmethod
    def _warn(msg: str) -> None:
        logger.warning("[rvol-ib] %s", msg)

    # ── connection / activation (§13.3) ──────────────────────────────────────
    @staticmethod
    def _under_pytest() -> bool:
        return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules

    def connect(self) -> bool:
        """One connect attempt on the FIXED clientId. Returns True when
        activated. On failure the caller retries LATER on the SAME id — this
        method never tries another id (F4)."""
        if not self._injected:
            if self._under_pytest() and os.environ.get("ALLOW_LIVE_GATEWAY_TESTS") != "1":
                self._warn("REFUSING to open a gateway socket under pytest "
                           "(set ALLOW_LIVE_GATEWAY_TESTS=1 to allow) — client unavailable; "
                           "tests must inject a FakeIB")
                self._ib = None
                return False
            if self._ib is None:
                if IB is None:
                    self._warn("ib_async not importable — client unavailable")
                    return False
                self._ib = IB()
        self.connect_attempts += 1
        self.last_connect_failure = None
        try:
            self._ib.connect(self.host, self.port, clientId=self.client_id, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            self.last_connect_failure = "in_use" if "326" in str(exc) else "refused"
            self._warn(f"connect failed on clientId {self.client_id}: {exc} — will retry the "
                       "SAME id (a phantom session is paged, never dodged)")
            try:
                self._ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._connected = False
            return False
        return self._activate()

    def _activate(self) -> bool:
        """Verify a PAPER account and wire the event handlers (copy of the
        options client's `_activate`; the D-prefix rail is the safety abort)."""
        try:
            accts = list(self._ib.managedAccounts() or [])
        except Exception as exc:  # noqa: BLE001
            self.last_connect_failure = "accounts_failed"
            self._warn(f"managedAccounts() failed: {exc} — client unavailable")
            self._connected = False
            return False
        paper = [a for a in accts if a and str(a).startswith(ACCOUNT_PREFIX)]
        if not paper:
            self.last_connect_failure = "non_paper_account"
            self._warn(f"SAFETY ABORT: no paper ('{ACCOUNT_PREFIX}') account in {accts} — "
                       "REFUSING to trade (a live account must never place orders)")
            self._connected = False
            self._account = None
            return False
        self._account = paper[0]
        try:
            self._ib.RequestTimeout = REQUEST_TIMEOUT_S
        except Exception:  # noqa: BLE001
            pass
        if not self._wired:
            for name, handler in (("errorEvent", self._on_error),
                                  ("orderStatusEvent", self._on_order_status),
                                  ("execDetailsEvent", self._on_exec_details),
                                  ("commissionReportEvent", self._on_commission),
                                  ("disconnectedEvent", self._on_disconnected),
                                  ("connectedEvent", self._on_connected)):
                try:
                    ev = getattr(self._ib, name)
                    ev += handler
                    setattr(self._ib, name, ev)
                except Exception:  # noqa: BLE001
                    pass
            self._wired = True
        self._connected = True
        self.gateway_lost = False
        self._log(f"activated paper account {self._account} on clientId {self.client_id} "
                  f"(tag={TAG}) — {STAMP}")
        return True

    def reconnect(self) -> bool:
        """Bounded reconnect on the SAME clientId: RECONNECT_ATTEMPTS × RECONNECT_WAIT_S.
        False -> available() stays False; the caller's loop continues (a dead
        client never freezes the loop, L-2026-08-22)."""
        if self._ib is None:
            return self.connect()
        for attempt in range(1, RECONNECT_ATTEMPTS + 1):
            try:
                self._ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            if self.connect():
                self._log(f"reconnected on clientId {self.client_id} (attempt {attempt})")
                return True
            if attempt < RECONNECT_ATTEMPTS:
                self._clock.sleep(RECONNECT_WAIT_S)
        self._connected = False
        self._warn(f"reconnect FAILED after {RECONNECT_ATTEMPTS} attempts on clientId "
                   f"{self.client_id} — broker: unknown")
        return False

    def disconnect(self) -> None:
        try:
            if self._ib is not None:
                self._ib.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._connected = False

    def available(self) -> bool:
        if not (self._connected and self._account and self._ib is not None):
            return False
        try:
            return bool(self._ib.isConnected())
        except Exception:  # noqa: BLE001
            return False

    @property
    def account(self) -> str | None:
        return self._account

    # ── event handlers ───────────────────────────────────────────────────────
    def _on_error(self, reqId, errorCode, errorString, contract=None, *extra):
        try:
            code = int(errorCode)
        except (TypeError, ValueError):
            code = None
        msg = str(errorString or "")
        try:
            oid = int(reqId)
        except (TypeError, ValueError):
            oid = -1
        if code in (1100, 1300):
            self.gateway_lost = True
        elif code == 1102:
            self.gateway_lost = False
        if oid > 0:
            self._err_seq += 1
            self._error_log.append((self._err_seq, oid, code, msg))
            self.errors[oid] = (code, msg)
            if code == 404:
                self.held.add(oid)
                self._held_since.setdefault(oid, self._clock.now_et())
        else:
            self.last_general_error = (code, msg)

    def _on_order_status(self, trade):
        try:
            if not self._is_own(trade):
                return
            self._remember(trade)
            st = trade.orderStatus
            oid = int(getattr(trade.order, "orderId", 0) or 0)
            why = getattr(st, "whyHeld", "") or ""
            if st.status in _TERMINAL:
                self.held.discard(oid)                   # terminal wins over a stale whyHeld
            elif why.lower() == "locate":
                self.held.add(oid)
                self._held_since.setdefault(oid, self._clock.now_et())
            elif oid in self.held and not why:
                self.held.discard(oid)
        except Exception:  # noqa: BLE001
            pass

    def _on_exec_details(self, trade, fill):
        try:
            if self._is_own_fill(fill):
                self._exec_seen[str(fill.execution.execId)] = fill
        except Exception:  # noqa: BLE001
            pass

    def _on_commission(self, trade, fill, report):
        try:
            c = float(getattr(report, "commission", 0) or 0)
            if c > 0:
                self._commissions[str(getattr(report, "execId", "") or fill.execution.execId)] = c
        except Exception:  # noqa: BLE001
            pass

    def _on_disconnected(self):
        self._connected = False
        self.gateway_lost = True
        self._warn(f"gateway disconnected (clientId {self.client_id}) — broker: unknown "
                   "until reconnect")

    def _on_connected(self):
        pass                                        # activation must re-verify the account

    # ── ownership ────────────────────────────────────────────────────────────
    @staticmethod
    def _is_own(trade) -> bool:
        c = getattr(trade, "contract", None)
        o = getattr(trade, "order", None)
        return (getattr(c, "secType", "") == "STK"
                and (getattr(o, "orderRef", "") or "") == TAG)

    @staticmethod
    def _is_own_fill(fill) -> bool:
        c = getattr(fill, "contract", None)
        ex = getattr(fill, "execution", None)
        return (getattr(c, "secType", "") == "STK"
                and (getattr(ex, "orderRef", "") or "") == TAG)

    @staticmethod
    def _identity_of(trade):
        """permId FIRST (IBKR's permanent id; orderId is a per-session handle
        and completed records carry orderId=0) — copy of the options client."""
        o = getattr(trade, "order", None)
        perm = getattr(o, "permId", None)
        if isinstance(perm, int) and perm > 0:
            return perm
        oid = getattr(o, "orderId", None)
        if isinstance(oid, int) and oid > 0:
            return oid
        return None

    def _remember(self, trade) -> None:
        ident = self._identity_of(trade)
        if ident is None:
            return
        self._trades[ident] = trade
        oid = int(getattr(trade.order, "orderId", 0) or 0)
        if oid > 0:
            self._by_order_id[oid] = trade
            # `held` is rebuilt from BROKER TRUTH too (a restart must not forget
            # a 404-held short, or locate_expired() would never fire)
            st = trade.orderStatus
            why = (getattr(st, "whyHeld", "") or "").lower()
            if getattr(st, "status", "") in _TERMINAL:
                self.held.discard(oid)
            elif why == "locate":
                self.held.add(oid)
                self._held_since.setdefault(oid, self._clock.now_et())

    def _find_trade(self, order_id, refresh: bool = True):
        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            return None
        tr = self._by_order_id.get(oid) or self._trades.get(oid)
        if tr is None and refresh:
            self._refresh_from_broker()
            tr = self._by_order_id.get(oid) or self._trades.get(oid)
        return tr

    # ── contract qualification (§7.7) ────────────────────────────────────────
    def qualify(self, sym: str):
        """Stock(sym, 'SMART', 'USD') with a conId, else None (error 200)."""
        sym = str(sym).upper()
        c = self._contracts.get(sym)
        if c is not None and getattr(c, "conId", 0):
            return c
        if not self.available() or Stock is None:
            return None
        try:
            c = Stock(sym, "SMART", "USD")
            self._ib.qualifyContracts(c)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"qualify failed for {sym}: {exc}")
            return None
        if not getattr(c, "conId", 0):
            self._warn(f"qualify: no security definition for {sym} (error 200)")
            return None
        self._contracts[sym] = c
        return c

    # ── order construction (§8.1) ────────────────────────────────────────────
    def _stamp(self, order, tif: str = "DAY") -> None:
        order.orderRef = TAG
        order.tif = tif
        order.outsideRth = False
        order.transmit = True
        if self._account:
            order.account = self._account

    @staticmethod
    def _gtd_string(gtd_et) -> str:
        if isinstance(gtd_et, datetime):
            dt = gtd_et.astimezone(ET) if gtd_et.tzinfo else gtd_et
            return f"{dt.strftime('%Y%m%d %H:%M:%S')} US/Eastern"
        return str(gtd_et)

    def place_stop_limit_entry_with_stop_child(self, sym: str, side: str, qty, level: float,
                                               limit: float, stop_provisional: float,
                                               oca_group: str, tif: str = "GTD",
                                               gtd_et=None, _retry: bool = False) -> dict | None:
        """STP-LMT entry parent (transmit=False) + attached STP child (OCA,
        transmit=True) — both tagged. Returns a classify_post_result-compatible
        envelope: {"order": {"id", "parent_id", "child_id", "perm_id",
        "child_perm_id", "status"}} | {"error", "transport_error"?} | None."""
        if not self.available():
            return None
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return {"error": f"bad_qty: {qty!r}"}
        if qty < 1:
            return {"error": "qty_lt_1", "reason": "shares < 1"}
        contract = self.qualify(sym)
        if contract is None:
            return {"error": f"qualify_failed: {sym}", "reason": "qualify"}
        is_long = str(side).lower() in ("long", "buy")
        entry_action = "BUY" if is_long else "SELL"
        exit_action = "SELL" if is_long else "BUY"
        level = round(float(level), 2)
        limit = round(float(limit), 2)
        stop_provisional = round(float(stop_provisional), 2)
        tif = "GTD" if str(tif).upper() == "GTD" else "DAY"

        parent = StopLimitOrder(entry_action, qty, lmtPrice=limit, stopPrice=level)
        self._stamp(parent, tif)
        if tif == "GTD":
            if gtd_et is None:
                return {"error": "gtd_required", "reason": "tif GTD without gtd_et"}
            parent.goodTillDate = self._gtd_string(gtd_et)
        parent.triggerMethod = 2
        parent.transmit = False                                 # held until the child is sent

        child = StopOrder(exit_action, qty, stopPrice=stop_provisional)
        self._stamp(child, "DAY")
        child.ocaGroup = oca_group
        child.ocaType = 1
        child.triggerMethod = 2
        child.transmit = True

        # ── AMBIGUITY BOUNDARY (R1) — everything from here on may leave a LIVE
        # order at IBKR even if it raises: report transport_error, never a reject.
        try:
            t_parent = self._ib.placeOrder(contract, parent)
            child.parentId = int(parent.orderId)
            t_child = self._ib.placeOrder(contract, child)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"place AMBIGUOUS for {sym}: {exc} — order may be live; intent stays "
                       "unbound for recovery")
            return {"error": f"place_failed: {exc}", "transport_error": True}
        try:
            self._remember(t_parent)
            self._remember(t_child)
            self._settle(t_parent)
            pid = int(t_parent.order.orderId)
            cid = int(t_child.order.orderId)
            status = self._status_of(t_parent)
            code, reason = self._last_error(pid)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"post-place confirmation AMBIGUOUS for {sym}: {exc}")
            return {"error": f"post_place_confirm_failed: {exc}", "transport_error": True}

        if status == "rejected":
            reason = self._reject_reason(t_parent) or reason or "order rejected"
            if code == 110 and not _retry:
                # §8.3 price conformity: re-round to the tick and retry ONCE
                self._warn(f"{sym}: error 110 (price increment) — re-rounding and retrying once")
                return self.place_stop_limit_entry_with_stop_child(
                    sym, side, qty, self._reround(level), self._reround(limit),
                    self._reround(stop_provisional), oca_group, tif, gtd_et, _retry=True)
            env = {"error": reason, "reason": reason, "code": code,
                   "order": {"id": pid, "parent_id": pid, "child_id": cid,
                             "perm_id": getattr(t_parent.order, "permId", 0),
                             "status": "rejected"}}
            if is_short_unavailable_reason(code, reason):
                env["short_unavailable"] = True
                self._log(f"{sym}: SHORT UNAVAILABLE ({code}: {reason})")
            else:
                self._warn(f"{sym}: entry REJECTED order {pid}: {code} {reason}")
            return env
        self._log(f"placed {entry_action} {qty} {sym} STP-LMT stop={level} lmt={limit} "
                  f"tif={tif} -> parent {pid}; child STP {exit_action} @ {stop_provisional} "
                  f"-> {cid} (oca={oca_group}) status={status} tag={TAG}")
        return {"order": {"id": pid, "parent_id": pid, "child_id": cid,
                          "perm_id": getattr(t_parent.order, "permId", 0),
                          "child_perm_id": getattr(t_child.order, "permId", 0),
                          "status": status, "oca_group": oca_group, "tif": tif,
                          "held": pid in self.held}}

    @staticmethod
    def _reround(px: float, inc: float = _TICK) -> float:
        return round(round(float(px) / inc) * inc, 2)

    def amend_stop(self, child_id, new_stop_px: float, new_qty=None) -> dict:
        """Modify the SAME child order (re-place with its orderId): price and
        optionally quantity. A refused modify rolls the local object back."""
        if not self.available():
            return {"error": "unavailable"}
        tr = self._find_trade(child_id)
        if tr is None:
            return {"error": "not_found", "order": {"id": child_id, "status": "not_found"}}
        if tr.orderStatus.status in _TERMINAL:
            return {"error": "not_modifiable", "order": {"id": int(tr.order.orderId),
                                                         "status": self._status_of(tr)}}
        oid = int(tr.order.orderId)
        prev = (tr.order.auxPrice, tr.order.totalQuantity)
        tr.order.auxPrice = round(float(new_stop_px), 2)
        if new_qty is not None:
            tr.order.totalQuantity = int(new_qty)
        mark = self._err_seq
        try:
            self._ib.placeOrder(tr.contract, tr.order)
        except Exception as exc:  # noqa: BLE001
            tr.order.auxPrice, tr.order.totalQuantity = prev
            return {"error": f"amend_failed: {exc}", "transport_error": True,
                    "order": {"id": oid}}
        self._settle(tr)
        err = self._errors_since(mark, oid)
        if err is not None:
            tr.order.auxPrice, tr.order.totalQuantity = prev
            return {"error": err[1], "code": err[0],
                    "order": {"id": oid, "status": self._status_of(tr)}}
        return {"order": {"id": oid, "status": self._status_of(tr),
                          "aux_price": tr.order.auxPrice, "qty": tr.order.totalQuantity}}

    def cancel(self, order_id) -> dict:
        """cancelOrder with 10147/10148 mapped (§13.2): both re-read broker
        truth and say so — never assume the order is fine."""
        if not self.available():
            return {"error": "unavailable"}
        tr = self._find_trade(order_id)
        if tr is None:
            return {"error": "not_found", "order": {"id": order_id, "status": "not_found"}}
        oid = int(tr.order.orderId)
        mark = self._err_seq
        try:
            self._ib.cancelOrder(tr.order)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"cancel {oid} AMBIGUOUS: {exc}")
            return {"error": f"cancel_failed: {exc}", "transport_error": True,
                    "order": {"id": oid}}
        self._settle(tr)
        err = self._errors_since(mark, oid)
        if err is not None and err[0] == 10147:
            self._refresh_from_broker()
            return {"error": "cancel_target_not_found", "code": 10147, "reread": True,
                    "order": {"id": oid, "status": self._status_of(tr)}}
        if err is not None and err[0] == 10148:
            self._refresh_from_broker()
            return {"code": 10148, "reread": True,
                    "order": {"id": oid, "status": self._status_of(tr)}}
        status = self._status_of(tr)
        self._log(f"cancel requested for order {oid} -> {status}")
        return {"order": {"id": oid, "status": status}}

    def flatten_market(self, sym: str, qty, side: str, oca_group: str | None = None) -> dict | None:
        """MKT for OWN qty (never the account position), joining the child's
        OCA group so the fill cancels the stop (§8.6). If the OCA-join is
        rejected, a plain MKT is placed (`mode: plain`) and the caller must
        cancel the child on its fill."""
        if not self.available():
            return None
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return {"error": f"bad_qty: {qty!r}"}
        if qty < 1:
            return {"error": "qty_lt_1"}
        contract = self.qualify(sym)
        if contract is None:
            return {"error": f"qualify_failed: {sym}", "reason": "qualify"}
        s = str(side).lower()
        action = "SELL" if s in ("long", "sell") else "BUY"
        res = self._place_mkt(contract, action, qty, oca_group or "")
        if oca_group and res.get("order", {}).get("status") == "rejected":
            self._warn(f"{sym}: OCA-join flatten rejected ({res.get('error')}) — placing a "
                       "plain MKT; the child must be cancelled on its fill")
            res2 = self._place_mkt(contract, action, qty, "")
            res2["mode"] = "plain"
            res2["child_cancel_required"] = True
            res2["oca_join_reject"] = res.get("error")
            return res2
        res["mode"] = "oca" if oca_group else "plain"
        res["child_cancel_required"] = not bool(oca_group)
        return res

    def place_stop_order(self, sym: str, action: str, qty, stop_px: float,
                         oca_group: str | None = None) -> dict | None:
        """A STANDALONE STP (no parent) for the stop reconciler (§11.4
        `stop_missing_replaced`) and §8.2 `stop_replaced`: same tag, same OCA
        group as the original child so a flatten MKT can still join it.
        Envelope shape as the other placements."""
        if not self.available():
            return None
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return {"error": f"bad_qty: {qty!r}"}
        if qty < 1:
            return {"error": "qty_lt_1"}
        contract = self.qualify(sym)
        if contract is None:
            return {"error": f"qualify_failed: {sym}", "reason": "qualify"}
        act = "SELL" if str(action).upper() in ("SELL", "LONG") else "BUY"
        stp = StopOrder(act, qty, stopPrice=round(float(stop_px), 2))
        self._stamp(stp, "DAY")
        stp.ocaGroup = oca_group or ""
        stp.ocaType = 1 if oca_group else 0
        stp.triggerMethod = 2
        try:
            tr = self._ib.placeOrder(contract, stp)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"stop place AMBIGUOUS for {sym}: {exc}")
            return {"error": f"place_failed: {exc}", "transport_error": True}
        try:
            self._remember(tr)
            self._settle(tr)
            oid = int(tr.order.orderId)
            status = self._status_of(tr)
            code, reason = self._last_error(oid)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"post_place_confirm_failed: {exc}", "transport_error": True}
        if status == "rejected":
            reason = self._reject_reason(tr) or reason or "order rejected"
            return {"error": reason, "reason": reason, "code": code,
                    "order": {"id": oid, "perm_id": getattr(tr.order, "permId", 0),
                              "status": "rejected"}}
        self._log(f"stop {act} {qty} {sym} STP @ {stp.auxPrice} oca={oca_group!r} -> {oid} "
                  f"status={status} tag={TAG}")
        return {"order": {"id": oid, "perm_id": getattr(tr.order, "permId", 0),
                          "status": status, "oca_group": oca_group or "", "aux_price": stp.auxPrice}}

    def _place_mkt(self, contract, action: str, qty: int, oca_group: str) -> dict:
        mkt = MarketOrder(action, qty)
        self._stamp(mkt, "DAY")
        mkt.ocaGroup = oca_group
        mkt.ocaType = 1 if oca_group else 0
        try:
            tr = self._ib.placeOrder(contract, mkt)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"flatten place AMBIGUOUS for {contract.symbol}: {exc}")
            return {"error": f"place_failed: {exc}", "transport_error": True}
        try:
            self._remember(tr)
            self._settle(tr)
            oid = int(tr.order.orderId)
            status = self._status_of(tr)
            code, reason = self._last_error(oid)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"post_place_confirm_failed: {exc}", "transport_error": True}
        if status == "rejected":
            reason = self._reject_reason(tr) or reason or "order rejected"
            return {"error": reason, "reason": reason, "code": code,
                    "order": {"id": oid, "perm_id": getattr(tr.order, "permId", 0),
                              "status": "rejected"}}
        self._log(f"flatten {action} {qty} {contract.symbol} MKT oca={oca_group!r} -> {oid} "
                  f"status={status} tag={TAG}")
        return {"order": {"id": oid, "perm_id": getattr(tr.order, "permId", 0),
                          "status": status, "oca_group": oca_group}}

    # ── broker truth ─────────────────────────────────────────────────────────
    def _refresh_from_broker(self) -> bool:
        """openTrades ∪ reqAllOpenOrders ∪ reqCompletedOrders(False), filtered
        to OURS (STK ∧ TAG), keyed permId-first. False = a read FAILED (the
        caller must surface None, never a silent [])."""
        if self._ib is None:
            return False
        try:
            trades = list(self._ib.openTrades() or [])
            trades += list(self._ib.reqAllOpenOrders() or [])
            trades += list(self._ib.reqCompletedOrders(False) or [])
        except Exception as exc:  # noqa: BLE001
            self._warn(f"broker order refresh FAILED: {exc} — reads report None (unknown)")
            return False
        for tr in trades:
            ident = self._identity_of(tr)
            if ident is None:
                continue                          # unidentifiable record is not evidence
            if not self._is_own(tr):
                key = (ident, getattr(getattr(tr, "contract", None), "symbol", "?"),
                       getattr(getattr(tr, "order", None), "orderRef", "") or "")
                if key not in self.foreign_seen:
                    self.foreign_seen.add(key)
                    self._log(f"IGNORING foreign broker order {key[0]} ({key[1]}, "
                              f"secType={getattr(tr.contract, 'secType', '?')}, "
                              f"orderRef={key[2]!r}) — not ours; never cancelled")
                continue
            self._remember(tr)
        return True

    def get_own_orders(self, open_only: bool = False) -> list[dict] | None:
        if not self.available():
            return None
        if not self._refresh_from_broker():
            return None
        out, seen = [], set()
        for tr in list(self._trades.values()):
            if id(tr) in seen:
                continue
            seen.add(id(tr))
            self._settle(tr)
            d = self._normalize_order(tr)
            if open_only and d["status"] in ("filled", "canceled", "rejected"):
                continue
            out.append(d)
        out.sort(key=lambda d: (d["symbol"] or "", d["perm_id"] or 0, d["id"] or 0))
        return out

    def order_status(self, order_id) -> dict | None:
        if not self.available():
            return None
        tr = self._find_trade(order_id)
        if tr is None:
            return None
        self._settle(tr)
        return self._normalize_order(tr)

    def match_own_order(self, symbol: str, role: str, action: str | None = None,
                        parent_id: int | None = None, oca_group: str | None = None,
                        qty=None, lmt_price=None, aux_price=None,
                        exclude_ids=()) -> dict | None:
        """Role-key binding for recovery (§10.5): among own orders not already
        bound, the one matching (symbol, role, and any given keys)."""
        orders = self.get_own_orders()
        if orders is None:
            return None
        cands = []
        for d in orders:
            if d["symbol"] != symbol or d["role"] != role:
                continue
            if d["id"] in exclude_ids or d["perm_id"] in exclude_ids:
                continue
            if action is not None and d["action"] != action:
                continue
            if parent_id is not None and int(d["parent_id"] or 0) != int(parent_id):
                continue
            if oca_group is not None and d["oca_group"] != oca_group:
                continue
            if qty is not None and abs(float(d["qty"]) - float(qty)) > 1e-9:
                continue
            if lmt_price is not None and abs(float(d["lmt_price"]) - float(lmt_price)) > 1e-9:
                continue
            if aux_price is not None and abs(float(d["aux_price"]) - float(aux_price)) > 1e-9:
                continue
            cands.append(d)
        return cands[0] if cands else None

    def get_own_executions(self) -> list[dict] | None:
        """Own executions today: `ib.fills()` ∪ event-collected, with a
        `reqExecutions(ExecutionFilter(secType='STK', time=<today 00:00 ET>))`
        re-pull when `fills()` is empty. Filter: STK ∧ execution.orderRef == TAG."""
        if not self.available():
            return None
        try:
            fills = list(self._ib.fills() or [])
            if not fills and ExecutionFilter is not None:
                since = self._clock.now_et().strftime("%Y%m%d-00:00:00")
                fills = list(self._ib.reqExecutions(
                    ExecutionFilter(secType="STK", time=since)) or [])
        except Exception as exc:  # noqa: BLE001
            self._warn(f"executions read FAILED: {exc} — None (unknown)")
            return None
        merged: dict[str, object] = {}
        for f in fills:
            try:
                merged[str(f.execution.execId)] = f
            except Exception:  # noqa: BLE001
                continue
        for k, f in self._exec_seen.items():
            merged.setdefault(k, f)
        out = [self._normalize_fill(f) for f in merged.values() if self._is_own_fill(f)]
        out.sort(key=lambda d: (d["time_et"] or "", d["exec_id"]))
        return out

    def get_own_positions(self) -> dict[str, dict] | None:
        execs = self.get_own_executions()
        if execs is None:
            return None
        return fifo_positions(execs)

    def foreign_stk_symbols(self) -> set[str] | None:
        """Symbols the ACCOUNT holds as STK (any owner) — the §7.6 exclusion
        set. None when the read fails."""
        if not self.available():
            return None
        try:
            positions = list(self._ib.positions() or [])
        except Exception as exc:  # noqa: BLE001
            self._warn(f"positions() failed: {exc}")
            return None
        return {p.contract.symbol for p in positions
                if getattr(p.contract, "secType", "") == "STK"
                and float(getattr(p, "position", 0) or 0) != 0}

    def positions_bound(self) -> dict[str, float] | None:
        """Account STK position per symbol — a CONSISTENCY read only (§11.7),
        never a source of own state."""
        if not self.available():
            return None
        try:
            positions = list(self._ib.positions() or [])
        except Exception as exc:  # noqa: BLE001
            self._warn(f"positions() failed: {exc}")
            return None
        out: dict[str, float] = {}
        for p in positions:
            if getattr(p.contract, "secType", "") != "STK":
                continue
            out[p.contract.symbol] = out.get(p.contract.symbol, 0.0) + float(p.position or 0)
        return out

    # ── quotes (F11: may be absent; §11.10 degradation is the lane's) ────────
    def subscribe_quotes(self, syms, market_data_type: int = 1) -> set[str]:
        subscribed: set[str] = set()
        if not self.available():
            return subscribed
        try:
            self._ib.reqMarketDataType(int(market_data_type))
            self.quote_type = int(market_data_type)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"reqMarketDataType({market_data_type}) failed: {exc}")
        for s in syms:
            c = self.qualify(s)
            if c is None:
                continue
            try:
                self._tickers[str(s).upper()] = self._ib.reqMktData(c, "", False, False)
                subscribed.add(str(s).upper())
            except Exception as exc:  # noqa: BLE001
                self._warn(f"reqMktData failed for {s}: {exc}")
        return subscribed

    def last(self, sym: str) -> dict | None:
        tk = self._tickers.get(str(sym).upper())
        if tk is None:
            return None
        try:
            self._ib.sleep(0)
        except Exception:  # noqa: BLE001
            pass

        def _ok(v):
            try:
                return v is not None and math.isfinite(float(v)) and float(v) > 0
            except (TypeError, ValueError):
                return False

        last = float(tk.last) if _ok(getattr(tk, "last", None)) else None
        bid = float(tk.bid) if _ok(getattr(tk, "bid", None)) else None
        ask = float(tk.ask) if _ok(getattr(tk, "ask", None)) else None
        mark = last if last is not None else ((bid + ask) / 2 if bid and ask else None)
        if mark is None:
            return None                              # a -1/NaN quote is "no mark"
        t = getattr(tk, "time", None)
        age = None
        if isinstance(t, datetime):
            try:
                age = (self._clock.now_utc() - t.astimezone(timezone.utc)).total_seconds()
            except Exception:  # noqa: BLE001
                age = None
        return {"last": last, "bid": bid, "ask": ask, "mark": mark,
                "time": t.isoformat() if isinstance(t, datetime) else None, "age_s": age,
                "source": "ibkr_delayed" if self.quote_type == 3 else "ibkr_live"}

    def unsubscribe_all(self) -> None:
        for s, tk in list(self._tickers.items()):
            try:
                self._ib.cancelMktData(tk.contract)
            except Exception:  # noqa: BLE001
                pass
        self._tickers = {}

    # ── short / locate (§13.2) ───────────────────────────────────────────────
    def held_for_s(self, order_id) -> float | None:
        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            return None
        if oid not in self.held:
            return None
        since = self._held_since.get(oid)
        if since is None:
            return 0.0
        return max(0.0, (self._clock.now_et() - since).total_seconds())

    def locate_expired(self, order_id) -> bool:
        """404 'held while securities are located' has persisted longer than
        SHORT_LOCATE_WAIT_S — the lane cancels and books short_unavailable."""
        h = self.held_for_s(order_id)
        return h is not None and h >= SHORT_LOCATE_WAIT_S

    # ── status mapping (copies) ──────────────────────────────────────────────
    def _settle(self, trade, cycles: int = 3) -> None:
        for _ in range(cycles):
            try:
                self._ib.sleep(0)
            except Exception:  # noqa: BLE001
                break

    def _last_error(self, oid: int) -> tuple:
        e = self.errors.get(int(oid))
        return (e[0], e[1]) if e else (None, None)

    def _errors_since(self, mark: int, oid: int):
        hit = None
        for seq, o, code, msg in self._error_log:
            if seq > mark and o == int(oid):
                hit = (code, msg)
        return hit

    def _reject_reason(self, trade) -> str | None:
        """Copy of the options client's: captured error first (a plain 202
        cancel notice is NOT a reject), then the terminal trade-log message."""
        oid = int(getattr(trade.order, "orderId", 0) or 0)
        if oid in self.errors:
            code, msg = self.errors[oid]
            if self._is_cancel_notice(code, msg):
                return None
            if code in (10147, 10148, 404, 399, 202):
                return None                          # informational / cancel-path codes
            return msg
        for entry in (getattr(trade, "log", None) or []):
            st = getattr(entry, "status", "")
            msg = getattr(entry, "message", "")
            code = getattr(entry, "errorCode", None)
            if st in ("Cancelled", "Inactive", "Rejected") and msg and \
                    not self._is_cancel_notice(code, msg):
                return msg
        return None

    @staticmethod
    def _is_cancel_notice(code, msg) -> bool:
        """IB 202 / 'Order Canceled - reason:' with an EMPTY reason = cancelled,
        not refused (copy of the options client's)."""
        m = str(msg or "").lower()
        mm = re.search(r"order cancel+ed\s*-\s*reason:\s*(.*)$", m)
        if mm is not None:
            return mm.group(1).strip() == ""
        if "202" in m and "cancel" in m and "reason" not in m:
            return True
        try:
            return code is not None and int(code) == 202 and not m
        except (TypeError, ValueError):
            return False

    def _status_of(self, trade) -> str:
        """pending | open | partially_filled | filled | canceled | rejected.

        Copy of the options client's mapping with ONE deliberate difference:
        terminal states are decided FIRST, so a parent cancelled after a
        partial (10:25 / first-10 / any cancel) reads `canceled` with
        `filled > 0` in the normalized dict — the §9 "parent terminal with
        0 < filled < qty -> held" transition needs the terminal word, and the
        executed quantity is always carried separately. The phantom rule is
        kept verbatim: a 'Filled' word with filled == 0 executed NOTHING."""
        st = trade.orderStatus
        raw = getattr(st, "status", "") or ""
        filled = float(getattr(st, "filled", 0) or 0)
        remaining = float(getattr(st, "remaining", 0) or 0)
        if raw == "Filled" and filled <= 0:
            self._warn(f"IGNORING phantom 'Filled' record with filled=0.0 for order "
                       f"{getattr(trade.order, 'orderId', '?')} — no execution, not a fill")
            return "canceled"
        if raw == "Filled" or (filled > 0 and remaining == 0):
            return "filled"
        reject = self._reject_reason(trade)
        if raw in ("Cancelled", "ApiCancelled", "Inactive"):
            return "rejected" if (reject or raw == "Inactive") else "canceled"
        if filled > 0 and remaining > 0:
            return "partially_filled"
        if reject:
            return "rejected"
        if raw == "PendingCancel":
            return "canceled"
        if raw in ("PendingSubmit", "PreSubmitted", "ApiPending"):
            return "pending"
        return "open"

    def _normalize_order(self, trade) -> dict:
        o, st, c = trade.order, trade.orderStatus, trade.contract
        oid = int(getattr(o, "orderId", 0) or 0)
        status = self._status_of(trade)
        code, msg = self._last_error(oid) if oid else (None, None)
        reason = self._reject_reason(trade) if status == "rejected" else None
        return {
            "id": oid, "perm_id": int(getattr(o, "permId", 0) or 0),
            "client_id": int(getattr(o, "clientId", 0) or 0),
            "symbol": getattr(c, "symbol", None), "con_id": int(getattr(c, "conId", 0) or 0),
            "sec_type": getattr(c, "secType", None),
            "action": getattr(o, "action", None), "order_type": getattr(o, "orderType", None),
            "role": role_of(getattr(o, "orderType", ""), int(getattr(o, "parentId", 0) or 0)),
            "qty": float(getattr(o, "totalQuantity", 0) or 0),
            "lmt_price": float(getattr(o, "lmtPrice", 0) or 0),
            "aux_price": float(getattr(o, "auxPrice", 0) or 0),
            "parent_id": int(getattr(o, "parentId", 0) or 0),
            "oca_group": getattr(o, "ocaGroup", "") or "",
            "oca_type": int(getattr(o, "ocaType", 0) or 0),
            "tif": getattr(o, "tif", None), "good_till_date": getattr(o, "goodTillDate", "") or "",
            "transmit": bool(getattr(o, "transmit", True)),
            "raw_status": getattr(st, "status", ""), "status": status,
            "filled": float(getattr(st, "filled", 0) or 0),
            "remaining": float(getattr(st, "remaining", 0) or 0),
            "avg_fill_price": float(getattr(st, "avgFillPrice", 0) or 0),
            "why_held": getattr(st, "whyHeld", "") or "",
            "held": oid in self.held,
            "tag": getattr(o, "orderRef", "") or "",
            "reject_code": code if status == "rejected" else None,
            "reject_reason": reason,
            "short_unavailable": bool(status == "rejected"
                                      and is_short_unavailable_reason(code, reason or msg)),
        }

    def _normalize_fill(self, fill) -> dict:
        ex, c = fill.execution, fill.contract
        t = getattr(ex, "time", None)
        if isinstance(t, datetime):
            t_et = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(ET).isoformat()
        else:
            t_et = None
        rep = getattr(fill, "commissionReport", None)
        comm = float(getattr(rep, "commission", 0) or 0) if rep is not None else 0.0
        if comm <= 0:
            comm = self._commissions.get(str(ex.execId))
        oid = int(getattr(ex, "orderId", 0) or 0)
        perm = int(getattr(ex, "permId", 0) or 0)
        # permId FIRST: a prior-session completed record carries orderId=0, so
        # only the permId ties a recovered fill back to its order's role
        tr = self._trades.get(perm) if perm > 0 else None
        if tr is None:
            tr = self._by_order_id.get(oid)
        role = role_of(getattr(tr.order, "orderType", ""), int(getattr(tr.order, "parentId", 0) or 0)) \
            if tr is not None else None
        return {
            "exec_id": str(ex.execId), "perm_id": int(getattr(ex, "permId", 0) or 0),
            "order_id": oid, "client_id": int(getattr(ex, "clientId", 0) or 0),
            "symbol": getattr(c, "symbol", None), "con_id": int(getattr(c, "conId", 0) or 0),
            "side": getattr(ex, "side", None), "shares": float(getattr(ex, "shares", 0) or 0),
            "price": float(getattr(ex, "price", 0) or 0), "time_et": t_et,
            "cum_qty": float(getattr(ex, "cumQty", 0) or 0),
            "avg_price": float(getattr(ex, "avgPrice", 0) or 0),
            "commission": comm, "tag": getattr(ex, "orderRef", "") or "", "role": role,
        }
