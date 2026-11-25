"""Atomic file writes + torn-line-tolerant JSONL readers.

Two SEV1 failure modes this module exists to prevent:

1. TORN WRITES. A plain ``path.write_text(...)`` is not atomic: a crash (or an
   OOM kill, or a full disk) mid-write leaves a truncated/half-written file on
   disk. The next reader then sees a corrupt file. ``atomic_save`` writes to a
   sibling ``.tmp`` and ``os.replace()``s it into place — ``os.replace`` is
   atomic on POSIX, so a reader ever sees either the whole old file or the
   whole new file, never a torn one. (Mirrors src/control/state.py, which
   already does this correctly.)

2. ONE CORRUPT LINE BRICKING A WHOLE JOB. An append-only ``.jsonl`` ledger can
   acquire a single torn line (e.g. a crash mid-append before this module
   existed, or an out-of-band edit). A bare ``json.loads`` in a loop then
   raises on that one line and takes the entire nightly/enrichment job down
   every run. ``read_jsonl`` skips undecodable lines, counts them, and logs a
   single warning — the job keeps processing every good line.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("trader")


def atomic_save(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path``.

    Writes to ``path.with_suffix('.tmp')`` first, then ``os.replace()`` into
    place. ``os.replace`` is atomic on the same filesystem, so a concurrent or
    subsequent reader never observes a torn/partial file: it sees the whole
    prior contents or the whole new contents. On any failure the partial temp
    file is cleaned up and the original ``path`` is left untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())          # durability: text hits the platter
        os.replace(tmp, path)              # atomic swap
    except Exception:
        # never leave a stray temp file masquerading as recoverable state
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield the json.loads of each non-blank line in ``path``.

    Torn-line tolerant: an undecodable line is SKIPPED (not raised on) and
    counted; if any were skipped a single warning is logged naming the count
    and file. A missing file yields nothing. This is the safe drop-in for the
    ``for line in path.read_text().splitlines(): json.loads(line)`` pattern
    that otherwise bricks a job on the first corrupt line.

    Newline-heal note: a torn append typically manifests as ONE bad line
    (usually the last, a half-written record) surrounded by good lines. Because
    each line is decoded independently, the good lines on either side are still
    read — the file self-heals on its next clean append/rewrite; no manual
    repair is required for the job to make progress.
    """
    path = Path(path)
    if not path.exists():
        return
    skipped = 0
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                skipped += 1
                continue
    if skipped:
        logger.warning("read_jsonl: skipped %d corrupt line(s) in %s", skipped, path)


def load_jsonl_list(path: Path) -> list[dict]:
    """Eager list() of :func:`read_jsonl` — same torn-line tolerance, for call
    sites that need a materialized list rather than a generator."""
    return list(read_jsonl(path))
