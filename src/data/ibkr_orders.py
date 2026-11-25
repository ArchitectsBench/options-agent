"""IBKR PAPER option-order execution — the drop-in replacement for
`PaperOrderClient` (src/data/tradier_orders.py) during the options-book
migration.

HARD SAFETY RAILS
-----------------
1. PAPER ONLY. Connects to the IBKR **paper** gateway (127.0.0.1:4002) and
   REFUSES to activate unless `managedAccounts()` reports a paper account
   (IB paper accounts are 'D'-prefixed, e.g. DU1234567). A production/live
   account short-circuits `available()` to False — no order can be placed.
2. Every order is tagged `UNVALIDATED-PAPER-ONLY` (Order.orderRef) — the same
   tag the Tradier path uses, so reconciliation's tag-scoped ownership logic
   keeps working unchanged.
3. No real connection is ever opened in tests: the whole ib_async layer is
   injectable (`IbkrOrderClient(ib=<mock>)`) and mocked.

WHY THIS EXISTS
---------------
`src/engine/paper_trader.py` is the only caller. It talks to the broker through
exactly 7 methods and depends on the Tradier RESPONSE SHAPES. This adapter
exposes the identical surface and NORMALIZES ib_async objects into those exact
dict shapes so paper_trader works unchanged:

  available()            -> bool
  place_option_order()   -> {"order": {"id", "status"}} | {"error": ...} | None
  place_spread_order()   -> {"order": {"id", "status"}} | {"error": ...} | None
  get_order(id)          -> ORDER dict (STATUS enum, leg[]/single-leg fields) | None
  cancel_order(id)       -> dict | None
  get_orders()           -> [ORDER dict, ...]
  get_positions()        -> [{"symbol"(OCC), "quantity"(signed), "cost_basis"($)}]

IB order-state -> Tradier STATUS enum
  Submitted                         -> "open"
  PendingSubmit/PreSubmitted/ApiPending -> "pending"
  filled>0 & remaining>0            -> "partially_filled"
  Filled (remaining==0)            -> "filled"
  Cancelled/ApiCancelled/PendingCancel -> "canceled"
  Inactive/Cancelled + reject reason   -> "rejected"  (BP/margin rejects included,
                                          reason string names the buying power)

Adapter-internal failure modes (each normalized + one log line + a test):
  order rejection, partial fill, buying-power limit, duplicate-order protection.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

# ib_async is imported at module level so tests can monkeypatch these names
# (patch.object(ibkr_orders, "IB", MockIB)). The import is guarded so this
# module — and therefore src.data.order_client / src.engine.paper_trader —
# always imports even on a host without ib_async (available() just returns
# False, i.e. graceful degradation, never an import crash).
try:  # pragma: no cover - exercised via the mocked path in tests
    from ib_async import IB, ComboLeg, Contract, LimitOrder, Option
except Exception:  # pragma: no cover
    IB = None
    ComboLeg = None
    Contract = None
    LimitOrder = None
    Option = None

logger = logging.getLogger("trader")

ORDER_TAG = "UNVALIDATED-PAPER-ONLY"
HOST = "127.0.0.1"
DEFAULT_PORT = 4002            # IBKR PAPER gateway (matches executor.DEFAULT_PORT)
DEFAULT_CLIENT_ID = 42         # distinct from the trendbook executor (23)
CONNECT_TIMEOUT_S = 20
OPTION_MULTIPLIER = "100"
DEDUP_WINDOW_S = 10.0          # refuse an identical live order inside this window

# reject-reason keywords that identify a buying-power / margin rejection
_BP_KEYWORDS = ("buying power", "insufficient", "margin", "available funds",
                "exceed", "not enough")


# ── OCC <-> IBKR contract mapping ────────────────────────────────────────────

def parse_occ(occ: str) -> tuple[str, str, str, float]:
    """OCC option symbol -> (root, 'YYYYMMDD', right('C'|'P'), strike).

    OCC = ROOT + YYMMDD + C/P + 8-digit strike(thousandths); the 15-char
    suffix is fixed, so root = occ[:-15] (mirrors paper_trader's sym[:-15]).
    Example: 'SMCI260717P00028500' -> ('SMCI','20260717','P',28.5).
    """
    if not occ or len(occ) <= 15:
        raise ValueError(f"not an OCC option symbol: {occ!r}")
    root = occ[:-15]
    ymd = occ[-15:-9]           # YYMMDD
    right = occ[-9].upper()
    if right not in ("C", "P"):
        raise ValueError(f"bad OCC right {right!r} in {occ!r}")
    strike = int(occ[-8:]) / 1000.0
    return root, f"20{ymd}", right, strike


def occ_from_fields(root: str, yyyymmdd: str, right: str, strike: float) -> str:
    """Inverse of parse_occ — rebuild the OCC symbol from contract fields."""
    yymmdd = yyyymmdd[2:8]
    r = "C" if str(right).upper().startswith("C") else "P"
    strike_i = int(round(float(strike) * 1000))
    return f"{root}{yymmdd}{r}{strike_i:08d}"


class IbkrOrderClient:
    """Option PAPER orders against the IBKR paper gateway, normalized to the
    Tradier `PaperOrderClient` response shapes."""

    def __init__(self, host: str = HOST, port: int = DEFAULT_PORT,
                 client_id: int = DEFAULT_CLIENT_ID, timeout: int = CONNECT_TIMEOUT_S,
                 dedup_window_s: float = DEDUP_WINDOW_S, ib=None, connect: bool = True):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.timeout = timeout
        self.dedup_window_s = dedup_window_s

        self._ib = None
        self._connected = False
        self._account: str | None = None
        self._trades: dict[int, object] = {}          # order_id -> Trade
        self._meta: dict[int, dict] = {}              # order_id -> normalization meta
        self._errors: dict[int, tuple] = {}           # order_id -> (code, message)
        self._partial_logged: set[int] = set()
        self._foreign_logged: set[int] = set()  # unattributable orders, logged once
        self._recent: dict[tuple, tuple] = {}         # dedup key -> (order_id, monotonic ts)

        if ib is not None:                            # injected (tests) — no socket
            self._ib = ib
            self._activate()
        elif connect and IB is not None:              # real paper gateway
            self._connect()

    # ── logging helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _log(msg: str) -> None:
        logger.info("[ibkr] %s", msg)

    @staticmethod
    def _warn(msg: str) -> None:
        logger.warning("[ibkr] %s", msg)

    # ── connection / activation ──────────────────────────────────────────────
    @staticmethod
    def _under_pytest() -> bool:
        import os
        import sys
        return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules

    def _connect(self) -> None:
        """Connect to the paper gateway, retrying on a small clientId family in
        case a phantom session holds the default id (mirrors executor)."""
        # ITEM 8 HERMETICITY (2026-08-05): a test run must NEVER open a real
        # gateway socket. Leaving broker=ibkr_paper in config made the SUITE
        # resolve to IBKR and hit the live gateway — 21 failures with genuine
        # Error 200s. Tests must depend on their mocks, not on the operator's
        # live config. Opt in explicitly with ALLOW_LIVE_GATEWAY_TESTS=1.
        import os
        if self._under_pytest() and os.environ.get("ALLOW_LIVE_GATEWAY_TESTS") != "1":
            self._warn("REFUSING to connect to the live gateway under pytest "
                       "(set ALLOW_LIVE_GATEWAY_TESTS=1 to allow) — adapter "
                       "unavailable, tests must use an injected mock")
            self._ib = None
            return
        self._ib = IB()
        last_err = None
        for cid in (self.client_id, self.client_id + 10,
                    self.client_id + 20, self.client_id + 30):
            try:
                self._ib.connect(self.host, self.port, clientId=cid, timeout=self.timeout)
                if cid != self.client_id:
                    self._log(f"connected on fallback clientId {cid}")
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                try:
                    self._ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        else:
            self._warn(f"connect failed on all clientIds: {last_err} — adapter unavailable")
            return
        self._activate()

    def _activate(self) -> None:
        """Verify a PAPER account and wire the reject-capturing error handler."""
        try:
            accts = list(self._ib.managedAccounts() or [])
        except Exception as exc:  # noqa: BLE001
            self._warn(f"managedAccounts() failed: {exc} — adapter unavailable")
            return
        paper = [a for a in accts if a and str(a).startswith("D")]
        if not paper:
            self._warn(f"SAFETY ABORT: no paper ('D') account in {accts} — "
                       "REFUSING to trade (live account must never place orders)")
            self._connected = False
            self._account = None
            return
        self._account = paper[0]
        try:
            self._ib.errorEvent += self._on_error
        except Exception:  # noqa: BLE001
            pass
        self._connected = True
        self._log(f"activated paper account {self._account} (broker=ibkr_paper, "
                  f"tag={ORDER_TAG})")

    def _on_error(self, reqId, errorCode, errorString, contract=None, *extra):
        """IB error/reject callback. Order-scoped errors carry reqId == orderId;
        record the reason so status mapping can classify the order as rejected
        (incl. buying-power / margin rejects, codes 201/202/...)."""
        try:
            if reqId and int(reqId) in self._trades:
                self._errors[int(reqId)] = (errorCode, str(errorString))
        except Exception:  # noqa: BLE001
            pass

    def disconnect(self) -> None:  # pragma: no cover - real path only
        try:
            if self._ib is not None:
                self._ib.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._connected = False

    # ── the 7-method surface ─────────────────────────────────────────────────
    def available(self) -> bool:
        return bool(self._connected and self._account)

    RECONNECT_MIN_GAP_S = 240     # at most one attempt per 5-minute cycle

    def reconnect(self) -> bool:
        """C-12.2 (2026-09-28): re-attempt the gateway connection IN SESSION.

        The constructor connects once. On 09-28 that one attempt met a gateway
        that was not logged in yet, and the engine then ran 78 cycles with no
        broker although the gateway came up during the day. No-op when already
        connected; rate-limited; reuses _connect() (clientId family, paper-account
        check, error handler). MUST be called on the thread that owns the client
        (ThreadConfinedIbkrClient marshals it)."""
        if self.available():
            return True
        now = time.monotonic()
        if now - getattr(self, "_last_reconnect", -1e9) < self.RECONNECT_MIN_GAP_S:
            return False
        self._last_reconnect = now
        try:
            if self._ib is not None:
                try:
                    self._ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self._connected = False
            self._connect()
        except Exception as exc:  # noqa: BLE001
            self._warn(f"reconnect failed: {exc}")
            return False
        if self.available():
            self._log("RECONNECTED in session")
        return self.available()

    def place_option_order(self, underlying: str, option_symbol: str, side: str,
                           quantity: int, limit_price: float, tif: str = "DAY") -> dict | None:
        """Single-leg option limit order. side ∈ {buy_to_open, sell_to_open,
        buy_to_close, sell_to_close}."""
        if not self.available():
            return None
        quantity = max(1, int(quantity))
        key = self._dedup_key(underlying, [(option_symbol, side, quantity)])
        dup = self._dedup_check(key)
        if dup is not None:
            self._warn(f"DUPLICATE ORDER blocked: {underlying} {side} {option_symbol} "
                       f"x{quantity} matches live order {dup} within {self.dedup_window_s:.0f}s")
            return {"error": "duplicate_order", "duplicate_of": dup,
                    "reason": f"identical live order {dup} placed within "
                              f"{self.dedup_window_s:.0f}s window"}
        try:
            contract = self._qualify_option(option_symbol)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"qualify failed for {option_symbol}: {exc}")
            return {"error": f"qualify_failed: {exc}"}

        action = "BUY" if str(side).startswith("buy") else "SELL"
        order = self._limit_order(action, quantity, round(float(limit_price), 2), tif=tif)
        # ── AMBIGUITY BOUNDARY (R1) ─────────────────────────────────────────
        # Everything from placeOrder onward may leave a LIVE order at IBKR even
        # if it raises (submitted, confirmation lost). Such a failure MUST be
        # reported ambiguous (transport_error) so the engine keeps the intent
        # unbound for resolve_intents — never a definitive reject, which would
        # destroy the intent and hide a live order (double-close on the close
        # path). A qualify failure ABOVE this line is definitive: never sent.
        try:
            trade = self._ib.placeOrder(contract, order)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"place AMBIGUOUS for {option_symbol}: {exc} — order may be "
                       "live at the broker; intent left unbound for resolve_intents")
            return {"error": f"place_failed: {exc}", "transport_error": True}
        try:
            oid = int(trade.order.orderId)
            self._trades[oid] = trade
            self._meta[oid] = {
                "underlying": underlying, "multileg": False, "side": side,
                "create_date": _now_iso(),
                "legs": [{"option_symbol": option_symbol, "side": side,
                          "quantity": quantity, "con_id": getattr(contract, "conId", None)}],
            }
            self._settle(trade)
            status = self._status_of(trade)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"post-place confirmation AMBIGUOUS for {option_symbol}: {exc} "
                       "— order was submitted; intent left unbound")
            return {"error": f"post_place_confirm_failed: {exc}", "transport_error": True}
        if status == "rejected":
            return self._reject_result(oid, trade, underlying, option_symbol)
        self._dedup_record(key, oid)
        self._log(f"placed single-leg {side} {quantity}x {option_symbol} @ {limit_price} "
                  f"-> order {oid} status={status} tag={ORDER_TAG}")
        return {"order": {"id": oid, "status": status}}

    def place_spread_order(self, underlying: str, legs: list[dict],
                           limit_price: float, order_type: str = "debit",
                           tif: str = "DAY") -> dict | None:
        """Multileg (BAG combo) limit order. legs = [{option_symbol, side,
        quantity}]; order_type 'debit' opens, 'credit' closes.

        IBKR combo convention used here: each ComboLeg carries its own BUY/SELL
        action; the parent BAG order is always a BUY whose signed limit price
        encodes the net — positive for a debit paid, negative for a credit
        received. paper_trader passes an unsigned limit + order_type, which we
        translate to that signed price."""
        if not self.available():
            return None
        if not legs:
            return {"error": "no legs"}
        key = self._dedup_key(
            underlying, [(l["option_symbol"], l["side"], int(l["quantity"])) for l in legs])
        dup = self._dedup_check(key)
        if dup is not None:
            self._warn(f"DUPLICATE ORDER blocked: {underlying} spread "
                       f"{[l['option_symbol'] for l in legs]} matches live order {dup} "
                       f"within {self.dedup_window_s:.0f}s")
            return {"error": "duplicate_order", "duplicate_of": dup,
                    "reason": f"identical live spread {dup} placed within "
                              f"{self.dedup_window_s:.0f}s window"}
        try:
            bag, leg_meta, combo_qty = self._qualify_combo(underlying, legs)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"qualify failed for {underlying} spread: {exc}")
            return {"error": f"qualify_failed: {exc}"}

        signed_price = round(float(limit_price), 2)
        if order_type == "credit":
            signed_price = -signed_price
        order = self._limit_order("BUY", combo_qty, signed_price, tif=tif)
        # ── AMBIGUITY BOUNDARY (R1) — see place_option_order for the rationale.
        # A spread is the dangerous case: a blind close-resubmit here can leave
        # an unintended OPPOSITE multi-leg position.
        try:
            trade = self._ib.placeOrder(bag, order)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"spread place AMBIGUOUS for {underlying}: {exc} — order may "
                       "be live at the broker; intent left unbound for resolve_intents")
            return {"error": f"place_failed: {exc}", "transport_error": True}
        try:
            oid = int(trade.order.orderId)
            self._trades[oid] = trade
            self._meta[oid] = {
                "underlying": underlying, "multileg": True, "order_type": order_type,
                "create_date": _now_iso(), "legs": leg_meta,
            }
            self._settle(trade)
            status = self._status_of(trade)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"spread post-place confirmation AMBIGUOUS for {underlying}: "
                       f"{exc} — order was submitted; intent left unbound")
            return {"error": f"post_place_confirm_failed: {exc}", "transport_error": True}
        if status == "rejected":
            return self._reject_result(oid, trade, underlying,
                                       ",".join(l["option_symbol"] for l in legs))
        self._dedup_record(key, oid)
        self._log(f"placed {order_type} spread {[l['option_symbol'] for l in legs]} "
                  f"@ {limit_price} -> order {oid} status={status} tag={ORDER_TAG}")
        return {"order": {"id": oid, "status": status}}

    def _refresh_from_broker(self) -> bool:
        """B1 FIX: pull the BROKER's own order book into the `_trades` cache.

        `_trades` is a CACHE of what this process placed — it is NOT the broker's
        book. After a restart it is empty, so any read built on it alone reports
        "no orders" while real orders sit live at IBKR. `resolve_intents` reads
        that as a genuine empty and drops unbound intents (the R2 failure mode),
        and `poll_fills` cannot see a post-restart fill.

        Returns True on a successful refresh, False on a transport failure (the
        caller must then surface None, never a silent []).
        """
        try:
            trades = list(self._ib.reqAllOpenOrders() or [])
            try:                                    # completed orders: best-effort
                trades += list(self._ib.reqCompletedOrders(False) or [])
            except Exception as exc:                # noqa: BLE001
                self._log(f"reqCompletedOrders unavailable ({exc}) — open orders only")
            for tr in trades:
                o = getattr(tr, "order", None)
                oid = self._identity_of(tr)
                if oid is None:
                    # UNIDENTIFIABLE (2026-08-05, item 1): reqCompletedOrders
                    # returns records with orderId=0 AND permId missing. Keying
                    # those by 0 collides every completed order into one slot and
                    # feeds the engine a fabricated "order 0". Such a record is
                    # NOT evidence of anything — drop it.
                    self._log("dropping unidentifiable completed-order record "
                              f"(orderId={getattr(o,'orderId',None)}, "
                              f"permId={getattr(o,'permId',None)}) — no identity, "
                              "cannot be used as order-state evidence")
                    continue
                # ACCOUNT ISOLATION (2026-08-04): this IBKR paper account is
                # SHARED with the trend book (ETF/futures legs, e.g. CPER) — the
                # broker query returns those too. The options engine must never
                # see, adopt, or CANCEL another strategy's orders: reconcile
                # treats unknown broker orders as orphans. Mirror the filter
                # get_positions already applies (secType OPT) and keep only
                # option orders: OPT (single leg) and BAG (spread combo).
                if not self._is_option_trade(tr):
                    continue
                # ATTRIBUTION: claim ONLY orders carrying our orderRef. Anything
                # else on this shared account — trend book, a human's manual
                # option trade, another strategy — is logged once and IGNORED.
                # Ignored, never cancelled: an order we cannot prove is ours is
                # by definition someone else's.
                ref = getattr(getattr(tr, "order", None), "orderRef", "") or ""
                if ref != ORDER_TAG:
                    if oid not in self._foreign_logged:
                        self._foreign_logged.add(oid)
                        self._log(f"IGNORING unattributable broker order {oid} "
                                  f"({getattr(getattr(tr,'contract',None),'symbol','?')}, "
                                  f"orderRef={ref!r}) — not ours; never cancelled")
                    continue
                oid = int(oid)
                self._trades[oid] = tr             # broker truth wins over cache
                if oid not in self._meta:
                    self._meta[oid] = self._meta_from_trade(tr)
            return True
        except Exception as exc:                    # noqa: BLE001
            self._warn(f"broker order refresh FAILED: {exc} — reads will report "
                       "None (unknown), never a silent empty")
            return False

    @staticmethod
    def _identity_of(trade):
        """Stable identity for a broker order record (item 1, 2026-08-05).

        orderId is the live-session handle, but reqCompletedOrders returns
        completed records with orderId=0 — keying by that collides EVERY
        completed order onto one entry and manufactured the phantom "order 0"
        the engine then reasoned about. permId is IBKR's permanent, unique
        order identifier and survives sessions, so it is the correct key.

        Returns the orderId when it is a real handle (non-zero), else the
        permId, else None (unidentifiable -> the record is not evidence).
        """
        o = getattr(trade, "order", None)
        # permId FIRST (corrected 2026-08-06). The earlier version preferred
        # orderId whenever it was > 0, on the assumption that only completed
        # records carried orderId=0. LIVE orders collide too: both B5 drill
        # orders were orderId=4 with permIds 1838451540/41, so they collapsed
        # into ONE cache entry and the adapter reported 1 open order while the
        # gateway held 2 (caught by preflight sentinel (g)). permId is IBKR's
        # permanent unique identifier; orderId is only a per-session handle.
        perm = getattr(o, "permId", None)
        if isinstance(perm, int) and perm > 0:
            return perm
        oid = getattr(o, "orderId", None)
        if isinstance(oid, int) and oid > 0:
            return oid
        return None

    @staticmethod
    def _is_option_trade(trade) -> bool:
        """True only for OPTION orders (OPT single-leg / BAG combo). Everything
        else on this shared paper account — the trend book's ETF and futures
        legs — is invisible to the options engine by construction."""
        return getattr(getattr(trade, "contract", None), "secType", "") in ("OPT", "BAG")

    @staticmethod
    def _meta_from_trade(trade) -> dict:
        """Reconstruct minimal meta for an order this process did not place
        (recovered from the broker after a restart)."""
        c = getattr(trade, "contract", None)
        return {
            "underlying": getattr(c, "symbol", None),
            "multileg": getattr(c, "secType", "") == "BAG",
            "create_date": None,
            "recovered_from_broker": True,
            "legs": [],
        }

    def get_order(self, order_id) -> dict | None:
        """Normalized ORDER dict (Tradier shape). B1: falls back to a BROKER
        query when the id is not in the local cache (e.g. after a restart), so
        a live order is never reported as missing. Returns None when the order
        genuinely does not exist OR when the broker read failed."""
        if not self.available():
            return None
        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            return None
        trade = self._trades.get(oid)
        if trade is None:
            # a fresh process has an empty local book: pull the broker's first
            try:
                self._refresh_from_broker()
            except Exception:  # noqa: BLE001
                pass
            trade = self._trades.get(oid)
        if trade is None:
            # RESTART IDENTITY (2026-09-12): the engine binds the per-session
            # orderId returned by place_*; after a restart _refresh_from_broker
            # re-keys the book by permId, so the bound id no longer resolves and
            # a fill placed just before a crash would never be booked. Resolve
            # by scanning for the trade whose order carries that orderId.
            for t in self._trades.values():
                try:
                    if int(getattr(t.order, "orderId", -1)) == oid:
                        trade = t
                        break
                except (TypeError, ValueError):
                    continue
        if trade is None:
            if not self._refresh_from_broker():
                return None                        # read FAILED — unknown, not absent
            trade = self._trades.get(oid)
        if trade is None:
            return None
        self._settle(trade)
        return self._normalize_order(oid, trade)

    def cancel_order(self, order_id) -> dict | None:
        if not self.available():
            return None
        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            return None
        trade = self._trades.get(oid)
        if trade is None:
            return {"order": {"id": order_id, "status": "not_found"}}
        try:
            self._ib.cancelOrder(trade.order)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"cancel failed for order {oid}: {exc}")
            return {"error": f"cancel_failed: {exc}"}
        self._settle(trade)
        status = self._status_of(trade)
        self._log(f"cancel requested for order {oid} -> status={status}")
        return {"order": {"id": oid, "status": status}}

    def get_orders(self) -> list[dict] | None:
        """The BROKER's orders (any status), Tradier-normalized.

        B1/B2 FIX. Previously this iterated `_trades` — a process-local cache —
        so a fresh instance reported [] regardless of what IBKR actually held,
        and it had no way to signal a failed read.

        Now: refresh from the broker first, then normalize. `None` = the read
        FAILED (broker view unknown; recovery paths must abort, never drop
        intents). `[]` = the broker authoritatively reports no orders. This
        matches Tradier's contract and the R2 requirement.
        """
        if not self.available():
            # Same contract violation as get_positions() (fixed 2026-08-22).
            # "Callers guard available() first" held for the engine and for the
            # B1 sentinel, but the premise is unenforceable and it silently
            # produced the exact silent-empty this method's own docstring
            # forbids. An unavailable client cannot report broker truth, so it
            # must say UNKNOWN.
            return None
        if not self._refresh_from_broker():
            return None        # transport failure — unknown, NOT "empty"
        out = []
        seen = set()
        for oid, trade in list(self._trades.items()):
            if id(trade) in seen:
                continue                      # same trade keyed by orderId AND permId
            seen.add(id(trade))
            self._settle(trade)
            out.append(self._normalize_order(oid, trade))
        return out

    def get_positions(self) -> list[dict] | None:
        """Current OPTION positions, normalized to Tradier POSITION dicts:
        {symbol(OCC), quantity(signed +long/-short), cost_basis(DOLLARS)}.

        R2 contract (matches PaperOrderClient): `[]` = broker authoritatively
        reports no positions; `None` = the read FAILED (gateway error) and the
        broker view is unknown. Recovery/reconcile callers must abort on None,
        never treat it as "empty"."""
        if not self.available():
            # CONTRACT VIOLATION FIXED (2026-08-22). This returned [] on the
            # premise that "recovery paths guard available()". They do; ops
            # checks do not. scripts/ops_checks.py built its broker view
            # straight off this call, so an unavailable client produced an
            # empty dict and `broker_reachable: True` — the check reported the
            # broker healthy through THREE zero-execution sessions (08-19..21),
            # and on 08-21 said reachable at 18:40 while lsof showed no gateway
            # process at all seven minutes later. An unavailable client is
            # precisely "the view is UNKNOWN", which this contract spells None.
            return None
        out = []
        try:
            positions = list(self._ib.positions())
        except Exception as exc:  # noqa: BLE001
            self._warn(f"positions() failed: {exc}")
            return None  # unknown broker view — NOT genuinely-empty
        for p in positions:
            c = getattr(p, "contract", None)
            if c is None or getattr(c, "secType", "") != "OPT":
                continue  # options book only — never touch stock/future legs
            try:
                occ = self._contract_to_occ(c)
            except Exception as exc:  # noqa: BLE001
                self._warn(f"skip un-mappable position {getattr(c, 'localSymbol', c)}: {exc}")
                continue
            qty = float(getattr(p, "position", 0) or 0)
            avg_cost = float(getattr(p, "avgCost", 0) or 0)  # per-contract $ (incl. multiplier)
            out.append({"symbol": occ, "quantity": qty,
                        "cost_basis": round(qty * avg_cost, 4)})
        return out

    # ── contract qualification ───────────────────────────────────────────────
    def _qualify_option(self, occ: str):
        root, yyyymmdd, right, strike = parse_occ(occ)
        contract = Option(root, yyyymmdd, strike, right, "SMART", currency="USD")
        contract.multiplier = OPTION_MULTIPLIER
        self._ib.qualifyContracts(contract)
        if not getattr(contract, "conId", None):
            raise RuntimeError(f"could not qualify option {occ}")
        return contract

    def _qualify_combo(self, underlying: str, legs: list[dict]):
        """Qualify each leg and assemble a BAG combo. Returns (bag, leg_meta,
        combo_qty). Equal-quantity legs (the norm here) collapse to combo_qty =
        that quantity with ratio 1; unequal quantities fall back to combo_qty 1
        with per-leg ratios."""
        qtys = [int(l["quantity"]) for l in legs]
        equal = len(set(qtys)) == 1
        combo_qty = qtys[0] if equal else 1
        combo_legs, leg_meta = [], []
        for l in legs:
            opt = self._qualify_option(l["option_symbol"])
            ratio = 1 if equal else int(l["quantity"])
            action = "BUY" if str(l["side"]).startswith("buy") else "SELL"
            combo_legs.append(ComboLeg(conId=int(opt.conId), ratio=ratio,
                                       action=action, exchange="SMART"))
            leg_meta.append({"option_symbol": l["option_symbol"], "side": l["side"],
                             "quantity": int(l["quantity"]), "con_id": int(opt.conId)})
        bag = Contract(secType="BAG", symbol=underlying, currency="USD",
                       exchange="SMART", comboLegs=combo_legs)
        return bag, leg_meta, combo_qty

    def _contract_to_occ(self, c) -> str:
        root = c.symbol
        ymd = c.lastTradeDateOrContractMonth  # YYYYMMDD
        return occ_from_fields(root, ymd, c.right, c.strike)

    def _limit_order(self, action: str, quantity, lmt_price: float, tif: str = "DAY"):
        order = LimitOrder(action, quantity, lmt_price)
        order.orderRef = ORDER_TAG
        # SCALP-v2 (2026-09-12): "IOC" = immediate-or-cancel, the marketable-or-
        # cancel fill mode. A resting DAY limit at the mid only executes when the
        # market comes back through it (adverse selection); IOC takes liquidity
        # that is there NOW or leaves no order behind.
        order.tif = tif if tif in ("DAY", "IOC") else "DAY"
        if self._account:
            order.account = self._account
        return order

    # ── status mapping / normalization ───────────────────────────────────────
    def _settle(self, trade, cycles: int = 3) -> None:
        """Pump the ib_async event loop so the trade reflects the latest broker
        state. No-op against the mock (state is set synchronously)."""
        for _ in range(cycles):
            try:
                self._ib.sleep(0)
            except Exception:  # noqa: BLE001
                break

    def _reject_reason(self, trade) -> str | None:
        """Reject reason for a trade: prefer the captured error event, fall back
        to the terminal trade-log message."""
        oid = int(trade.order.orderId)
        if oid in self._errors:
            code, msg = self._errors[oid]
            # SCALP-v2 (2026-09-12): IB reports EVERY cancel — including an
            # unfilled IOC that simply found no liquidity — as error 202
            # "Order Canceled - reason:". That is not a rejection. Treating it
            # as one made every unfilled IOC 'rejected', so entry_ioc_unfilled
            # never logged, the attempt cap could never bind and the fill-rate
            # denominator vanished (found by 5 of 6 adversarial hunters).
            if self._is_cancel_notice(code, msg):
                return None
            return msg
        for entry in (getattr(trade, "log", None) or []):
            st = getattr(entry, "status", "")
            msg = getattr(entry, "message", "")
            code = getattr(entry, "errorCode", None)
            if st in ("Cancelled", "Inactive", "Rejected") and msg and \
                    not self._is_cancel_notice(code, msg):
                return msg      # IB delivers rejects as Cancelled + a reason; a plain
                                # "Order Canceled" notice is the one case that is not a reject
        return None

    @staticmethod
    def _is_cancel_notice(code, msg) -> bool:
        """IB 202 / 'Order Canceled' = the order was cancelled (by us, by IOC,
        by the exchange) — not refused. A refusal is 201 (BP/margin), 10147,
        'rejected', etc."""
        m = str(msg or "").lower()
        # IB's plain cancel is "Order Canceled - reason:" with an EMPTY reason
        # (108/108 on this account). A cancel that CARRIES a reason (margin,
        # buying power) is a refusal and must stay a reject.
        import re as _re
        mm = _re.search(r"order cancel+ed\s*-\s*reason:\s*(.*)$", m)
        if mm is not None:
            return mm.group(1).strip() == ""
        if "202" in m and "cancel" in m and "reason" not in m:
            return True
        try:
            # bare 202 with no text: IB's cancel code, not a refusal
            return code is not None and int(code) == 202 and not m
        except (TypeError, ValueError):
            return False

    def _status_of(self, trade) -> str:
        """Map an ib_async Trade to the Tradier STATUS enum."""
        st = trade.orderStatus
        raw = getattr(st, "status", "") or ""
        filled = float(getattr(st, "filled", 0) or 0)
        remaining = float(getattr(st, "remaining", 0) or 0)
        reject = self._reject_reason(trade)

        if filled > 0 and remaining > 0:
            return "partially_filled"
        if raw == "Filled" and filled <= 0:
            # ITEM 2 (2026-08-05): a "Filled" summary with filled=0.0 executed
            # NOTHING. reqCompletedOrders returns exactly this shape, and taking
            # it as a fill is what made escalate_stale_close conclude
            # "exit_fill_during_cancel" 13 times in a row while the position
            # stayed open. A status word is not an execution — quantity is.
            self._warn(f"IGNORING phantom 'Filled' record with filled=0.0 "
                       f"(raw={raw!r}) — no execution, not a fill")
            return "canceled"
        if raw == "Filled" or (filled > 0 and remaining == 0):
            return "filled"
        if reject:                                   # terminal reject (incl. BP/margin)
            return "rejected"
        if raw in ("Cancelled", "ApiCancelled", "PendingCancel"):
            return "canceled"
        if raw in ("PendingSubmit", "PreSubmitted", "ApiPending"):
            return "pending"
        if raw == "Inactive":
            return "rejected"
        if raw == "Submitted":
            return "open"
        return "open"

    def _build_legs(self, trade, meta: dict) -> list[dict]:
        """Per-leg fill detail for a multileg order — aggregate executions by
        conId into {option_symbol, side, avg_fill_price, exec_quantity}."""
        agg: dict[object, list] = {}
        for f in (getattr(trade, "fills", None) or []):
            ex = getattr(f, "execution", None)
            if ex is None:
                continue
            cid = getattr(ex, "conId", None)
            if cid is None:
                cid = getattr(getattr(f, "contract", None), "conId", None)
            shares = float(getattr(ex, "shares", 0) or 0)
            price = float(getattr(ex, "price", 0) or 0)
            bucket = agg.setdefault(cid, [0.0, 0.0])
            bucket[0] += shares
            bucket[1] += shares * price
        legs = []
        for lg in meta["legs"]:
            cid = lg.get("con_id")
            tot, wsum = agg.get(cid, [0.0, 0.0])
            avg = round(wsum / tot, 4) if tot else 0.0
            legs.append({"option_symbol": lg["option_symbol"], "side": lg["side"],
                         "avg_fill_price": avg, "exec_quantity": tot})
        return legs

    def _transaction_date(self, trade, status: str) -> str | None:
        times = [getattr(f, "time", None) for f in (getattr(trade, "fills", None) or [])]
        times = [t for t in times if t is not None]
        if times:
            try:
                return max(times).isoformat()
            except Exception:  # noqa: BLE001
                return str(max(times))
        return _now_iso() if status in ("filled", "partially_filled") else None

    def _normalize_order(self, oid: int, trade) -> dict:
        meta = self._meta.get(oid, {})
        st = trade.orderStatus
        status = self._status_of(trade)

        # partial-fill visibility: one adapter log line the first time we see it
        if status == "partially_filled" and oid not in self._partial_logged:
            self._partial_logged.add(oid)
            self._warn(f"PARTIAL FILL order {oid} ({meta.get('underlying')}): "
                       f"filled {float(getattr(st, 'filled', 0) or 0)} / "
                       f"remaining {float(getattr(st, 'remaining', 0) or 0)} — legging risk")

        order = {
            "id": oid,
            "status": status,
            "avg_fill_price": abs(float(getattr(st, "avgFillPrice", 0) or 0)),  # unsigned
            # ITEM 2: executed quantity — the ONLY trustworthy evidence of a
            # fill. Callers must verify against this, never against a status word.
            "filled_quantity": float(getattr(st, "filled", 0) or 0),
            "create_date": meta.get("create_date"),
            "transaction_date": self._transaction_date(trade, status),
            # CROSS-BOOK (2026-08-04): report the order's REAL orderRef, never a
            # hardcoded constant. Hardcoding it made every broker order — the
            # trend book's, a human's, any other strategy sharing this paper
            # account — report as OURS, which defeats reconcile's `adopt_foreign`
            # guard and gets a foreign order CANCELLED. Ownership must be
            # attributed from what the broker actually holds.
            "tag": (getattr(getattr(trade, "order", None), "orderRef", "") or ""),
            "symbol": meta.get("underlying"),
        }
        if meta.get("multileg"):
            order["leg"] = self._build_legs(trade, meta)
        else:
            legs = meta.get("legs") or [{}]
            order["option_symbol"] = legs[0].get("option_symbol")
            order["side"] = meta.get("side")  # order_fill_detail signs single-leg by this
        if status == "rejected":
            reason = self._reject_reason(trade) or "rejected"
            order["reason"] = reason
            order["error"] = reason
        return order

    def _reject_result(self, oid: int, trade, underlying: str, symbol: str) -> dict:
        reason = self._reject_reason(trade) or "order rejected"
        if _is_bp_reason(reason):
            self._warn(f"order REJECTED (BUYING POWER) {underlying} {symbol} "
                       f"order {oid}: {reason}")
        else:
            self._warn(f"order REJECTED {underlying} {symbol} order {oid}: {reason}")
        # reject is NOT recorded for dedup — a legitimate retry must be allowed.
        # error_code (e.g. 201) lets the ledger tell a broker REFUSAL from a
        # no-liquidity IOC cancel (2026-09-15: every v2 combo IOC was a 201).
        code = (self._errors.get(int(oid)) or (None, None))[0]
        return {"error": reason, "reason": reason, "error_code": code,
                "order": {"id": oid, "status": "rejected"}}

    # ── duplicate-order protection ───────────────────────────────────────────
    @staticmethod
    def _dedup_key(underlying: str, legs) -> tuple:
        """A stable signature of the intended order. The leg SIDES encode intent
        (buy/sell_to_open = open, buy/sell_to_close = close), so (underlying,
        sorted (symbol, side, qty)) uniquely identifies a (rec, intent, leg-set)
        for the short defense-in-depth window."""
        return (underlying, tuple(sorted((str(s), str(sd), int(q)) for s, sd, q in legs)))

    def _dedup_check(self, key: tuple):
        now = time.monotonic()
        # purge expired
        for k, (oid, ts) in list(self._recent.items()):
            if now - ts > self.dedup_window_s:
                del self._recent[k]
        hit = self._recent.get(key)
        return hit[0] if hit else None

    def _dedup_record(self, key: tuple, oid: int) -> None:
        self._recent[key] = (oid, time.monotonic())


# ── module helpers ───────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_bp_reason(reason: str | None) -> bool:
    r = (reason or "").lower()
    return any(k in r for k in _BP_KEYWORDS)
