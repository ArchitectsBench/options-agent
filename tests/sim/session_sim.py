"""SESSION SIMULATOR — replays a recorded trading day through the REAL engine
cycle (src.engine.pipeline.run_cycle) against a fake broker with IBKR-faithful
order semantics, under a controllable clock. Hermetic: conftest's O-20 seal
redirects every production path to tmp; nothing here touches logs/ or data/.

Why it exists (2026-09-12): SCALP-v2 was activated with unit coverage only.
The operator asked for no surprises during trading hours; the only way to earn
that is to run a whole session — 78 cycles, entries, IOC fills/cancels/partials,
exits, escalations, caps, disconnects — before the market does.

UNVALIDATED — PAPER ONLY.
"""
from __future__ import annotations

import contextlib
import io
import json
import random
from datetime import datetime as _dt, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


# ── controllable clock ──────────────────────────────────────────────────────
class Clock:
    """Wall clock the simulator advances per cycle. Modules that do
    `from datetime import datetime` get a subclass whose now() reads this."""
    current: _dt = _dt(2026, 1, 1, 6, 30)   # naive local PT, like the engine
    # A real clock never returns the same instant twice. Frozen per-cycle time
    # gave two intents in one cycle the same microsecond intent_seq, and
    # _bind_intent then bound BOTH to the second order (found 2026-09-15 when
    # a close and an entry shared a cycle). now() advances 1 us per call.
    _tick: int = 0

    @classmethod
    def set(cls, ts: str):
        cls.current = _dt.fromisoformat(ts)
        cls._tick = 0


from datetime import date as _date, timedelta as _td


class FakeDate(_date):
    """The expiry guard uses date.today() — must follow the simulated clock or
    every recorded contract looks lapsed."""
    @classmethod
    def today(cls):
        c = Clock.current
        return cls(c.year, c.month, c.day)


class FakeDatetime(_dt):
    @classmethod
    def now(cls, tz=None):
        Clock._tick += 1
        naive = Clock.current + _td(microseconds=Clock._tick)
        if tz is None:
            return cls(naive.year, naive.month, naive.day, naive.hour, naive.minute, naive.second, naive.microsecond)
        aware = naive.replace(tzinfo=PT).astimezone(tz)
        return cls(aware.year, aware.month, aware.day, aware.hour, aware.minute, aware.second, aware.microsecond, tzinfo=tz)

    @classmethod
    def utcnow(cls):
        return cls.now(ZoneInfo("UTC")).replace(tzinfo=None)


# ── fake broker with IBKR-like semantics ────────────────────────────────────
class SimBroker:
    """Order client + data client in one. IOC fills immediately if the limit
    crosses the quote (buy: limit >= net ask; credit: limit <= net bid), else
    it is CANCELED — nothing rests. DAY limits rest and settle on tick() when
    the net mid crosses. Partial IOC fills and transport errors are knobs."""

    # IBKR refuses immediate-or-cancel on a SMART-routed option combo (BAG) —
    # found live 2026-09-15 after every v2 spread came back with this text. The
    # sim accepted IOC combos until then, which is how it certified an order
    # type the broker never takes. `combo_ioc_rejected=False` restores the old
    # (wrong) behaviour for tests that exercise fill mechanics on spreads.
    COMBO_IOC_REJECT = ("Order rejected - reason:Inter exchange combo order "
                        "should be DAY/GTC order.")

    def __init__(self, quotes: dict[str, dict] | None = None, *, ioc_fill_prob=1.0,
                 partial_prob=0.0, disconnect_at_cycle=None, transport_error_on_place=False,
                 seed=7, combo_ioc_rejected=True, lose_positions_at_cycle=None,
                 empty_positions_at_cycles=(), reconnect_at_cycle=None):
        self.q: dict[str, dict] = dict(quotes or {})
        self.orders: dict[int, dict] = {}
        self.positions: dict[str, float] = {}
        self.cost: dict[str, float] = {}
        self._next = 1000
        self.rng = random.Random(seed)
        self.ioc_fill_prob = ioc_fill_prob
        self.partial_prob = partial_prob
        self.disconnect_at_cycle = disconnect_at_cycle
        self.transport_error_on_place = transport_error_on_place
        self.combo_ioc_rejected = combo_ioc_rejected
        # C-9.1 scenarios: (a) the broker "loses" every held position from this
        # cycle on (expiry/assignment/manual close outside the engine); (b) the
        # position view reads [] for exactly these cycles (a freshly reconnected
        # client catching up) and is truthful again afterwards.
        self.lose_positions_at_cycle = lose_positions_at_cycle
        self.empty_positions_at_cycles = set(empty_positions_at_cycles or ())
        self.lost_positions: dict[str, float] = {}
        # C-12.2: the gateway comes back at this cycle, but the CLIENT stays
        # disconnected until someone calls reconnect() — exactly the 09-28 shape.
        self.reconnect_at_cycle = reconnect_at_cycle
        self.gateway_up = True
        self.reconnects = 0
        self.cycle = 0
        self.connected = True
        self.placed = []          # audit trail of every place call (kwargs incl tif)

    # -- data client (exit path) --
    def quotes(self, symbols):
        if not self.connected:
            return None
        return {s: {"symbol": s, "bid": self.q[s]["bid"], "ask": self.q[s]["ask"],
                    "last": (self.q[s]["bid"] + self.q[s]["ask"]) / 2}
                for s in symbols if s in self.q}

    # -- order client --
    def available(self):
        return self.connected

    def _proceeds(self, legs, immediate: bool):
        """Cash we RECEIVE for executing these legs now: sell legs bring the
        bid (or mid), buy legs cost the ask (or mid). Same rule for opening and
        closing legs — buy_* pays, sell_* receives — so a credit close prices as
        (sell_to_close @bid) - (buy_to_close @ask). (An earlier version priced
        closes as if they were entries and no close could ever fill.)"""
        tot = 0.0
        for l in legs:
            q = self.q.get(l["symbol"])
            if q is None:
                return None
            buy = str(l["side"]).startswith("buy")
            if immediate:
                tot += -q["ask"] if buy else q["bid"]
            else:
                m = (q["bid"] + q["ask"]) / 2
                tot += -m if buy else m
        return round(tot, 4)

    def _place(self, underlying, legs, qty, limit, order_type, tif):
        if not self.connected:
            return {"error": "qualify_failed: Not connected"}
        if self.transport_error_on_place:
            return {"transport_error": True, "error": "timeout"}
        oid = self._next; self._next += 1
        o = {"id": oid, "underlying": underlying, "legs": legs, "qty": int(qty),
             "limit": float(limit), "order_type": order_type, "tif": tif,
             "status": "open", "filled": 0, "fills": [],
             "create_date": Clock.current.isoformat(timespec="seconds"),
             "transaction_date": None}
        self.orders[oid] = o
        self.placed.append({"id": oid, "tif": tif, "limit": limit, "type": order_type,
                            "legs": [(l["symbol"], l["side"]) for l in legs]})
        if tif == "IOC" and len(legs) > 1 and self.combo_ioc_rejected:
            # the real adapter's _reject_result shape (IB error 201)
            o["status"] = "rejected"
            o["transaction_date"] = Clock.current.isoformat(timespec="seconds")
            return {"error": self.COMBO_IOC_REJECT, "reason": self.COMBO_IOC_REJECT,
                    "error_code": 201, "order": {"id": oid, "status": "rejected"}}
        if tif == "IOC":
            self._try_fill(o, immediate=True)
            if o["status"] == "open":
                o["status"] = "canceled"
                o["transaction_date"] = Clock.current.isoformat(timespec="seconds")
        return {"order": {"id": oid, "status": o["status"]}}

    def _try_fill(self, o, immediate=False):
        legs = o["legs"]
        proceeds = self._proceeds(legs, immediate)
        if proceeds is None:
            return
        if o["order_type"] == "debit":        # we pay: cost = -proceeds; fill if limit >= cost
            px = -proceeds
            ok = o["limit"] >= px
        else:                                 # credit: fill if limit <= proceeds
            px = proceeds
            ok = o["limit"] <= px
        if not ok:
            return
        if immediate and self.rng.random() > self.ioc_fill_prob:
            return
        qty = o["qty"] - o["filled"]
        if immediate and self.partial_prob and self.rng.random() < self.partial_prob and qty > 1:
            qty = max(1, qty // 2)
        fill_px = px if immediate else o["limit"]
        o["filled"] += qty
        o["fills"].append((qty, fill_px))
        o["transaction_date"] = Clock.current.isoformat(timespec="seconds")
        o["status"] = "filled" if o["filled"] >= o["qty"] else "partially_filled"
        # positions
        for l in legs:
            sgn = 1 if str(l["side"]).startswith("buy") else -1
            self.positions[l["symbol"]] = self.positions.get(l["symbol"], 0) + sgn * qty
            if abs(self.positions[l["symbol"]]) < 1e-9:
                self.positions.pop(l["symbol"], None)

    def place_option_order(self, underlying, option_symbol, side, quantity, limit_price, tif="DAY"):
        legs = [{"symbol": option_symbol, "side": side}]
        otype = "debit" if str(side).endswith("_open") and str(side).startswith("buy") else \
                ("credit" if str(side).startswith("sell") else "debit")
        return self._place(underlying, legs, quantity, limit_price, otype, tif)

    def place_spread_order(self, underlying, legs, limit_price, order_type="debit", tif="DAY"):
        L = [{"symbol": l["option_symbol"], "side": l["side"]} for l in legs]
        return self._place(underlying, L, legs[0]["quantity"], limit_price, order_type, tif)

    def _normalize(self, o):
        avg = 0.0
        if o["fills"]:
            tot = sum(q * p for q, p in o["fills"]); n = sum(q for q, _ in o["fills"])
            avg = round(tot / n, 4)
        legs = []
        if len(o["legs"]) > 1:
            for l in o["legs"]:
                q = self.q.get(l["symbol"], {"bid": 0, "ask": 0})
                lp = q["ask"] if str(l["side"]).startswith("buy") else q["bid"]
                legs.append({"option_symbol": l["symbol"], "side": l["side"],
                             "avg_fill_price": lp if o["filled"] else 0.0,
                             "exec_quantity": o["filled"]})
        return {"id": o["id"], "status": o["status"], "avg_fill_price": avg if o["filled"] else 0.0,
                "filled_quantity": float(o["filled"]), "create_date": o["create_date"],
                "transaction_date": o["transaction_date"], "tag": "UNVALIDATED-PAPER-ONLY",
                "symbol": o["underlying"], "rejected": None, "leg": legs}

    def get_order(self, order_id):
        if not self.connected:
            return None
        o = self.orders.get(int(order_id))
        return self._normalize(o) if o else None

    def get_orders(self):
        if not self.connected:
            return None
        return [self._normalize(o) for o in self.orders.values()]

    def get_positions(self):
        if not self.connected:
            return None
        if self.cycle in self.empty_positions_at_cycles:
            return []                                    # a lying-empty read, one cycle
        return [{"symbol": s, "quantity": q, "cost_basis": 0.0} for s, q in self.positions.items()]

    def cancel_order(self, order_id):
        if not self.connected:
            return None
        o = self.orders.get(int(order_id))
        if o and o["status"] in ("open", "partially_filled"):
            o["status"] = "canceled" if o["filled"] == 0 else "partially_filled"
        return {"ok": True}

    # -- simulator hooks --
    def tick(self, cycle_no: int, quotes: dict[str, dict]):
        self.cycle = cycle_no
        self.q.update(quotes)
        if self.disconnect_at_cycle is not None and cycle_no >= self.disconnect_at_cycle:
            back = self.reconnect_at_cycle is not None and cycle_no >= self.reconnect_at_cycle
            self.gateway_up = back
            if not back:
                self.connected = False          # link is down and cannot come back yet
            # once the gateway is back the client stays as it is until reconnect()
        if self.lose_positions_at_cycle is not None and cycle_no >= self.lose_positions_at_cycle and self.positions:
            self.lost_positions.update(self.positions); self.positions.clear()
        for o in self.orders.values():
            if o["tif"] == "DAY" and o["status"] == "open":
                self._try_fill(o)

    def connection_state(self):
        return "connected" if self.connected else "disconnected"

    def reconnect(self):
        if self.connected:
            return True
        if self.gateway_up:
            self.connected = True; self.reconnects += 1
        return self.connected

    def resting_ioc_orders(self):
        return [o for o in self.orders.values() if o["tif"] == "IOC" and o["status"] == "open"]


# ── recorded-day replay ────────────────────────────────────────────────────
def load_day(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def quotes_from_cycle(cycle: dict, drift: float, base: dict[str, dict]) -> dict[str, dict]:
    """Quotes for this cycle: decision-time leg quotes from recorded attempts,
    plus a per-cycle drift applied to every symbol we already know (so held
    positions move and exits get exercised)."""
    out = {}
    for s, q in base.items():
        b = max(0.01, round(q["bid"] * (1 + drift), 2)); a = max(b + 0.01, round(q["ask"] * (1 + drift), 2))
        out[s] = {"bid": b, "ask": a}
    for a in (cycle.get("paper_execution_tests") or []):
        for l in a["trade"]["legs"]:
            if "bid" in l and "ask" in l and l["symbol"] not in out:
                out[l["symbol"]] = {"bid": float(l["bid"]), "ask": float(l["ask"])}
    return out


class Session:
    """Run a recorded day through the real run_cycle with the SimBroker."""

    def __init__(self, day_path: Path, broker: SimBroker, cfg: dict, *,
                 drift_per_cycle: float = 0.0, cycles: int | None = None):
        self.cycles = load_day(day_path)
        if cycles:
            self.cycles = self.cycles[:cycles]
        self.broker = broker
        self.cfg = cfg
        self.drift = drift_per_cycle
        self.exceptions: list[tuple[int, str]] = []
        self.tracebacks: list[tuple[int, str]] = []
        self.results: list[dict] = []

    def _trade_for(self, cycle):
        specs = {(a["ticker"], a["strategy"]): a["trade"] for a in (cycle.get("paper_execution_tests") or [])}
        def build_trade(ticker, strat, direction, last, z_overlay=False):
            t = specs.get((ticker, strat))
            # skip_detail must carry 'detail' — the funnel path indexes it (a real
            # build_trade always includes it; a stub that omits it kills the cycle)
            return (json.loads(json.dumps(t)), None) if t else \
                (None, {"cause": "no_recorded_spec", "detail": "sim: no recorded spec this cycle"})
        return build_trade

    def run(self):
        import src.engine.pipeline as pl
        import src.engine.paper_trader as pt
        import src.engine.exit_manager as em
        import src.engine.risk as risk
        import src.engine.confirm as cf
        import src.engine.live_gate as lg
        import src.engine.earnings as earn
        import src.control.state as cstate
        import src.data.freshness as fresh
        # Every production write target on the cycle path goes to conftest's
        # tmp (pt.FILLS_LOG is already redirected there by the O-20 seal). The
        # seal REFUSES anything we miss — that refusal is a test failure, which
        # is exactly right: a simulator that touches real evidence is worse
        # than no simulator.
        tmp = Path(pt.FILLS_LOG).parent
        (tmp / "observation").mkdir(exist_ok=True)
        wanted = [
            (pl, "OBS_DIR", tmp / "observation"),
            (pt, "ESCALATION_COUNTS_FILE", tmp / "exit_escalations.json"),
            (cf, "CONFIRM_FILE", tmp / "pending_confirmations.json"),
            (lg, "KILL_SWITCH_FILE", tmp / "KILL_SWITCH"),
            (earn, "CALENDAR_FILE", tmp / "earnings_calendar.json"),
            (cstate, "STATE_FILE", tmp / "agent_state.json"),
        ]
        # REC_LOG / RISK_STATE / FILLS_LOG / LANE_DIR are already redirected by conftest.
        redirects = [patch.object(m, n, v) for m, n, v in wanted if hasattr(m, n)]
        base_quotes: dict[str, dict] = {}
        for i, cyc in enumerate(self.cycles, 1):
            Clock.set(cyc["ts"])
            q = quotes_from_cycle(cyc, self.drift if i > 1 else 0.0, base_quotes)
            base_quotes = q
            self.broker.tick(i, q)
            sig = {t: dict(v, _sim_ticker=t) for t, v in (cyc.get("signals") or {}).items()
                   if isinstance(v, dict)}
            app = cyc.get("applicability") or {}
            patches = [
                patch.object(pl, "_load_cfg", return_value=self.cfg),
                patch.object(pl, "build_regime", return_value=dict(cyc.get("regime") or {})),
                patch.object(pl, "format_regime", return_value="  [regime]"),
                patch.object(pl, "scan_watchlist", return_value=(sig, [])),
                # applicability keyed by ticker (run_cycle may copy the signal dicts,
                # so identity matching silently yielded {} -> zero candidates)
                patch.object(pl, "evaluate_strategies",
                             side_effect=lambda s, r, m, _app=app: dict(_app.get(s.get("_sim_ticker"), {}))),
                patch.object(pl, "update_state"),
                patch.object(pl, "datetime", FakeDatetime),
                patch.object(pt, "datetime", FakeDatetime),
                patch.object(em, "datetime", FakeDatetime),
                patch.object(risk, "datetime", FakeDatetime),
                # freshness compares bar_time to ITS OWN datetime.now(ET); without
                # this every recorded bar looks days stale and nothing ever builds
                patch.object(fresh, "datetime", FakeDatetime),
                # expiry guard: keep the REAL logic, feed it the simulated 'today'
                patch.object(pt, "expiry_is_past",
                             side_effect=lambda exp, today=None, _o=pt.expiry_is_past:
                             _o(exp, today or FakeDate.today())),
                # the sim must NEVER reach the network: Claude is patched to a veto
                patch("src.llm.claude.resolve_validation",
                      return_value={"status": "vetoed", "approve": False, "reason": "sim: funnel off",
                                    "budget_used": 0,
                                    "verdict": {"approve": False, "confidence": 0.0,
                                                "narrative": "sim: funnel off"}}),
                patch.object(pt, "get_order_client", return_value=self.broker),
                patch.object(em, "get_client", return_value=self.broker),
                patch("src.engine.options_selector.build_trade", side_effect=self._trade_for(cyc)),
                patch("src.engine.earnings.earnings_veto", return_value=None),
                patch("src.llm.claude.check_gate", return_value=None),
                patch("src.engine.recommendation.recent_rec_exists", return_value=None),
                patch("src.engine.recommendation.log_recommendation"),
                patch("src.engine.notify.notify_recommendation"),
                patch("src.engine.notify.notify_fill"),
                patch("src.engine.notify.notify_exit"),
                patch("src.engine.notify.track_health"),
                patch("src.engine.notify.send"),
                patch.object(risk, "_funnel_exposure", return_value=(0, 0)),
                patch("src.data.freshness.current_session_state", return_value="open"),
            ]
            state = {"watchlist_override": list(sig.keys())[:30], "sector": "all", "mode": "all",
                     "cycle_count": i - 1}
            with contextlib.ExitStack() as st:
                for p in redirects + patches:
                    st.enter_context(p)
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        self.results.append(pl.run_cycle(i, state=state))
                    except Exception as exc:  # noqa: BLE001 — the whole point is to SEE these
                        import traceback
                        tb = traceback.format_exc().strip().splitlines()
                        self.tracebacks.append((i, "\n".join(tb[-9:])))
                        self.exceptions.append((i, f"{type(exc).__name__}: {exc}"))
        return self

    # ── invariants ──
    def events(self):
        import src.engine.paper_trader as pt
        p = Path(pt.FILLS_LOG)
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []

    def report(self) -> dict:
        import src.engine.paper_trader as pt
        ev = self.events()
        fills = [e for e in ev if e.get("event") == "paper_fill"]
        v2 = [e for e in fills if e.get("fill_mode") == "marketable_ioc"]
        unfilled = [e for e in ev if e.get("event") == "entry_ioc_unfilled"]
        exits = [e for e in ev if e.get("event") == "paper_exit"]
        pend = pt._load(pt.PENDING_FILE)
        return {"cycles": len(self.results), "exceptions": self.exceptions,
                "fills": len(fills), "v2_fills": len(v2), "unfilled_ioc": len(unfilled),
                "broker_rejects": sum(1 for e in unfilled if e.get("broker_reject")),
                "broker_reconnected": sum(1 for e in ev if e.get("event") == "broker_reconnected"),
                "broker_down_events": sum(1 for e in ev if e.get("event") == "broker_down_in_session"),
                "broker_lost_flags": sum(1 for e in ev if e.get("event") == "position_broker_lost"),
                "qty_adopted": sum(1 for e in ev if e.get("event") == "position_quantity_adopted"),
                "legged_alarms": sum(1 for e in ev if e.get("event") == "position_legged_alarm"),
                "expiry_flat_exits": sum(1 for e in exits if str(e.get("close_reason", "")).startswith("expiry_flat")),
                "attempts": len(v2) + len(unfilled), "exits": len(exits),
                "open_positions": len(pt.load_positions()), "pending_intents": len(pend),
                "resting_ioc_at_broker": len(self.broker.resting_ioc_orders()),
                "ioc_placed": sum(1 for p in self.broker.placed if p["tif"] == "IOC" and p["type"] == "debit"),
                "day_placed_entries": sum(1 for p in self.broker.placed if p["tif"] == "DAY" and p["type"] == "debit"),
                "exit_reasons": sorted({str(e.get("close_reason") or "")[:24] for e in exits}),
                "fill_modes": sorted({e.get("fill_mode", "resting_mid") for e in fills})}
