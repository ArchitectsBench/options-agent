"""Heartbeat schema + the four deadman DECISION functions (spec §15.4) —
pure, unit-tested, NOT wired into `scripts/deadman.sh` on this branch (that
is commissioning item 3, same commit as arming).
"""

from __future__ import annotations

from datetime import datetime, time

from src.rvol_exec.constants import ET

HEARTBEAT_FIELDS = ("ts_et", "now_et", "monotonic", "date", "pid", "sha", "started_at_et", "phase",
                    "broker", "account", "t_ready_et", "snapshot_mode", "contamination_s", "planned",
                    "placed", "held", "slots_used", "positions_without_live_stop", "realized",
                    "unrealized", "marks_missing", "halted", "kill_switch", "hold",
                    "last_exec_poll_ok_et", "restarts_today", "stamp")

IN_SESSION_START = time(9, 40)
IN_SESSION_END = time(16, 0)
DEAD_AFTER_S = 120
BROKER_UNKNOWN_AFTER_S = 300


def heartbeat_ok(hb: dict) -> tuple[bool, list[str]]:
    missing = [f for f in HEARTBEAT_FIELDS if f not in (hb or {})]
    return not missing, missing


def _in_session(now_et: datetime) -> bool:
    t = now_et.timetz().replace(tzinfo=None)
    return now_et.weekday() < 5 and IN_SESSION_START <= t < IN_SESSION_END


def _age_s(hb: dict | None, now_et: datetime) -> float | None:
    if not hb or not hb.get("ts_et"):
        return None
    try:
        return (now_et - datetime.fromisoformat(hb["ts_et"]).astimezone(ET)).total_seconds()
    except (TypeError, ValueError):
        return None


def rvol_lane_dead_in_session(hb: dict | None, now_et: datetime) -> bool:
    """Weekday 09:40-16:00 ET and the heartbeat is absent or older than 120 s."""
    if not _in_session(now_et):
        return False
    age = _age_s(hb, now_et)
    return age is None or age > DEAD_AFTER_S


def rvol_lane_stop_missing(prev_hb: dict | None, hb: dict | None) -> bool:
    """`positions_without_live_stop > 0` on two consecutive reads."""
    return bool(prev_hb and hb and int(prev_hb.get("positions_without_live_stop") or 0) > 0
                and int(hb.get("positions_without_live_stop") or 0) > 0)


def rvol_lane_unflattened(marker_exists: bool) -> bool:
    return bool(marker_exists)


def rvol_lane_broker_unknown(history: list[tuple[datetime, dict]], now_et: datetime) -> bool:
    """`broker != connected` for > 5 min in-session with `held > 0` — history
    is a list of (read_time_et, heartbeat) oldest first."""
    if not _in_session(now_et) or not history:
        return False
    bad_since = None
    for ts, hb in history:
        if hb.get("broker") != "connected" and int(hb.get("held") or 0) > 0:
            bad_since = bad_since or ts
        else:
            bad_since = None
    return bad_since is not None and (now_et - bad_since).total_seconds() > BROKER_UNKNOWN_AFTER_S
