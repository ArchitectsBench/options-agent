#!/usr/bin/env python3
"""O-21 LIVENESS — session-aware alerting decisions.

WHY THIS EXISTS (2026-08-07): the engine was down for the first 2.5h of a
session and nobody was told. The deadman WORKED — it detected the dead pid and
alerted at 03:12:55 — but `alert()` applies a 6-HOUR PER-CONDITION THROTTLE, so
03:12+6h=09:12 suppressed the identical condition straight across the 06:30
open. An alert fired during legitimate overnight engine-down work silenced the
alert that mattered. Same family as the 07-27 probe silent week: severity must
track DECISION VALUE, not event frequency.

THE RULE: "down" and "down DURING MARKET HOURS" are DIFFERENT CONDITIONS.
The session-critical one is never suppressed by an out-of-session alert — its
throttle RESETS at every open, and it carries a short in-session throttle so a
continuing outage keeps announcing itself.

Decision logic lives here (not in shell) so it is directly testable.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
OPEN_H, OPEN_M = 6, 30            # config agent.market_hours.start
CLOSE_H, CLOSE_M = 13, 0
HEARTBEAT_GRACE_MIN = 10          # dead-man: alert if no heartbeat by open+10
IN_SESSION_THROTTLE_S = 1800      # 30 min — a live outage keeps announcing itself
OUT_OF_SESSION_THROTTLE_S = 21600  # 6 h — unchanged for non-urgent conditions

# Conditions whose meaning CHANGES inside the session. These get the
# session-aware treatment; everything else keeps the plain 6h throttle.
# broker-execution-dead joins these (2026-08-22): a dead execution path during
# the session is exactly as urgent as a dead agent — three full sessions were
# lost to it in silence — so it gets the -in-session slug and the 30-min
# throttle rather than the 6h default.
# broker-port-dead joins them (2026-10-12) for the same reason: see
# broker_port_dead() below — two consecutive sessions were lost in silence.
# host-on-battery joins them (2026-11-17), and today is the proof case: the
# deadman paged it at 08:35:15, the operator plugged in at 09:01:28, and the
# host came OFF AC again at 09:31:48 — but the 6h throttle had already consumed
# the window, so the second stint could not page again until 14:35, after the
# 13:00 close. An awake-on-battery state could therefore be announced at most
# ONCE per session no matter how often the host was unplugged. The cost case is
# 2026-11-16: 114 lost ticks (56 RTH), the worst session ever measured. The slug
# split also retroactively explains that day's silence — its 05:18:53 page was
# OUT of session (session_open is 06:30-13:00 while the battery layer's own
# window starts 05:00), so the 6h stamp carried straight across the open. That
# is precisely the O-21 failure mode this module was written to prevent.
SESSION_CRITICAL = ("agent-dead", "cycle-hung", "broker-execution-dead",
                    "broker-port-dead", "host-on-battery")


def _exchange_calendar() -> tuple[set, dict]:
    """(full holidays, {date: early-close 'HH:MM' PT}) from config.yaml.

    FAIL-OPEN BY DESIGN (2026-11-24): any read/parse failure returns empty
    collections, i.e. exactly the pre-calendar behaviour. A bad config read
    must never SUPPRESS a real in-session page — it may only fail to suppress
    a false one. Path is resolved from this file, not cwd: the deadman runs
    under launchd with an arbitrary working directory.
    """
    try:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "config" / "config.yaml").read_text()
        )["agent"]
        return set(cfg.get("holidays_2026") or []), dict(cfg.get("early_close_2026") or {})
    except Exception:
        return set(), {}


def session_open(now: datetime | None = None) -> bool:
    """True only when the exchange is actually trading.

    WHY THE CALENDAR IS HERE (2026-11-24): this function used to be weekday +
    static 06:30-13:00, and :162 named that holiday exposure without fixing
    it. The cost is measured, not hypothetical — on Labor Day 2026-09-07 (a
    weekday full holiday, engine healthy, 361 heartbeat-only ticks) the deadman
    paged `cycle-hung-in-session` at 06:34 and every 30 min after, plus
    `no-heartbeat-at-open` at 06:44: false pages for a market that was shut.
    `last_cycle` only advances on an in-session cycle, so a closed market reads
    as a hung engine. Thanksgiving 2026-11-26 would have repeated it, and the
    2026-11-27 early close (10:00 PT) would have paged for its last 3 hours —
    the storm is the market closing, not the engine stopping.
    """
    now = now or datetime.now(PT)
    if now.weekday() >= 5:
        return False
    holidays, early = _exchange_calendar()
    day = now.strftime("%Y-%m-%d")
    if day in holidays:
        return False
    close = early.get(day)
    if close:
        close_h, close_m = map(int, str(close).split(":"))
    else:
        close_h, close_m = CLOSE_H, CLOSE_M
    mins = now.hour * 60 + now.minute
    return (OPEN_H * 60 + OPEN_M) <= mins < (close_h * 60 + close_m)


def market_open_dt(now: datetime) -> datetime:
    return now.replace(hour=OPEN_H, minute=OPEN_M, second=0, microsecond=0)


def minutes_since_open(now: datetime) -> float:
    return (now - market_open_dt(now)).total_seconds() / 60.0


def slug_for(condition: str, now: datetime) -> str:
    """A session-critical condition gets a DISTINCT slug in-session, so its
    throttle stamp is separate from the out-of-session one. An overnight
    'agent-dead' can therefore never suppress 'agent-dead-in-session'."""
    if condition in SESSION_CRITICAL and session_open(now):
        return f"{condition}-in-session"
    return condition


def throttle_seconds(condition: str, now: datetime) -> int:
    if condition in SESSION_CRITICAL and session_open(now):
        return IN_SESSION_THROTTLE_S
    return OUT_OF_SESSION_THROTTLE_S


def should_alert(condition: str, now: datetime, last_alert_epoch: float | None) -> bool:
    """True when the alert must fire. The session-critical path uses its own
    slug + short throttle, so it is structurally immune to an out-of-session
    alert having 'used up' the window."""
    if last_alert_epoch is None:
        return True
    elapsed = now.timestamp() - float(last_alert_epoch)
    return elapsed >= throttle_seconds(condition, now)


def heartbeat_overdue(now: datetime, last_cycle: datetime | None) -> bool:
    """Dead-man at open+GRACE: the session is open, we are past the grace
    window, and there is no heartbeat from THIS session."""
    if not session_open(now):
        return False
    if minutes_since_open(now) < HEARTBEAT_GRACE_MIN:
        return False
    if last_cycle is None:
        return True
    return last_cycle < market_open_dt(now)


BROKER_PORT_GRACE_MIN = 30        # see broker_port_dead()
CYCLE_STALE_MIN = 15              # 3 missed 5-min cycles = the loop is hung


def cycle_hang_minutes(now: datetime, last_cycle: datetime | None) -> float | None:
    """Minutes of unexplained cycle silence, or None when there is nothing to
    alarm about.

    WHY THE OPEN IS A SPECIAL CASE (2026-09-30): `last_cycle` only advances on
    an IN-SESSION cycle — a market-closed tick is heartbeat-only and leaves it
    at yesterday's 13:00. The engine's first real cycle lands ~06:35 (the
    06:30:00 tick still reads closed), so between 06:30 and that cycle the raw
    age is ~1,050 min and the deadman paged the operator's phone every trading
    morning: 09-25 06:35, 09-28 06:35, 09-30 06:34. Measuring the age from
    max(last_cycle, today's open) removes exactly that artifact and nothing
    else: a mid-session hang still pages off last_cycle, and an engine that
    never cycles at all still pages from open+15 (~06:46), which is how the
    real 09-24 outage was caught.
    """
    if not session_open(now):
        return None
    reference = market_open_dt(now)
    if last_cycle is not None and last_cycle > reference:
        reference = last_cycle
    age = (now - reference).total_seconds() / 60.0
    return age if age > CYCLE_STALE_MIN else None


def broker_port_dead(now: datetime, port_open: bool) -> bool:
    """True when the session is live and the IBKR API port is unreachable.

    WHY (2026-10-12): on 10-09 the gateway was healthy at 06:35 and the API
    port died some time after, mid-session. EVERY layer above stayed silent and
    honestly so — the pid was alive, cycles advanced all 78 of them, and layer
    3b (broker-execution-dead) reads the engine's 'Not connected' ENTRY
    REJECTIONS, of which there were none because no candidate ever cleared its
    pre-placement guards. A brokerless-but-IDLE engine emits no observable at
    all, so the only honest detector is a direct probe of the port. 10-12 then
    lost a SECOND consecutive session to the same silence.

    This does NOT duplicate layer 3b: 3b answers "is the engine's long-lived
    client usable" (remedy: restart) and can fail with the port wide open; this
    answers "is the gateway listening at all" (remedy: log in).

    The 3b-era argument against probing ("a fresh probe connects fine, which is
    why go_nogo kept passing") does not apply here — it was about proving the
    ENGINE'S client alive. Here the port itself is the thing that died, so a
    probe is the strongest available observable, not the weakest.

    OWNERSHIP SPLIT, so the operator is not paged twice for one fact: the
    at-the-open case belongs to scripts/ops/await_broker_activation.sh, which
    pages on a closed port and re-kicks until its 06:45 deadline. That script
    then stops watching. This layer starts at open+BROKER_PORT_GRACE_MIN
    (07:00) and covers the rest of the session.

    Inherits the same holiday exposure as layers 2b/3: session_open() has no
    holiday calendar (named, not fixed here).
    """
    if not session_open(now):
        return False
    if minutes_since_open(now) < BROKER_PORT_GRACE_MIN:
        return False
    return not port_open


if __name__ == "__main__":   # tiny CLI for deadman.sh
    import json
    import sys
    now = datetime.now(PT)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "state"
    if cmd == "slug":
        print(slug_for(sys.argv[2], now))
    elif cmd == "throttle":
        print(throttle_seconds(sys.argv[2], now))
    elif cmd == "session_open":
        print("1" if session_open(now) else "0")
    elif cmd == "heartbeat_overdue":
        lc = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
        dt = None
        if lc:
            try:
                dt = datetime.fromisoformat(lc)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=PT)
            except Exception:
                dt = None
        print("1" if heartbeat_overdue(now, dt) else "0")
    elif cmd == "broker_port_dead":
        # deadman.sh does the probe (shell owns nc); the DECISION is here.
        print("1" if broker_port_dead(now, sys.argv[2] == "1") else "0")
    elif cmd == "cycle_hang":
        lc = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
        dt = None
        if lc:
            try:
                dt = datetime.fromisoformat(lc)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=PT)
            except Exception:
                dt = None
        mins = cycle_hang_minutes(now, dt)
        # empty = quiet; 'none' = in session, past grace, never cycled today
        print("" if mins is None else ("none" if dt is None else f"{mins:.1f}"))
    else:
        print(json.dumps({"session_open": session_open(now),
                          "minutes_since_open": round(minutes_since_open(now), 1)}))
