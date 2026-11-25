"""Agent loop: APScheduler fires every 5 minutes; cycles run only during
market hours (config: 06:30-13:00 PT, weekdays, skipping NYSE holidays).
The loop reads the control state each tick — stop/pause take effect within
one tick without killing the process externally."""

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.control.state import load_state, update_state
from src.engine.pipeline import logger, run_cycle

CONFIG = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())


def _market_open_static(now: datetime | None = None) -> bool:
    """Fallback when Tradier is unavailable: config hours + static holiday list."""
    cfg = CONFIG["agent"]["market_hours"]
    tz = ZoneInfo(cfg["tz"])
    now = now.astimezone(tz) if now else datetime.now(tz)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in CONFIG["agent"].get("holidays_2026", []):
        return False
    start_h, start_m = map(int, cfg["start"].split(":"))
    # Half-day awareness for the FALLBACK path (2026-11-24). market_open()'s
    # live path asks the Tradier clock, which already knows early closes, so
    # this only matters when Tradier is unavailable — but then the static
    # window would run cycles for 3h against a closed market (2026-11-27).
    early = CONFIG["agent"].get("early_close_2026", {}) or {}
    end = early.get(now.strftime("%Y-%m-%d"), cfg["end"])
    end_h, end_m = map(int, str(end).split(":"))
    minutes = now.hour * 60 + now.minute
    return start_h * 60 + start_m <= minutes < end_h * 60 + end_m


def market_open(now: datetime | None = None) -> bool:
    """Authoritative source: Tradier market clock (knows holidays and half
    days). Static config computation only as fallback."""
    if now is None:  # live query — ask the exchange calendar
        from src.data.tradier import get_client
        clock = get_client().clock() if get_client().available() else None
        if clock is not None:
            return clock.get("state") == "open"
    return _market_open_static(now)


_JOB_LOCK = __import__("threading").Lock()  # cycles and command polls never overlap


def poll_phone_commands():
    """Fast side-job (~30s): apply phone confirm/decline commands and, on a
    confirm, execute the queue immediately instead of waiting for the next
    5-min cycle. No-op unless confirmations are pending."""
    with _JOB_LOCK:
        try:
            from src.control.ntfy_commands import poll_and_apply
            events = poll_and_apply()
            if any(e["action"] == "confirm" and e["applied"] for e in events):
                from src.engine.confirm import process_queue
                from src.engine.live_gate import resolve_mode
                from src.engine.paper_trader import submit_paper_order
                mode = resolve_mode(CONFIG, strict=False)["mode"]
                for ev in process_queue(CONFIG, submit_paper_order, mode=mode):
                    print(f"[{datetime.now():%H:%M:%S}] CONFIRM QUEUE (phone): "
                          f"{ev['event']} — {ev['rec_id']}", flush=True)
        except Exception as exc:  # never take the loop down
            logger.warning("phone-command poll failed: %s", exc)


def refresh_earnings_calendar():
    """Daily earnings-calendar refresh (also at startup when stale). Failures
    log and leave prior entries; the veto's fail-closed staleness rule governs."""
    with _JOB_LOCK:
        try:
            from src.engine.earnings import calendar_is_fresh_today, refresh_calendar
            if calendar_is_fresh_today():
                return
            watchlist = CONFIG["watchlist"]["default"]
            refresh_calendar(watchlist)
            print(f"[{datetime.now():%H:%M:%S}] earnings calendar refreshed "
                  f"({len(watchlist)} tickers)", flush=True)
        except Exception as exc:
            logger.warning("earnings calendar refresh failed: %s", exc)


def tick():
    _tick_locked()


def _tick_locked():
    with _JOB_LOCK:
        _tick()


_LAST_TICK: datetime | None = None
WAKE_GAP_MIN = 15  # > 3 cycles of silence = the host slept (or the loop froze)


def _wake_guard():
    """Laptop-state hardening (2026-08-02). If the host slept mid-session, the
    book can hold fills that landed while we were dark, and a post-close wake
    leaves open positions UNMANAGED (the market-hours gate skips the cycle
    entirely — the 07-28 orphan mechanism). Detect the gap and run a
    protective sweep BEFORE the market-hours gate: record fills, resolve
    intents, and alert LOUDLY if positions are open when nothing can manage
    them. Never raises — a failed sweep logs and the loop continues."""
    global _LAST_TICK
    now = datetime.now()
    prev, _LAST_TICK = _LAST_TICK, now
    if prev is None or (now - prev).total_seconds() < WAKE_GAP_MIN * 60:
        return
    gap_min = round((now - prev).total_seconds() / 60)
    logger.warning("wake guard: %d-min tick gap detected — protective sweep", gap_min)
    try:
        from src.engine import paper_trader as pt
        recovered = pt.resolve_intents() or []
        fills = pt.poll_fills() or []
        positions = pt.load_positions()
        pending = pt._load(pt.PENDING_FILE)
        pt._log_event({"ts": now.isoformat(timespec="seconds"),
                       "event": "wake_gap_recovery", "gap_min": gap_min,
                       "fills_recorded": len(fills), "intents_resolved": len(recovered),
                       "positions_open": len(positions), "orders_pending": len(pending),
                       "market_open": market_open(),
                       "stamp": "UNVALIDATED — PAPER ONLY"})
        if not market_open() and (positions or pending):
            # nothing will manage these until the next session — human must know NOW
            from src.engine.notify import send
            send("WAKE: unmanaged positions after hours",
                 f"slept {gap_min}m; {len(positions)} open position(s) + "
                 f"{len(pending)} pending order(s) with market closed — "
                 "exit management resumes next session; review now",
                 priority="high", tags="warning")
            logger.error("wake guard: %d position(s)/%d order(s) open with market "
                         "closed — alerted", len(positions), len(pending))
        elif fills or recovered:
            logger.warning("wake guard: recorded %d fill event(s), resolved %d "
                           "intent(s) from the gap", len(fills), len(recovered))
    except Exception as exc:
        logger.exception("wake guard sweep failed (loop continues): %s", exc)


def _tick():
    _wake_guard()  # BEFORE the market-hours gate — post-close wakes must sweep
    state = load_state()
    if state["command"] == "stop":
        update_state(status="stopped", pid=None)
        logger.info("stop command received — shutting down")
        print("stop command received — shutting down", flush=True)
        raise SystemExit(0)
    if state["command"] == "pause":
        update_state(status="paused")
        logger.info("paused — skipping cycle")
        return
    update_state(status="running")
    if not market_open():
        logger.info("market closed — heartbeat only")
        print(f"[{datetime.now():%H:%M:%S}] market closed — heartbeat only", flush=True)
        return
    try:
        run_cycle(state["cycle_count"] + 1)
    except Exception as exc:
        # a cycle error counts toward the >2-cycle health alert; loop survives
        logger.exception("cycle failed: %s", exc)
        print(f"[{datetime.now():%H:%M:%S}] CYCLE ERROR (loop continues): {exc}", flush=True)
        from src.engine.notify import track_health
        track_health(True, f"cycle exception: {exc}")


def main():
    # Hard safety rail: refuse to start if the trading endpoint could be production.
    from src.data.tradier_orders import assert_paper_safety
    assert_paper_safety()

    # L-e: the retired SHARED book files must never be written again. Lane books
    # (data/lane/<lane>/) are authoritative; a repopulated shared file means some
    # path is still on the pre-migration layout. Fail loudly, do not warn.
    try:
        from src.engine.paper_trader import assert_shared_books_retired
        assert_shared_books_retired()
    except RuntimeError as _exc:
        raise SystemExit(f"REFUSING TO START: {_exc}")

    # O-1 FANTASY-FILL GUARD (2026-08-06). tradier_sandbox is retained ONLY as
    # the hermetic-test default and a documented rollback path. Its fills are
    # fantasy — on 2026-08-04 they disagreed with market truth on P&L SIGN in
    # 26% of trades. A session must never run on it by accident (the runbook's
    # STEP 1 exists because forgetting the flip is easy). Refuse, do not warn.
    try:
        from src.data.order_client import _resolve_broker
        _brk = _resolve_broker()
        if _brk == "tradier_sandbox" and not CONFIG.get("agent", {}).get(
                "allow_sandbox_session", False):
            raise SystemExit(
                "REFUSING TO START: broker is tradier_sandbox, whose fills are "
                "FANTASY (26% sign disagreement vs market truth on 2026-08-04). "
                "Set agent.broker: ibkr_paper (runbook STEP 1), or set "
                "agent.allow_sandbox_session: true to override deliberately.")
    except SystemExit:
        raise
    except Exception as _exc:  # noqa: BLE001 - never block on a config read error
        logger.warning("sandbox guard could not resolve the broker: %s", _exc)

    # Live-mode gate (charter: refuse, not warn). agent.mode=live raises
    # LiveModeRefused unless EVERY condition is met — see src/engine/live_gate.py.
    from src.engine.live_gate import resolve_mode, kill_switch_engaged
    mode_info = resolve_mode(CONFIG)  # strict: refusal propagates, agent does not start
    ks = kill_switch_engaged()
    print(f"agent mode: {mode_info['mode']}"
          + (f" | KILL SWITCH ENGAGED: {ks}" if ks else ""), flush=True)

    # Startup reconciliation: adopt unknown broker positions, cancel unknown
    # stale orders — BEFORE the first cycle. Failure logs, never blocks.
    try:
        from src.engine.paper_trader import reconcile_with_broker
        recon = reconcile_with_broker()
        if recon:
            print(f"reconciliation: {[(e['event'], e.get('ticker') or e.get('order_id')) for e in recon]}",
                  flush=True)
    except Exception as exc:
        logger.warning("startup reconciliation failed: %s", exc)

    update_state(status="running", pid=os.getpid(), command="run")
    logger.info("agent loop starting (pid %d)", os.getpid())
    print(f"agent loop starting (pid {os.getpid()}), cycle every "
          f"{CONFIG['agent']['cycle_minutes']} min", flush=True)
    # fast reflex loop: streaming stop/target watchdog (idles when book flat;
    # 5-min cycle remains the guaranteed exit backstop if the stream dies)
    from src.engine.watchdog import start_watchdog
    start_watchdog(_JOB_LOCK, CONFIG)

    tick()  # immediate first tick
    sched = BlockingScheduler()
    # misfire_grace_time (2026-07-03): the host SLEPT through 06:30-07:45 on
    # 07-02 and APScheduler skipped every missed job on wake — the agent lost
    # the market open. caffeinate in the launchd plist prevents sleep while
    # the trader runs; grace windows make wake-ups catch up, not skip.
    sched.add_job(tick, "cron", minute=f"*/{CONFIG['agent']['cycle_minutes']}",
                  misfire_grace_time=240, coalesce=True)
    # fast phone-confirmation poll (no-op when nothing is pending)
    sched.add_job(poll_phone_commands, "interval", seconds=30,
                  max_instances=1, coalesce=True, misfire_grace_time=30)
    # earnings calendar: refresh at startup if not fetched today, then daily
    # pre-open (the veto fails closed on single names while data is stale) —
    # a whole-day grace so a late wake still refreshes before Monday's open
    refresh_earnings_calendar()
    sched.add_job(refresh_earnings_calendar, "cron", hour=6, minute=0,
                  misfire_grace_time=6 * 3600, coalesce=True)
    try:
        sched.start()
    except SystemExit:
        pass


if __name__ == "__main__":
    main()
