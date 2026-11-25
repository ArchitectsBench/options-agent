"""SCALP marker rename — FORWARD-ONLY, both eras pinned (2026-08-08)."""
import unittest

import src.engine.paper_trader as pt


class BothErasRecognised(unittest.TestCase):
    def test_legacy_TEST_ids_still_route_to_scalp(self):
        """Historical ids are join keys into fills/enrichment/post-mortems and
        are NEVER rewritten — they must keep working forever."""
        self.assertTrue(pt.is_scalp("20260806-TEST-2528-NVDA-debit_spread"))
        self.assertEqual(pt.lane_of("20260806-TEST-2528-NVDA-debit_spread"), "scalp")

    def test_new_SCALP_ids_route_to_scalp(self):
        self.assertTrue(pt.is_scalp("20260810-SCALP-12-SPY-0dte_directional"))
        self.assertEqual(pt.lane_of("20260810-SCALP-12-SPY-0dte_directional"), "scalp")

    def test_funnel_ids_unaffected_in_both_eras(self):
        for rid in ("2026-08-06-001", "2026-08-10-004"):
            self.assertFalse(pt.is_scalp(rid))
            self.assertEqual(pt.lane_of(rid), "funnel")


class ForwardOnlyMinting(unittest.TestCase):
    def test_after_cutover_mints_SCALP(self):
        self.assertEqual(pt.scalp_marker_for("2026-08-08"), "-SCALP-")
        self.assertEqual(pt.scalp_marker_for("2026-09-01"), "-SCALP-")

    def test_before_cutover_mints_legacy(self):
        """Replaying an older date must mint the ERA-CORRECT id."""
        self.assertEqual(pt.scalp_marker_for("2026-08-07"), "-TEST-")

    def test_cutover_is_dated_and_documented(self):
        self.assertEqual(pt.SCALP_RENAME_CUTOVER, "2026-08-08")


class FilterSitesRouteThroughTheCanonicalPredicate(unittest.TestCase):
    """Post-rename audit: every consumer must recognise BOTH markers."""

    def test_risk_lane_partition_handles_both(self):
        from src.engine.risk import realized_pnl_today_by_lane  # noqa: F401
        import src.engine.risk as risk
        self.assertTrue(hasattr(risk, "_is_scalp"))

    def test_feed_funnel_split_handles_both(self):
        from src.api.feed import _is_funnel
        self.assertFalse(_is_funnel("20260810-SCALP-1-SPY-x"))
        self.assertFalse(_is_funnel("20260806-TEST-1-SPY-x"))
        self.assertTrue(_is_funnel("2026-08-10-001"))

    def test_daily_scalp_cap_counts_post_rename_ids(self):
        """C-1: today_test_entry_count() matched the literal '<date>-TEST-'
        prefix, so after the rename it returned 0 UNCONDITIONALLY and the daily
        cap never bound (11 entries placed on 2026-08-10 under a cap of 20).
        Both eras must count; other days and the funnel must not."""
        from unittest.mock import patch
        from datetime import datetime
        day = f"{datetime.now():%Y%m%d}"
        book = [{"rec_id": f"{day}-SCALP-1-SPY-0dte_directional"},
                {"rec_id": f"{day}-TEST-2-QQQ-debit_spread"},
                {"rec_id": f"{day}-SCALP-1-SPY-0dte_directional"},   # dup, one id
                {"rec_id": "20260101-SCALP-9-IWM-x"},                # another day
                {"rec_id": f"2026-08-10-004"}]                       # funnel
        with patch.object(pt, "_load", return_value=[]), \
                patch.object(pt, "load_positions", return_value=book), \
                patch.object(pt, "FILLS_LOG", pt.ROOT / "does-not-exist.jsonl"):
            self.assertEqual(pt.today_test_entry_count(), 2)

    def test_funnel_exposure_does_not_count_scalp(self):
        """C-1: SCALP positions/recs counted against the FUNNEL's caps, shutting
        the Claude-gated lane out (open 4/3, recs 19/10) while real funnel
        activity was zero. Lane isolation was always the intent."""
        from unittest.mock import patch
        import src.engine.risk as risk
        scalp_book = [{"rec_id": "20260810-SCALP-1-SPY-x"},
                      {"rec_id": "20260806-TEST-2-QQQ-x"}]
        pend = [{"intent": "open", "rec_id": "20260810-SCALP-3-IWM-x"}]
        with patch.object(pt, "load_positions", return_value=scalp_book), \
                patch.object(pt, "_load", return_value=pend), \
                patch.object(risk, "ROOT", risk.ROOT / "no-such-dir"):
            open_n, recs = risk._funnel_exposure()
        self.assertEqual(open_n, 0, "SCALP must not consume funnel open slots")
        self.assertEqual(recs, 0)

    def test_no_stranded_marker_literals_on_the_money_path(self):
        """THE LESSON (L-2026-08-10): introducing is_scalp() did not retire its
        predecessors — ~15 consumers kept testing the literal string and were
        found only when the operator noticed an empty cockpit. A canonical
        accessor is only canonical if nothing bypasses it, so assert that
        rather than trusting it. Scoped to src/ (the money path + API);
        reporting scripts are tracked separately in OBJECTIVES.md.
        """
        import ast
        from pathlib import Path
        root = Path(pt.__file__).resolve().parents[2]
        offenders = []
        for path in sorted((root / "src").rglob("*.py")):
            if path.name == "paper_trader.py":
                continue          # defines the constants; is the accessor
            tree = ast.parse(path.read_text())
            # docstrings are PROSE about the markers and must stay readable —
            # only executable string constants count as bypasses
            docs = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = getattr(node, "body", None)
                    if body and isinstance(body[0], ast.Expr) and \
                            isinstance(body[0].value, ast.Constant) and \
                            isinstance(body[0].value.value, str):
                        docs.add(id(body[0].value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and id(node) not in docs \
                        and ("-TEST-" in node.value or "-SCALP-" in node.value) \
                        and not node.value.startswith("EXECUTION-TEST"):
                    # 'EXECUTION-TEST[-FALLBACK]' is the BROKER ORDER TAG — a
                    # separate namespace that merely shares the substring.
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}: "
                                     f"{node.value!r}")
        self.assertEqual(offenders, [], "marker literal outside the canonical "
                                        "accessor — route it through is_scalp():\n"
                                        + "\n".join(offenders))
