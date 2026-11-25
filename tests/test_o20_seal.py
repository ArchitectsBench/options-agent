"""O-20 — both layers proven INDEPENDENTLY against BOTH real specimens."""
import builtins
from pathlib import Path

import pytest

from tests.conftest import ProductionWriteRefused, _is_protected, REPO

BOOK = REPO / "data" / "lane" / "funnel" / "positions.json"      # specimen 1 target
RECS = REPO / "logs" / "recommendations.jsonl"                    # specimen 2 target


class TestLayer2WriteRefusal:
    """LAYER 2 alone: a production WRITE raises, whatever the caller."""

    def test_specimen1_position_book_write_is_refused(self):
        with pytest.raises(ProductionWriteRefused):
            builtins.open(BOOK, "w").write("[]")

    def test_specimen2_recommendations_write_is_refused(self):
        with pytest.raises(ProductionWriteRefused):
            builtins.open(RECS, "a").write("{}\n")

    def test_append_and_update_modes_are_refused(self):
        for mode in ("w", "a", "x", "r+"):
            with pytest.raises(ProductionWriteRefused):
                builtins.open(RECS, mode)

    def test_reads_are_still_allowed(self):
        """The seal blocks WRITES; a read of production config must still work
        so tests that legitimately inspect shipped config keep functioning."""
        assert (REPO / "config" / "config.yaml").exists()
        builtins.open(REPO / "config" / "config.yaml").close()

    def test_tmp_writes_are_unaffected(self, tmp_path):
        f = tmp_path / "ok.json"
        builtins.open(f, "w").write("[]")
        assert f.read_text() == "[]"

    def test_protection_covers_data_logs_config(self):
        assert _is_protected(REPO / "data" / "x")
        assert _is_protected(REPO / "logs" / "x")
        assert _is_protected(REPO / "config" / "x")
        assert not _is_protected(REPO / "src" / "x")


class TestLayer1Redirect:
    """LAYER 1 alone: production module paths point at tmp, so even an
    unpatched test writes harmlessly — nothing has to remember."""

    def test_specimen1_book_paths_are_redirected(self):
        import src.engine.paper_trader as pt
        assert str(REPO / "data") not in str(pt.LANE_DIR), \
            "LANE_DIR must be redirected away from production data/"
        assert str(REPO / "logs") not in str(pt.FILLS_LOG)

    def test_specimen2_rec_log_is_redirected(self):
        import src.engine.recommendation as rec
        assert str(REPO / "logs") not in str(rec.REC_LOG), \
            "REC_LOG must be redirected — this is the file that leaked for 5 weeks"

    def test_risk_breaker_input_is_redirected(self):
        import src.engine.risk as risk
        assert str(REPO / "logs") not in str(risk.FILLS_LOG)

    def test_writing_through_the_redirected_path_hits_tmp(self):
        """The specimen-1 shape: write the book WITHOUT patching anything."""
        import src.engine.paper_trader as pt
        pt._save(pt.POSITIONS_FILE, [{"rec_id": "2026-01-01-001", "ticker": "X"}])
        assert len(pt.load_positions()) == 1
        assert str(REPO / "data") not in str(pt.lane_book_path("positions", "funnel"))


class TestLayer2BypassSurface:
    """Layer 2 patches builtins.open. Path.write_text resolves io.open
    separately, so coverage must be DEMONSTRATED, not assumed."""

    def test_path_write_text_to_production_is_also_refused(self):
        with pytest.raises((ProductionWriteRefused, PermissionError, OSError)):
            RECS.write_text("{}\n")
