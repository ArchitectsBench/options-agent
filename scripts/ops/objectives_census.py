#!/usr/bin/env python3
"""Census of docs/OBJECTIVES.md — every objective's status, counted mechanically.

Why this exists: OBJECTIVE RECONCILIATION is a standing discipline (CLAUDE.md),
but the file carries objectives in TWO structures — markdown TABLE ROWS
(`| O-1 ... | **PASS** |`) and `## O-n` SECTIONS with a prose `Status:` line —
and the status markup varies (`Status: **PASS`, `Status:** **PASS`, ...). A grep
over status lines structurally cannot see a table row, so earlier audits reported
PASS-by-omission over ~half the file.

The census parses both structures and asserts every objective it finds has a
status. It never interprets or changes a verdict: the token is reported verbatim,
and a token outside the legend is reported as OTHER:<token>, not coerced.

Read-only. Path is a required argument (no production default) so a test can
point it at a fixture.

Exit codes: 0 = every objective has a status; 1 = at least one objective has no
status, or an id appears twice.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# The legend at the top of OBJECTIVES.md. Tokens outside it are reported, not coerced.
LEGEND = ("PASS", "FAIL", "UNKNOWN", "CONFIRM")

# `| O-1 **(RESTATED ...)** | ... | ... | **PASS** |` and `| **O-20** | ... |`
ROW_RE = re.compile(r"^\|\s*\*{0,2}O-(\d+)\b")
# `## O-21 — LIVENESS WATCHDOG ...`
HEADING_RE = re.compile(r"^##\s+\*{0,2}O-(\d+)\b")
ANY_HEADING_RE = re.compile(r"^##\s")
# `Status: **PASS**`, `Status:** CONFIRM`, `* **Status:** **PASS** (mechanism)`
STATUS_LINE_RE = re.compile(r"Status:?\*{0,2}\s*\*{0,2}\s*([A-Z]{3,})", re.ASCII)
# every bare uppercase word on the status line, so a compound verdict survives
TOKEN_RE = re.compile(r"\b([A-Z]{3,})\b")


@dataclass
class Objective:
    ident: str  # "O-7"
    where: str  # "table" | "section"
    line: int  # 1-indexed line the status was read from (or the heading, if none)
    status: str | None  # verbatim compound, e.g. "PASS" or "PASS/CONFIRM"


def _tokens(text: str) -> list[str]:
    """Legend tokens on a status line, in order, deduped — keeps compound verdicts
    like `PASS (server-side) / CONFIRM (phone receipt)` intact."""
    seen: list[str] = []
    for tok in TOKEN_RE.findall(text):
        if tok in LEGEND and tok not in seen:
            seen.append(tok)
    return seen


def _row_status(line: str) -> str | None:
    """Status of a table row = last non-empty cell."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    cells = [c for c in cells if c]
    if not cells:
        return None
    toks = _tokens(cells[-1])
    if toks:
        return "/".join(toks)
    # a cell with a non-legend verdict word still counts as a status, reported verbatim
    other = TOKEN_RE.search(cells[-1].replace("*", ""))
    return f"OTHER:{other.group(1)}" if other else None


def _section_status(body: list[tuple[int, str]]) -> tuple[int | None, str | None]:
    """FIRST status line in the section wins — later ones are sub-decisions
    (O-30 carries six) and must not be mistaken for the objective's verdict."""
    for lineno, text in body:
        m = STATUS_LINE_RE.search(text)
        if not m:
            continue
        toks = _tokens(text[m.start() :])
        if toks:
            return lineno, "/".join(toks)
        return lineno, f"OTHER:{m.group(1)}"
    return None, None


def parse(path: Path) -> list[Objective]:
    lines = path.read_text().splitlines()
    found: list[Objective] = []

    for i, line in enumerate(lines, start=1):
        m = ROW_RE.match(line)
        if m:
            found.append(Objective(f"O-{m.group(1)}", "table", i, _row_status(line)))

    # sections: heading -> next `## ` heading (exclusive)
    heads = [(i, m) for i, line in enumerate(lines, start=1) if (m := HEADING_RE.match(line))]
    all_heads = [i for i, line in enumerate(lines, start=1) if ANY_HEADING_RE.match(line)]
    for lineno, m in heads:
        nxt = next((h for h in all_heads if h > lineno), len(lines) + 1)
        body = [(j, lines[j - 1]) for j in range(lineno, nxt)]
        sline, status = _section_status(body)
        found.append(Objective(f"O-{m.group(1)}", "section", sline or lineno, status))

    return found


def report(path: Path, out=sys.stdout) -> int:
    objs = parse(path)
    if not objs:
        print(f"NO OBJECTIVES FOUND in {path} — parser or file is wrong", file=out)
        return 1

    census = Counter(o.status or "MISSING" for o in objs)
    missing = [o for o in objs if o.status is None]
    dupes = sorted(k for k, v in Counter(o.ident for o in objs).items() if v > 1)

    print(f"OBJECTIVES CENSUS — {path}", file=out)
    print(f"  structures: {sum(1 for o in objs if o.where == 'table')} table rows + "
          f"{sum(1 for o in objs if o.where == 'section')} sections", file=out)
    for status, n in sorted(census.items()):
        print(f"  {status:<22} {n}", file=out)
    print(f"  {'TOTAL':<22} {len(objs)}", file=out)

    counted = sum(census.values())
    assert counted == len(objs), f"census {counted} != objectives {len(objs)}"
    print(f"  ids: {', '.join(o.ident for o in objs)}", file=out)

    rc = 0
    if missing:
        rc = 1
        print("\nOBJECTIVES WITH NO STATUS (an audit would score these by omission):", file=out)
        for o in missing:
            print(f"  {o.ident} ({o.where}, line {o.line})", file=out)
    if dupes:
        rc = 1
        print(f"\nDUPLICATE IDS: {', '.join(dupes)}", file=out)
    if rc == 0:
        print("\nOK — every objective found carries a status.", file=out)
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, help="path to OBJECTIVES.md (required, no default)")
    args = ap.parse_args(argv)
    if not args.path.is_file():
        print(f"not a file: {args.path}", file=sys.stderr)
        return 2
    return report(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
