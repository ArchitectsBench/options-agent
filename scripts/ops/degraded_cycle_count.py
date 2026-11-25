#!/usr/bin/env python3
"""Count cycles whose regime object came back degraded, and group them into windows.

Read-only ops tool, same family as ``tick_gap_count.py``: the observation jsonl
path is a REQUIRED argument (no production default) and nothing is written.

Why it exists (queue item 2026-10-10 weekend, six degraded cycles 5655-5660 on
2026-10-08): the per-cycle facts were on disk all along, but "6 degraded cycles
today vs 1 on 10-06" could only be established by a human reading a nightly
report. This makes that count queryable.

Two things it deliberately does NOT trust:

* ``regime.missing`` UNDER-REPORTS. In the 10-08 window every cycle carried
  ``missing: ['term_structure']`` while ``term_structure``, ``vol_outlook`` AND
  ``richness`` were all null. So null-valued regime fields are counted directly
  from the object, and ``missing`` is reported alongside, never instead.
* a day total alone loses the signal. The 10-08 incident was six degraded cycles
  *consecutively*, inside 23 minutes; one degraded cycle at the open (today,
  10-06, 10-07) is routine. Consecutive runs are therefore grouped into windows
  with start/end stamps and a span in minutes.

Usage:
    python3 scripts/ops/degraded_cycle_count.py logs/observation/2026-10-08.jsonl
    python3 scripts/ops/degraded_cycle_count.py <path> --date 2026-10-08
    python3 scripts/ops/degraded_cycle_count.py <path> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

# Regime keys that are bookkeeping rather than measured values; a null here is
# not a degraded feed.
NON_FEED_KEYS = frozenset({"degraded", "missing", "stale_feeds", "ts", "vix_note"})


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect(lines, date: str | None = None) -> dict:
    """Return degraded-cycle facts for an observation jsonl iterable."""
    parsed = 0
    skipped = 0
    cycles: list[dict] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        regime = row.get("regime")
        if not isinstance(regime, dict):
            skipped += 1
            continue
        ts = row.get("ts") or regime.get("ts") or ""
        if date and not str(ts).startswith(date):
            continue
        parsed += 1

        null_fields = sorted(
            k for k, v in regime.items() if v is None and k not in NON_FEED_KEYS
        )
        missing = regime.get("missing")
        missing = sorted(missing) if isinstance(missing, list) else []
        flag = bool(regime.get("degraded"))
        if not (flag or null_fields):
            continue
        cycles.append(
            {
                "cycle": row.get("cycle"),
                "ts": str(ts),
                "flag": flag,
                "null_fields": null_fields,
                "missing": missing,
            }
        )

    field_counts: dict[str, int] = {}
    for c in cycles:
        for f in c["null_fields"]:
            field_counts[f] = field_counts.get(f, 0) + 1

    return {
        "cycles_parsed": parsed,
        "lines_skipped": skipped,
        "degraded_cycles": len(cycles),
        "flag_only": sum(1 for c in cycles if c["flag"] and not c["null_fields"]),
        "null_without_flag": sum(1 for c in cycles if c["null_fields"] and not c["flag"]),
        "field_counts": dict(sorted(field_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "windows": _windows(cycles),
        "detail": cycles,
    }


def _windows(cycles: list[dict]) -> list[dict]:
    """Group cycles with consecutive cycle numbers into windows.

    A missing/non-integer cycle number cannot be proven adjacent to anything, so
    it starts its own window rather than being silently merged.
    """
    windows: list[dict] = []
    for c in cycles:
        num = c["cycle"] if isinstance(c["cycle"], int) else None
        prev = windows[-1] if windows else None
        adjacent = (
            prev is not None
            and num is not None
            and isinstance(prev["end_cycle"], int)
            and num == prev["end_cycle"] + 1
        )
        if adjacent:
            prev["end_cycle"] = num
            prev["end_ts"] = c["ts"]
            prev["cycles"] += 1
            for f in c["null_fields"]:
                if f not in prev["fields"]:
                    prev["fields"].append(f)
        else:
            windows.append(
                {
                    "start_cycle": num if num is not None else c["cycle"],
                    "end_cycle": num if num is not None else c["cycle"],
                    "start_ts": c["ts"],
                    "end_ts": c["ts"],
                    "cycles": 1,
                    "fields": list(c["null_fields"]),
                }
            )

    for w in windows:
        start, end = _parse_ts(w["start_ts"]), _parse_ts(w["end_ts"])
        w["span_min"] = (
            round((end - start).total_seconds() / 60.0, 1)
            if start and end and end >= start
            else None
        )
        w["fields"] = sorted(w["fields"])
    return windows


def render(result: dict) -> str:
    out = [
        f"cycles parsed: {result['cycles_parsed']}"
        + (f" ({result['lines_skipped']} line(s) skipped)" if result["lines_skipped"] else "")
    ]
    if not result["degraded_cycles"]:
        out.append("  (no degraded cycles)")
    else:
        out.append("degraded windows (consecutive cycles):")
        for w in result["windows"]:
            span = "n/a" if w["span_min"] is None else f"{w['span_min']} min"
            fields = ",".join(w["fields"]) or "flag only"
            out.append(
                f"  cycles {w['start_cycle']}-{w['end_cycle']}  "
                f"{w['start_ts']} -> {w['end_ts']}  "
                f"({w['cycles']} cycle(s), {span})  null: {fields}"
            )
        out.append("null regime fields (count of cycles):")
        for field, n in result["field_counts"].items():
            out.append(f"  {field}: {n}")
    longest = max((w["cycles"] for w in result["windows"]), default=0)
    out.append(
        f"TOTAL degraded_cycles={result['degraded_cycles']} "
        f"windows={len(result['windows'])} longest_window={longest} "
        f"of {result['cycles_parsed']} cycles "
        f"(flag_only={result['flag_only']} null_without_flag={result['null_without_flag']})"
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", help="observation jsonl to read (required; no default)")
    ap.add_argument("--date", help="only count cycles whose ts starts with this date")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    try:
        with open(args.path, encoding="utf-8") as fh:
            result = collect(fh, args.date)
    except OSError as exc:
        print(f"cannot read {args.path}: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
