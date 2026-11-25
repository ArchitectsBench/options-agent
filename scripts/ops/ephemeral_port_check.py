#!/usr/bin/env python3
"""Flag ephemeral-port (TIME_WAIT) exhaustion BEFORE it takes the host offline.

Why this exists (queue item 2026-10-23 evening, PAGED urgent): twice now — on
2026-08-24 and 2026-10-23 — this Mac consumed its entire ephemeral port range
with a wedged TIME_WAIT table and could not open ANY new outbound socket. Every
connect failed with `[Errno 49] Can't assign requested address`, which surfaced
as five unrelated-looking faults in one evening: cockpit `/api/feed` returning
000 while the service was healthy, `[ibkr] connect failed on all clientIds`,
the nightly chain stalling on its API step, ThetaData reading `unreachable`,
and a `RVOL scan FAILED` page. One cause, five symptoms, and nothing in the
monitoring could see it coming.

The shape of the fault, measured on 2026-10-23: **17,793 TIME_WAIT** sockets
(17,441 of them to :443) against a `net.inet.ip.portrange` of 49152..65535 =
**16,384 ports** — the range fully consumed. `net.inet.tcp.msl=15000` means
2*MSL = 30 s expiry, yet the table read 17794/17796/17796/17796 sampled over
45 s: **static**. A standing table that size at a 30 s expiry would need ~590
new sockets/s and nothing here does that, so the table is WEDGED, not flooded.
Both known occurrences cleared only at a reboot (uptime was ~60 days each
time), so the recurrence shape is "~60 days of uptime leaks the table dry".

Neither fix is available to the autopilot — widening the range needs root and a
reboot is the operator's call — so this tool does not try to fix anything. It
makes the recurrence **detectable before it bites**, which is the half that was
missing: at 80% consumed there is still headroom to act, where at 100% the box
cannot even send the alert about it.

Read-only by construction: it runs `netstat`/`sysctl` and prints. It never
writes, never kills a socket, and never changes a sysctl.

    python3 scripts/ops/ephemeral_port_check.py
    python3 scripts/ops/ephemeral_port_check.py --json
    python3 scripts/ops/ephemeral_port_check.py --threshold-pct 80

Exit codes: 0 = below threshold, 3 = BREACH (at/above threshold), 1 = could not
measure (unreadable netstat/sysctl). 3 is distinct from 1 on purpose — a caller
must never read "I could not measure" as "all clear".
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# A netstat -an line's state is its last field; we count the states that hold a
# source port hostage. TIME_WAIT is the one that wedged, but LAST_ACK and
# FIN_WAIT_1/2 also pin a port and were present (277 + 170) in the incident, so
# counting only TIME_WAIT would under-report the consumption that matters.
PINNING_STATES = ("TIME_WAIT", "LAST_ACK", "FIN_WAIT_1", "FIN_WAIT_2", "CLOSING")

DEFAULT_THRESHOLD_PCT = 80.0

# macOS defaults; only used when sysctl cannot be read, and reported as such.
FALLBACK_FIRST, FALLBACK_LAST = 49152, 65535

_STATE_RE = re.compile(r"\b(" + "|".join(PINNING_STATES) + r")\b")
_LOCAL_PORT_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+(\S+)\s")


def _run(cmd: list[str]) -> str:
    """Run a read-only command, returning stdout ('' on failure)."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout or ""


def read_netstat(runner=_run) -> str:
    return runner(["netstat", "-an", "-p", "tcp"])


def read_sysctl(runner=_run) -> str:
    return runner(
        [
            "sysctl",
            "net.inet.ip.portrange.first",
            "net.inet.ip.portrange.last",
            "net.inet.tcp.msl",
        ]
    )


def parse_range(sysctl_text: str) -> tuple[int, int, int | None, bool]:
    """-> (first, last, msl_ms, from_sysctl).

    `from_sysctl` is False when the values are the compiled-in fallback, so a
    report can say it is guessing rather than quietly presenting a default as a
    measurement.
    """
    vals: dict[str, int] = {}
    for line in sysctl_text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        try:
            vals[key.strip()] = int(val.strip())
        except ValueError:
            continue
    first = vals.get("net.inet.ip.portrange.first")
    last = vals.get("net.inet.ip.portrange.last")
    msl = vals.get("net.inet.tcp.msl")
    if first is None or last is None or last < first:
        return FALLBACK_FIRST, FALLBACK_LAST, msl, False
    return first, last, msl, True


def count_pinned(netstat_text: str, first: int, last: int) -> tuple[int, int]:
    """-> (pinned_in_range, pinned_total).

    Only local ports INSIDE the ephemeral range consume the resource that runs
    out; a TIME_WAIT on a listening service's own port (e.g. 8421) does not.
    Counting the whole table would over-report. Lines whose local port cannot be
    parsed still count toward the total, never toward the in-range figure.
    """
    in_range = total = 0
    for line in netstat_text.splitlines():
        if not _STATE_RE.search(line):
            continue
        total += 1
        m = _LOCAL_PORT_RE.match(line)
        if not m:
            continue
        # macOS prints `127.0.0.1.49231` / `*.8421` — the port is after the last dot.
        _, _, port_s = m.group(1).rpartition(".")
        try:
            port = int(port_s)
        except ValueError:
            continue
        if first <= port <= last:
            in_range += 1
    return in_range, total


def measure(netstat_text: str, sysctl_text: str, threshold_pct: float) -> dict:
    first, last, msl, from_sysctl = parse_range(sysctl_text)
    range_size = last - first + 1
    in_range, total = count_pinned(netstat_text, first, last)
    pct = (in_range / range_size) * 100.0 if range_size > 0 else 0.0
    return {
        "pinned_in_range": in_range,
        "pinned_total": total,
        "range_first": first,
        "range_last": last,
        "range_size": range_size,
        "pct_consumed": round(pct, 2),
        "threshold_pct": threshold_pct,
        "breach": pct >= threshold_pct,
        "msl_ms": msl,
        "range_from_sysctl": from_sysctl,
        "states_counted": list(PINNING_STATES),
    }


def summarize(r: dict) -> str:
    head = (
        f"ephemeral ports: {r['pinned_in_range']}/{r['range_size']} consumed "
        f"({r['pct_consumed']}%) by {'/'.join(PINNING_STATES[:2])}+ in "
        f"{r['range_first']}..{r['range_last']}"
    )
    if not r["range_from_sysctl"]:
        head += "  [range NOT read from sysctl — compiled fallback]"
    if r["breach"]:
        return (
            f"BREACH {head} >= {r['threshold_pct']}% threshold. This is the "
            "2026-08-24 / 2026-10-23 fault: at 100% every outbound connect "
            "fails with [Errno 49] and the host loses all market data, the "
            "broker line and the nightly chain at once. Needs root (widen "
            "net.inet.ip.portrange) or a reboot — neither is autopilot's."
        )
    return f"OK {head} (< {r['threshold_pct']}%)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--threshold-pct", type=float, default=DEFAULT_THRESHOLD_PCT)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument(
        "--netstat-file",
        help="read netstat output from a file instead of running it (testing)",
    )
    ap.add_argument(
        "--sysctl-file",
        help="read sysctl output from a file instead of running it (testing)",
    )
    args = ap.parse_args(argv)

    if args.netstat_file:
        with open(args.netstat_file) as fh:
            netstat_text = fh.read()
    else:
        netstat_text = read_netstat()
    if args.sysctl_file:
        with open(args.sysctl_file) as fh:
            sysctl_text = fh.read()
    else:
        sysctl_text = read_sysctl()

    if not netstat_text.strip():
        print("ephemeral ports: UNMEASURED — netstat produced no output", file=sys.stderr)
        return 1

    result = measure(netstat_text, sysctl_text, args.threshold_pct)
    print(json.dumps(result, indent=2) if args.as_json else summarize(result))
    return 3 if result["breach"] else 0


if __name__ == "__main__":
    sys.exit(main())
