"""Signal-data freshness — durable fix for the stale-bar defect (INV8 root cause).

The bug was that freshness was judged on the wrong axis (when a frame was fetched,
not how old its newest bar is), and the static session fallback did not know
half-days. This module is the source of truth for both, and is SESSION-AWARE:
a stale bar during an OPEN session is a feed defect (block + would-alarm); a
stale bar outside the session (pre-open / weekend / half-day close) is expected
(block the trade, but no alarm).

Pure functions (`bar_age_min`, `session_state`, `freshness_verdict`) are
unit-tested across regular / pre-open / post-close / weekend / half-day / halt.
The live wrappers (`current_session_state`, `assess`) add the calendar/clock and
config. Feed-guard floors are IRREVERSIBLE (they block a live trade) — see
config/signal_freshness.yaml for the funnel asymmetry rationale.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
_FLOORS_CACHE: dict = {}
logger = logging.getLogger("trader")

# R8: staleness floors (minutes) for the SLOWER frames that share a signal.
# The 5m primary keeps its lane feed_guard floor unchanged; a 30m frame is up
# to ~30 min old even when fresh, so its floor must clear the interval yet still
# catch a ~6h-stale prior-session frame. Overridable via config
# (stale_frame_floor_min) but defaulted here so no config edit is required.
DEFAULT_SLOW_FRAME_FLOOR_MIN = {"30m": 90}


def load_floors() -> dict:
    if not _FLOORS_CACHE:
        _FLOORS_CACHE.update(yaml.safe_load((ROOT / "config" / "signal_freshness.yaml").read_text()))
    return _FLOORS_CACHE


# ── pure, unit-tested ──
def bar_age_min(bar_ts, now) -> float | None:
    """Minutes between `now` and the bar timestamp; both coerced to aware ET.
    Naive inputs are assumed ET. Returns None if unparseable."""
    try:
        bar = bar_ts if isinstance(bar_ts, datetime) else datetime.fromisoformat(str(bar_ts))
        if bar.tzinfo is None:
            bar = bar.replace(tzinfo=ET)
        n = now if now.tzinfo else now.replace(tzinfo=ET)
        return (n.astimezone(ET) - bar.astimezone(ET)).total_seconds() / 60.0
    except Exception:
        return None


def session_state(now, day_cal) -> str:
    """'open' | 'premarket' | 'postmarket' | 'closed', from the day's calendar
    entry. `day_cal` = {'status':'open'|'closed', 'open_time':'HH:MM',
    'close_time':'HH:MM'} in ET, or None (not a trading day).

    close_time is AUTHORITATIVE — a half-day carries an early close, so this is
    correct WITHOUT any hardcoded 16:00. A wrong static 16:00 is exactly what
    would turn a half-day's afternoon into a stale-bar alarm storm."""
    if not day_cal or day_cal.get("status") != "open":
        return "closed"
    now_et = (now if now.tzinfo else now.replace(tzinfo=ET)).astimezone(ET)

    def _t(s):
        h, m = str(s).split(":")[:2]
        return time(int(h), int(m))

    o, c = _t(day_cal["open_time"]), _t(day_cal["close_time"])
    t = now_et.time()
    if t < o:
        return "premarket"
    if t >= c:
        return "postmarket"
    return "open"


def freshness_verdict(age_min, lane, sess, floors=None) -> tuple[bool, str]:
    """(allow, reason). Stale during OPEN = feed defect ('stale_bar'). Stale
    outside the session = expected ('market_closed'). None age = 'no_bar' (block).
    A halt looks like 'stale_bar' during open (correct: don't trade on stale
    data); halt vs feed-down is told apart by the block log's ticker spread."""
    floors = floors or load_floors()
    floor = (floors.get("feed_guard_floor_min") or {}).get(lane, 3)
    if age_min is None:
        return False, "no_bar"
    if age_min <= floor:
        return True, "fresh"
    return (False, "stale_bar") if sess == "open" else (False, "market_closed")


def slow_frame_verdict(age_min, label, sess, floors=None) -> tuple[bool, str]:
    """(allow, reason) for a SLOWER co-frame (e.g. the 30m squeeze/EMA bars).
    R8: the OLDER of the frames a signal rides on governs — a trade is only
    fresh if the slow frame is fresh too. Uses stale_frame_floor_min[label]
    (default DEFAULT_SLOW_FRAME_FLOOR_MIN) so normal 30m cadence isn't mistaken
    for stale, but a ~6h-stale (prior-session) frame still blocks during open.
    An ABSENT slow frame (age None) does NOT block here — its absence is handled
    upstream (SCALP is 5m-only; a missing 30m frame is flagged elsewhere)."""
    floors = floors or load_floors()
    floor = (floors.get("stale_frame_floor_min") or DEFAULT_SLOW_FRAME_FLOOR_MIN).get(
        label, DEFAULT_SLOW_FRAME_FLOOR_MIN.get(label, 90))
    if age_min is None:
        return True, "no_slow_frame"
    if age_min <= floor:
        return True, "fresh"
    return (False, "stale_bar") if sess == "open" else (False, "market_closed")


# ── live wrappers (calendar/clock + config) ──
def current_session_state(now=None) -> str:
    """Live session state, half-day aware. Tradier clock (authoritative) ->
    calendar close_time reconstruction -> static config hours (LAST resort,
    half-day-blind; logs degraded)."""
    try:
        from src.data.tradier import get_client
        clk = get_client().clock()
        st = (clk or {}).get("state")
        if st in ("open", "premarket", "postmarket", "closed"):
            return st
    except Exception:
        pass
    now = now or datetime.now(ET)
    try:  # calendar reconstruction keeps half-days correct without the clock
        from src.data.tradier import get_client
        cal = get_client().calendar(now.month, now.year) or {}
        day = cal.get(now.strftime("%Y-%m-%d"))
        if day:
            # R12: tradier.calendar() returns the intraday times under `open`/
            # `close`, NOT open_time/close_time. Reading the wrong keys made this
            # whole half-day layer dead code (both None -> session_state garbage,
            # swallowed below), so early-close days fell through to the half-day-
            # BLIND static fallback and positions rode through 13:00 ET closes.
            return session_state(now, {"status": day.get("status"),
                                       "open_time": day.get("open"),
                                       "close_time": day.get("close")})
    except Exception as exc:
        # R12: was `pass` — a broken calendar path must be VISIBLE, not silently
        # dead. Still non-fatal: fall through to the static fallback.
        logger.warning("freshness: calendar half-day reconstruction failed "
                       "(%s: %s) — falling back to static market hours", type(exc).__name__, exc)
    from src.scheduler import _market_open_static  # half-day-BLIND fallback
    return "open" if _market_open_static(now) else "closed"


def assess(ticker, bar_ts, lane, now=None, sess=None, floors=None) -> dict:
    """Live one-ticker verdict. Returns {allow, age_min, reason, session}."""
    now = now or datetime.now(ET)
    sess = sess or current_session_state(now)
    floors = floors or load_floors()
    age = bar_age_min(bar_ts, now)
    allow, reason = freshness_verdict(age, lane, sess, floors)
    return {"allow": allow, "age_min": None if age is None else round(age, 2),
            "reason": reason, "session": sess}


def assess_slow_frame(ticker, bar_ts, label, now=None, sess=None, floors=None) -> dict:
    """R8 live one-frame verdict for a slower co-frame (label e.g. '30m').
    Returns {allow, age_min, reason, session, frame}. allow=True (reason
    'no_slow_frame') when the frame is absent — absence does not block here."""
    now = now or datetime.now(ET)
    sess = sess or current_session_state(now)
    floors = floors or load_floors()
    age = bar_age_min(bar_ts, now)
    allow, reason = slow_frame_verdict(age, label, sess, floors)
    return {"allow": allow, "age_min": None if age is None else round(age, 2),
            "reason": reason, "session": sess, "frame": label}
