"""Hermetic tests for scripts/ops/objectives_census.py.

Fixtures only — never the real docs/OBJECTIVES.md. The real file evolves every
session, so asserting its counts here would make the suite fail on correct edits;
the real-file run is the audit, not the test. (Also: the census is read-only, but
pointing a test at a production path is the pattern CLAUDE.md's destructive-probe
rule exists to stop.)
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ops"))

import objectives_census as oc  # noqa: E402


# Covers every shape the real file actually contains, each of which broke an
# earlier grep-based audit:
#   - the LEGEND line (a `Status:` line belonging to no objective)
#   - a table row whose id is plain, and one whose id is BOLDED (`| **O-20** |`)
#   - a table row with trailing prose after the token (`**PASS** (narrow form)`)
#   - a section with MULTIPLE status lines (only the first is the verdict)
#   - a compound verdict on one line (`PASS (...) / CONFIRM (...)`)
#   - markup variants `Status: **X`, `Status:** X`, `* **Status:** **X**`
#   - an off-legend verdict word (reported verbatim, never coerced)
#   - non-contiguous ids (the real file has no O-13 and no O-25)
FIXTURE = """# Objectives

Status: **PASS** (verified) · **FAIL** (verified broken) · **UNKNOWN** (not yet
verified) · **CONFIRM** (needs the operator).

## Execution

| # | Objective | How it is verified | Status |
|---|---|---|---|
| O-1 **(RESTATED 2026-08-06)** | plain id | `tests/test_a.py` | **PASS** |
| **O-20** | bolded id | NOT YET BUILT | **PASS** — proven both layers |
| O-14 | trailing prose | `tests/test_b.py` | **PASS** (narrow form) |
| O-4 | needs operator | MANUAL | **CONFIRM** |

## O-21 — MULTI-STATUS SECTION

Status: **PASS** — the objective's own verdict.

Some later sub-decision, which is NOT this objective's status:

Status: **CONFIRM**.

## O-28 — COMPOUND

* **Status:** **PASS** (server-side delivery) / **CONFIRM** (phone receipt)

## O-31 — MARKUP VARIANT

(c) the earnings-source swap only after C-8. Status:** UNKNOWN (nothing built
yet).

## O-32 — OFF-LEGEND TOKEN

Status: **ACTIVE from the next engine restart** (operator prose).

## Not an objective heading

Status: **PASS** — belongs to no objective and must not be counted.
"""

STATUSLESS = FIXTURE + """
## O-99 — NO STATUS AT ALL

This objective states a goal and never says where it stands.
"""


def _write(tmp: str, text: str) -> Path:
    p = Path(tmp) / "OBJECTIVES.md"
    p.write_text(text)
    return p


class ParseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.objs = oc.parse(_write(self._tmp.name, FIXTURE))
        self.by_id = {o.ident: o for o in self.objs}

    def test_finds_both_structures_and_nothing_else(self):
        # 4 table rows + 4 `## O-` sections; the legend line and the
        # "Not an objective heading" section contribute nothing.
        self.assertEqual(
            sorted(self.by_id), ["O-1", "O-14", "O-20", "O-21", "O-28", "O-31", "O-32", "O-4"]
        )
        self.assertEqual(len(self.objs), 8)
        self.assertEqual(sum(1 for o in self.objs if o.where == "table"), 4)
        self.assertEqual(sum(1 for o in self.objs if o.where == "section"), 4)

    def test_bolded_table_id_is_not_missed(self):
        # `| **O-20** |` escaped the plain `^\| O-` grep that earlier audits used.
        self.assertEqual(self.by_id["O-20"].where, "table")
        self.assertEqual(self.by_id["O-20"].status, "PASS")

    def test_table_status_is_last_cell_token_only(self):
        self.assertEqual(self.by_id["O-1"].status, "PASS")
        self.assertEqual(self.by_id["O-14"].status, "PASS")  # "(narrow form)" dropped
        self.assertEqual(self.by_id["O-4"].status, "CONFIRM")

    def test_first_status_line_wins_in_a_multi_status_section(self):
        # O-30 in the real file carries six; a later sub-decision is not the verdict.
        self.assertEqual(self.by_id["O-21"].status, "PASS")

    def test_compound_verdict_survives_intact(self):
        self.assertEqual(self.by_id["O-28"].status, "PASS/CONFIRM")

    def test_markup_variants_are_tolerated(self):
        self.assertEqual(self.by_id["O-31"].status, "UNKNOWN")

    def test_off_legend_token_is_reported_verbatim_never_coerced(self):
        self.assertEqual(self.by_id["O-32"].status, "OTHER:ACTIVE")

    def test_non_contiguous_ids_are_not_an_error(self):
        self.assertEqual(oc.report(_write(self._tmp.name, FIXTURE), out=io.StringIO()), 0)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_clean_file_exits_zero_and_census_totals_match(self):
        out = io.StringIO()
        rc = oc.report(_write(self._tmp.name, FIXTURE), out=out)
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("TOTAL                  8", text)
        self.assertIn("4 table rows + 4 sections", text)
        self.assertIn("PASS/CONFIRM", text)
        self.assertIn("OTHER:ACTIVE", text)

    def test_statusless_objective_fails_loudly(self):
        # The whole point: an objective an audit would otherwise score by omission.
        out = io.StringIO()
        rc = oc.report(_write(self._tmp.name, STATUSLESS), out=out)
        self.assertEqual(rc, 1)
        self.assertIn("OBJECTIVES WITH NO STATUS", out.getvalue())
        self.assertIn("O-99", out.getvalue())

    def test_duplicate_id_fails_loudly(self):
        dup = FIXTURE + "\n## O-1 — same id twice\n\nStatus: **PASS**.\n"
        out = io.StringIO()
        rc = oc.report(_write(self._tmp.name, dup), out=out)
        self.assertEqual(rc, 1)
        self.assertIn("DUPLICATE IDS: O-1", out.getvalue())

    def test_empty_file_is_an_error_not_a_clean_pass(self):
        out = io.StringIO()
        self.assertEqual(oc.report(_write(self._tmp.name, "# nothing\n"), out=out), 1)
        self.assertIn("NO OBJECTIVES FOUND", out.getvalue())

    def test_missing_path_returns_2(self):
        self.assertEqual(oc.main([str(Path(self._tmp.name) / "nope.md")]), 2)


if __name__ == "__main__":
    unittest.main()
