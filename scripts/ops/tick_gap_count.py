#!/usr/bin/env python3
"""Count scheduler ticks lost to host sleep — read-only, log path as argument.

Why this exists (queue item 2026-10-02, closed 2026-10-08): the engine already
records a `wake_gap_recovery` row in `logs/observation/fills.jsonl` for every
gap it sweeps, but that row is gated by `src/scheduler.py:WAKE_GAP_MIN = 15`,
so the *single lost tick* case (a 10-11 min gap) leaves no counted artefact —
exactly the signature of the battery/sleep problem before the open. Lowering
WAKE_GAP_MIN is a behaviour change (it gates the protective sweep), so the
count lives here instead, outside the engine.

This tool NEVER writes and NEVER defaults to a production path: the log file is
a required argument. Classification of each lost tick into pre_open / rth /
post_close uses the configured market hours, so "how many ticks did sleep eat
before the open" is a number, not a narration.

    python3 scripts/ops/tick_gap_count.py logs/agent.log --date 2026-10-08

CAUSE ATTRIBUTION (added 2026-11-06, queue item filed 2026-11-05 post_close).
Every gap is also attributed to `clamshell | idle_sleep | unknown` by reading
`pmset -g log` (read-only, never written, failure is degraded mode not a crash).
This exists because on 2026-11-05 a lid close cost 7 RTH ticks and the engine's
own surfaces could not say why — diagnosing it took a manual pmset correlation.
`clamshell` matters specifically: lid-close sleep is NOT prevented by
caffeinate at all, so it is a different fault from `idle_sleep` and has a
different fix (leave the lid open) — a report that merges them hides that.
Use `--pmset-log PATH` to attribute against a saved log instead of the host's.

THE TWO COUNTERS ARE DIFFERENT MEASURES OF THE SAME WINDOW — neither is wrong.
This tool counts *missed 5-minute tick slots* between surviving ticks, at a
400 s threshold. The engine's `wake_gap_recovery` rows in
`logs/observation/fills.jsonl` count *sweeps it performed on waking*, gated by
`src/scheduler.py:WAKE_GAP_MIN = 15`, and report the wall-clock `gap_min` of
the window. So one sleep can read "5 gaps / 7 lost ticks" here and "2 rows,
gap_min 22 and 17" there (the real 2026-11-05 numbers): sub-15-min gaps are
invisible to the engine, and one engine row can span several of this tool's
gaps. The `N of M visible to the engine's counter` line below is the bridge.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

# A scheduler tick leaves exactly one of these two lines in agent.log. Other
# INFO/WARNING lines (restart banners, sweeps, lane chatter) are ignored, so a
# noisy log cannot hide a gap in the tick sequence.
TICK_PATTERNS = ("market closed — heartbeat only", "cycle #")

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})[,.]?(\d{0,3})")

CYCLE_SECONDS = 300  # agent.cycle_minutes = 5
# config/config.yaml agent.market_hours (America/Los_Angeles); agent.log
# timestamps are already local, so no tz conversion is needed.
MARKET_OPEN = time(6, 30)
MARKET_CLOSE = time(13, 0)

# 400 s, not 300 s: one on-time tick can land a few seconds late (cycle work
# takes 10-20 s), so >400 s is the first unambiguous "a tick is missing".
DEFAULT_THRESHOLD_SECONDS = 400

# `pmset -g log` sleep lines, e.g.
#   2026-11-05 11:05:07 -0800 Sleep  Entering Sleep state due to 'Clamshell
#   Sleep': Using Batt (Charge:100) ...
# The quoted reason is what distinguishes a lid close from an idle sleep.
SLEEP_RE = re.compile(r"Entering Sleep state due to '([^']*)'")
CAUSE_CLAMSHELL = "clamshell"
CAUSE_IDLE = "idle_sleep"
CAUSE_UNKNOWN = "unknown"


def phase_of(ts: datetime) -> str:
    """pre_open / rth / post_close for a missed tick slot."""
    t = ts.time()
    if t < MARKET_OPEN:
        return "pre_open"
    if t < MARKET_CLOSE:
        return "rth"
    return "post_close"


@dataclass
class Gap:
    start: datetime
    end: datetime
    lost_ticks: int
    phases: dict[str, int] = field(default_factory=dict)
    cause: str = CAUSE_UNKNOWN
    # The pmset reason string behind `cause`, kept verbatim so a report never
    # asks the reader to trust the classifier over the host's own words.
    cause_detail: str = ""

    @property
    def gap_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def gap_minutes(self) -> float:
        return round(self.gap_seconds / 60, 1)


def parse_tick_times(lines, date_filter: str | None = None) -> list[datetime]:
    """Timestamps of scheduler tick lines. Unparseable lines are skipped."""
    out: list[datetime] = []
    for line in lines:
        if not any(p in line for p in TICK_PATTERNS):
            continue
        m = TS_RE.match(line)
        if not m:
            continue
        day, clock, _ms = m.groups()
        if date_filter and day != date_filter:
            continue
        try:
            out.append(datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
    return out


def parse_sleep_events(lines, date_filter: str | None = None):
    """(timestamp, cause, reason) for every sleep entry in a `pmset -g log`.

    Takes log TEXT or an iterable of lines — never a subprocess handle — so the
    classifier is testable against a fixture without shelling out. Unparseable
    lines are skipped, matching `parse_tick_times`.
    """
    if isinstance(lines, str):
        lines = lines.splitlines()
    out: list[tuple[datetime, str, str]] = []
    for line in lines:
        m_reason = SLEEP_RE.search(line)
        if not m_reason:
            continue
        m_ts = TS_RE.match(line)
        if not m_ts:
            continue
        day, clock, _ms = m_ts.groups()
        if date_filter and day != date_filter:
            continue
        try:
            ts = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        reason = m_reason.group(1)
        cause = CAUSE_CLAMSHELL if "clamshell" in reason.lower() else CAUSE_IDLE
        out.append((ts, cause, reason))
    return out


def attribute_gaps(gaps, sleep_events):
    """Stamp each gap with the cause of any sleep that began inside it.

    A gap with no sleep event stays `unknown` — the host may have been busy,
    the process may have died, or pmset's ring buffer may no longer reach back
    that far (it retains only days). `clamshell` wins over `idle_sleep` when a
    window contains both, because lid close is the stronger finding: caffeinate
    does not prevent it, so it survives the fix that stops idle sleep.
    """
    for g in gaps:
        inside = [
            (ts, cause, reason)
            for ts, cause, reason in sleep_events
            if g.start <= ts <= g.end
        ]
        if not inside:
            continue
        clamshells = [e for e in inside if e[1] == CAUSE_CLAMSHELL]
        ts, cause, reason = (clamshells or inside)[0]
        g.cause = cause
        g.cause_detail = f"{reason} @ {ts:%H:%M:%S}"
    return gaps


def read_host_pmset_log(timeout_seconds: float = 20.0) -> str:
    """`pmset -g log` from this host, or "" if it cannot be read.

    Read-only and non-fatal by design (CLAUDE.md: a missing data source is
    degraded mode, never a crash) — an unattributed report still carries the
    gap counts, which is the number this tool exists for.
    """
    try:
        proc = subprocess.run(
            ["pmset", "-g", "log"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def find_gaps(
    tick_times,
    threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
    cycle_seconds: int = CYCLE_SECONDS,
) -> list[Gap]:
    """Gaps in the tick sequence longer than `threshold_seconds`.

    Lost ticks are the 5-minute slots that should have fired between the two
    surviving ticks, each attributed to the session phase it fell in — so a gap
    straddling the open is split, not credited whole to one side.
    """
    gaps: list[Gap] = []
    for prev, now in zip(tick_times, tick_times[1:]):
        delta = (now - prev).total_seconds()
        if delta <= threshold_seconds:
            continue
        phases: dict[str, int] = {}
        slot = prev
        lost = 0
        while True:
            slot = slot + timedelta(seconds=cycle_seconds)
            # A slot within one cycle of `now` is the tick that actually fired.
            if (now - slot).total_seconds() < cycle_seconds / 2:
                break
            lost += 1
            ph = phase_of(slot)
            phases[ph] = phases.get(ph, 0) + 1
            if lost > 10_000:  # pathological input guard
                break
        gaps.append(Gap(start=prev, end=now, lost_ticks=lost, phases=phases))
    return gaps


def summarize(gaps) -> dict:
    totals: dict[str, int] = {"pre_open": 0, "rth": 0, "post_close": 0}
    for g in gaps:
        for ph, n in g.phases.items():
            totals[ph] = totals.get(ph, 0) + n
    causes: dict[str, int] = {}
    ticks_by_cause: dict[str, int] = {}
    for g in gaps:
        causes[g.cause] = causes.get(g.cause, 0) + 1
        ticks_by_cause[g.cause] = ticks_by_cause.get(g.cause, 0) + g.lost_ticks
    return {
        "gaps": len(gaps),
        "lost_ticks": sum(g.lost_ticks for g in gaps),
        "lost_ticks_by_phase": totals,
        "wake_gap_min_would_report": sum(1 for g in gaps if g.gap_minutes > 15),
        "gaps_by_cause": causes,
        "lost_ticks_by_cause": ticks_by_cause,
    }


def render(gaps, summary, threshold_seconds) -> str:
    lines = [
        f"tick gaps > {threshold_seconds:.0f}s "
        f"(engine's own wake_gap_recovery only records > 15 min)",
    ]
    for g in gaps:
        ph = ", ".join(f"{k}={v}" for k, v in sorted(g.phases.items())) or "none"
        flag = "" if g.gap_minutes > 15 else "   <- INVISIBLE to wake_gap_recovery"
        detail = f" [{g.cause_detail}]" if g.cause_detail else ""
        lines.append(
            f"  {g.start:%Y-%m-%d %H:%M:%S} -> {g.end:%H:%M:%S}  "
            f"{g.gap_minutes:5.1f} min  lost={g.lost_ticks} ({ph})  "
            f"cause={g.cause}{detail}{flag}"
        )
    if not gaps:
        lines.append("  (none)")
    by_cause = summary.get("lost_ticks_by_cause") or {}
    if by_cause:
        lines.append(
            "BY CAUSE (pmset): "
            + ", ".join(
                f"{c}={n} lost ticks" for c, n in sorted(by_cause.items())
            )
        )
        if by_cause.get(CAUSE_CLAMSHELL):
            lines.append(
                "  NOTE clamshell = LID CLOSE, which caffeinate does not "
                "prevent; the fix is leaving the lid open, not an assertion."
            )
    t = summary["lost_ticks_by_phase"]
    lines.append(
        f"TOTAL gaps={summary['gaps']} lost_ticks={summary['lost_ticks']} "
        f"(pre_open={t.get('pre_open', 0)} rth={t.get('rth', 0)} "
        f"post_close={t.get('post_close', 0)}); "
        f"{summary['wake_gap_min_would_report']} of {summary['gaps']} "
        f"visible to the engine's counter"
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log_path", help="path to an agent.log (required; never defaulted)")
    ap.add_argument("--date", help="restrict to one YYYY-MM-DD")
    ap.add_argument(
        "--threshold-seconds", type=float, default=DEFAULT_THRESHOLD_SECONDS
    )
    ap.add_argument(
        "--pmset-log",
        help="attribute causes from a saved `pmset -g log` file "
        "instead of this host (useful for a past date pmset no longer retains)",
    )
    ap.add_argument(
        "--no-attribute",
        action="store_true",
        help="skip cause attribution (no pmset read at all)",
    )
    args = ap.parse_args(argv)

    with open(args.log_path, "r", errors="replace") as fh:
        ticks = parse_tick_times(fh, date_filter=args.date)
    if len(ticks) < 2:
        print(f"only {len(ticks)} tick line(s) matched — nothing to measure")
        return 0
    gaps = find_gaps(ticks, threshold_seconds=args.threshold_seconds)

    if gaps and not args.no_attribute:
        if args.pmset_log:
            with open(args.pmset_log, "r", errors="replace") as fh:
                pmset_text = fh.read()
        else:
            pmset_text = read_host_pmset_log()
        if pmset_text:
            attribute_gaps(gaps, parse_sleep_events(pmset_text))
        else:
            print(
                "note: `pmset -g log` unavailable — gaps reported without "
                "cause attribution"
            )

    summary = summarize(gaps)
    print(f"ticks parsed: {len(ticks)} "
          f"({ticks[0]:%Y-%m-%d %H:%M:%S} -> {ticks[-1]:%Y-%m-%d %H:%M:%S})")
    print(render(gaps, summary, args.threshold_seconds))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
