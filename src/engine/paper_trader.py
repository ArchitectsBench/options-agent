"""Paper execution in observation mode (Track B step 9 / Task 3).

Order lifecycle:
  submit (intent=open) -> fill -> POSITION created -> exit_manager watches it
  -> close order (intent=close) -> fill -> position closed, realized paper
  P&L recorded + notified.

Tracking files:
  data/paper_orders.json    — pending orders (either intent)
  data/paper_positions.json — open positions
Events land in logs/observation/fills.jsonl (entry slippage, exits with
P&L) — forward-test data for Track A. Sandbox-only by hard assertion.
"""

import json
import yaml
import logging
from datetime import datetime
from pathlib import Path

from src.data.order_client import (AMBIGUOUS, DEFINITIVE_REJECT,
                                    classify_post_result, get_order_client)
from src.data.tradier_orders import PaperOrderClient  # noqa: F401 - kept: canonical
# symbol the factory resolves for the tradier_sandbox default AND that existing
# tests patch (patch.object(paper_trader, "PaperOrderClient", ...)).
from src.util.atomic import atomic_save

ROOT = Path(__file__).resolve().parents[2]
# ── L-e RUNTIME REPOINT (2026-08-06): per-lane book storage ─────────────────
# Isolation is now STRUCTURAL, not a rec_id convention: each lane owns its own
# files and a write in one lane physically cannot land in the other's.
#
#   data/lane/funnel/{positions,orders}.json
#   data/lane/scalp/{positions,orders}.json
#
# POSITIONS_FILE / PENDING_FILE are retained as SENTINELS meaning "the whole
# book". _load(sentinel) returns the UNION across lanes; _save(sentinel, rows)
# PARTITIONS rows by lane and writes each lane's file. Every existing call site
# therefore keeps working unchanged while storage became lane-private. A real
# path (tests monkeypatch these to a tmp file) is still read/written directly.
# The old shared files are ARCHIVED and must never be opened again — see
# assert_shared_books_retired().
LANES = ("funnel", "scalp")
LANE_DIR = ROOT / "data" / "lane"
PENDING_FILE = LANE_DIR / "__ALL__orders.json"        # sentinel, never written
POSITIONS_FILE = LANE_DIR / "__ALL__positions.json"   # sentinel, never written
# IMMUTABLE sentinel identities. _sentinel_kind must compare against THESE, not
# against the module attributes: tests monkeypatch PENDING_FILE/POSITIONS_FILE
# to a tmp path, and comparing against the patched attribute made a tmp file
# look like a sentinel (union read) instead of a plain file.
_SENTINEL_POSITIONS = str(POSITIONS_FILE)
_SENTINEL_PENDING = str(PENDING_FILE)
LEGACY_PENDING = ROOT / "data" / "paper_orders.json"
LEGACY_POSITIONS = ROOT / "data" / "paper_positions.json"


def lane_book_path(kind: str, lane: str) -> Path:
    """Per-lane book file. kind: 'positions' | 'orders'."""
    return LANE_DIR / lane / f"{kind}.json"


def _sentinel_kind(path) -> str | None:
    p = str(path)
    if p == _SENTINEL_POSITIONS:
        return "positions"
    if p == _SENTINEL_PENDING:
        return "orders"
    return None
FILLS_LOG = ROOT / "logs" / "observation" / "fills.jsonl"

logger = logging.getLogger("trader")

# ITEM 2 loop breaker (2026-08-05): a malfunctioning exit path must never spin
# forever. Counted per rec_id, persisted alongside the book so a restart cannot
# silently reset the counter and resume looping.
MAX_EXIT_ESCALATIONS = 3

# ITEM 3 (2026-08-05): a partially-filled ENTRY is a real position. Book the
# filled quantity immediately so it enters normal management (stop/target/13:00
# flatten); cancel the unfilled remainder after this many seconds. Deliberately
# NO signal revalidation — out of scope by decision.
def partial_fill_remainder_cancel_seconds() -> int:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
        return int((cfg.get("agent") or {}).get(
            "partial_fill_remainder_cancel_seconds", 60))
    except Exception:  # noqa: BLE001
        return 60
ESCALATION_COUNTS_FILE = ROOT / "data" / "exit_escalations.json"


def _bump_exit_escalations(rec_id: str) -> int:
    """Increment and return this position's escalation count."""
    try:
        counts = json.loads(ESCALATION_COUNTS_FILE.read_text()) if ESCALATION_COUNTS_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        counts = {}
    counts[rec_id] = int(counts.get(rec_id, 0)) + 1
    try:
        atomic_save(ESCALATION_COUNTS_FILE, json.dumps(counts, indent=1))
    except Exception:  # noqa: BLE001
        pass
    return counts[rec_id]


def clear_exit_escalations(rec_id: str) -> None:
    """Reset on a successful close so a later re-open starts fresh."""
    try:
        if not ESCALATION_COUNTS_FILE.exists():
            return
        counts = json.loads(ESCALATION_COUNTS_FILE.read_text())
        if counts.pop(rec_id, None) is not None:
            atomic_save(ESCALATION_COUNTS_FILE, json.dumps(counts, indent=1))
    except Exception:  # noqa: BLE001
        pass

# The BOOK files — the authoritative record of what the engine believes it
# holds. A torn/unparseable book must HALT, never read as empty (an empty read
# stands all exits down while the broker still holds the legs).
_BOOK_FILES = (PENDING_FILE, POSITIONS_FILE)


class TornStateError(RuntimeError):
    """A book file exists on disk but will not parse. Raising this ABORTS the
    cycle (a HALT): we must never trade on an unknown book. An ABSENT file is
    normal startup and is NOT a torn state (returns [])."""


def _is_book_file(path: Path) -> bool:
    try:
        return Path(path).resolve() in {Path(p).resolve() for p in _BOOK_FILES}
    except OSError:
        return Path(path) in set(_BOOK_FILES)


def _load(path: Path) -> list[dict]:
    """Load a JSON list from ``path``.

    L-e: a SENTINEL (POSITIONS_FILE / PENDING_FILE) loads the UNION across
    lanes; pass a lane file directly for a lane-private read.

    Absent/empty file -> [] (normal startup). A CORRUPT existing BOOK file
    (paper_orders.json / paper_positions.json) is a SEV1 condition: it is
    logged CRITICAL, an URGENT notify is fired, and TornStateError is RAISED so
    the cycle aborts (HALT) rather than silently standing down every exit on an
    empty read. Non-book files retain the tolerant []-on-corrupt behavior."""
    kind = _sentinel_kind(path)
    if kind is not None:
        rows: list[dict] = []
        for lane in LANES:
            rows.extend(_load(lane_book_path(kind, lane)))
        return rows
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError:
        if _is_book_file(path):
            _halt_on_torn_book(path, "unreadable")
        return []
    if not text.strip():          # empty file == normal startup, not torn
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if _is_book_file(path):
            _halt_on_torn_book(path, str(exc))
        return []


def _halt_on_torn_book(path: Path, detail: str) -> None:
    """Log CRITICAL, fire an URGENT notify (a notify failure must NEVER mask the
    halt), and raise TornStateError. Propagates up to abort the cycle."""
    logger.critical(
        "TORN BOOK FILE %s (%s) — refusing to trade on an unknown book; "
        "cycle HALTED, run scripts/reconcile_positions.py", path, detail)
    try:
        from src.engine.notify import send
        send("TORN STATE — TRADING HALTED",
             f"book file {Path(path).name} is corrupt ({detail}); the engine "
             "cannot know its positions and has HALTED. Reconcile before "
             "resuming.\nUNVALIDATED — PAPER ONLY",
             priority="urgent", tags="rotating_light")
    except Exception as notify_exc:  # notify failure must not swallow the halt
        logger.error("torn-book notify failed (halt still raised): %s", notify_exc)
    raise TornStateError(f"{path}: {detail}")


def _save(path: Path, items: list[dict]):
    """L-e: a SENTINEL write PARTITIONS rows by lane and writes each lane's own
    file, so a row belonging to one lane can never land in the other's book."""
    kind = _sentinel_kind(path)
    if kind is not None:
        parts: dict[str, list[dict]] = {lane: [] for lane in LANES}
        for row in items:
            parts[lane_of(row.get("rec_id", ""))].append(row)
        for lane, rows in parts.items():
            f = lane_book_path(kind, lane)
            f.parent.mkdir(parents=True, exist_ok=True)
            atomic_save(f, json.dumps(rows, indent=2, default=str))
        return
    atomic_save(path, json.dumps(items, indent=2, default=str))


def load_positions_for_lane(lane: str) -> list[dict]:
    """Lane-PRIVATE position read — never sees the other lane's rows."""
    return _load(lane_book_path("positions", lane))


def load_pending_for_lane(lane: str) -> list[dict]:
    """Lane-PRIVATE pending read — never sees the other lane's rows."""
    return _load(lane_book_path("orders", lane))


def assert_shared_books_retired() -> None:
    """Startup check (L-e): the legacy SHARED book files must never be opened
    again. They are archived read-only; if a live one reappears with rows, some
    code path is still writing the pre-migration layout — fail loudly."""
    for legacy in (LEGACY_POSITIONS, LEGACY_PENDING):
        if not legacy.exists():
            continue
        try:
            rows = json.loads(legacy.read_text() or "[]")
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            raise RuntimeError(
                f"SHARED BOOK STILL IN USE: {legacy} holds {len(rows)} row(s). "
                "Lane books are authoritative (data/lane/<lane>/); a code path "
                "is still writing the retired shared layout.")


def load_positions() -> list[dict]:
    return _load(POSITIONS_FILE)


def _log_event(event: dict):
    FILLS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(FILLS_LOG, "a") as fh:
        fh.write(json.dumps(event, default=str) + "\n")


def expiry_is_past(expiration, today=None) -> bool:
    """True when an option expiry is BEFORE today. Submitting into a lapsed
    contract can never fill and, if it somehow does, cannot be managed. Seen
    live on 2026-08-07: a card offered a 2026-07-10 expiry a month after the
    fact (it was a test fixture, but nothing in the submit path would have
    refused it)."""
    if not expiration:
        return False
    from datetime import date
    today = today or date.today()
    try:
        exp = date.fromisoformat(str(expiration)[:10])
    except ValueError:
        return False          # unparseable -> do not block on a format we don't know
    return exp < today


def rec_id_already_live(rec_id: str) -> bool:
    """WRITE-SIDE DEDUP: this rec_id already has a pending order or an open
    position. Two records for one decision is how the queue showed the same
    card repeatedly."""
    rid = str(rec_id)
    return any(str(o.get("rec_id")) == rid for o in _load(PENDING_FILE)) or \
           any(str(p.get("rec_id")) == rid for p in load_positions())


def _closing_action(action: str) -> str:
    return {"buy_to_open": "sell_to_close", "sell_to_open": "buy_to_close"}[action]


def _occ_strike(symbol: str) -> float | None:
    """Strike from an OCC option symbol (last 8 digits, thousandths)."""
    try:
        return int(symbol[-8:]) / 1000.0
    except (TypeError, ValueError):
        return None


def strike_width(legs: list[dict]) -> float | None:
    """Max strike distance across legs in per-share dollars — the hard value
    ceiling of a defined-risk vertical/condor. None for single legs or
    unparseable symbols."""
    strikes = [s for s in (_occ_strike(l.get("symbol") or "") for l in legs) if s is not None]
    return round(max(strikes) - min(strikes), 4) if len(strikes) >= 2 else None


WIDTH_TOLERANCE = 0.05  # quotes can cross a hair past width; beyond this = corrupt fill


def exit_reason_code(reason: str | None) -> str:
    """Canonical exit_reason enum for the observation dataset."""
    if not reason:
        return "unknown"
    if reason.startswith("time_flat"):
        return "flatten_1545"
    if reason.startswith("invalidation"):
        return "vwap_invalidation"
    if reason.startswith("stop_loss"):
        return "stop_50"
    if reason.startswith("profit_target"):
        return "target_50"
    if reason.startswith("quote_outage_flatten"):
        return "quote_outage_flatten"
    return "other"


def executed_quantity(order: dict, detail: dict, intent_qty=None):
    """ITEM 6 — FRESH-TRUTH SIZING. The executed contract count, taken from
    BROKER TRUTH, never from the intent.

    The 2026-08-05 GOOGL incident: order 23 requested 5, the broker filled 4,
    and the book recorded 5 because the position quantity was copied from
    `p["quantity"]` (the request). The phantom 5th then drove an exit that could
    not fill, the position was dropped from the book while contracts remained at
    the broker, and a later entry stacked on the residue.

    Preference order:
      1. order["filled_quantity"] (IBKR: st.filled — the executed count)
      2. min leg exec_quantity across legs (a spread is only as filled as its
         least-filled leg; anything else would claim legs we do not hold)
      3. intent quantity (Tradier reports neither; documented fallback)
    """
    fq = (order or {}).get("filled_quantity")
    if fq is not None and float(fq) > 0:
        return int(float(fq))
    legs = (detail or {}).get("legs") or []
    qtys = [float(l.get("quantity") or 0) for l in legs]
    qtys = [q for q in qtys if q > 0]
    if qtys:
        return int(min(qtys))
    # NO broker truth available. Do NOT invent a number — that is the very
    # substitution this function exists to prevent. Callers decide: the full-fill
    # path falls back to the intent (documented, Tradier reports no filled qty),
    # the PARTIAL path refuses to book at all.
    return None if intent_qty is None else int(intent_qty or 1)


def order_fill_detail(order: dict) -> dict:
    """Leg-aware fill accounting. Tradier reports CREDIT (closing) multileg
    orders with a NEGATIVE parent avg_fill_price (root cause of the 6/11
    sign-corrupted exit records) — leg-level prices are unambiguous, so net
    everything from legs: net_debit = sum(buy legs) - sum(sell legs).
    Positive = paid (entry debit); negative = received (closing credit)."""
    legs = order.get("leg", [])
    legs = [legs] if isinstance(legs, dict) else legs
    leg_detail, buys, sells, have_leg_fills = [], 0.0, 0.0, False
    for l in legs:
        avg = float(l.get("avg_fill_price") or 0)
        side = l.get("side", "")
        leg_detail.append({"symbol": l.get("option_symbol") or l.get("symbol"),
                           "side": side, "avg_fill_price": avg,
                           "quantity": float(l.get("exec_quantity") or 0)})
        if avg:
            have_leg_fills = True
        if side.startswith("buy"):
            buys += avg
        elif side.startswith("sell"):
            sells += avg
    if have_leg_fills:
        net_debit = round(buys - sells, 4)
    else:  # single-leg orders carry no leg array. The parent avg_fill_price is
           # UNSIGNED (always positive), so it MUST be signed by the order side:
           # sell_to_* is a credit received (negative debit). Without this a
           # single-leg sell-to-close was booked as a positive debit, flipping the
           # closing credit negative (2026-06-25 SPY 0DTE phantom -$349 loss).
        avg = float(order.get("avg_fill_price") or 0)
        net_debit = round(-avg if str(order.get("side", "")).startswith("sell") else avg, 4)
    return {"legs": leg_detail, "net_debit": net_debit,
            "broker_created": order.get("create_date"),
            "broker_closed": order.get("transaction_date")}


# ── SCALP MARKER RENAME (2026-08-08, FORWARD-ONLY) ──────────────────────────
# '-TEST-' marked the one class that is NOT a test: the SCALP stream is real
# strategy trading with its own FROZEN forward gate (config/scalp_forward_
# criteria.yaml). Worse, it collided with pytest-fixture ids ending 'TEST-1-...',
# and that collision made a quarantine filter sweep in 8 REAL traded funnel recs.
#
# HISTORICAL IDS ARE NEVER REWRITTEN. They are join keys into fills.jsonl,
# enriched_trades_v1.jsonl, post-mortems and verified evidence; rewriting them
# would break every published finding. New ids carry '-SCALP-' from the cutover;
# every reader accepts BOTH markers, forever.
SCALP_MARKER = "-SCALP-"
LEGACY_SCALP_MARKER = "-TEST-"
SCALP_RENAME_CUTOVER = "2026-08-08"      # ids minted on/after this date use -SCALP-


def is_scalp(rec_id) -> bool:
    """Canonical SCALP test — accepts both eras. THE single place that knows
    about markers; every filter must route through this."""
    rid = str(rec_id or "")
    return SCALP_MARKER in rid or LEGACY_SCALP_MARKER in rid


def scalp_marker_for(day: str | None = None) -> str:
    """Marker to MINT with. Forward-only: legacy before the cutover."""
    from datetime import datetime
    day = day or f"{datetime.now():%Y-%m-%d}"
    return SCALP_MARKER if day >= SCALP_RENAME_CUTOVER else LEGACY_SCALP_MARKER


def lane_of(rec_id: str) -> str:
    """Stream lane from the rec_id convention: SCALP execution-test ids carry
    -TEST-; everything else is the funnel."""
    return "scalp" if is_scalp(rec_id) else "funnel"


def _lane_isolation_on() -> bool:
    """Dated amendment 2026-08-03 (user-approved, effective immediately): the
    SCALP and funnel streams keep INDEPENDENT position books — the shared
    one-position-per-ticker rule let the faster SCALP lane starve the funnel
    out of its own names (08-03: HOOD x10, WMT x6 position_exists kills).
    Config-flagged for rollback; combined per-name exposure is bounded by
    book_underlying_risk_usd + risk.max_book_risk_per_underlying_usd."""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
        return bool(cfg.get("agent", {}).get("lane_isolation", False))
    except Exception:
        return False


def has_open_exposure(ticker: str, strategy: str, direction: str,
                      lane: str | None = None) -> str | None:
    """One position per ticker+strategy+direction PER LANE (when lane_isolation
    is on and a lane is given; global otherwise). Checks open positions AND
    pending entry orders. Returns a description or None."""
    scoped = lane is not None and _lane_isolation_on()
    for p in load_positions():
        if (p["ticker"], p.get("strategy"), p.get("direction")) == (ticker, strategy, direction):
            if scoped and lane_of(p.get("rec_id", "")) != lane:
                continue                      # other lane's position — not a blocker
            return f"open position {p['rec_id']} ({p['structure']})"
    for o in _load(PENDING_FILE):
        if o.get("intent") == "open" and \
                (o["ticker"], o.get("strategy"), o.get("direction")) == (ticker, strategy, direction):
            if scoped and lane_of(o.get("rec_id", "")) != lane:
                continue
            return f"pending entry order {o['order_id']} ({o['structure']})"
    return None


def book_underlying_risk_usd(ticker: str) -> float:
    """Combined max-loss (dollars) currently at risk on one underlying across
    BOTH lanes — open positions + pending entry intents. The book-level
    concentration bound that makes lane independence safe: sleeves are
    independent, the book still can't stack unlimited exposure on one name.
    max_loss is per-share of structure -> x100 x quantity."""
    total = 0.0
    for p in load_positions() + [o for o in _load(PENDING_FILE) if o.get("intent") == "open"]:
        if p.get("ticker") != ticker:
            continue
        ml = p.get("max_loss")
        qty = int(p.get("quantity") or 1)
        if ml is not None:
            total += abs(float(ml)) * 100 * qty
    return round(total, 2)


def open_paper_count(lane: str | None = None) -> int:
    """Open positions + pending OPEN orders.

    L-d (2026-08-06): PER-LANE when `lane` is given. Previously this pooled both
    lanes, so FUNNEL positions consumed SCALP's concurrency budget — a lane
    could be starved by the other's activity. Execution state is lane-isolated;
    only account-level RISK aggregates span lanes (restated O-13).
    `lane=None` keeps the pooled count for callers that want the book total.
    """
    positions = load_positions()
    pending_open = [o for o in _load(PENDING_FILE) if o.get("intent") == "open"]
    if lane is not None:
        positions = [p for p in positions if lane_of(p.get("rec_id", "")) == lane]
        pending_open = [o for o in pending_open if lane_of(o.get("rec_id", "")) == lane]
    return len(positions) + len(pending_open)


def max_open_for_lane(lane: str, cfg: dict | None = None) -> int:
    """Per-lane concurrency budget (L-d). Falls back to the legacy single cap."""
    try:
        if cfg is None:
            import yaml
            cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
        per = ((cfg.get("risk") or {}).get("max_open_per_lane") or {})
        if lane in per:
            return int(per[lane])
        agent = cfg.get("agent") or {}
        return int(agent.get("paper_test_max_open", 7) if lane == "scalp"
                   else (cfg.get("risk") or {}).get("max_open_recommendations", 3))
    except Exception:  # noqa: BLE001
        return 7 if lane == "scalp" else 3


def today_test_entry_count() -> int:
    """Distinct EXECUTION-TEST entries placed TODAY (pending + positions + fills) —
    the real daily throttle. Concurrent cap alone let it churn (positions close ->
    slot frees -> re-fires every cycle)."""
    # C-1 (2026-08-10, operator-approved): this matched the literal prefix
    # '<YYYYMMDD>-TEST-'. After the 2026-08-08 forward-only rename ids are
    # minted '<YYYYMMDD>-SCALP-', so it returned 0 UNCONDITIONALLY and the
    # daily cap never bound — 11 entries were placed on 2026-08-10 against a
    # cap of 20 that could not fire. Split the two questions it was conflating:
    # "is this today's id" (date prefix) and "is this the SCALP lane"
    # (is_scalp, which accepts BOTH marker eras). Restores the configured cap;
    # does not change it.
    day = f"{datetime.now():%Y%m%d}-"
    def _today_scalp(rid: str) -> bool:
        return rid.startswith(day) and is_scalp(rid)
    ids = set()
    for o in _load(PENDING_FILE) + load_positions():
        rid = str(o.get("rec_id", ""))
        if _today_scalp(rid):
            ids.add(rid)
    if FILLS_LOG.exists():
        for line in FILLS_LOG.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            # SCALP-v2 (2026-09-12): this counted ANY row carrying today's rec_id,
            # so an entry_ioc_unfilled row consumed a FILLED-entry slot — ten
            # unfilled IOCs ended the day with zero positions and the separate
            # attempts cap could never bind. Only executions count here.
            if e.get("event") not in ("paper_fill", "partial_fill_booked"):
                continue
            rid = str(e.get("rec_id", ""))
            if _today_scalp(rid):
                ids.add(rid)
    return len(ids)


def _bind_intent(intent_seq: str, order_id) -> None:
    """Attach the broker order id to a write-ahead intent record."""
    pending = _load(PENDING_FILE)
    for o in pending:
        if o.get("intent_seq") == intent_seq:
            o["order_id"] = order_id
    _save(PENDING_FILE, pending)


def _drop_intent(intent_seq: str) -> None:
    _save(PENDING_FILE, [o for o in _load(PENDING_FILE)
                         if o.get("intent_seq") != intent_seq])


def scalp_v2_config(cfg: dict | None = None) -> dict:
    """agent.scalp_v2 with defaults. OFF unless the operator flips `enabled`
    (docs/scalp_v2_prereg.md). When off, v1 behaviour is byte-identical."""
    try:
        if cfg is None:
            cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
        v2 = (cfg.get("agent") or {}).get("scalp_v2") or {}
    except Exception as exc:  # noqa: BLE001
        # FAIL LOUD (2026-09-12): on activation night this returned {} — i.e.
        # "v2 OFF" — because `yaml` was only imported inside OTHER functions;
        # the NameError was swallowed here and the engine restarted believing
        # the operator's activation had not happened. A config read that fails
        # must never quietly answer "disabled".
        logger.error("scalp_v2_config: config read FAILED (%s: %s) — treating as "
                     "DISABLED; this is a DEFECT, not a setting", type(exc).__name__, exc)
        v2 = {"_read_failed": True}
    if not isinstance(v2, dict):          # `scalp_v2: yes` / a list / a string
        logger.error("scalp_v2_config: agent.scalp_v2 is %s, not a mapping — treating as "
                     "read_failed/DISABLED", type(v2).__name__)
        v2 = {"_read_failed": True}
    out = {"enabled": False, "read_failed": bool(v2.get("_read_failed")),
           "fill_mode": "marketable_ioc", "aggressiveness": 0.5, "min_net_debit": 1.0,
           "max_per_day": 10, "max_attempts_per_day": 30,
           # marketable_day only (2026-09-16): how long the DAY limit may rest
           # before cancel_stale_entries kills it, and how long a cancelled
           # setup is barred from re-firing (churn guard).
           "entry_timeout_min": 5, "reattempt_cooldown_min": 30}
    if out["read_failed"]:
        return out
    try:
        if not isinstance(v2, dict):
            raise TypeError(f"agent.scalp_v2 must be a mapping, got {type(v2).__name__}")
        en = v2.get("enabled", False)
        # bool() would make the STRINGS "false"/"off"/"0"/"no" activate v2.
        if isinstance(en, str):
            en = en.strip().lower() in ("true", "1", "yes", "on")
        out["enabled"] = bool(en)
        fm = str(v2.get("fill_mode", "marketable_ioc")).strip()
        if fm not in ("marketable_ioc", "marketable_day", "resting_mid"):
            raise ValueError(f"unknown fill_mode {fm!r} "
                             f"(expected marketable_ioc|marketable_day|resting_mid)")
        out["fill_mode"] = fm
        out["aggressiveness"] = float(v2.get("aggressiveness", 0.5))
        out["min_net_debit"] = float(v2.get("min_net_debit", 1.0))
        out["max_per_day"] = int(v2.get("max_per_day", 10))
        out["max_attempts_per_day"] = int(v2.get("max_attempts_per_day", 30))
        out["entry_timeout_min"] = int(v2.get("entry_timeout_min", 5))
        out["reattempt_cooldown_min"] = int(v2.get("reattempt_cooldown_min", 30))
    except Exception as exc:  # noqa: BLE001 — a malformed VALUE must not kill the cycle
        logger.error("scalp_v2_config: malformed config (%s: %s) — treating as "
                     "read_failed/DISABLED; this is a DEFECT, not a setting",
                     type(exc).__name__, exc)
        out.update({"enabled": False, "read_failed": True})
    return out



V3_DEFAULTS = {"tickers": ["SPY", "QQQ", "IWM"], "strategies": ["0dte_directional"],
               "entry_window_et": ["09:35", "11:30"], "min_volume_ratio": 0.8,
               "fill_mode": "marketable_ioc", "shadow_direction_frame": "5m"}


def scalp_v3_config(cfg: dict | None = None) -> dict:
    """agent.scalp_v3 with defaults (docs/scalp_v3_prereg.md). v3 is a FILTER
    LAYER on top of SCALP-v2: v2's caps, price floor, exits and sizing still
    apply; v3 narrows the universe/structures/window, adds the volume gate and
    sends single-leg entries as IOC. OFF unless the operator flips `enabled`;
    when off, v2 behaviour is byte-identical.

    Fail-closed like scalp_v2_config: an unreadable or malformed block reports
    read_failed=True and the pipeline places NOTHING (a torn v3 config must
    never quietly trade v2's wider rules)."""
    out = {"enabled": False, "read_failed": False, **{k: (list(v) if isinstance(v, list) else v)
                                                       for k, v in V3_DEFAULTS.items()}}
    try:
        if cfg is None:
            cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
        v3 = (cfg.get("agent") or {}).get("scalp_v3")
        if v3 is None:
            return out
        if not isinstance(v3, dict):
            raise TypeError(f"agent.scalp_v3 must be a mapping, got {type(v3).__name__}")
        en = v3.get("enabled", False)
        if isinstance(en, str):
            en = en.strip().lower() in ("true", "1", "yes", "on")
        out["enabled"] = bool(en)
        tk = [str(t).strip().upper() for t in v3.get("tickers", out["tickers"])]
        st = [str(x).strip() for x in v3.get("strategies", out["strategies"])]
        win = [str(x).strip() for x in v3.get("entry_window_et", out["entry_window_et"])]
        if not tk or not st:
            raise ValueError("tickers/strategies must be non-empty")
        if len(win) != 2:
            raise ValueError(f"entry_window_et must be [start, end], got {win!r}")
        s_m = [int(a) * 60 + int(b) for a, b in (w.split(":") for w in win)]
        if not (0 <= s_m[0] < s_m[1] <= 24 * 60):
            raise ValueError(f"entry_window_et must be increasing HH:MM, got {win!r}")
        fm = str(v3.get("fill_mode", "marketable_ioc")).strip()
        if fm not in ("marketable_ioc", "marketable_day"):
            raise ValueError(f"v3 fill_mode must be marketable_ioc|marketable_day, got {fm!r}")
        out.update({"tickers": tk, "strategies": st, "entry_window_et": win,
                    "entry_window_min": s_m, "fill_mode": fm,
                    "min_volume_ratio": float(v3.get("min_volume_ratio", 0.8)),
                    "shadow_direction_frame": str(v3.get("shadow_direction_frame", "5m"))})
    except Exception as exc:  # noqa: BLE001 — never kill the cycle; never trade wider rules
        logger.error("scalp_v3_config: config read/parse FAILED (%s: %s) — placing "
                     "nothing; this is a DEFECT, not a setting", type(exc).__name__, exc)
        out.update({"enabled": False, "read_failed": True})
    return out



def _ioc_unfilled_already_logged(rec_id: str) -> bool:
    day = f"{datetime.now():%Y-%m-%d}"
    if not FILLS_LOG.exists():
        return False
    for line in FILLS_LOG.read_text().splitlines():
        if '"entry_ioc_unfilled"' not in line or rec_id not in line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") == "entry_ioc_unfilled" and e.get("rec_id") == rec_id \
                and str(e.get("ts", "")).startswith(day):
            return True
    return False


def today_v2_attempt_count() -> int:
    """IOC ATTEMPTS today = v2 fills + unfilled IOCs. An unfilled IOC consumes
    no fill slot, so the fill cap alone could not bound attempts."""
    day = f"{datetime.now():%Y-%m-%d}"
    n = 0
    if FILLS_LOG.exists():
        for line in FILLS_LOG.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not str(e.get("ts", "")).startswith(day):
                continue
            if e.get("event") == "entry_ioc_unfilled" or \
                    (e.get("event") in ("paper_fill", "partial_fill_booked")
                     and str(e.get("fill_mode", "")).startswith("marketable")) or \
                    (e.get("event") == "entry_expired"
                     and str(e.get("fill_mode", "")).startswith("marketable")):
                n += 1
    return n


def recent_entry_expiry(ticker: str, strategy: str, direction: str,
                        cooldown_min: int) -> str | None:
    """marketable_day churn guard (2026-09-16). Once a DAY entry is cancelled at
    the timeout there is no position and no pending order, so a signal that
    persists would re-fire the same setup every cycle. Returns a reason while
    ticker+strategy+direction is inside the cooldown, else None. Only
    marketable_* expiries count — a v1 30-min expiry is a different mechanism."""
    if cooldown_min <= 0 or not FILLS_LOG.exists():
        return None
    for line in reversed(FILLS_LOG.read_text().splitlines()):
        if '"entry_expired"' not in line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") != "entry_expired" or \
                not str(e.get("fill_mode", "")).startswith("marketable"):
            continue
        if (e.get("ticker"), e.get("strategy"), e.get("direction")) != \
                (ticker, strategy, direction):
            continue
        try:
            ts = datetime.fromisoformat(str(e.get("ts")))
        except (TypeError, ValueError):
            continue
        age_min = (datetime.now() - ts).total_seconds() / 60
        # newest matching expiry decides: inside -> block, outside -> allow
        return (f"re-attempt cooldown: {ticker} {strategy} expired {ts:%H:%M} "
                f"({cooldown_min}m)") if age_min <= cooldown_min else None
    return None


def v2_entry_limit(trade: dict, aggressiveness: float) -> float:
    """SCALP-v2 marketable limit: decision mid + aggressiveness x quoted spread.
    aggressiveness 0.5 == the natural (ask for a debit); 0 == v1's mid.
    For a spread the net spread is the SUM of the legs' spreads (worst case
    for a combo, which fills at the net NBBO)."""
    legs = trade.get("legs") or []
    def _spread(l):
        # a leg with NO quote fields cannot be priced marketably; a silent
        # spread of 0 would send an IOC at the mid that can never fill
        if l.get("spread") is not None and float(l["spread"]) > 0:
            return float(l["spread"])
        if l.get("ask") is None or l.get("bid") is None:
            raise ValueError(f"leg {l.get('symbol')} has no bid/ask/spread — cannot price a marketable IOC")
        return float(l["ask"]) - float(l["bid"])
    if len(legs) == 1:
        leg = legs[0]
        return round(max(0.01, float(leg["mid"]) + aggressiveness * _spread(leg)), 2)
    sp = sum(_spread(l) for l in legs)
    return round(max(0.01, float(trade["net_debit"]) + aggressiveness * sp), 2)


def submit_paper_order(rec: dict, tag: str = "UNVALIDATED-PAPER-ONLY",
                       quantity: int = 1, fill_mode: str = "resting_mid") -> dict | None:
    """Submit the recommendation as a sandbox paper ENTRY order.

    quantity comes from the risk kernel (risk.gate_new_entry) — never
    hardcode sizes at call sites.

    Write-ahead: the intent record hits disk BEFORE the broker POST, so a
    crash in between leaves a recoverable order_id=None intent (resolved by
    resolve_intents) instead of an invisible live order."""
    # ── PRE-SUBMIT GUARDS (2026-08-07) ──────────────────────────────────────
    trade = rec.get("trade") or {}
    exp = trade.get("expiration")
    if expiry_is_past(exp):
        logger.error("REFUSING ORDER %s: expiration %s is BEFORE today — a "
                     "lapsed contract cannot fill or be managed",
                     rec.get("rec_id"), exp)
        _log_event({"ts": datetime.now().isoformat(timespec="seconds"),
                    "event": "order_refused_expired", "rec_id": rec.get("rec_id"),
                    "ticker": rec.get("ticker"), "expiration": exp,
                    "stamp": "UNVALIDATED — PAPER ONLY"})
        return {"submitted": False, "refused": "expiry_in_past", "expiration": exp}
    if rec_id_already_live(rec.get("rec_id")):
        logger.warning("REFUSING DUPLICATE %s: rec_id already has a pending "
                       "order or open position", rec.get("rec_id"))
        return {"submitted": False, "refused": "duplicate_rec_id"}

    client = get_order_client()
    if not client.available():
        # Until 2026-10-20 this was a bare `return None` with no logger line and
        # no ledger row, so a submit into an absent broker left NO trace at all
        # (two silent submits, recs 5898/5906, 10-13 and 10-14). Behaviour is
        # UNCHANGED — still None — only the attempt becomes queryable.
        # The event name is deliberately NEW: every attempt/fill counter keys
        # off entry_ioc_unfilled / paper_fill / partial_fill_booked, so this row
        # cannot move the SCALP-v3 ioc_fill_rate denominator. Whether a
        # brokerless attempt SHOULD count there is an operator decision, and
        # must not be settled by how this plumbing happens to be built.
        logger.warning("SUBMIT DROPPED %s (%s): broker unavailable at submit",
                       rec.get("rec_id"), rec.get("ticker"))
        _log_event({"ts": datetime.now().isoformat(timespec="seconds"),
                    "event": "broker_unavailable_at_submit", "intent": "open",
                    "rec_id": rec.get("rec_id"), "ticker": rec.get("ticker"),
                    "stamp": "UNVALIDATED — PAPER ONLY"})
        return None
    trade, ticker = rec["trade"], rec["ticker"]
    quantity = max(1, int(quantity))

    intent = {
        "intent": "open",
        "intent_seq": datetime.now().isoformat(timespec="microseconds"),
        "order_id": None,
        "rec_id": rec["rec_id"],
        "ticker": ticker,
        "quantity": quantity,
        "structure": trade["structure"],
        "strategy": trade["strategy"],
        "direction": trade["direction"],
        "legs": [{"symbol": leg["symbol"], "action": leg["action"]} for leg in trade["legs"]],
        "rec_price": trade["net_debit"],
        "max_profit": trade["max_profit"],
        "max_loss": trade["max_loss"],
        "expiration": trade["expiration"],
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "tag": tag,
        # SCALP-v2 (2026-09-12): the fill MECHANISM is part of the evidence.
        # v1 rests a DAY limit at the decision mid; v2 sends immediate-or-cancel
        # at mid + aggressiveness*spread. Stamped on the intent so every fill
        # and exit downstream can be scoped by mode, and so v1/v2 evidence can
        # never be blended.
        "fill_mode": fill_mode,
    }
    if fill_mode not in ("marketable_ioc", "marketable_day", "resting_mid"):
        logger.error("REFUSING ORDER %s: unknown fill_mode %r — not resting a DAY "
                     "order in v2's name", rec.get("rec_id"), fill_mode)
        return {"submitted": False, "refused": "unknown_fill_mode", "fill_mode": fill_mode}
    if fill_mode in ("marketable_ioc", "marketable_day"):
        v2 = scalp_v2_config()
        if v2.get("read_failed"):
            logger.error("REFUSING ORDER %s: v2 config read FAILED — will not send a "
                         "marketable order on default parameters", rec.get("rec_id"))
            return {"submitted": False, "refused": "v2_config_read_failed"}
        try:
            limit_sent = v2_entry_limit(trade, v2["aggressiveness"])
        except (ValueError, KeyError, TypeError) as exc:
            # a leg without quotes cannot be priced marketably — REFUSE this
            # candidate; never let the exception kill the cycle
            logger.error("REFUSING ORDER %s: cannot price marketable entry (%s)", rec.get("rec_id"), exc)
            return {"submitted": False, "refused": "unquoted_leg", "detail": str(exc)}
        # marketable_day (2026-09-16): SAME price as v2, but DAY — IBKR refuses
        # IOC on a SMART combo (error 201), and a limit at the natural is
        # marketable on arrival anyway (measured median wait: 0 min). The
        # "don't linger" half of IOC is done by cancel_stale_entries at
        # scalp_v2.entry_timeout_min instead of by the broker.
        tif = "IOC" if fill_mode == "marketable_ioc" else "DAY"
    else:
        limit_sent = round(max(0.01, trade["legs"][0]["mid"] if len(trade["legs"]) == 1
                               else trade["net_debit"]), 4)
        tif = "DAY"
    intent["limit_sent"] = limit_sent
    intent["tif"] = tif
    pending = _load(PENDING_FILE)
    pending.append(intent)
    _save(PENDING_FILE, pending)

    if len(trade["legs"]) == 1:
        leg = trade["legs"][0]
        result = client.place_option_order(ticker, leg["symbol"], leg["action"], quantity,
                                           limit_sent, tif=tif)
    else:
        result = client.place_spread_order(
            ticker,
            [{"option_symbol": leg["symbol"], "side": leg["action"], "quantity": quantity}
             for leg in trade["legs"]],
            limit_sent, tif=tif,
        )
    outcome = classify_post_result(result)
    if outcome["status"] == AMBIGUOUS:
        # R1: the POST got no broker response — the entry order MAY be live. Do
        # NOT drop the intent and do NOT resubmit. Leaving it UNBOUND (order_id
        # stays None) hands it to resolve_intents, which binds it to the live
        # broker order by tag + legs, or drops it only once the broker view
        # proves no such order exists.
        logger.warning("paper entry AMBIGUOUS for %s: %s — intent left unbound "
                       "for resolve_intents (no resubmit)", ticker, outcome.get("reason"))
        return {"submitted": False, "ambiguous": True, "response": result}
    if outcome["status"] == DEFINITIVE_REJECT:
        _drop_intent(intent["intent_seq"])
        if fill_mode in ("marketable_ioc", "marketable_day") and isinstance(result, dict) \
                and result.get("order"):
            # The broker ACCEPTED the POST and terminated it unfilled (IOC with
            # no liquidity, or a post-time reject). That is a completed attempt
            # — the attempt cap and fill-rate denominator must see it. A qualify
            # failure (no 'order' in the response) never reached the market and
            # is not counted.
            row = {"ts": datetime.now().isoformat(timespec="seconds"),
                   "event": "entry_ioc_unfilled", "rec_id": rec["rec_id"],
                   "ticker": ticker, "order_id": (result.get("order") or {}).get("id"),
                   "limit_sent": limit_sent, "fill_mode": fill_mode,
                   "terminal_status": (result.get("order") or {}).get("status"),
                   "stamp": "UNVALIDATED — PAPER ONLY"}
            if (result.get("order") or {}).get("status") == "rejected":
                # 2026-09-15: a broker REFUSAL (IBKR 201 — IOC is not accepted
                # on SMART combos) never reached the market. Same event name
                # (the frozen attempt cap and fill-rate clause read it), with
                # the broker's reason kept so reports can separate the two.
                row["broker_reject"] = {"code": result.get("error_code"),
                                        "text": result.get("reason") or result.get("error")}
            _log_event(row)
        logger.warning("paper entry rejected for %s: %s", ticker, result)
        return {"submitted": False, "response": result}
    if fill_mode in ("marketable_ioc", "marketable_day") \
            and outcome.get("order_status") in ("canceled", "expired", "rejected"):
        # The IOC was already terminal in the POST response (no liquidity at
        # our price). That is a completed ATTEMPT: log it now so the attempt
        # cap and fill-rate denominator see it, and drop the dead intent.
        _log_event({"ts": datetime.now().isoformat(timespec="seconds"),
                    "event": "entry_ioc_unfilled", "rec_id": rec["rec_id"],
                    "ticker": ticker, "order_id": outcome.get("order_id"),
                    "limit_sent": limit_sent, "fill_mode": fill_mode,
                    "stamp": "UNVALIDATED — PAPER ONLY"})
        _drop_intent(intent["intent_seq"])
        logger.info("IOC unfilled at post time for %s (%s)", ticker, outcome.get("order_status"))
        return {"submitted": False, "ioc_unfilled": True, "response": result}

    order = result.get("order", {})
    _bind_intent(intent["intent_seq"], order.get("id"))
    logger.info("paper entry %s submitted for %s (%s)", order.get("id"), ticker, trade["structure"])
    return {"submitted": True, "order_id": order.get("id"), "status": order.get("status")}


def submit_close_order(position: dict, limit_price: float, reason: str) -> dict | None:
    """Submit the closing order for an open position. Write-ahead like
    submit_paper_order: intent on disk before the broker POST."""
    if position.get("broker_lost"):
        # L20: broker no longer holds the legs — every submit is a guaranteed
        # reject. Central guard (covers exit manager, watchdog, escalation).
        logger.warning("close suppressed for %s: broker_lost, awaiting reconcile",
                       position["ticker"])
        return None
    client = get_order_client()
    if not client.available():
        # Latent twin of the open-path hole, and the worse one: a silently
        # dropped EXIT attempt on an open position. Same treatment — the None
        # return is preserved, the attempt is recorded.
        logger.warning("CLOSE DROPPED %s (%s): broker unavailable at submit",
                       position.get("rec_id"), position.get("ticker"))
        _log_event({"ts": datetime.now().isoformat(timespec="seconds"),
                    "event": "broker_unavailable_at_submit", "intent": "close",
                    "rec_id": position.get("rec_id"),
                    "ticker": position.get("ticker"), "reason": reason,
                    "stamp": "UNVALIDATED — PAPER ONLY"})
        return None
    quantity = max(1, int(position.get("quantity", 1)))
    intent = {
        "intent": "close",
        "intent_seq": datetime.now().isoformat(timespec="microseconds"),
        "order_id": None,
        "rec_id": position["rec_id"],
        "ticker": position["ticker"],
        "quantity": quantity,
        "structure": position["structure"],
        "entry_price": position["entry_price"],
        "close_limit": round(float(limit_price), 4),  # decision-time mark; trustworthy
                                                       # fallback if the broker fill is garbage
        "close_reason": reason,
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
    }
    pending = _load(PENDING_FILE)
    pending.append(intent)
    _save(PENDING_FILE, pending)

    legs = [{"option_symbol": leg["symbol"], "side": _closing_action(leg["action"]),
             "quantity": quantity} for leg in position["legs"]]

    def _post():
        if len(legs) == 1:
            return client.place_option_order(position["ticker"], legs[0]["option_symbol"],
                                             legs[0]["side"], quantity, max(0.01, limit_price))
        return client.place_spread_order(position["ticker"], legs, max(0.01, limit_price),
                                         order_type="credit")

    def _mark_closing(closing: bool) -> None:
        positions = load_positions()
        for p in positions:
            if p["rec_id"] == position["rec_id"]:
                p["closing"] = closing
                if closing:
                    p["close_reason"] = reason
        _save(POSITIONS_FILE, positions)

    def _ambiguous_close(result) -> dict:
        # R1: the close POST got no broker response — the order MAY be live.
        # NEVER blind-resubmit a close (that is the double-close path). Keep the
        # intent UNBOUND for resolve_intents, and defensively mark the position
        # closing so the exit manager does not re-fire another close inside the
        # resolve window (if the order did land, resolve_intents binds it; if it
        # did not, resolve_intents re-arms the position for a fresh close).
        _mark_closing(True)
        logger.warning("paper close AMBIGUOUS for %s (%s) — intent left unbound, "
                       "NOT resubmitting (resolve_intents will adjudicate)",
                       position["ticker"], (result or {}).get("error"))
        return {"submitted": False, "ambiguous": True, "response": result}

    # fast retry (2026-07-14): one in-process retry on a transient submission
    # failure (the 12:50 broker-500 on 07-13 cost a full 5-min cycle of the
    # 15-min flatten runway). Bounded: one retry, 3s apart. R1: a retry is only
    # safe on a DEFINITIVE reject (no live order); an AMBIGUOUS result must NOT
    # be retried — the first POST may already be live at the broker.
    result = _post()
    outcome = classify_post_result(result)
    if outcome["status"] == AMBIGUOUS:
        return _ambiguous_close(result)
    if outcome["status"] == DEFINITIVE_REJECT:
        logger.warning("paper close submit failed for %s (%s) — retrying in 3s",
                       position["ticker"], result)
        import time as _time
        _time.sleep(3)
        result = _post()
        outcome = classify_post_result(result)
        if outcome["status"] == AMBIGUOUS:
            return _ambiguous_close(result)
        if outcome["status"] == DEFINITIVE_REJECT:
            _drop_intent(intent["intent_seq"])
            logger.warning("paper close rejected for %s: %s", position["ticker"], result)
            return None

    order = result.get("order", {})
    _bind_intent(intent["intent_seq"], order.get("id"))
    # mark position as closing so the exit manager doesn't re-trigger
    _mark_closing(True)
    logger.info("paper close %s submitted for %s (%s)", order.get("id"), position["ticker"], reason)
    return {"submitted": True, "order_id": order.get("id")}


def cancel_stale_entries(timeout_min: int, mode_timeouts: dict | None = None) -> list[dict]:
    """Cancel unfilled ENTRY orders older than the signal-expiry window.

    mode_timeouts maps fill_mode -> its own (shorter) timeout, so marketable_day
    can be cancelled at ~5 min while v1's resting orders keep the 30-min
    default. Unknown modes fall back to timeout_min."""
    client = get_order_client()
    if not client.available():
        return []
    pending = _load(PENDING_FILE)
    canceled, keep = [], []
    now = datetime.now()
    for p in pending:
        age_min = (now - datetime.fromisoformat(p["submitted_at"])).total_seconds() / 60
        _to = (mode_timeouts or {}).get(p.get("fill_mode"), timeout_min)
        # C-10 (2026-09-25, operator-approved): "cancel after N minutes" means the
        # first cycle check AT OR AFTER N minutes. This sweep runs once per 5-min
        # cycle; with a strict `age > N` the first check landed seconds short of
        # N and the order rested a whole extra cycle (TSLA 09-25: declared 5,
        # cancelled at 10). Half a minute of tolerance makes the declared rule
        # the observed rule; v1's 30-min timeout is unaffected in practice.
        if p["intent"] == "open" and age_min >= _to - 0.5:
            client.cancel_order(p["order_id"])
            # R5: a fill can land inside the cancel window. Re-GET the order and
            # branch on what actually happened — if it filled (or partially
            # filled) the cancel lost the race, so KEEP the pending record and
            # let poll_fills book the position. Dropping it here would silently
            # lose a real fill (phantom missing position + orphan broker legs).
            recheck = client.get_order(p["order_id"])
            rstatus = (recheck or {}).get("status")
            if rstatus in ("filled", "partially_filled"):
                logger.warning("entry %s for %s FILLED during cancel window (%s) — "
                               "keeping for poll_fills, not expiring",
                               p["order_id"], p["ticker"], rstatus)
                keep.append(p)
                continue
            # fill_mode/strategy/direction are stamped (2026-09-16) because a
            # cancelled MARKETABLE entry is a completed ATTEMPT that must reach
            # the attempt cap, the fill-rate denominator and the churn guard.
            # Legacy v1 rows carry no fill_mode and are counted by none of them.
            event = {"ts": now.isoformat(timespec="seconds"), "event": "entry_expired",
                     "exit_reason": "entry_expired_canceled",
                     "rec_id": p["rec_id"], "ticker": p["ticker"], "order_id": p["order_id"],
                     "strategy": p.get("strategy"), "direction": p.get("direction"),
                     "fill_mode": p.get("fill_mode"),
                     "age_min": round(age_min), "stamp": "UNVALIDATED — PAPER ONLY"}
            _log_event(event)
            canceled.append(event)
            logger.info("entry %s for %s canceled (signal expired, %dm)", p["order_id"], p["ticker"], age_min)
        else:
            keep.append(p)
    if canceled:
        _save(PENDING_FILE, keep)
    return canceled


ESCALATE_AFTER_S = 300  # one cycle unfilled -> chase the fill
FINAL_CHASE_ET = (15, 55)  # after this, cross the spread hard


def escalate_stale_close(position: dict, quotes: dict, now_et) -> dict | None:
    """Weekend fix #4 (found live 6/12): a submitted close is not a closed
    position. If a close order sits unfilled past one cycle, cancel and
    re-price at the marketable level (sell longs at bid / buy shorts at ask);
    after 15:55 ET, cross harder. 'Flat' is the mandate, not 'submitted'."""
    from datetime import datetime
    pending = _load(PENDING_FILE)
    entry = next((o for o in pending if o.get("intent") == "close"
                  and o.get("rec_id") == position["rec_id"]), None)
    if entry is None:
        return None
    age = (datetime.now() - datetime.fromisoformat(entry["submitted_at"])).total_seconds()
    if age < ESCALATE_AFTER_S:
        return None
    client = get_order_client()
    if not client.available():
        return None
    order = client.get_order(entry["order_id"])
    if (order or {}).get("status") not in ("open", "pending", "partially_filled"):
        return None  # filled or dead — normal polling handles it
    credit = 0.0
    for leg in position["legs"]:
        q = quotes.get(leg["symbol"]) or {}
        if q.get("bid") is None or q.get("ask") is None:
            return None  # no quotes — can't price an escalation safely
        credit += q["bid"] if leg["action"] == "buy_to_open" else -q["ask"]
    buffer = 0.10 if (now_et.hour, now_et.minute) >= FINAL_CHASE_ET else 0.05
    aggressive = max(0.05, round(credit - buffer, 2))
    client.cancel_order(entry["order_id"])
    # R5: a close fill can land inside the cancel window. Re-GET after the cancel
    # and branch on whether it actually filled — if it did, DO NOT re-arm and DO
    # NOT resubmit (that is a double-close). Leave the pending close record in
    # place so poll_fills books the exit normally.
    recheck = client.get_order(entry["order_id"])
    # ITEM 2 (2026-08-05): verify a fill by EXECUTED QUANTITY, never by a status
    # word. On 08-05 this branch fired 13 consecutive times on GOOGL while the
    # position stayed open: reqCompletedOrders returned records with
    # status="Filled" but filled=0.0, and the old test trusted the word. A
    # status of Cancelled with filled=0.0 is NOT a fill.
    _rs = (recheck or {}).get("status")
    _fq = (recheck or {}).get("filled_quantity")
    _requested = int(entry.get("quantity") or position.get("quantity") or 0)
    # C-3 (2026-08-28): "filled" means filled THE ORDER, not filled ANYTHING.
    # The old test (`filled_quantity > 0`) let a 1-of-7 partial claim a full
    # fill; the cancel above had killed the remainder, this branch stopped
    # managing, and the position ran unmanaged 23h through the 15:45 flatten
    # (IWM 08-27, then again 5-of-6 on 08-28). Full = executed >= requested.
    _executed = int(float(_fq)) if _fq is not None else None
    _really_filled = (
        _rs in ("filled", "partially_filled")
        and (
            (_executed is None and _rs == "filled")  # Tradier reports no qty
            or (_executed is not None and _executed > 0
                and (_requested <= 0 or _executed >= _requested))
        )
    )
    _partially_filled = (
        _rs in ("filled", "partially_filled")
        and _executed is not None and 0 < _executed < _requested
    )
    if _rs in ("filled", "partially_filled") and _executed == 0:
        logger.error("stale close for %s reported %s but filled_quantity=%s — "
                     "NOT treating as a fill (phantom completed-order record)",
                     position["ticker"], _rs, _fq)
    if _really_filled:
        _bump_exit_escalations(position["rec_id"])  # C-3(b): count every R5 pass
        logger.warning("stale close for %s FILLED during cancel window — not "
                       "resubmitting (poll_fills will book the exit)", position["ticker"])
        return {"event": "exit_fill_during_cancel", "ticker": position["ticker"],
                "rec_id": position["rec_id"], "old_order": entry["order_id"],
                "filled_quantity": _fq, "submitted": False}
    if _partially_filled:
        # C-3(a): the cancel above killed the UNFILLED remainder — the order is
        # dead. Converge in one pass: book the executed portion, drop the dead
        # pending record, re-arm the remainder so the exit manager submits a
        # fresh close next cycle. C-3(b): count the pass so a repeating partial
        # wedge trips the loop breaker instead of looping silently.
        _n = _bump_exit_escalations(position["rec_id"])
        logger.warning("stale close for %s PARTIALLY FILLED %s/%s during cancel "
                       "window — booking the executed portion and re-arming the "
                       "remainder (escalation %d)",
                       position["ticker"], _executed, _requested, _n)
        booked_event = None
        try:
            _pos = next((q for q in load_positions()
                         if q["rec_id"] == position["rec_id"]), None)
            if _pos is not None:
                _inc = _close_booked_increment(entry, _pos, recheck)
                if _inc > 0:
                    booked_event = _book_close_fill(entry, _pos, recheck, _inc)
        except Exception as exc:  # noqa: BLE001 — poll_fills is the backstop
            logger.error("partial-close booking failed for %s (%s) — poll_fills "
                         "will book it", position["ticker"], exc)
        _save(PENDING_FILE, [o for o in _load(PENDING_FILE)
                             if o.get("order_id") != entry["order_id"]])
        positions = load_positions()
        for q in positions:
            if q["rec_id"] == position["rec_id"]:
                q["closing"] = False
        _save(POSITIONS_FILE, positions)
        if _n > MAX_EXIT_ESCALATIONS:
            logger.critical("EXIT LOOP BREAKER: %s (%s) hit %d escalations via "
                            "repeated partial closes — alarming (remainder stays "
                            "re-armed).", position["ticker"], position["rec_id"], _n)
            try:
                from src.engine.notify import send
                send("EXIT LOOP BREAKER — repeated partial closes",
                     f"{position['ticker']} ({position['rec_id']}) partial-closed "
                     f"{_n} times without completing. Remainder re-armed; manual "
                     "review advised.\nUNVALIDATED — PAPER ONLY",
                     priority="urgent", tags="rotating_light")
            except Exception:  # noqa: BLE001
                pass
        return {"event": "exit_partial_during_cancel", "ticker": position["ticker"],
                "rec_id": position["rec_id"], "old_order": entry["order_id"],
                "filled_quantity": _executed, "requested_quantity": _requested,
                "booked": bool(booked_event), "submitted": False}

    # LOOP BREAKER (item 2): no infinite loops in the money path, ever. Count
    # escalations per position; at MAX_EXIT_ESCALATIONS, alarm and STOP retrying
    # this position so a malfunctioning exit path cannot spin all session.
    _n = _bump_exit_escalations(position["rec_id"])
    if _n > MAX_EXIT_ESCALATIONS:
        logger.critical("EXIT LOOP BREAKER: %s (%s) escalated %d times without "
                        "closing — halting retries for this position and "
                        "alarming. Manual review required.",
                        position["ticker"], position["rec_id"], _n - 1)
        try:
            from src.engine.notify import send
            send("EXIT LOOP BREAKER — retries halted",
                 f"{position['ticker']} ({position['rec_id']}) failed to close "
                 f"after {_n - 1} escalations. Retries STOPPED; position may "
                 "still be open at the broker.\nUNVALIDATED — PAPER ONLY",
                 priority="urgent", tags="rotating_light")
        except Exception:  # noqa: BLE001
            pass
        return {"event": "exit_loop_breaker", "ticker": position["ticker"],
                "rec_id": position["rec_id"], "escalations": _n - 1,
                "submitted": False, "halted": True}
    # Crash-window fix (2026-07-01): re-arm the position BEFORE dropping the old
    # pending record, and only then resubmit. A crash at any point now leaves a
    # recoverable state (worst case: exit manager re-triggers the close next
    # cycle) instead of the old limbo — closing=True with no pending order,
    # which neither the manage list nor recovery ever picked up again.
    positions = load_positions()
    for q in positions:
        if q["rec_id"] == position["rec_id"]:
            q["closing"] = False
    _save(POSITIONS_FILE, positions)
    _save(PENDING_FILE, [o for o in _load(PENDING_FILE)
                         if o.get("order_id") != entry["order_id"]])
    logger.warning("escalating stale close for %s: order %s unfilled %.0fs -> re-priced at %.2f",
                   position["ticker"], entry["order_id"], age, aggressive)
    result = submit_close_order(position, aggressive,
                                (entry.get("close_reason") or "") + " [escalated]")
    return {"event": "exit_escalated", "ticker": position["ticker"],
            "rec_id": position["rec_id"], "old_order": entry["order_id"],
            "new_limit": aggressive, "unfilled_s": round(age),
            "submitted": bool(result)}


def update_quote_streak(rec_id: str, missing: bool) -> int:
    """Track consecutive cycles a position was unpriceable (no leg quotes).
    While quotes are missing the P&L stop_loss/profit_target CANNOT be
    evaluated — that must be loud, never silent. Returns the new streak."""
    positions = load_positions()
    streak = 0
    for p in positions:
        if p["rec_id"] == rec_id:
            streak = 0 if not missing else int(p.get("quote_miss_streak", 0)) + 1
            p["quote_miss_streak"] = streak
    _save(POSITIONS_FILE, positions)
    return streak


def update_position_marks(rec_id: str, value: float):
    """Persist the running high/low mark for MFE/MAE (data-gap fix #4) plus
    the LAST successfully priced value — the anchor for the quote-outage
    protective flatten. Called by the exit manager each cycle with the
    position's current close-out value — pure recording, no exit decisions."""
    positions = load_positions()
    changed = False
    for p in positions:
        if p["rec_id"] == rec_id:
            p["mark_high"] = max(p.get("mark_high", value), value)
            p["mark_low"] = min(p.get("mark_low", value), value)
            # ordered intra-trade path (lesson L13): mark_high/mark_low give the
            # extremes but not their sequence, which a trailing-stop exit needs
            # to be validated. Record (t, value) each cycle; a session is <=78
            # cycles so this stays tiny. Pure recording, no exit decision.
            now_iso = datetime.now().isoformat(timespec="seconds")
            p.setdefault("mark_series", []).append([now_iso, value])
            p["last_value"] = value
            p["last_value_at"] = now_iso
            changed = True
    if changed:
        _save(POSITIONS_FILE, positions)


INTENT_RESOLVE_AGE_S = 120  # an unbound intent younger than this may still be mid-POST


def resolve_intents(client: "PaperOrderClient | None" = None) -> list[dict]:
    """Crash recovery for write-ahead intents. A pending record with
    order_id=None means the process died between writing the intent and
    binding the broker order id. Match it to a live broker order by agent
    tag + leg symbols; if none matches, drop the intent (re-arming the
    position for close intents) and log the loss. Never raises."""
    from src.data.tradier_orders import ORDER_TAG
    pending = _load(PENDING_FILE)
    now = datetime.now()
    unbound = [o for o in pending if o.get("order_id") is None and
               (now - datetime.fromisoformat(o["submitted_at"])).total_seconds() > INTENT_RESOLVE_AGE_S]
    if not unbound:
        return []
    client = client or get_order_client()
    if not client.available():
        return []
    try:
        broker = client.get_orders()
    except Exception as exc:
        logger.warning("resolve_intents: broker order list unavailable (%s)", exc)
        return []
    # R2: `None` means the broker view FAILED (transport/broker error) — it is
    # NOT "broker says no orders". Dropping unbound intents here on a flaky
    # broker (the crash-restart case) is precisely the silent-no-op R2 guards
    # against. Abort and retry next cycle; NEVER drop an intent on an error.
    if broker is None:
        logger.warning("resolve_intents: broker order view unavailable (error, not "
                       "empty) — aborting, %d unbound intent(s) preserved for retry",
                       len(unbound))
        return []
    bound_ids = {o.get("order_id") for o in pending if o.get("order_id")}
    positions = load_positions()
    events = []
    for intent in unbound:
        # R5: match on symbols + SIDES (not symbols alone). A close intent shares
        # the entry's leg SYMBOLS, so symbol-only matching could bind it to the
        # already-filled ENTRY order -> phantom exit + orphan live close. The
        # SIDES disambiguate (entry = *_to_open, close = *_to_close).
        if intent["intent"] == "open":
            want_pairs = {(leg["symbol"], leg.get("action"))
                          for leg in intent.get("legs", [])}
        else:  # close intents carry no legs — take them from the position and
               # map each entry action to its closing side
            pos = next((q for q in positions if q["rec_id"] == intent["rec_id"]), None)
            want_pairs = {(leg["symbol"], _closing_action(leg["action"]))
                          for leg in (pos or {}).get("legs", [])}
        match = None
        for b in broker:
            if b.get("id") in bound_ids or str(b.get("tag") or "") != ORDER_TAG:
                continue
            # R5: only a LIVE-or-FILLED order can be the one we lost the id for;
            # a dead (canceled/expired/rejected) order must never be adopted.
            if b.get("status") not in ("open", "pending", "partially_filled", "filled"):
                continue
            legs = b.get("leg", [])
            legs = [legs] if isinstance(legs, dict) else legs
            have_pairs = ({(l.get("option_symbol") or l.get("symbol"), l.get("side"))
                           for l in legs} if legs
                          else {(b.get("option_symbol") or b.get("symbol"), b.get("side"))})
            if not want_pairs:
                continue
            if {s for s, _ in have_pairs} != {s for s, _ in want_pairs}:
                continue  # different leg set
            # R5: when the broker reports leg SIDES they MUST agree — this is what
            # stops a close intent from binding to the already-filled ENTRY (same
            # symbols, opposite sides). If the broker omits sides entirely
            # (degraded response), fall back to the symbol match above.
            broker_sides = {sd for _, sd in have_pairs if sd is not None}
            if broker_sides and have_pairs != want_pairs:
                continue
            match = b
            break
        ts = now.isoformat(timespec="seconds")
        if match:
            intent["order_id"] = match.get("id")
            bound_ids.add(match.get("id"))
            events.append({"ts": ts, "event": "intent_recovered", "rec_id": intent["rec_id"],
                           "order_id": match.get("id"), "stamp": "UNVALIDATED — PAPER ONLY"})
            logger.warning("resolve_intents: bound crashed intent %s -> broker order %s",
                           intent["rec_id"], match.get("id"))
        else:
            pending = [o for o in pending if o.get("intent_seq") != intent.get("intent_seq")]
            if intent["intent"] == "close":
                for q in positions:
                    if q["rec_id"] == intent["rec_id"]:
                        q["closing"] = False  # re-arm: exit manager retries next cycle
                _save(POSITIONS_FILE, positions)
            events.append({"ts": ts, "event": "intent_lost", "rec_id": intent["rec_id"],
                           "intent": intent["intent"], "stamp": "UNVALIDATED — PAPER ONLY"})
            logger.warning("resolve_intents: dropped unmatched %s intent for %s",
                           intent["intent"], intent["rec_id"])
    _save(PENDING_FILE, pending)
    for e in events:
        _log_event(e)
    return events


_BROKER_DOWN_CYCLES = 0


def ensure_broker(cfg: dict | None = None, client=None) -> dict | None:
    """C-12.2 (2026-09-28, operator "go"): once per cycle, if the broker client is
    DISCONNECTED, try to reconnect; on success reconcile BEFORE anything trades.

    Returns an event dict when the state changed (reconnected / first failure),
    else None. Never raises.
      * flag risk.broker_reconnect_in_session false      -> no-op
      * client has no reconnect (sandbox / test stubs)    -> no-op
      * state 'unresponsive' (wedged worker)              -> no-op: a reconnect
        would queue behind the stuck call; the 15 s timeouts already degrade it
      * pending intents do NOT block a reconnect: refusing to reconnect because
        work is outstanding is the 09-12 restart deadlock again (positions could
        not close because the broker was dead, and the broker stayed dead because
        positions were open). resolve_intents binds or drops them afterwards.
    """
    global _BROKER_DOWN_CYCLES
    rcfg = ((cfg or {}).get("risk") or {}) if cfg else {}
    if not rcfg.get("broker_reconnect_in_session", False):
        return None
    try:
        client = client or get_order_client()
        if not hasattr(client, "reconnect") or not hasattr(client, "connection_state"):
            return None
        state = client.connection_state()
        if state == "connected":
            _BROKER_DOWN_CYCLES = 0
            return None
        if state != "disconnected":
            return None
        _BROKER_DOWN_CYCLES += 1
        now = datetime.now().isoformat(timespec="seconds")
        if client.reconnect():
            down = _BROKER_DOWN_CYCLES; _BROKER_DOWN_CYCLES = 0
            ev = {"ts": now, "event": "broker_reconnected", "down_cycles": down,
                  "stamp": "UNVALIDATED — PAPER ONLY"}
            try:
                ev["reconcile"] = [e.get("event") for e in (reconcile_with_broker() or [])]
            except Exception as exc:  # noqa: BLE001
                ev["reconcile_error"] = f"{type(exc).__name__}: {exc}"
            _log_event(ev)
            logger.warning("broker RECONNECTED in session after %d cycle(s) down", down)
            _notify_drift("Broker reconnected in session", "default",
                          f"The engine re-established its IBKR connection after {down} cycle(s) down "
                          f"and reconciled the book. Trading resumes this cycle.")
            return ev
        if _BROKER_DOWN_CYCLES == 1:
            ev = {"ts": now, "event": "broker_down_in_session", "stamp": "UNVALIDATED — PAPER ONLY"}
            _log_event(ev)
            logger.warning("broker DISCONNECTED in session — reconnect attempt failed; retrying each cycle")
            _notify_drift("BROKER DOWN in session — log in to IB Gateway", "urgent",
                          "The engine has no IBKR connection and a reconnect attempt failed. It retries every "
                          "cycle. If IB Gateway is at its login screen, log in (paper).")
            return ev
    except Exception as exc:  # noqa: BLE001 — never kill the cycle
        logger.warning("ensure_broker failed: %s: %s", type(exc).__name__, exc)
    return None


DRIFT_PERSIST_CYCLES = 2      # act only when the same classification holds this many cycles


def apply_drift_in_loop(cfg: dict | None = None, client=None) -> list[dict]:
    """C-9.1 (2026-09-25, operator-approved): reconcile the book with the broker
    INSIDE the cycle and act on drift that persists.

    Before this, reconcile_drift() existed but nothing in the loop called it.
    On 09-23 the book carried 3 positions the broker no longer held and an AMZN
    quantity of 1 against 3 at the broker; the exit manager kept trying to
    close what did not exist and would have under-closed what did.

    Guards (each one is a way this could hurt on Monday morning):
      * broker view None (read failed)        -> do nothing; unknown is not empty
      * flag risk.reconcile_in_loop false      -> detect-only, as before
      * position has ANY working intent        -> skip (partial-fill / close race:
        C-3 books the increment; adopting a reduced qty first would double-book)
      * position marked closing                -> skip
      * a leg shared by two rec_ids            -> skip (ambiguous)
      * PERSISTENCE: act only on the 2nd consecutive cycle with the same class.
        A freshly (re)activated client can report [] for one cycle before the
        stream catches up; acting on first sight would flag every real position.
    Per position: all legs gone -> broker_lost (flag, closes suppressed, page);
    all legs present with the same |broker qty| != book -> adopt broker qty;
    legs disagree -> LEGGED: page urgent, touch nothing (a human decides).
    """
    rcfg = ((cfg or {}).get("risk") or {}) if cfg else {}
    if not rcfg.get("reconcile_in_loop", False):
        return []
    from src.engine.reconcile_drift import reconcile_drift
    client = client or get_order_client()
    if not client.available():
        return []
    broker = client.get_positions()
    if broker is None:                      # R2: read FAILED != empty
        return []
    positions = load_positions()
    if not positions:
        return []
    pending = _load(PENDING_FILE)
    busy = {o.get("rec_id") for o in pending}
    res = reconcile_drift(positions, broker, adopt_foreign=False)
    by_sym = {d["symbol"]: d for d in res["drift"]}
    # a symbol claimed by two positions cannot be attributed — leave both alone
    multi = {d["symbol"] for d in res["drift"] if len((d.get("detail") or {}).get("rec_ids") or []) > 1}
    events, changed = [], False
    now = datetime.now().isoformat(timespec="seconds")
    for p in positions:
        rid = p["rec_id"]
        syms = [l.get("symbol") for l in (p.get("legs") or []) if l.get("symbol")]
        if not syms or rid in busy or p.get("closing") or p.get("broker_lost") or any(x in multi for x in syms):
            continue
        classes = [by_sym[x]["class"] if x in by_sym else "in_sync" for x in syms]
        if all(c == "in_sync" for c in classes):
            if p.get("drift_class"):
                p.pop("drift_class", None); p.pop("drift_streak", None); changed = True
            continue
        if all(c == "broker_lost" for c in classes):
            cls, target_qty = "broker_lost", None
        elif all(c == "quantity_mismatch" for c in classes):
            qs = {abs(by_sym[x]["broker_qty"]) for x in syms}
            cls, target_qty = ("quantity_mismatch", qs.pop()) if len(qs) == 1 else ("legged", None)
        else:
            cls, target_qty = "legged", None
        streak = int(p.get("drift_streak", 0)) + 1 if p.get("drift_class") == cls else 1
        p["drift_class"], p["drift_streak"] = cls, streak; changed = True
        if streak < DRIFT_PERSIST_CYCLES:
            continue
        ev = {"ts": now, "ticker": p.get("ticker"), "rec_id": rid, "legs": syms,
              "book_quantity": p.get("quantity"), "stamp": "UNVALIDATED — PAPER ONLY"}
        if cls == "broker_lost":
            p["broker_lost"] = True; p["closing"] = False
            ev.update(event="position_broker_lost",
                      reason="broker holds none of this position's legs on 2 consecutive cycles; closes suppressed")
            _notify_drift("Position gone at the broker — closes stopped", "high",
                          f"{p.get('ticker')} {rid}: the book still shows qty {p.get('quantity')}, IBKR holds none. "
                          f"Flagged broker_lost; the exit manager will not try to close it. Reconcile/archive it.")
        elif cls == "quantity_mismatch":
            old = p.get("quantity"); p["quantity"] = int(target_qty)
            ev.update(event="position_quantity_adopted", old_quantity=old, new_quantity=int(target_qty),
                      reason="every leg present at the broker at the same quantity, != book, 2 consecutive cycles")
            _notify_drift("Position quantity adopted from the broker", "default",
                          f"{p.get('ticker')} {rid}: book {old} -> broker {int(target_qty)} (all legs agree).")
        else:
            if p.get("legged_alerted"):
                continue
            p["legged_alerted"] = True
            ev.update(event="position_legged_alarm",
                      reason="legs disagree at the broker (partial/assignment/manual) — nothing changed; needs a human",
                      legs_detail={x: (by_sym[x]["engine_qty"], by_sym[x]["broker_qty"]) for x in syms if x in by_sym})
            _notify_drift("LEGGED POSITION — needs you", "urgent",
                          f"{p.get('ticker')} {rid}: the broker holds a different mix of legs than the book. "
                          f"Nothing was changed. Details in fills.jsonl (position_legged_alarm).")
        _log_event(ev); events.append(ev)
        logger.warning("reconcile-in-loop: %s %s -> %s", p.get("ticker"), rid, ev["event"])
    if changed:
        _save(POSITIONS_FILE, positions)
    return events


def _notify_drift(title: str, priority: str, body: str) -> None:
    try:
        from src.engine.notify import send
        send(title, body + "\nUNVALIDATED — PAPER ONLY", priority=priority, tags="warning")
    except Exception:  # noqa: BLE001 — a page must never break the cycle
        pass


def audit_book_vs_broker_checked(client=None) -> dict:
    """audit_book_vs_broker() with the OUTCOME distinguished from the RESULT.

    THE DEFECT THIS EXISTS FOR (flagged 2026-08-10, bit on 2026-08-17):
    audit_book_vs_broker() returns [] both when the book is CLEAN and when the
    broker could not be reached. That is right for the engine, which is
    detect-only and must not act on uncertainty — but the standing runbook uses
    it as a GATE, where [] reads as "pass". On 2026-08-17 the pre-start gate
    passed vacuously while the broker was unreachable AND a phantom WMT
    position sat in the book; only the post-start gate caught it.

    Returns {"status": "clean" | "alarms" | "could_not_check", "alarms": [...]}.
    A gate must treat could_not_check as a FAILURE: not knowing whether the
    book is stale is not the same as knowing it is fine.
    """
    client = client or get_order_client()
    if not client.available():
        return {"status": "could_not_check", "alarms": [],
                "detail": "broker client unavailable — book state UNKNOWN"}
    if client.get_positions() is None:
        return {"status": "could_not_check", "alarms": [],
                "detail": "broker position read FAILED (not an empty account)"}
    alarms = audit_book_vs_broker(client)
    return {"status": "alarms" if alarms else "clean", "alarms": alarms}


def audit_book_vs_broker(client=None) -> list[dict]:
    """ITEM 4 — DETECT-ONLY reconciliation alarms. Never closes anything.

    Three mismatches, all of which occurred on 2026-08-05:
      (a) broker position with NO book entry   -> unmanaged live position
      (b) book position with NO broker position -> phantom the engine will try
          to exit forever (the GOOGL exit loop)
      (c) QUANTITY mismatch                     -> the qty-5 bug

    Returns a list of alarm dicts (empty when clean) and logs each one. It is
    deliberately observational: auto-closing on a mismatch would act on exactly
    the uncertain state that produced the incident.
    """
    client = client or get_order_client()
    if not client.available():
        return []
    broker = client.get_positions()
    if broker is None:                      # R2: read FAILED != empty
        logger.warning("book/broker audit skipped — broker position view "
                       "unavailable (error, not empty)")
        return []
    # broker symbols -> signed qty
    bq: dict[str, float] = {}
    for p in broker:
        bq[p.get("symbol")] = bq.get(p.get("symbol"), 0) + float(p.get("quantity") or 0)
    # book legs -> expected qty per option symbol
    kq: dict[str, float] = {}
    for pos in load_positions():
        q = float(pos.get("quantity") or 0)
        for leg in (pos.get("legs") or []):
            sign = 1 if str(leg.get("action", "")).startswith("buy") else -1
            sym = leg.get("symbol")
            kq[sym] = kq.get(sym, 0) + sign * q
    # WHY THIS ALARMS REGARDLESS OF adopt_foreign (decided 2026-08-06):
    # The standing rule is that the human's manual positions must never be
    # touched or flagged. That rule is already honoured STRUCTURALLY here —
    # get_positions() returns OPTION positions only, while the manual holdings
    # (HON/GOOGL/DELL/PLTR) are STOCK and the trend book's are ETF/futures, so
    # none of them can appear in this comparison at all. An unexplained OPTION
    # position is therefore almost certainly OURS and lost from the book — the
    # exact 08-05 unmanaged-orphan state, which must never be suppressed.
    # This is also DETECT-ONLY: it logs, it never touches anything.
    alarms = []
    now = datetime.now().isoformat(timespec="seconds")
    for sym in sorted(set(bq) | set(kq)):
        b, k = bq.get(sym, 0.0), kq.get(sym, 0.0)
        if b == k:
            continue
        kind = ("broker_position_not_in_book" if k == 0 else
                "book_position_not_at_broker" if b == 0 else
                "quantity_mismatch")

        alarm = {"ts": now, "event": "reconcile_alarm", "kind": kind,
                 "symbol": sym, "broker_quantity": b, "book_quantity": k,
                 "action": "DETECT ONLY — no auto-close",
                 "stamp": "UNVALIDATED — PAPER ONLY"}
        logger.critical("RECONCILE ALARM [%s] %s: broker=%s book=%s "
                        "(detect-only, no auto-close)", kind, sym, b, k)
        _log_event(alarm)
        alarms.append(alarm)
    return alarms


def reconcile_with_broker() -> list[dict]:
    """Startup reconciliation + an ALWAYS-RUN book/broker audit.

    ITEM 4 WIRING (2026-08-06): audit_book_vs_broker() existed but had ZERO
    callers — it only ran when a human invoked it by hand, which is how a stale
    book (7 phantom contracts the engine did not own) reached a live start. An
    uncalled detector is not detection.

    The audit runs AFTER reconciliation (so it reports genuine RESIDUAL
    divergence, not positions reconcile is about to legitimately adopt) and runs
    on EVERY exit path — _reconcile_inner has several early returns (client
    unavailable, a None order read, a None position read), and those are exactly
    the degraded conditions under which a divergence most needs surfacing.
    DETECT-ONLY: it never closes or cancels anything.
    """
    events = _reconcile_inner()
    try:
        events.extend(audit_book_vs_broker())
    except Exception as exc:  # noqa: BLE001 - an audit must never block startup
        logger.error("book/broker audit failed (startup continues): %s", exc)
    return events


def _reconcile_inner() -> list[dict]:
    """Startup safety net: compare the internal book against the sandbox.
    - broker OPEN orders we don't know -> cancel, log reconciled_orphan_order
    - broker POSITIONS we don't know  -> adopt into the book as
      reconciled_orphan so the exit manager governs them
    Runs before the first cycle; failures are logged, never block startup."""
    client = get_order_client()
    if not client.available():
        return []
    from src.data.tradier_orders import ORDER_TAG
    # config guard: adopt_foreign=false means a human shares this sandbox account
    # for manual trades. Reworked 2026-07-01: that flag now scopes only POSITION
    # adoption (broker positions carry no tags, so ownership is unknowable).
    # ORDERS are always reconciled TAG-SCOPED — an open order tagged ORDER_TAG is
    # provably ours (crash orphan) and gets cancelled regardless of the flag;
    # untagged orders are provably not ours and are always left alone.
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    adopt_foreign = cfg.get("agent", {}).get("adopt_foreign", True)
    events = resolve_intents(client)  # crash-recovery for write-ahead intents first
    now = datetime.now().isoformat(timespec="seconds")

    # R2: read the broker order view ONCE. `None` = the read FAILED (transport/
    # broker error), which is NOT "no orders". Reconciling against a failed read
    # would cancel/adopt on a phantom-empty broker — abort instead. Intents were
    # already preserved by resolve_intents (which likewise aborts on None).
    broker_orders = client.get_orders()
    if broker_orders is None:
        logger.warning("reconciliation: broker order view unavailable (error, not "
                       "empty) — skipping order reconciliation this run")
        return events
    known_orders = {p.get("order_id") for p in _load(PENDING_FILE)}
    for o in broker_orders:
        if o.get("status") not in ("open", "pending", "partially_filled") or o.get("id") in known_orders:
            continue
        ours = str(o.get("tag") or "") == ORDER_TAG
        if not ours:
            # CROSS-BOOK (2026-08-04): an order we cannot prove is ours is
            # someone else's — the trend book (shares this IBKR paper account),
            # a human's manual trade, another strategy. LOG AND IGNORE, NEVER
            # CANCEL, and do NOT let `adopt_foreign` override that: the previous
            # form cancelled foreign orders whenever adopt_foreign was true,
            # contradicting this function's own docstring. Cancelling another
            # book's live order is unrecoverable damage done on a guess.
            logger.info("reconciliation: IGNORING unattributable order %s (%s, "
                        "tag=%r) — not ours, never cancelled",
                        o.get("id"), o.get("symbol"), o.get("tag"))
            continue
        client.cancel_order(o["id"])
        event = {"ts": now, "event": "reconciled_orphan_order", "action": "canceled",
                 "order_id": o.get("id"), "symbol": o.get("symbol"),
                 "agent_tagged": ours,
                 "status_at_broker": o.get("status"), "stamp": "UNVALIDATED — PAPER ONLY"}
        _log_event(event)
        events.append(event)
        logger.warning("reconciliation: canceled unknown open order %s (%s, tagged=%s)",
                       o.get("id"), o.get("symbol"), ours)

    positions = load_positions()
    known_legs = {leg["symbol"] for p in positions for leg in p.get("legs", [])}
    # R2: read the broker position view ONCE; `None` = failed read (not empty).
    # Adopting/omitting positions off a failed read would either strand real
    # orphans or falsely report the book empty — abort position reconciliation.
    broker_positions = client.get_positions()
    if broker_positions is None:
        logger.warning("reconciliation: broker position view unavailable (error, not "
                       "empty) — skipping position reconciliation this run")
        return events
    unknown_syms = [bp.get("symbol", "") for bp in broker_positions
                    if bp.get("symbol") and bp.get("symbol") not in known_legs]
    if not adopt_foreign:
        if unknown_syms:  # visible, not silent — these may include our own crash orphans
            logger.warning("reconciliation: adopt_foreign=false — %d untracked broker position(s) "
                           "left ungoverned: %s", len(unknown_syms), ", ".join(sorted(unknown_syms)))
        return events
    orphans: dict[str, list[dict]] = {}
    for bp in broker_positions:
        sym = bp.get("symbol", "")
        if sym in known_legs or not sym:
            continue
        # OCC symbol = ROOT + YYMMDD + C/P + 8-digit strike (15-char suffix)
        underlying = sym[:-15] if len(sym) > 15 else sym
        orphans.setdefault(underlying, []).append(bp)
    for underlying, legs in orphans.items():
        qty_sign = lambda q: "buy_to_open" if float(q or 0) > 0 else "sell_to_open"  # noqa: E731
        net_cost = round(sum(float(l.get("cost_basis") or 0) for l in legs) / 100, 4)
        position = {
            "rec_id": f"orphan-{datetime.now():%Y%m%d%H%M%S}-{underlying}",
            "ticker": underlying, "structure": "reconciled_orphan",
            "strategy": None, "direction": None,
            "legs": [{"symbol": l["symbol"], "action": qty_sign(l.get("quantity"))} for l in legs],
            # ITEM 6 applied to ADOPTION (2026-08-06): an adopted orphan must
            # carry the BROKER's real contract count. It previously had NO
            # quantity at all, so every consumer read it as 0 — risk sizing, the
            # exit path, and the book/broker audit (which then alarmed on every
            # adopted position, i.e. cried wolf). Contract multiple = the largest
            # absolute leg quantity (a 1-lot vertical is legs +1/-1 -> 1).
            "quantity": int(max((abs(float(l.get("quantity") or 0)) for l in legs),
                                default=1)) or 1,
            "entry_price": net_cost,
            "max_profit": None, "max_loss": net_cost,
            "expiration": None,
            "opened_at": now, "closing": False, "orphan": True,
        }
        positions.append(position)
        event = {"ts": now, "event": "reconciled_orphan", "action": "adopted",
                 "ticker": underlying, "legs": position["legs"],
                 "entry_price": net_cost, "stamp": "UNVALIDATED — PAPER ONLY"}
        _log_event(event)
        events.append(event)
        logger.warning("reconciliation: adopted orphan position %s (%d legs)", underlying, len(legs))
    if orphans:
        _save(POSITIONS_FILE, positions)
    return events


_SPY_TREND_CACHE: dict = {}  # {date_str: "up"|"down"|None} — one parquet read per session


def _spy_trend_vs_50dma() -> str | None:
    """SPY last daily close vs its 50-session MA from data/historical/SPY.parquet
    — cached per calendar day (module-level; the trader process is long-lived,
    so this avoids re-reading the parquet on every paper_fill this session).
    Item 1 of the 2026-07-19 observation-instrumentation batch (see
    docs/external_edge_map.md) — descriptive only, not a gate."""
    today_key = datetime.now().strftime("%Y-%m-%d")
    if today_key in _SPY_TREND_CACHE:
        return _SPY_TREND_CACHE[today_key]
    trend = None
    try:
        import pandas as pd
        closes = pd.read_parquet(ROOT / "data" / "historical" / "SPY.parquet")["Close"]
        if len(closes) >= 50:
            ma50 = float(closes.rolling(50).mean().iloc[-1])
            trend = "up" if float(closes.iloc[-1]) > ma50 else "down"
    except Exception:
        trend = None
    _SPY_TREND_CACHE.clear()  # keep exactly one entry (today) for the process lifetime
    _SPY_TREND_CACHE[today_key] = trend
    return trend


def _vix_bucket(vix: float | None) -> str | None:
    """Provisional descriptive bucket — NOT a gate/threshold consumed by any
    entry/exit/sizing logic (2026-07-19)."""
    if vix is None:
        return None
    if vix < 15:
        return "low"
    if vix <= 22:
        return "mid"
    return "high"


def regime_tags(regime: dict | None) -> dict:
    """Item 1 of the 2026-07-19 observation-instrumentation batch (see
    docs/external_edge_map.md, analysis/discovery/IDEAS.md F3). Purely
    descriptive context stamped on paper ENTRY (paper_fill) records for
    weekly-review slicing. All three buckets are provisional/descriptive —
    NOT gates, NOT consulted by any entry/exit/sizing/evidence logic."""
    breadth_daily = (regime or {}).get("breadth_daily") or {}
    healthy = breadth_daily.get("healthy")
    breadth_health = "healthy" if healthy is True else ("unhealthy" if healthy is False else "unknown")
    return {
        "vix_bucket": _vix_bucket((regime or {}).get("vix")),
        "spy_trend": _spy_trend_vs_50dma(),
        "breadth_health": breadth_health,
    }


def _mid_at_fill(legs: list[dict]) -> dict | None:
    """Contemporaneous net mid + total spread for a leg set — the
    execution-quality anchor (2026-07-06): fill price vs the mid at FILL
    time (not decision time) is the true 'did we capture mid' metric.
    Best-effort; None when quotes are unavailable."""
    try:
        from src.data.tradier import get_client
        quotes = get_client().quotes([l["symbol"] for l in legs]) or {}
        net_mid, spread_total = 0.0, 0.0
        for leg in legs:
            q = quotes.get(leg["symbol"]) or {}
            if q.get("bid") is None or q.get("ask") is None:
                return None
            mid = (q["bid"] + q["ask"]) / 2
            spread_total += q["ask"] - q["bid"]
            net_mid += mid if str(leg.get("action", "")).startswith("buy") else -mid
        return {"net_mid": round(net_mid, 4), "spread_total": round(spread_total, 4)}
    except Exception:
        return None


def _book_close_fill(p: dict, position: dict, order: dict, qty_to_book: int) -> dict:
    """C-3 (2026-08-28): book qty_to_book contracts of a CLOSE fill — full or
    partial — as a paper_exit. Single booking path for every close consumer
    (poll_fills filled/partial branches, escalate_stale_close R5) so a partial
    can never book differently from a full fill.

    Idempotency contract (R4): the caller derives qty_to_book as
    (executed - already_booked) where already_booked = requested - position
    quantity. The position quantity reduction here is what advances
    already_booked, and it is persisted BEFORE the pending record is rewritten,
    so a crash-replay recomputes a zero increment instead of double-booking.

    Prices from broker legs with the 06-25 sign/width guards. Removes the
    position when nothing remains (and resets the escalation counter);
    otherwise reduces quantity so the exit manager governs the remainder.
    Logs and returns the paper_exit event."""
    detail = order_fill_detail(order)
    exit_credit = round(-detail["net_debit"], 4)  # closing: credit received
    # Guard: a DEBIT structure (entry_price paid > 0) can NEVER be closed for a
    # negative credit (corrupt broker fill, Tradier sandbox 2026-06-25). Fall
    # back to the decision-time mark and flag the round trip.
    fill_guard = None
    if p["entry_price"] > 0 and exit_credit < 0:
        fallback = p.get("close_limit")
        fill_guard = (f"rejected broker exit_credit={exit_credit} "
                      f"(<0 for debit structure); fell back to mark {fallback}")
        logger.warning("paper exit guard [%s]: %s", p["ticker"], fill_guard)
        exit_credit = round(float(fallback), 4) if fallback is not None else 0.0
    # Companion invariant: a defined-risk debit structure can never be worth
    # MORE than its strike width either.
    width = strike_width(position.get("legs", []))
    if (fill_guard is None and p["entry_price"] > 0 and width is not None
            and exit_credit > width + WIDTH_TOLERANCE):
        fallback = p.get("close_limit")
        fill_guard = (f"rejected broker exit_credit={exit_credit} (> strike width "
                      f"{width}); fell back to mark {fallback}")
        logger.warning("paper exit guard [%s]: %s", p["ticker"], fill_guard)
        exit_credit = round(float(fallback), 4) if fallback is not None else width
    pnl = round((exit_credit - p["entry_price"]) * 100, 2)
    qty = int(qty_to_book)
    pnl_total = round(pnl * qty, 2)
    requested = int(p.get("quantity") or position.get("quantity") or qty)
    remaining = max(0, int(position.get("quantity") or qty) - qty)
    # Fold the actual exit into the running marks so the invariant
    # mfe >= exit_credit >= mae holds by construction.
    mfe = position.get("mark_high")
    mae = position.get("mark_low")
    mfe = exit_credit if mfe is None else max(mfe, exit_credit)
    mae = exit_credit if mae is None else min(mae, exit_credit)
    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": "paper_exit",
        "rec_id": p["rec_id"], "ticker": p["ticker"],
        "order_id": p["order_id"],
        "entry_price": p["entry_price"], "exit_price": exit_credit,
        "pnl_per_contract": pnl,
        "quantity": qty, "pnl_total": pnl_total,
        "fill_guard": fill_guard,
        "close_reason": p.get("close_reason"),
        "exit_reason": exit_reason_code(p.get("close_reason")),
        # C-3: partial-close bookkeeping, absent/False on ordinary full closes
        "partial_close": qty < requested,
        "requested_quantity": requested,
        "remaining_quantity": remaining,
        "round_trip": {
            "entry_order_id": position.get("entry_order_id"),
            "exit_order_id": p["order_id"],
            "entry_legs": position.get("entry_legs"),
            "exit_legs": detail["legs"],
            "entry_filled_at": position.get("entry_filled_at"),
            "exit_filled_at": detail["broker_closed"],
            "entry_net_debit": p["entry_price"],
            "exit_net_credit": exit_credit,
            "pnl_per_contract": pnl,
            "quantity": qty, "pnl_total": pnl_total,
            "mfe_value": mfe,
            "mae_value": mae,
            "mfe_mae_granularity": "5m_marks+entry+exit",
            # ordered path for trailing-stop validation (lesson L13)
            "mark_series": position.get("mark_series"),
        },
        "stamp": "UNVALIDATED — PAPER ONLY",
    }
    positions = load_positions()
    if remaining <= 0:
        positions = [q for q in positions if q["rec_id"] != p["rec_id"]]
        clear_exit_escalations(p["rec_id"])  # documented reset point, now wired
    else:
        for q in positions:
            if q["rec_id"] == p["rec_id"]:
                q["quantity"] = remaining
    _save(POSITIONS_FILE, positions)
    _log_event(event)
    if qty < requested:
        logger.warning("PARTIAL CLOSE BOOKED %s: %s of %s contracts exited "
                       "(pnl %+.2f); %s remain under management",
                       p["ticker"], qty, requested, pnl_total, remaining)
    return event


def _close_booked_increment(p: dict, position: dict, order: dict) -> int:
    """C-3: how many EXECUTED-but-UNBOOKED contracts a close order holds.
    already_booked derives from the position quantity (see _book_close_fill's
    idempotency contract); returns 0 when there is no broker quantity truth."""
    fq = executed_quantity(order, order_fill_detail(order), None)
    if not fq or fq <= 0:
        return 0
    requested = int(p.get("quantity") or position.get("quantity") or 0)
    if requested <= 0:
        return 0
    booked = max(0, requested - int(position.get("quantity") or 0))
    return max(0, min(int(fq), requested) - booked)


def poll_fills(regime: dict | None = None) -> list[dict]:
    """Check pending orders; entry fills create positions, exit fills close
    them with realized paper P&L. Returns all fill events this cycle.

    regime (optional, 2026-07-19 item 1): the current cycle's regime object,
    passed through so paper_fill (entry) records can carry descriptive
    regime_tags. None is handled gracefully (all tags come back null/unknown)
    — callers that don't pass it (e.g. ad-hoc scripts) are unaffected."""
    pending = _load(PENDING_FILE)
    if not pending:
        return []
    client = get_order_client()
    if not client.available():
        return []

    if any(o.get("order_id") is None for o in pending):  # crashed mid-submit
        resolve_intents(client)
        pending = _load(PENDING_FILE)

    events, still_pending = [], []

    def _checkpoint(idx: int) -> None:
        # R4: rewrite `pending` in the SAME step as each mutation. On disk it is
        # always (kept-so-far) + (not-yet-processed), so a crash can NEVER replay
        # a fill that was already booked: a filled/terminal order is dropped here
        # immediately after its position mutation was persisted above.
        _save(PENDING_FILE, still_pending + pending[idx + 1:])

    for i, p in enumerate(pending):
        order = client.get_order(p["order_id"]) if p["order_id"] else None
        status = (order or {}).get("status")

        # ── ITEM 3: PARTIALLY-FILLED ENTRY ─────────────────────────────────
        # Contracts are LIVE at the broker. Book them now (real quantity) so
        # the exit manager governs them; leaving them unbooked is what stranded
        # an unmanaged GOOGL position on 2026-08-05. The order stays pending so
        # further fills accumulate; the remainder is cancelled after the
        # configured timeout.
        if status == "partially_filled" and p.get("intent") == "open":
            _det = order_fill_detail(order)
            _fq = executed_quantity(order, _det, None)   # None => no truth, do not book
            _already = next((q for q in load_positions()
                             if q["rec_id"] == p["rec_id"]), None)
            if _fq and _fq > 0 and _already is None:
                _px = _det["net_debit"] or p["rec_price"]
                _positions = load_positions()
                _positions.append({
                    "rec_id": p["rec_id"], "ticker": p["ticker"],
                    "structure": p["structure"], "strategy": p.get("strategy"),
                    "direction": p["direction"], "legs": p["legs"],
                    "entry_price": _px,          # stop/target from ACTUAL avg fill
                    "quantity": _fq,             # ITEM 6: broker truth
                    "mark_high": _px, "mark_low": _px,
                    "entry_order_id": p["order_id"], "entry_legs": _det["legs"],
                    "entry_filled_at": _det["broker_closed"],
                    "max_profit": p.get("max_profit"), "max_loss": p.get("max_loss"),
                    "expiration": p.get("expiration"),
                    "opened_at": datetime.now().isoformat(timespec="seconds"),
                    "closing": False, "partial": True,
                })
                _save(POSITIONS_FILE, _positions)
                _ev = {"ts": datetime.now().isoformat(timespec="seconds"),
                       "event": "partial_fill_booked", "rec_id": p["rec_id"],
                       "ticker": p["ticker"], "order_id": p["order_id"],
                       "quantity": _fq, "requested": p.get("quantity"),
                       "fill_price": _px,
                       # SCALP-v2: a partial IOC is still a v2 entry — stamp it so
                       # the gate does not exclude the round trip as wrong_fill_mode
                       "fill_mode": p.get("fill_mode", "resting_mid"),
                       "limit_sent": p.get("limit_sent"), "tif": p.get("tif", "DAY"),
                       "stamp": "UNVALIDATED — PAPER ONLY"}
                _log_event(_ev); events.append(_ev)
                logger.warning("PARTIAL FILL BOOKED %s: %s of %s contracts now "
                               "MANAGED (remainder still working)",
                               p["ticker"], _fq, p.get("quantity"))
            elif _fq and _fq > 0 and _already is not None and _already.get("quantity") != _fq:
                # more of the remainder filled — keep the book on broker truth
                _positions = load_positions()
                for q in _positions:
                    if q["rec_id"] == p["rec_id"]:
                        q["quantity"] = _fq
                _save(POSITIONS_FILE, _positions)
                logger.warning("PARTIAL FILL GREW %s -> %s contracts", p["ticker"], _fq)
            # remainder timeout -> cancel what is still working
            try:
                _age = (datetime.now() - datetime.fromisoformat(
                    p["submitted_at"])).total_seconds()
            except Exception:  # noqa: BLE001
                _age = 0
            if not _fq:
                pass          # no executed qty from the broker -> legacy alert path below
            elif _age >= partial_fill_remainder_cancel_seconds():
                logger.warning("PARTIAL REMAINDER TIMEOUT %s: cancelling unfilled "
                               "remainder of order %s after %.0fs",
                               p["ticker"], p["order_id"], _age)
                client.cancel_order(p["order_id"])
                _rev = {"ts": datetime.now().isoformat(timespec="seconds"),
                        "event": "partial_remainder_cancelled",
                        "rec_id": p["rec_id"], "ticker": p["ticker"],
                        "order_id": p["order_id"], "filled": _fq,
                        "requested": p.get("quantity"),
                        "stamp": "UNVALIDATED — PAPER ONLY"}
                _log_event(_rev); events.append(_rev)
                _checkpoint(i)
                continue
            if _fq:
                still_pending.append(p)
                _checkpoint(i)
                continue
            # else: fall through to the legacy partial alert / stay-pending path

        if status == "filled":
            detail = order_fill_detail(order)
            if p["intent"] == "open":
                # R4 dedup: if a position already exists for this rec_id, a prior
                # (possibly crashed-then-replayed) poll already booked this fill.
                # Skip idempotently — no duplicate position, no duplicate
                # paper_fill event — and drop the order from pending.
                _existing = next((q for q in load_positions()
                                  if q["rec_id"] == p["rec_id"]), None)
                if _existing is not None:
                    # R4 dedup: do NOT create a second position. But if this
                    # rec_id was booked from a PARTIAL fill, the order has since
                    # completed and the final executed quantity is the truth —
                    # reconcile it, or the book keeps the partial count while the
                    # broker holds more (the same qty-mismatch class as the 08-05
                    # qty-5 bug; caught by the replay regression test).
                    _final = executed_quantity(order, order_fill_detail(order), None)
                    if _final and _existing.get("quantity") != _final:
                        _ps = load_positions()
                        for q in _ps:
                            if q["rec_id"] == p["rec_id"]:
                                q["quantity"] = _final
                                q["partial"] = False
                        _save(POSITIONS_FILE, _ps)
                        logger.warning("PARTIAL COMPLETED %s: book quantity %s -> %s "
                                       "(broker truth on final fill)",
                                       p["ticker"], _existing.get("quantity"), _final)
                    else:
                        logger.info("paper fill for %s (rec_id %s) already booked — "
                                    "skipping duplicate (R4 idempotent replay)",
                                    p["ticker"], p["rec_id"])
                    _checkpoint(i)
                    continue
                entry_price = detail["net_debit"]  # positive: debit paid
                # Write-time invariant: a debit entry can't fill at <= 0 or above
                # the structure's strike width. Either means a corrupt broker fill
                # (the 06-25 class of bug) — book the rec price and flag it rather
                # than poisoning every downstream P&L number.
                entry_guard = None
                width = strike_width(p.get("legs", []))
                if p["rec_price"] > 0 and (entry_price <= 0 or
                                           (width is not None and entry_price > width + WIDTH_TOLERANCE)):
                    entry_guard = (f"suspect entry fill {entry_price} for debit structure "
                                   f"(rec {p['rec_price']}, width {width}); fell back to rec_price")
                    logger.warning("paper fill guard [%s]: %s", p["ticker"], entry_guard)
                    entry_price = p["rec_price"]
                maf = _mid_at_fill(p.get("legs", []))
                event = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "event": "paper_fill",
                    "rec_id": p["rec_id"], "ticker": p["ticker"],
                    "order_id": p["order_id"],
                    "fill_mode": p.get("fill_mode", "resting_mid"),   # SCALP-v2 scope key
                    "limit_sent": p.get("limit_sent"), "tif": p.get("tif", "DAY"),
                    "rec_price": p["rec_price"], "fill_price": entry_price,
                    # ITEM 6: broker truth, not the request
                    "quantity": executed_quantity(order, detail, p.get("quantity")),
                    "slippage": round(entry_price - p["rec_price"], 4),
                    # execution quality: + = filled BELOW the contemporaneous
                    # mid (captured better than mid on this debit entry)
                    "mid_at_fill": (maf or {}).get("net_mid"),
                    "spread_at_fill": (maf or {}).get("spread_total"),
                    "capture_vs_mid": round((maf["net_mid"] - entry_price), 4) if maf else None,
                    "fill_guard": entry_guard,
                    "legs": detail["legs"],
                    "broker_created": detail["broker_created"],
                    "broker_filled": detail["broker_closed"],
                    # item 1 (2026-07-19): descriptive-only context tags, not gates
                    "regime_tags": regime_tags(regime),
                    "stamp": "UNVALIDATED — PAPER ONLY",
                }
                positions = load_positions()
                positions.append({
                    "rec_id": p["rec_id"], "ticker": p["ticker"],
                    "structure": p["structure"], "strategy": p.get("strategy"),
                    "direction": p["direction"],
                    "legs": p["legs"], "entry_price": entry_price,
                    # ITEM 6: the position is what the BROKER filled, never what
                    # we asked for. This single substitution is the qty-5 bug.
                    "quantity": executed_quantity(order, detail, p.get("quantity")),
                    # seed MFE/MAE at entry so fast scalps that never see a full
                    # cycle still get coherent excursions (06-25..07-01 data had
                    # exit > recorded MFE in 16/40 exits for lack of this)
                    "mark_high": entry_price, "mark_low": entry_price,
                    "entry_order_id": p["order_id"],
                    "entry_legs": detail["legs"],
                    "entry_filled_at": detail["broker_closed"],
                    "max_profit": p["max_profit"], "max_loss": p["max_loss"],
                    "expiration": p["expiration"],
                    "opened_at": datetime.now().isoformat(timespec="seconds"),
                    "closing": False,
                })
                _save(POSITIONS_FILE, positions)
            else:  # close fill — realize paper P&L and link the round trip
                # R4 dedup: the position for this rec_id must still exist to book
                # an exit. If it is already gone, a prior (crashed-then-replayed)
                # poll already booked this exit — skip idempotently so we do NOT
                # emit a duplicate paper_exit (which would double-count the
                # daily-loss breaker) — and drop the order from pending.
                position = next((q for q in load_positions() if q["rec_id"] == p["rec_id"]), None)
                if position is None:
                    logger.info("paper exit for %s (rec_id %s) already booked — "
                                "skipping duplicate (R4 idempotent replay)",
                                p["ticker"], p["rec_id"])
                    _checkpoint(i)
                    continue
                # C-3: book only the executed-but-unbooked increment. On an
                # ordinary full fill (no prior partial) this equals the intent
                # quantity; after partial bookings it is the completed tail —
                # never a double count. Tradier reports no filled quantity, so
                # with no broker truth the filled status books the remainder.
                requested = int(p.get("quantity") or position.get("quantity") or 1)
                fq = executed_quantity(order, detail, None)
                booked = max(0, requested - int(position.get("quantity") or requested))
                if fq is None:            # documented Tradier fallback: filled = all
                    inc = requested - booked
                else:
                    inc = max(0, min(int(fq), requested) - booked)
                if inc > 0:
                    event = _book_close_fill(p, position, order, inc)
                    events.append(event)
                else:
                    logger.info("paper exit for %s (rec_id %s): nothing unbooked "
                                "on filled close %s — skipping (C-3 idempotent)",
                                p["ticker"], p["rec_id"], p["order_id"])
                # A 'filled' close that executed fewer than requested (phantom
                # quantity) leaves a remainder in the book — re-arm it so the
                # exit manager submits a fresh close instead of stranding it.
                leftover = next((q for q in load_positions()
                                 if q["rec_id"] == p["rec_id"]), None)
                if leftover is not None:
                    positions = load_positions()
                    for q in positions:
                        if q["rec_id"] == p["rec_id"]:
                            q["closing"] = False
                    _save(POSITIONS_FILE, positions)
                    logger.warning("close %s for %s filled short (%s of %s booked) — "
                                   "remainder re-armed for a fresh close",
                                   p["order_id"], p["ticker"], booked + inc, requested)
                event = None  # entry branch logs its own event below
            if event is not None:
                _log_event(event)
                events.append(event)
        elif status == "partially_filled":
            # ── C-3: PARTIALLY-FILLED CLOSE ────────────────────────────────
            # Book the executed portion NOW (contracts really exited at the
            # broker) so the book tracks broker truth while the chase continues.
            # NOTE the adapter reports cancelled-after-partial as
            # "partially_filled" forever (filled>0 & remaining>0 wins over
            # "Cancelled"), so this branch is also the wedge-recovery path:
            # once everything executed is booked and the position is gone,
            # the dead record is dropped instead of pending forever.
            if p.get("intent") == "close":
                _pos = next((q for q in load_positions()
                             if q["rec_id"] == p["rec_id"]), None)
                if _pos is None:
                    # everything executed was booked and the position is gone —
                    # the remainder (if any) was cancelled; record is dead.
                    logger.info("close %s for %s fully booked — dropping dead "
                                "partial record (C-3)", p["order_id"], p["ticker"])
                    _checkpoint(i)
                    continue
                _inc = _close_booked_increment(p, _pos, order)
                if _inc > 0:
                    events.append(_book_close_fill(p, _pos, order, _inc))
            # Legging risk made visible (was: silently left pending). Alert once
            # per order; escalate_stale_close still owns the chase for closes.
            if not p.get("partial_alerted"):
                p["partial_alerted"] = True
                logger.warning("paper order %s for %s PARTIALLY FILLED — legging risk, monitoring",
                               p["order_id"], p["ticker"])
                _log_event({"ts": datetime.now().isoformat(timespec="seconds"),
                            "event": "partial_fill_detected", "rec_id": p["rec_id"],
                            "ticker": p["ticker"], "order_id": p["order_id"],
                            "intent": p["intent"], "stamp": "UNVALIDATED — PAPER ONLY"})
                try:
                    from src.engine.notify import send
                    send("Partial fill — legging risk",
                         f"{p['ticker']} {p['intent']} order {p['order_id']} partially filled",
                         priority="high", tags="warning")
                except Exception:
                    pass
            still_pending.append(p)
        elif status in ("canceled", "expired", "rejected"):
            logger.info("paper order %s for %s ended: %s", p["order_id"], p["ticker"], status)
            if p.get("intent") == "open" \
                    and str(p.get("fill_mode", "")).startswith("marketable") \
                    and status in ("canceled", "rejected", "expired") \
                    and not _ioc_unfilled_already_logged(p["rec_id"]):
                # ANY terminal zero-fill outcome of an IOC is an unfilled attempt
                # (the adapter now maps IB 202 to canceled, but a genuine reject
                # is still an attempt that did not fill). Idempotent: a crash
                # between _log_event and _checkpoint must not double count.
                # SCALP-v2: an unfilled IOC is not a failure, it is the design —
                # liquidity was not there at our price, so nothing was left
                # resting. Logged distinctly because the FILL RATE is a frozen
                # acceptance criterion (min_fill_rate); without this event the
                # denominator would be invisible.
                row = {"ts": datetime.now().isoformat(timespec="seconds"),
                       "event": "entry_ioc_unfilled", "rec_id": p["rec_id"],
                       "ticker": p["ticker"], "order_id": p["order_id"],
                       "limit_sent": p.get("limit_sent"),
                       # the INTENT's mode, never a literal: a hardcoded
                       # "marketable_ioc" here would file a marketable_day
                       # attempt under v2's evidence and blend two candidates
                       "fill_mode": p.get("fill_mode"),
                       "stamp": "UNVALIDATED — PAPER ONLY"}
                if status == "rejected":
                    # a broker refusal, not a no-liquidity cancel (see submit path)
                    row["broker_reject"] = {"code": None,
                                            "text": (order or {}).get("reason")}
                _log_event(row)
            if p["intent"] == "close":  # close failed — position still open, re-arm
                # C-3: a close can die WITH partial executions (cancelled after a
                # partial fill). Book the executed portion before re-arming, or
                # the book keeps contracts the broker already closed.
                _pos = next((q for q in load_positions()
                             if q["rec_id"] == p["rec_id"]), None)
                if _pos is not None:
                    _inc = _close_booked_increment(p, _pos, order)
                    if _inc > 0:
                        events.append(_book_close_fill(p, _pos, order, _inc))
                positions = load_positions()
                for q in positions:
                    if q["rec_id"] == p["rec_id"]:
                        q["closing"] = False
                        if status == "rejected":
                            q["close_reject_count"] = q.get("close_reject_count", 0) + 1
                            # L20 circuit breaker (2026-07-14: sandbox silently
                            # deleted overnight positions; the re-arm loop spent
                            # the day submitting 59 guaranteed rejects). Repeated
                            # rejects mean the broker likely no longer holds the
                            # legs — stop retrying, flag, alert, reconcile.
                            if q["close_reject_count"] >= 3 and not q.get("broker_lost"):
                                q["broker_lost"] = True
                                logger.error(
                                    "BROKER-LOST suspected [%s %s]: %d consecutive close "
                                    "rejects — retries stopped, run scripts/reconcile_positions.py",
                                    q["ticker"], q["rec_id"], q["close_reject_count"])
                                try:
                                    from src.engine.notify import send
                                    send("BROKER-LOST POSITION",
                                         f"{q['ticker']} {q['rec_id']}: "
                                         f"{q['close_reject_count']} close rejects — retries "
                                         "stopped, reconcile needed", priority="high",
                                         tags="warning")
                                except Exception:
                                    pass
                        else:
                            q["close_reject_count"] = 0
                _save(POSITIONS_FILE, positions)
        else:
            still_pending.append(p)
        # R4: transactional checkpoint after this order's mutation (position
        # writes above already hit disk first, so a crash here at worst replays
        # into the idempotent dedup guards, never into a duplicate).
        _checkpoint(i)
    _save(PENDING_FILE, still_pending)
    return events


def restart_blocking_positions(today: str | None = None, checked: dict | None = None) -> tuple[int, str]:
    """Positions that genuinely forbid an engine restart, with the reason.

    DEADLOCK FIX (2026-09-12). The pre-session restart job used to refuse
    whenever the book was not flat. But the book is not flat BECAUSE the broker
    is dead — positions cannot be closed — so the guard fired exactly when the
    restart was most needed, and the outage sustained itself. It cost 09-02,
    09-03, 09-04, 09-08, 09-10 and 09-11, held open by contracts that had
    ALREADY EXPIRED and that the broker no longer held. Refusing protected
    nothing.

    Only a LIVE position the engine can actually still manage deserves
    protection, i.e. one that is (a) not past expiry and (b) confirmed present
    at the broker. An UNKNOWN broker view does not block: nothing is being
    managed either way, and the restart is what restores management.

    Returns (count, reason). count == 0 means "safe to restart".
    """
    from datetime import date as _date
    today = today or _date.today().isoformat()
    positions = load_positions()
    pending = _load(PENDING_FILE)
    live = [p for p in positions
            if str(p.get("expiration") or "9999-12-31") >= today]
    if not live and not pending:
        return 0, "nothing live (flat, or every position already expired)"
    r = checked if checked is not None else audit_book_vs_broker_checked()
    if r["status"] == "could_not_check":
        return 0, "broker view UNKNOWN — a restart is the remedy, not the risk"
    absent = {a.get("symbol") for a in r["alarms"]
              if a.get("kind") == "book_position_not_at_broker"}
    real = [p for p in live
            if not all(leg.get("symbol") in absent for leg in (p.get("legs") or []))]
    # A pending INTENT blocks only if it is from TODAY. An IOC is terminal within
    # the placing second, so a day-old v2 intent still sitting in the orders
    # book is bookkeeping debris, not a manageable live order — counting it
    # refused the 06:20 restart with a false "LIVE at the broker" page. (The
    # audit does not consult the broker's order book for intents; pinned by
    # tests so any change here is deliberate.)
    live_pending = sum(1 for o in pending if str(o.get("submitted_at", ""))[:10] == today)
    n = len(real) + live_pending
    return n, ("live position(s)/order(s) confirmed at the broker" if n
               else "every blocking position/order is absent at the broker")
