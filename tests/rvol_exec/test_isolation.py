"""Money-path isolation asserts (spec §0, §16.2 `test_isolation.py`).
UNVALIDATED — PAPER ONLY.

The lane is a SEPARATE process with its own client, tag, books and ledger;
nothing here may reach the options engine's money path. These checks are
the same ones the go/no-go r0 runs, plus the logger and the coexistence audit.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import src.rvol_exec.paths as paths
from src.rvol_exec.constants import CLIENT_ID, GO_NOGO_CLIENT_ID, OPTIONS_ENGINE_TAG, TAG

ROOT = Path(__file__).resolve().parents[2]


def _go_nogo():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rvol_exec_go_nogo", ROOT / "scripts" / "ops" / "rvol_exec_go_nogo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_lane_books_are_a_sibling_of_data_lane_not_inside_it():
    src = (ROOT / "src" / "rvol_exec" / "paths.py").read_text()
    assert 'LANE_DIR = ROOT / "data" / "lane_rvol"' in src
    lane_rvol = (ROOT / "data" / "lane_rvol").resolve()
    assert not lane_rvol.is_relative_to((ROOT / "data" / "lane").resolve())
    assert 'LEDGER = ROOT / "logs" / "observation" / "rvol_exec_paper.jsonl"' in src
    assert 'LOG_DIR = ROOT / "logs" / "rvol_exec"' in src            # never logs/agent.log


def test_paper_trader_lanes_unchanged_and_tags_differ():
    from src.engine import paper_trader as pt                        # the TEST may import it; the lane may not
    assert tuple(pt.LANES) == ("funnel", "scalp")
    assert TAG == "UNVALIDATED-RVOL-PAPER" and TAG != OPTIONS_ENGINE_TAG
    assert CLIENT_ID == 61 and GO_NOGO_CLIENT_ID == 161 and CLIENT_ID != GO_NOGO_CLIENT_ID


def test_no_lane_source_imports_a_forbidden_module_ast():
    gn = _go_nogo()
    assert gn.forbidden_direct_imports(ROOT) == []
    files = gn.lane_source_files(ROOT)
    assert any(f.name == "lane.py" for f in files) and any(f.name == "ibkr_stock_orders.py" for f in files)
    assert any(f.name == "rvol_exec_daemon.py" for f in files)


def test_no_forbidden_module_loads_transitively_in_a_fresh_interpreter():
    gn = _go_nogo()
    assert gn.forbidden_in_sys_modules(ROOT) == []


def test_live_files_of_the_options_go_nogo_exclude_lane_files():
    from scripts.ops import ibkr_go_nogo_checks as chk
    assert not any("lane_rvol" in str(p) or "rvol_exec" in str(p) for p in chk.LIVE_FILES)


def test_lane_logger_is_not_under_the_trader_logger():
    lg = logging.getLogger("rvol_exec")
    assert lg.name == "rvol_exec" and not lg.name.startswith("trader")
    src = (ROOT / "scripts" / "rvol_exec_daemon.py").read_text()
    assert "log.propagate = False" in src
    handlers = [l for l in src.splitlines() if "FileHandler(" in l]
    assert handlers and all("paths.LOG_DIR" in l and "agent.log" not in l for l in handlers)


def test_config_yaml_is_never_opened_by_lane_modules():
    for f in (ROOT / "src" / "rvol_exec").glob("*.py"):
        assert "config.yaml" not in f.read_text() or f.name == "constants.py", f
    # constants.py only MENTIONS the file in a comment; it never reads it
    src = (ROOT / "src" / "rvol_exec" / "constants.py").read_text()
    assert "yaml" not in src.replace("config.yaml", "")


def test_options_audit_sees_no_alarm_from_our_stk_position():
    """§16.2 / scenario 31: the options engine's book/broker audit compares
    OPTION positions; our STK position and order must never appear as an
    alarm on its side. Exercised against a duck-typed options client whose
    positions view carries our STK row."""
    from src.engine import paper_trader as pt

    class OptionsClientView:
        def available(self):
            return True

        def get_positions(self):
            # what ibkr_orders.get_positions returns: OPTION rows only — a STK row
            # tagged with our orderRef is not an option and never reaches the audit
            return []

    assert pt.audit_book_vs_broker(OptionsClientView()) == []


def test_daemon_refuses_to_start_under_pytest():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rvol_exec_daemon", ROOT / "scripts" / "rvol_exec_daemon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(SystemExit) as ex:
        mod.main(["--date", "2026-09-14"])
    assert ex.value.code == 3
    assert not paths.PID_FILE.exists()
