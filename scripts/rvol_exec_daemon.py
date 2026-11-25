#!/usr/bin/env python3
"""RVOL-EXEC paper lane daemon (spec §18). UNVALIDATED — PAPER ONLY.

A SEPARATE PROCESS from the options engine: its own IBKR client (clientId 61,
tag UNVALIDATED-RVOL-PAPER), its own books (data/lane_rvol/), its own ledger
(logs/observation/rvol_exec_paper.jsonl), its own process log
(logs/rvol_exec/<date>.log — never logs/agent.log). It never imports the
options engine's paper_trader / pipeline / scheduler / ibkr_orders /
ibkr_thread / order_client, never signals any other process, and refuses to
start under pytest.

    scripts/rvol_exec_daemon.py [--date YYYY-MM-DD] [--dry-run] [--port 4002] [--flatten-now]

SHADOW mechanism: if data/lane_rvol/SHADOW exists the session is FORCED into
--dry-run (snapshot, T_ready, rank, the would-be orders ledgered with
`dry_run: true`, ZERO placement) whatever the arguments say — the Tuesday
2026-09-15 SHADOW day runs with that file present so a loaded plist cannot
place. NOTE: a dry run still CONNECTS to the paper gateway on clientId 61
(foreign-held exclusion + quotes), so the gateway must be up and 61 free.

Exit codes: 0 clean (flat, session_closed) and every deterministic refusal
(weekend/holiday, HOLD/KILL at boot, prep stale, non-D account, breaker,
late); 2 unflattened; other non-zero = crash (launchd KeepAlive restarts).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.rvol_exec.paths as paths  # noqa: E402
from src.rvol_exec.constants import (CLIENT_ID, ET, HOST, LOOP_S, PORT, STAMP, T_HARD_STOP,  # noqa: E402
                                     T_DAEMON_START)

FORBIDDEN_MODULES = ("src.engine.paper_trader", "src.engine.pipeline", "src.scheduler",
                     "src.data.ibkr_orders", "src.data.ibkr_thread", "src.data.order_client")


def _refuse_under_pytest() -> None:
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        print(f"[{STAMP}] refusing to start the daemon under pytest", file=sys.stderr)
        raise SystemExit(3)


def _setup_logging(date_str: str) -> None:
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("rvol_exec")
    log.setLevel(logging.INFO)
    log.propagate = False                       # never reaches the 'trader' handler / agent.log
    if not any(isinstance(h, logging.FileHandler) for h in log.handlers):
        fh = logging.FileHandler(paths.LOG_DIR / f"{date_str}.log")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(sh)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _claim_pid_file() -> bool:
    p = paths.PID_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            other = int(p.read_text().strip() or 0)
        except ValueError:
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            print(f"[{STAMP}] pid file {p} held by live pid {other} — refusing", file=sys.stderr)
            return False
    p.write_text(str(os.getpid()))
    return True


def _release_pid_file() -> None:
    try:
        if paths.PID_FILE.exists() and paths.PID_FILE.read_text().strip() == str(os.getpid()):
            paths.PID_FILE.unlink()
    except OSError:
        pass


def _assert_isolation() -> None:
    bad = [m for m in FORBIDDEN_MODULES if m in sys.modules]
    if bad:
        raise SystemExit(f"[{STAMP}] isolation breach: forbidden modules imported: {bad}")


def shadow_forced() -> bool:
    """data/lane_rvol/SHADOW present -> dry_run is forced (never placement)."""
    try:
        return paths.SHADOW_FILE.exists()
    except OSError:
        return True                                 # unreadable = shadow (fail-closed)


def build_lane(date_str: str, port: int, dry_run: bool, clock=None, ib=None, polygon=None,
               force_live: bool = False):
    """`force_live` is used ONLY by --flatten-now: a flatten must not be stamped
    `dry_run: true` (the scorer would exclude the day) just because SHADOW is
    present — dry_run gates ENTRIES, a flatten places nothing new."""
    from src.data.ibkr_stock_orders import IbkrStockClient
    from src.rvol_exec.clock import SystemClock
    from src.rvol_exec.lane import Lane
    clock = clock or SystemClock()
    forced = shadow_forced() and not force_live
    dry_run = bool(dry_run) or forced
    if forced:
        logging.getLogger("rvol_exec").info("%s SHADOW file present (%s) -> dry_run FORCED", STAMP, paths.SHADOW_FILE)
    client = IbkrStockClient(host=HOST, port=port, client_id=CLIENT_ID, connect=False, clock=clock, ib=ib)
    if polygon is None:
        from src.rvol_exec.snapshot import PolygonSnapshotClient
        polygon = PolygonSnapshotClient()
    lane = Lane(date_str, clock, client, polygon, dry_run=dry_run)
    return lane, client


def flatten_now(date_str: str, port: int) -> int:
    """One-shot flatten when the daemon is NOT running (§11.9): same §8.6
    ordering through the lane's flatten phase; exit 0 flat / 2 not."""
    import json
    from src.util.atomic import atomic_save
    if paths.PID_FILE.exists():
        try:
            other = int(paths.PID_FILE.read_text().strip() or 0)
        except ValueError:
            other = 0
        if other and _pid_alive(other):
            print(f"[{STAMP}] daemon pid {other} is live — write data/lane_rvol/FLATTEN_NOW instead")
            return 2
    atomic_save(paths.FLATTEN_NOW_FILE, json.dumps({"reason": "manual --flatten-now",
                                                    "at": datetime.now(ET).isoformat(timespec="seconds")}))
    lane, client = build_lane(date_str, port, dry_run=False, force_live=True)
    try:
        code = lane.run(LOOP_S)
    finally:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            paths.FLATTEN_NOW_FILE.unlink()
        except OSError:
            pass
    return 0 if code == 0 else 2


def main(argv=None) -> int:
    _refuse_under_pytest()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=None, help="session date (default: today ET)")
    ap.add_argument("--dry-run", action="store_true", help="snapshot + rank + ledger, zero orders")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--flatten-now", action="store_true", help="one-shot flatten when the daemon is not running")
    a = ap.parse_args(argv)
    date_str = a.date or datetime.now(ET).strftime("%Y-%m-%d")
    _setup_logging(date_str)
    log = logging.getLogger("rvol_exec")
    log.info("%s daemon start date=%s dry_run=%s shadow_file=%s port=%s clientId=%s pid=%s", STAMP, date_str,
             a.dry_run, shadow_forced(), a.port, CLIENT_ID, os.getpid())
    if a.flatten_now:
        return flatten_now(date_str, a.port)
    if not _claim_pid_file():
        return 0
    try:
        lane, client = build_lane(date_str, a.port, a.dry_run)
        _assert_isolation()
        code = lane.run(LOOP_S)
        _assert_isolation()
        log.info("%s daemon exit code=%s phase=%s reason=%s", STAMP, code, lane.s.phase, lane.s.close_reason)
        return int(code)
    finally:
        try:
            client.disconnect()          # noqa: F821 — defined when build_lane succeeded
        except Exception:  # noqa: BLE001
            pass
        _release_pid_file()


if __name__ == "__main__":
    raise SystemExit(main())
