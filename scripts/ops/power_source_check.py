#!/usr/bin/env python3
"""Flag that the Mac is AWAKE ON BATTERY during a trading-day window.

Why this exists (queue item 2026-10-29 post_close): the host sleeping is now a
twice-proven way to lose ticks. On 2026-10-22 it slept at 07:53:31 (`pmset -g
log`: *initiating sleep 'Clamshell Sleep' ... Using Batt (Charge:84%)*) and cost
5 gaps / 8 RTH ticks; on 2026-10-29 it slept 18 times for **67 lost ticks**
(60 pre-open, 7 RTH) and went straight through the 06:30 open in a single 30.4
min gap. Both days the host was on BATTERY, and `caffeinate -s` — which the
engine runs under — is **void on battery by design**, so the assertions are
listed and the host sleeps anyway.

Nothing detected it. `grep -rln "pmset|Battery Power|on battery"` over
`scripts/ src/ deploy/` returned nothing on 2026-10-29, and both `PLUG IN` pages
that morning were sent **by hand** by an autopilot run that then died mid-
response *from the host sleeping*. Detector and fault shared one point of
failure.

**Known limit, stated so this is not mistaken for a fix: a deadman layer cannot
fire while the host is asleep.** This catches the *awake-on-battery* state — the
warning BEFORE the next sleep. The lid-close sleep itself, and the power policy
that would prevent it (`pmset -b` as root, or lid-open-on-AC), stay operator-only.

Charge % is deliberately NOT a condition. The 10-22 sleep initiated at 84% and
the 10-26 read was 100% with ~10:55 remaining — the hazard is *sleep*, not
drain, so any battery reading inside the window is a breach. The % and the
estimate are reported so the operator can judge urgency.

Window: weekdays 05:00-13:30 PT by default. 05:00 is ~90 min of plug-in lead
before the 06:30 open — today's damage concentrated BEFORE the open (60 of 67
ticks), so a session-only window would have watched the wrong hours. No holiday
calendar, matching `scripts/ops/liveness.py:session_open`.

Read-only by construction: it runs `pmset -g batt` and prints. It never changes
a power setting.

    python3 scripts/ops/power_source_check.py
    python3 scripts/ops/power_source_check.py --json
    python3 scripts/ops/power_source_check.py --pmset-file fixture.txt --now 2026-10-29T10:00
    python3 scripts/ops/power_source_check.py --pmset-log saved_pmset_log.txt

TRANSITION BRACKET (added 2026-11-12, queue item "the nightly report states power
durations it never measured"). `pmset -g batt` is point-in-time only: it cannot
say how long the host has been unplugged. The 2026-11-09 nightly report therefore
*inferred* "~12 h off AC since the 06:19:48 page" when `pmset -g log` dates the
host on AC for all of RTH. So this tool now also reads `pmset -g log` (a DIFFERENT
input from `--pmset-file`, which is `pmset -g batt` text) and reports a BRACKET:

    last_seen_ac            latest `Using AC` observation at or before --now
    first_seen_on_battery   earliest `Using Batt` observation after it
    basis                   "measured-bracket" only when BOTH ends exist, else "unknown"

**It is a bracket, not a duration, and the field names say so.** Those lines are
sparse — 2026-11-11 carried two `Using AC` lines and ZERO `Using Batt` across 13 h
— so the first observation of a stint is an UPPER BOUND on when that stint began,
never the transition itself. A consumer may quote the two timestamps; it may NOT
subtract them and call the result "time on battery". When the ring buffer does not
reach back far enough, `basis` is `unknown` and both fields are null: say unknown,
never infer.

The bracket is REPORTING ONLY. Exit codes and the breach condition are unchanged —
a failed, empty or unparseable `pmset -g log` degrades to `basis: unknown` and can
never turn a clean read into a breach nor a breach into exit 1.

Exit codes: 0 = clean (on AC, or on battery outside the window), 3 = BREACH (on
battery inside the window), 1 = could not measure (unparseable/absent `pmset`).
3 is distinct from 1 on purpose — a caller must never read "I could not measure"
as "all clear".
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")

# Window defaults, in PT minutes-of-day. See the module docstring for why this
# starts well before the 06:30 open rather than tracking session_open().
DEFAULT_START = "05:00"
DEFAULT_END = "13:30"

# `pmset -g batt` line 1 is "Now drawing from 'AC Power'" / "'Battery Power'".
_SOURCE_RE = re.compile(r"Now drawing from '([^']+)'")
# Line 2, when a battery is present: "-InternalBattery-0 (id=...)\t42%; discharging; 4:18 remaining present: true"
_PCT_RE = re.compile(r"(\d{1,3})%")
_STATE_RE = re.compile(r"%;\s*([a-zA-Z ]+?)\s*;")
_REMAIN_RE = re.compile(r"(\d+:\d{2})\s+remaining")


def _run(cmd: list[str]) -> str:
    """Run a read-only command, returning stdout ('' on failure)."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout or ""


def read_pmset(runner=_run) -> str:
    return runner(["pmset", "-g", "batt"])


def parse_pmset(text: str) -> dict | None:
    """-> {source, on_battery, pct, state, remaining} or None if unparseable.

    None means "could not measure" and maps to exit 1. A source string we do not
    recognise (e.g. 'UPS Power') is unparseable ON PURPOSE: guessing whether a
    UPS counts as mains is exactly the kind of silent wrong answer this tool
    exists to avoid.
    """
    m = _SOURCE_RE.search(text or "")
    if not m:
        return None
    source = m.group(1).strip()
    low = source.lower()
    if "ac power" in low:
        on_battery = False
    elif "battery power" in low:
        on_battery = True
    else:
        return None

    pct = state = remaining = None
    for line in (text or "").splitlines():
        if "InternalBattery" not in line:
            continue
        pm = _PCT_RE.search(line)
        if pm:
            pct = int(pm.group(1))
        sm = _STATE_RE.search(line)
        if sm:
            state = sm.group(1).strip()
        rm = _REMAIN_RE.search(line)
        if rm:
            remaining = rm.group(1)
        break
    return {
        "source": source,
        "on_battery": on_battery,
        "pct": pct,
        "state": state,
        "remaining": remaining,
    }


# `pmset -g log` carries the power source on many line types, not only sleep
# entries — 2026-11-12's observations were all `Assertions Summary-` lines:
#   2026-11-12 09:50:15 -0800 Assertions  Summary- [...] Using Batt(Charge: 100)
#   2026-10-22 07:53:31 -0800 Sleep       Clamshell Sleep ... Using Batt (Charge:84%)
# Both spacings and both charge formats must parse, so the charge group is not
# captured at all: only the timestamp and AC-vs-Batt matter here.
_LOG_OBS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?\bUsing (AC|Batt)\b"
)
BASIS_MEASURED = "measured-bracket"
BASIS_UNKNOWN = "unknown"


def read_host_pmset_log(timeout_seconds: float = 20.0) -> str:
    """`pmset -g log` from this host, or "" if it cannot be read.

    Mirrors `scripts/ops/tick_gap_count.py:read_host_pmset_log`. "" is a normal
    degraded answer, never an error: the caller maps it to `basis: unknown`.
    """
    try:
        out = subprocess.run(
            ["pmset", "-g", "log"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout or ""


def parse_power_observations(text: str) -> list[tuple[datetime, bool]]:
    """-> [(naive PT timestamp, on_battery)] for every `Using AC|Batt` line.

    Naive on purpose: `pmset -g log` prints host-local wall time with a fixed
    offset, and every comparison here is against `--now` in the same local
    frame. Unparseable lines are skipped silently — a log the host wrote in a
    shape we do not know must degrade, not crash.
    """
    obs: list[tuple[datetime, bool]] = []
    for line in (text or "").splitlines():
        m = _LOG_OBS_RE.match(line.strip())
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        obs.append((ts, m.group(2) == "Batt"))
    obs.sort(key=lambda t: t[0])
    return obs


def power_bracket(
    observations: list[tuple[datetime, bool]], now: datetime, on_battery: bool
) -> dict:
    """Bracket the CURRENT stint from sparse observations. Never a duration.

    `basis` is `measured-bracket` only when both ends of the current stint are
    observed; anything else is `unknown` with null fields. Specifically unknown:
    an empty/unreadable log, a ring buffer that no longer reaches the previous
    AC stint, and the sparse case where the host reads battery from `pmset -g
    batt` but the log holds no `Using Batt` line yet (2026-11-11 in miniature).
    """
    unknown = {
        "last_seen_ac": None,
        "first_seen_on_battery": None,
        "basis": BASIS_UNKNOWN,
    }
    if not on_battery:
        # On AC there is no battery stint to bracket. Reporting the last AC
        # observation would invite reading it as "plugged in at", which it is
        # not — it is merely the last time the log happened to mention AC.
        return unknown

    cutoff = now.replace(tzinfo=None) if now.tzinfo else now
    past = [o for o in observations if o[0] <= cutoff]
    if not past:
        return unknown

    ac = [ts for ts, batt in past if not batt]
    if not ac:
        return unknown
    last_ac = max(ac)
    batt_after = [ts for ts, batt in past if batt and ts > last_ac]
    if not batt_after:
        return unknown
    return {
        "last_seen_ac": last_ac.strftime("%Y-%m-%dT%H:%M:%S"),
        "first_seen_on_battery": min(batt_after).strftime("%Y-%m-%dT%H:%M:%S"),
        "basis": BASIS_MEASURED,
    }


def _hhmm_to_minutes(s: str) -> int:
    h, _, m = s.partition(":")
    return int(h) * 60 + int(m)


def in_window(now: datetime, start: str = DEFAULT_START, end: str = DEFAULT_END) -> bool:
    """Weekday + PT clock window. Saturday/Sunday are never in-window."""
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return _hhmm_to_minutes(start) <= mins < _hhmm_to_minutes(end)


def measure(
    pmset_text: str,
    now: datetime,
    start: str,
    end: str,
    pmset_log_text: str = "",
) -> dict | None:
    parsed = parse_pmset(pmset_text)
    if parsed is None:
        return None
    window = in_window(now, start, end)
    bracket = power_bracket(
        parse_power_observations(pmset_log_text), now, parsed["on_battery"]
    )
    return {
        **parsed,
        "now": now.strftime("%Y-%m-%dT%H:%M"),
        "window_start": start,
        "window_end": end,
        "in_window": window,
        "breach": bool(parsed["on_battery"] and window),
        **bracket,
    }


def bracket_phrase(r: dict) -> str:
    """The bracket as prose, stating bounds and NEVER an elapsed figure.

    Empty string when there is nothing to say (on AC). On battery with an
    unknown basis it says so out loud — a silent omission would let a reader
    assume the stint began at the page they happened to be looking at, which is
    the exact inference this field exists to stop.
    """
    if not r.get("on_battery"):
        return ""
    if r.get("basis") != BASIS_MEASURED:
        return (
            " Battery stint: basis unknown — `pmset -g log` does not bracket it, "
            "so how long the host has been unplugged is NOT measurable here; "
            "report it as unknown rather than inferring it from a page timestamp."
        )
    return (
        f" Battery stint bracket (basis measured-bracket): last seen on AC "
        f"{r['last_seen_ac']}, first seen on battery {r['first_seen_on_battery']} "
        "— both are OBSERVATIONS from a sparse log, so the true transition lies "
        "between them; do not subtract them and call it time on battery."
    )


def summarize(r: dict) -> str:
    charge = f"{r['pct']}%" if r["pct"] is not None else "charge unknown"
    state = r["state"] or "state unknown"
    remain = f", {r['remaining']} remaining" if r["remaining"] else ""
    head = (
        f"power: {r['source']} — {charge}, {state}{remain} "
        f"(window {r['window_start']}-{r['window_end']} PT weekdays: "
        f"{'IN' if r['in_window'] else 'OUT'})"
    )
    if r["breach"]:
        return (
            f"BREACH {head}. The host is AWAKE ON BATTERY inside a trading-day "
            "window. caffeinate -s is void on battery, so the next lid close or "
            "idle sleep takes the engine down mid-tick — that cost 8 RTH ticks "
            "on 2026-10-22 and 67 ticks (straight through the open) on "
            "2026-10-29. Charge % is not the point; being unplugged is."
            f"{bracket_phrase(r)}"
        )
    return f"OK {head}{bracket_phrase(r)}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--window-start", default=DEFAULT_START)
    ap.add_argument("--window-end", default=DEFAULT_END)
    ap.add_argument(
        "--pmset-file",
        help="read `pmset -g batt` output from a file instead of running it (testing)",
    )
    ap.add_argument(
        "--pmset-log",
        help="bracket the battery stint from a saved `pmset -g log` instead of "
        "this host's (hermetic tests, or a past date pmset no longer retains). "
        "A DIFFERENT input from --pmset-file: that one is `pmset -g batt`.",
    )
    ap.add_argument(
        "--no-bracket",
        action="store_true",
        help="skip the `pmset -g log` read entirely (basis reads unknown)",
    )
    ap.add_argument(
        "--now",
        help="ISO datetime to evaluate the window against, PT (testing)",
    )
    args = ap.parse_args(argv)

    if args.pmset_file:
        with open(args.pmset_file) as fh:
            text = fh.read()
    else:
        text = read_pmset()

    now = datetime.now(PT) if not args.now else datetime.fromisoformat(args.now)

    # Reporting-only input: any failure here degrades to basis unknown. It must
    # never reach the exit-code path, which belongs to `pmset -g batt` alone.
    log_text = ""
    if not args.no_bracket:
        if args.pmset_log:
            try:
                with open(args.pmset_log, "r", errors="replace") as fh:
                    log_text = fh.read()
            except OSError:
                log_text = ""
        else:
            log_text = read_host_pmset_log()

    result = measure(text, now, args.window_start, args.window_end, log_text)
    if result is None:
        print(
            "power: UNMEASURED — pmset produced no recognisable power source",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, indent=2) if args.as_json else summarize(result))
    return 3 if result["breach"] else 0


if __name__ == "__main__":
    sys.exit(main())
