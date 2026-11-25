#!/usr/bin/env python3
"""RVOL-EXEC go/no-go (spec §17) — MANUAL ONLY, never scheduled.
UNVALIDATED — PAPER ONLY.

Emits `id|PASS|FAIL|NA|DEGRADED|detail` lines and a verdict; writes
logs/ops/rvol_exec_go_nogo-<date>.txt. On any FAIL it writes
data/lane_rvol/HOLD (and says so) unless --advisory. It never touches
data/SHADOW_DISABLE, data/KILL_SWITCH or anything under data/lane/.

Probes connect on clientId 161; 61 is used ONLY by r2a (the "is 61 free"
check). r0/r1/r6/r7(local part)/r8/r9 are offline and ALWAYS run. The broker
probes r2/r2a/r3/r4/r5 and r7's broker read run ONLY with an explicit
`--i-mean-it` (they connect to the paper gateway and r4 PLACES a tagged,
unfillable probe order); without it they report `NA|pass --i-mean-it`.
r10 (one Polygon call) likewise needs `--with-snapshot`.

    scripts/ops/rvol_exec_go_nogo.py [--advisory] [--i-mean-it] [--with-snapshot] [--arm]

`--arm` (needs `--i-mean-it`, RTH, D-account, no live daemon pid): the
one-time commissioning round trip (spec §17): 1-share MKT buy of a liquid
name, a far-away STP in an OCA group, a MKT flatten JOINING that group, then
a fresh connect on 61 — each a PASS/FAIL line `arm1..arm7`. It is the only
standing-script path that ever FILLS an order (2 shares round-tripped).

Isolation asserts (r0): data/lane_rvol is not under data/lane;
paper_trader.LANES == ("funnel", "scalp"); no lane module imports a forbidden
module (AST + subprocess sys.modules check); TAG != the options tag;
ibkr_go_nogo_checks.LIVE_FILES excludes lane files.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import src.rvol_exec.paths as paths  # noqa: E402
from src.rvol_exec.constants import (ET, GO_NOGO_CLIENT_ID, CLIENT_ID, HOST, OPTIONS_ENGINE_TAG,  # noqa: E402
                                     PARENT_TIF, PORT, SNAPSHOT_MIN_TICKERS, STAMP, TAG)

FORBIDDEN = ("src.engine.paper_trader", "src.engine.pipeline", "src.scheduler", "src.data.ibkr_orders",
             "src.data.ibkr_thread", "src.data.order_client")
LANE_SOURCES = ("src/rvol_exec", "src/data/ibkr_stock_orders.py", "scripts/rvol_exec_daemon.py",
                "scripts/rvol_exec_prep.py", "scripts/ops/rvol_exec_go_nogo.py")
LANE_MODULES = ("src.rvol_exec.lane", "src.rvol_exec.snapshot", "src.rvol_exec.prep", "src.rvol_exec.ledger",
                "src.rvol_exec.rules", "src.rvol_exec.state", "src.rvol_exec.intents", "src.rvol_exec.causal",
                "src.rvol_exec.liveness_rules", "src.rvol_exec.post_result", "src.data.ibkr_stock_orders")


class Report:
    def __init__(self):
        self.lines: list[tuple[str, str, str]] = []

    def emit(self, cid: str, status: str, detail: str) -> None:
        self.lines.append((cid, status, detail))
        print(f"{cid}|{status}|{detail}")

    @property
    def failed(self) -> list[str]:
        return [c for c, s, _ in self.lines if s == "FAIL"]


# ── r0 isolation (offline) ───────────────────────────────────────────────────
def lane_source_files(root: Path = ROOT) -> list[Path]:
    out = []
    for rel in LANE_SOURCES:
        p = root / rel
        if p.is_dir():
            out += sorted(p.glob("*.py"))
        elif p.exists():
            out.append(p)
    return out


def forbidden_direct_imports(root: Path = ROOT) -> list[str]:
    hits = []
    for f in lane_source_files(root):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError as exc:
            hits.append(f"{f}: syntax error {exc}")
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if any(n == m or n.startswith(m + ".") for m in FORBIDDEN):
                    hits.append(f"{f.relative_to(root)}: imports {n}")
    return hits


def forbidden_in_sys_modules(root: Path = ROOT, python: str | None = None) -> list[str]:
    code = ("import sys, importlib\n"
            f"for m in {LANE_MODULES!r}:\n    importlib.import_module(m)\n"
            f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]\n"
            "print(','.join(bad))\n")
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    out = subprocess.run([python or sys.executable, "-c", code], cwd=str(root), capture_output=True,
                         text=True, timeout=120, env=env)
    if out.returncode != 0:
        return [f"import check failed: {out.stderr.strip()[-300:]}"]
    bad = out.stdout.strip()
    return bad.split(",") if bad else []


def check_r0(rep: Report, root: Path = ROOT) -> None:
    problems = []
    lane_dir = paths.LANE_DIR.resolve()
    if lane_dir.is_relative_to((root / "data" / "lane").resolve()):
        problems.append("data/lane_rvol is under data/lane")
    try:
        from src.engine import paper_trader as pt                 # the go/no-go MAY import it; the lane may not
        if tuple(pt.LANES) != ("funnel", "scalp"):
            problems.append(f"paper_trader.LANES changed: {pt.LANES!r}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"paper_trader import failed: {exc}")
    if TAG == OPTIONS_ENGINE_TAG:
        problems.append("lane tag equals the options tag")
    try:
        from scripts.ops import ibkr_go_nogo_checks as chk
        if any("lane_rvol" in str(p) for p in chk.LIVE_FILES):
            problems.append("LIVE_FILES includes a lane file")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"ibkr_go_nogo_checks import failed: {exc}")
    problems += forbidden_direct_imports(root)
    problems += [f"loaded transitively: {m}" for m in forbidden_in_sys_modules(root)]
    if problems:
        rep.emit("r0", "FAIL", "; ".join(problems)[:400])
    else:
        rep.emit("r0", "PASS", f"lane_dir={lane_dir.name} LANES unchanged; no forbidden import in "
                               f"{len(lane_source_files(root))} lane files (AST + subprocess)")


# ── r1 prep fresh ────────────────────────────────────────────────────────────
def check_r1(rep: Report, today: str) -> None:
    from src.rvol_exec.prep import freshness, read_meta
    ok, why = freshness(read_meta(paths.PREP_DIR, today), today)
    rep.emit("r1", "PASS" if ok else "FAIL", f"prep {today}: {why}")


# ── r2 / r2a gateway ─────────────────────────────────────────────────────────
def _client(client_id: int, ib=None):
    from src.data.ibkr_stock_orders import IbkrStockClient
    return IbkrStockClient(host=HOST, port=PORT, client_id=client_id, ib=ib, connect=True)


def check_r2(rep: Report, ib_factory=None) -> object | None:
    try:
        c = _client(GO_NOGO_CLIENT_ID, ib=ib_factory() if ib_factory else None)
    except Exception as exc:  # noqa: BLE001
        rep.emit("r2", "FAIL", f"client construction failed: {exc}")
        return None
    if not c.available():
        rep.emit("r2", "FAIL", f"gateway unavailable on clientId {GO_NOGO_CLIENT_ID} "
                               f"({getattr(c, 'last_connect_failure', None)})")
        return None
    acct = str(c.account or "")
    if not acct.startswith("D"):
        rep.emit("r2", "FAIL", f"account {acct!r} is not a paper D account")
        c.disconnect()
        return None
    rep.emit("r2", "PASS", f"connected on {GO_NOGO_CLIENT_ID}, paper account {acct}")
    return c


def check_r2a(rep: Report, ib_factory=None) -> None:
    pid = _pid_from_file()
    if pid and _pid_alive(pid):
        rep.emit("r2a", "NA", f"daemon pid {pid} is live and holds clientId {CLIENT_ID}")
        return
    try:
        c = _client(CLIENT_ID, ib=ib_factory() if ib_factory else None)
    except Exception as exc:  # noqa: BLE001
        rep.emit("r2a", "FAIL", f"clientId {CLIENT_ID} probe errored: {exc}")
        return
    if c.available():
        c.disconnect()
        rep.emit("r2a", "PASS", f"clientId {CLIENT_ID} free (connected and released)")
    else:
        rep.emit("r2a", "FAIL", f"clientId {CLIENT_ID} NOT connectable "
                                f"({getattr(c, 'last_connect_failure', None)}) — phantom session?")


# ── r3 quotes, r4 round-trip, r5 shortable ───────────────────────────────────
def check_r3(rep: Report, c, syms=("IBM", "AAPL"), wait_s: float = 10.0) -> None:
    if c is None:
        rep.emit("r3", "NA", "no client")
        return
    got = {}
    c.subscribe_quotes(list(syms))
    deadline = c._clock.now_utc().timestamp() + wait_s
    while c._clock.now_utc().timestamp() < deadline:
        for s in syms:
            q = c.last(s)
            if q and (q.get("last") or (q.get("bid") and q.get("ask"))):
                got[s] = q
        if len(got) == len(syms):
            break
        try:
            c._ib.sleep(0.5)
        except Exception:  # noqa: BLE001
            break
    c.unsubscribe_all()
    missing = [s for s in syms if s not in got]
    if missing:
        rep.emit("r3", "DEGRADED", f"no live STK quote for {missing} (11.10 fallbacks apply): "
                                   f"{ {s: round(v['mark'], 2) for s, v in got.items()} }")
    else:
        rep.emit("r3", "PASS", f"quotes: { {s: round(v['mark'], 2) for s, v in got.items()} }")


def check_r4(rep: Report, c, sym: str = "IBM", pct_above: float = 0.08) -> None:
    """Unfillable STP-LMT BUY + attached STP child, our tag; visible by tag;
    cancelled in a finally. NEVER fillable."""
    if c is None:
        rep.emit("r4", "NA", "no client")
        return
    parent_id = None
    try:
        c.subscribe_quotes([sym])
        q = c.last(sym)
        ref = float(q["mark"]) if q and q.get("mark") else None
        if ref is None:
            try:
                ref = float(getattr(c._ib, "marks", {}).get(sym) or 0) or None
            except Exception:  # noqa: BLE001
                ref = None
        if not ref:
            rep.emit("r4", "NA", f"no reference price for {sym} (r3 degraded) — round-trip skipped")
            return
        level = round(ref * (1 + pct_above), 2)
        gtd = c._clock.now_et().replace(microsecond=0)
        from datetime import timedelta
        gtd = gtd + timedelta(minutes=2)
        env = c.place_stop_limit_entry_with_stop_child(sym, "long", 1, level, round(level + 0.05, 2),
                                                       round(level - 1.0, 2), f"RVXGO-{sym}", tif=PARENT_TIF,
                                                       gtd_et=gtd)
        if not env or "order" not in env or env["order"].get("status") == "rejected":
            rep.emit("r4", "FAIL", f"probe order rejected/none: {env}")
            return
        parent_id, child_id = env["order"]["parent_id"], env["order"]["child_id"]
        opened = c.get_own_orders(open_only=True) or []
        ids = {o["id"] for o in opened}
        linked = any(o["id"] == child_id and int(o["parent_id"] or 0) == parent_id for o in opened)
        if parent_id in ids and child_id in ids and linked:
            rep.emit("r4", "PASS", f"parent {parent_id} + child {child_id} visible by tag, linked, tif={PARENT_TIF}")
        else:
            rep.emit("r4", "FAIL", f"probe not visible by tag/linked: open={sorted(ids)} parent={parent_id} child={child_id}")
    except Exception as exc:  # noqa: BLE001
        rep.emit("r4", "FAIL", f"probe errored: {exc}")
    finally:
        if parent_id is not None:
            try:
                c.cancel(parent_id)
                left = [o for o in (c.get_own_orders(open_only=True) or []) if o["symbol"] == sym]
                for o in left:
                    c.cancel(o["id"])
                left = [o for o in (c.get_own_orders(open_only=True) or []) if o["symbol"] == sym]
                if left:
                    rep.emit("r4x", "FAIL", f"probe orders still open after cancel: {[o['id'] for o in left]}")
                else:
                    rep.emit("r4x", "PASS", "probe orders cancelled and absent from the open list")
            except Exception as exc:  # noqa: BLE001
                rep.emit("r4x", "FAIL", f"cancel in finally errored: {exc}")
        try:
            c.unsubscribe_all()
        except Exception:  # noqa: BLE001
            pass


def check_r5(rep: Report, c, syms=("IBM", "AAPL")) -> None:
    if c is None:
        rep.emit("r5", "NA", "no client")
        return
    t = c._clock.now_et().timetz().replace(tzinfo=None)
    if not (datetime.strptime("09:30", "%H:%M").time() <= t <= datetime.strptime("16:00", "%H:%M").time()):
        rep.emit("r5", "NA", "outside RTH")
        return
    got = {}
    for s in syms:
        contract = c.qualify(s)
        if contract is None:
            continue
        try:
            tk = c._ib.reqMktData(contract, "236", False, False)
            c._ib.sleep(2)
            val = getattr(tk, "shortableShares", None)
            sh = getattr(tk, "shortable", None)
            got[s] = (sh, val)
            c._ib.cancelMktData(contract)
        except Exception as exc:  # noqa: BLE001
            got[s] = f"err:{exc}"
    ok = any(isinstance(v, tuple) and (v[0] is not None or v[1] is not None) for v in got.values())
    rep.emit("r5", "PASS" if ok else "FAIL", f"generic tick 236: {got}")


# ── r6 flags, r7 overnight, r8 pid, r9 clock, r10 snapshot ───────────────────
def check_r6(rep: Report) -> None:
    from src.rvol_exec.lane import default_kill_switch_reader
    flags = {"KILL_SWITCH": default_kill_switch_reader(), "HOLD": paths.HOLD_FILE.exists(),
             "FLATTEN_NOW": paths.FLATTEN_NOW_FILE.exists(), "UNFLATTENED": paths.UNFLATTENED_FILE.exists()}
    rep.emit("r6", "PASS", json.dumps(flags))


def check_r7(rep: Report, c, today: str) -> None:
    problems = []
    if paths.UNFLATTENED_FILE.exists():
        problems.append("UNFLATTENED marker present")
    from src.rvol_exec.prep import previous_trading_day
    from datetime import date
    prev = previous_trading_day(date.fromisoformat(today)).isoformat()
    sdir = paths.session_dir(prev)
    st = None
    try:
        if (sdir / "state.json").exists():
            st = json.loads((sdir / "state.json").read_text())
    except Exception:  # noqa: BLE001
        st = None
    if st is not None:
        open_names = [s for s, n in (st.get("names") or {}).items() if float(n.get("held_qty") or 0) > 0]
        if open_names:
            problems.append(f"yesterday's state shows held {open_names}")
    try:
        from src.util.atomic import read_jsonl
        closed = [r for r in read_jsonl(paths.LEDGER) if r.get("date") == prev and r.get("event") == "session_closed"]
        skipped = [r for r in read_jsonl(paths.LEDGER) if r.get("date") == prev
                   and r.get("event") in ("session_skipped", "session_noop")]
        if st is not None and not closed and not skipped:
            problems.append("yesterday has a session dir but no session_closed")
        if closed and closed[-1].get("unflattened"):
            problems.append("yesterday closed unflattened")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"ledger read failed: {exc}")
    if c is not None:
        own = c.get_own_orders(open_only=True)
        if own is None:
            problems.append("own open orders read failed (None)")
        elif own:
            problems.append(f"own open orders at the broker: {[o['id'] for o in own]}")
    rep.emit("r7", "FAIL" if problems else "PASS", "; ".join(problems) if problems else "no overnight own position/order, no marker")


def _pid_from_file() -> int | None:
    try:
        return int(paths.PID_FILE.read_text().strip() or 0) or None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def check_r8(rep: Report) -> None:
    pid = _pid_from_file()
    if pid and _pid_alive(pid):
        rep.emit("r8", "FAIL", f"lane daemon running (pid {pid})")
    else:
        rep.emit("r8", "PASS", "no live lane pid" + (f" (stale pid {pid})" if pid else ""))


def check_r9(rep: Report) -> None:
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)
    t935 = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    from zoneinfo import ZoneInfo
    pt = t935.astimezone(ZoneInfo("America/Los_Angeles"))
    dst = bool(now_et.dst())
    ok = pt.hour == 6 and pt.minute == 35
    rep.emit("r9", "PASS" if ok else "FAIL", f"now_et={now_et.isoformat(timespec='seconds')} 09:35ET->{pt.strftime('%H:%M')}PT dst={dst}")


def check_r10(rep: Report, polygon=None) -> None:
    try:
        if polygon is None:
            from src.rvol_exec.snapshot import PolygonSnapshotClient
            polygon = PolygonSnapshotClient()
        snap = polygon.snapshot_all()
    except Exception as exc:  # noqa: BLE001
        rep.emit("r10", "FAIL", f"snapshot errored: {exc}")
        return
    if not snap or "tickers" not in snap:
        rep.emit("r10", "FAIL", f"snapshot error: {(snap or {}).get('error')}")
        return
    from src.rvol_exec.snapshot import parse_snapshot
    m = parse_snapshot(snap, [], datetime.now(timezone.utc))
    ok = m["n_tickers"] >= SNAPSHOT_MIN_TICKERS
    rep.emit("r10", "PASS" if ok else "FAIL", f"tickers={m['n_tickers']} implied_delay_s={m['delay_s']} realtime={m['realtime']}")


# ── orchestration ────────────────────────────────────────────────────────────
def run_checks(advisory: bool = False, with_snapshot: bool = False, offline: bool = False,
               today: str | None = None, ib_factory=None, polygon=None, write_report: bool = True,
               broker: bool = False) -> Report:
    """`broker=True` (CLI `--i-mean-it`) is the ONLY way the gateway probes run;
    the default never constructs a client, never connects, never places."""
    rep = Report()
    today = today or datetime.now(ET).strftime("%Y-%m-%d")
    check_r0(rep)
    check_r1(rep, today)
    c = None
    if broker and not offline:
        c = check_r2(rep, ib_factory)
        check_r2a(rep, ib_factory)
        check_r3(rep, c)
        check_r4(rep, c)
        check_r5(rep, c)
    else:
        for cid in ("r2", "r2a", "r3", "r4", "r5"):
            rep.emit(cid, "NA", "offline" if offline else "pass --i-mean-it to run the gateway probes")
    check_r6(rep)
    check_r7(rep, c, today)
    check_r8(rep)
    check_r9(rep)
    if with_snapshot:
        check_r10(rep, polygon)
    else:
        rep.emit("r10", "NA", "pass --with-snapshot for one live call")
    if c is not None:
        try:
            c.disconnect()
        except Exception:  # noqa: BLE001
            pass
    failed = rep.failed
    verdict = "NO-GO" if failed else "GO"
    print(f"verdict|{verdict}|{'failed: ' + ','.join(failed) if failed else 'all PASS/NA'}|{STAMP}")
    if failed and not advisory:
        try:
            paths.HOLD_FILE.parent.mkdir(parents=True, exist_ok=True)
            paths.HOLD_FILE.write_text(json.dumps({"reason": f"go_nogo_fail:{','.join(failed)}",
                                                   "source": "rvol_exec_go_nogo", "at": datetime.now(ET).isoformat()}))
            print(f"hold|WRITTEN|{paths.HOLD_FILE}")
        except Exception as exc:  # noqa: BLE001
            print(f"hold|ERROR|{exc}")
    elif failed:
        print("hold|ADVISORY|not written")
    if write_report:
        try:
            paths.GO_NOGO_LOG_DIR.mkdir(parents=True, exist_ok=True)
            out = paths.GO_NOGO_LOG_DIR / f"rvol_exec_go_nogo-{today}.txt"
            out.write_text("\n".join(f"{c}|{s}|{d}" for c, s, d in rep.lines) + f"\nverdict|{verdict}|{STAMP}\n")
        except Exception as exc:  # noqa: BLE001
            print(f"report|ERROR|{exc}")
    return rep


# ── bounded run: a hung probe must still produce NO-GO (fail closed) ─────────
# 2026-10-05: the port ACCEPTED and then stalled, so the post-connect waits in
# r3/r4/r5 hung ~15 h 21 m and the verdict was written only when the gateway
# came back. An absent gateway (ECONNREFUSED) fails fast and does NOT reproduce
# it. So the bound belongs around the whole check sequence, not around connect.
GO_NOGO_TIMEOUT_S = 300.0


def write_timeout_verdict(today: str, advisory: bool, timeout_s: float,
                          write_report: bool = True) -> str:
    """Write the fail-closed NO-GO for a run that never came back.

    Mirrors run_checks' own verdict side effects exactly: the dated report
    always, HOLD only when not advisory. Returns the verdict line.
    """
    detail = f"timeout after {timeout_s:g}s (probe did not return)"
    verdict = f"NO-GO|{detail}"
    print(f"verdict|NO-GO|{detail}|{STAMP}")
    if not advisory:
        try:
            paths.HOLD_FILE.parent.mkdir(parents=True, exist_ok=True)
            paths.HOLD_FILE.write_text(json.dumps({"reason": f"go_nogo_timeout:{timeout_s:g}s",
                                                   "source": "rvol_exec_go_nogo", "at": datetime.now(ET).isoformat()}))
            print(f"hold|WRITTEN|{paths.HOLD_FILE}")
        except Exception as exc:  # noqa: BLE001
            print(f"hold|ERROR|{exc}")
    else:
        print("hold|ADVISORY|not written")
    if write_report:
        try:
            paths.GO_NOGO_LOG_DIR.mkdir(parents=True, exist_ok=True)
            out = paths.GO_NOGO_LOG_DIR / f"rvol_exec_go_nogo-{today}.txt"
            out.write_text(f"timeout|FAIL|{detail}\nverdict|NO-GO|{STAMP}\n")
        except Exception as exc:  # noqa: BLE001
            print(f"report|ERROR|{exc}")
    return verdict


def run_checks_bounded(timeout_s: float = GO_NOGO_TIMEOUT_S, **kw) -> Report | None:
    """run_checks under a wall-clock bound.

    Returns the Report on completion, or None when the bound expired — in which
    case the fail-closed NO-GO has already been written. The probe runs in a
    daemon thread on purpose: a stalled IB wait is not reliably interruptible
    from Python, so we abandon it rather than pretend we can cancel it. The
    CLI must therefore force-exit instead of returning normally.
    """
    box: dict[str, Report] = {}

    def _run() -> None:
        try:
            box["rep"] = run_checks(**kw)
        except Exception as exc:  # noqa: BLE001
            print(f"checks|ERROR|{exc}")

    t = threading.Thread(target=_run, name="go_nogo_checks", daemon=True)
    t.start()
    t.join(timeout_s)
    if "rep" in box:
        return box["rep"]
    today = kw.get("today") or datetime.now(ET).strftime("%Y-%m-%d")
    write_timeout_verdict(today, bool(kw.get("advisory")), timeout_s,
                          write_report=bool(kw.get("write_report", True)))
    return None


# ── --arm: one-time commissioning round trip (§17) ───────────────────────────
ARM_SYMBOL = "F"                         # liquid, ~$10: a 1-share round trip costs cents


def _wait_execs(c, sym: str, n: int, wait_s: float = 15.0) -> list[dict]:
    deadline = c._clock.now_utc().timestamp() + wait_s
    while True:
        ex = [e for e in (c.get_own_executions() or []) if e.get("symbol") == sym]
        if len(ex) >= n or c._clock.now_utc().timestamp() >= deadline:
            return ex
        try:
            c._ib.sleep(0.5)
        except Exception:  # noqa: BLE001
            return ex


def run_arm(rep: Report, ib_factory=None, sym: str = ARM_SYMBOL, rth_check: bool = True) -> None:
    """arm1 connect on 61 (D account, pid absent/dead) · arm2 MKT buy 1 fills with
    orderRef == TAG · arm3 far STP in OCA group accepted · arm4 MKT flatten joins
    the group, fills, the STP is Cancelled · arm5 own positions show the round
    trip and nothing else · arm6 fresh connect on 61 -> executions carry the tag
    · arm7 partial-parent child quantity: NA (cannot be forced live; recorded
    from the first live partial as `stop_requantified`). Everything opened is
    closed in a finally."""
    from datetime import time as _time
    pid = _pid_from_file()
    if pid and _pid_alive(pid):
        rep.emit("arm1", "FAIL", f"daemon pid {pid} is live on clientId {CLIENT_ID} — never arm beside a running lane")
        return
    try:
        c = _client(CLIENT_ID, ib=ib_factory() if ib_factory else None)
    except Exception as exc:  # noqa: BLE001
        rep.emit("arm1", "FAIL", f"client construction failed: {exc}")
        return
    if not c.available() or not str(c.account or "").startswith("D"):
        rep.emit("arm1", "FAIL", f"not connected on {CLIENT_ID} to a D account "
                                 f"({getattr(c, 'last_connect_failure', None)}, account={c.account!r})")
        return
    t = c._clock.now_et().timetz().replace(tzinfo=None)
    if rth_check and not (_time(9, 35) <= t <= _time(15, 45)):
        rep.emit("arm1", "NA", f"outside RTH ({t}) — the round trip needs a fillable market")
        c.disconnect()
        return
    rep.emit("arm1", "PASS", f"connected on {CLIENT_ID}, paper account {c.account}, pid file absent/dead")
    oca = f"RVXARM-{sym}-{c._clock.now_et().strftime('%H%M%S')}"
    child_id = None
    try:
        contract = c.qualify(sym)
        if contract is None:
            rep.emit("arm2", "FAIL", f"{sym} does not qualify")
            return
        buy = c._place_mkt(contract, "BUY", 1, "")
        if "error" in buy or not buy.get("order"):
            rep.emit("arm2", "FAIL", f"MKT buy rejected: {buy}")
            return
        ex = _wait_execs(c, sym, 1)
        buys = [e for e in ex if e.get("side") == "BOT"]
        if buys and all(e.get("tag") == TAG for e in buys):
            rep.emit("arm2", "PASS", f"MKT buy 1 {sym} filled @ {buys[-1]['price']} orderRef == {TAG}")
        else:
            rep.emit("arm2", "FAIL", f"no tagged buy execution within 15 s: {ex}")
            return
        ref = float(buys[-1]["price"])
        stp = c.place_stop_order(sym, "SELL", 1, round(ref * 0.5, 2), oca)
        if not stp or "error" in stp or stp.get("order", {}).get("status") == "rejected":
            rep.emit("arm3", "FAIL", f"standalone STP in OCA group rejected: {stp}")
        else:
            child_id = int(stp["order"]["id"])
            rep.emit("arm3", "PASS", f"STP SELL 1 @ {round(ref * 0.5, 2)} accepted in OCA {oca} (id {child_id})")
        flat = c.flatten_market(sym, 1, "long", oca if child_id is not None else None)
        if not flat or "error" in flat or not flat.get("order"):
            rep.emit("arm4", "FAIL", f"flatten MKT rejected: {flat}")
        else:
            ex = _wait_execs(c, sym, 2)
            sells = [e for e in ex if e.get("side") == "SLD"]
            child_state = None
            if child_id is not None:
                for _ in range(20):                       # OCA_VERIFY_S at 0.5 s
                    st = c.order_status(child_id) or {}
                    child_state = st.get("status")
                    if child_state in ("canceled", "filled", "rejected"):
                        break
                    try:
                        c._ib.sleep(0.5)
                    except Exception:  # noqa: BLE001
                        break
            ok = bool(sells) and flat.get("mode") == "oca" and (child_id is None or child_state == "canceled")
            rep.emit("arm4", "PASS" if ok else "FAIL",
                     f"flatten mode={flat.get('mode')} sells={len(sells)} child={child_state} "
                     f"(OCA-join {'accepted, fill cancelled the STP' if ok else 'NOT proven'})")
        pos = c.get_own_positions()
        if pos is None:
            rep.emit("arm5", "FAIL", "own positions read failed")
        else:
            others = {k: v for k, v in pos.items() if k != sym and abs(float(v.get("qty") or 0)) > 1e-9}
            net = float((pos.get(sym) or {}).get("qty") or 0)
            rep.emit("arm5", "PASS" if not others and abs(net) < 1e-9 else "FAIL",
                     f"{sym} net {net}; other own symbols: {sorted(others) or 'none'}")
        c.disconnect()
        # a real gateway can hold the clientId for a few seconds after a disconnect (326):
        # bounded retry so arm6 measures the tag on recovered executions, not the gateway's grace period
        c2 = None
        for _ in range(3):
            c2 = _client(CLIENT_ID, ib=ib_factory() if ib_factory else None)
            if c2.available():
                break
            try:
                c2._ib.sleep(2)
            except Exception:  # noqa: BLE001
                pass
        ex2 = [e for e in (c2.get_own_executions() or []) if e.get("symbol") == sym] if c2.available() else []
        if c2.available() and len(ex2) >= 2 and all(e.get("tag") == TAG for e in ex2):
            rep.emit("arm6", "PASS", f"fresh connect on {CLIENT_ID}: {len(ex2)} executions carry orderRef == {TAG}")
        else:
            rep.emit("arm6", "FAIL", f"fresh connect: available={c2.available()} tagged executions={len(ex2)}")
        c = c2
        rep.emit("arm7", "NA", "partial-parent child quantity cannot be forced live; recorded from the first live "
                               "partial (`stop_requantified` / `partial_fill` ledger lines)")
    except Exception as exc:  # noqa: BLE001
        rep.emit("armx", "FAIL", f"arm errored: {exc}")
    finally:
        try:
            if c.available():
                for o in (c.get_own_orders(open_only=True) or []):
                    if o.get("symbol") == sym:
                        c.cancel(o["id"])
                pos = c.get_own_positions() or {}
                net = float((pos.get(sym) or {}).get("qty") or 0)
                if abs(net) > 1e-9:
                    r = c.flatten_market(sym, int(abs(net)), "long" if net > 0 else "short", None)
                    rep.emit("armx", "FAIL", f"residual {sym} net {net} flattened in finally: {r}")
                left = [o["id"] for o in (c.get_own_orders(open_only=True) or []) if o.get("symbol") == sym]
                rep.emit("arm_cleanup", "PASS" if not left else "FAIL",
                         "no own open order on the arm symbol" if not left else f"open: {left}")
                c.disconnect()
        except Exception as exc:  # noqa: BLE001
            rep.emit("arm_cleanup", "FAIL", f"cleanup errored: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--advisory", action="store_true", help="report only; never write HOLD")
    ap.add_argument("--i-mean-it", dest="i_mean_it", action="store_true",
                    help="run the gateway probes r2/r2a/r3/r4/r5 + r7's broker read (connects; r4 places a probe)")
    ap.add_argument("--with-snapshot", action="store_true", help="one Polygon snapshot call (r10)")
    ap.add_argument("--offline", action="store_true", help="explicitly offline (same as the default; kept for scripts)")
    ap.add_argument("--arm", action="store_true", help="one-time commissioning round trip (needs --i-mean-it, RTH)")
    ap.add_argument("--date", default=None)
    ap.add_argument("--timeout", type=float, default=GO_NOGO_TIMEOUT_S,
                    help="wall-clock bound on the check sequence; on expiry write NO-GO and exit 2")
    a = ap.parse_args(argv)
    if "PYTEST_CURRENT_TEST" in os.environ:
        print("refusing to run the go/no-go CLI under pytest")
        return 3
    if a.arm:
        if not a.i_mean_it:
            print(f"arm|NA|--arm places and FILLS 2 shares on the paper account: pass --i-mean-it|{STAMP}")
            return 3
        rep = Report()
        run_arm(rep)
        failed = rep.failed
        print(f"arm_verdict|{'FAIL' if failed else 'PASS'}|{','.join(failed) if failed else 'record arm1-arm7 in STATE.md'}|{STAMP}")
        try:
            paths.GO_NOGO_LOG_DIR.mkdir(parents=True, exist_ok=True)
            out = paths.GO_NOGO_LOG_DIR / f"rvol_exec_arm-{datetime.now(ET).strftime('%Y-%m-%d')}.txt"
            out.write_text("\n".join(f"{c}|{s}|{d}" for c, s, d in rep.lines) + f"\n{STAMP}\n")
        except Exception as exc:  # noqa: BLE001
            print(f"report|ERROR|{exc}")
        return 1 if failed else 0
    rep = run_checks_bounded(timeout_s=a.timeout, advisory=a.advisory, with_snapshot=a.with_snapshot,
                             offline=a.offline, today=a.date, broker=a.i_mean_it)
    if rep is None:
        # The abandoned probe thread may never return; a normal exit would wait
        # on nothing but is not worth the risk of a non-daemon dependency.
        sys.stdout.flush()
        os._exit(2)
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
