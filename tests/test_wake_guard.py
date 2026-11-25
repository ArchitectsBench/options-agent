"""Wake-guard tests (laptop-state hardening, 2026-08-02).

The host sleeping mid-session caused the 07-28/29 orphaned-position messes:
fills landed while the process was dark, and post-close wakes left open
positions unmanaged because the market-hours gate skips the whole cycle.
_wake_guard() runs BEFORE that gate. These tests pin its contract:

  - no gap (fresh ticks)            -> no sweep
  - first tick ever                 -> no sweep (startup reconcile owns that)
  - big gap, market open            -> sweep runs, NO after-hours alert
  - big gap, market closed, book open -> sweep runs + LOUD notify
  - sweep failure                   -> swallowed (loop must survive)
"""
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import src.scheduler as sched


def _run_guard(gap_min, market_open, positions, pending, poll_side_effect=None):
    """Drive _wake_guard with a synthetic previous-tick time and mocks."""
    sched._LAST_TICK = datetime.now() - timedelta(minutes=gap_min)
    pt = MagicMock()
    pt.resolve_intents.return_value = []
    pt.poll_fills.return_value = [] if poll_side_effect is None else poll_side_effect
    if isinstance(poll_side_effect, Exception):
        pt.poll_fills.side_effect = poll_side_effect
        pt.poll_fills.return_value = None
    pt.load_positions.return_value = positions
    pt._load.return_value = pending
    notify = MagicMock()
    with patch.dict("sys.modules", {}), \
         patch("src.engine.paper_trader.resolve_intents", pt.resolve_intents), \
         patch("src.engine.paper_trader.poll_fills", pt.poll_fills), \
         patch("src.engine.paper_trader.load_positions", pt.load_positions), \
         patch("src.engine.paper_trader._load", pt._load), \
         patch("src.engine.paper_trader._log_event", pt._log_event), \
         patch("src.engine.notify.send", notify), \
         patch.object(sched, "market_open", return_value=market_open):
        sched._wake_guard()
    return pt, notify


def test_no_gap_no_sweep():
    pt, notify = _run_guard(gap_min=5, market_open=True, positions=[], pending=[])
    pt.poll_fills.assert_not_called()
    notify.assert_not_called()


def test_first_tick_no_sweep():
    sched._LAST_TICK = None
    pt = MagicMock()
    with patch("src.engine.paper_trader.poll_fills", pt.poll_fills):
        sched._wake_guard()
    pt.poll_fills.assert_not_called()
    assert sched._LAST_TICK is not None  # baseline established


def test_gap_market_open_sweeps_quietly():
    pt, notify = _run_guard(gap_min=45, market_open=True,
                            positions=[{"rec_id": "x"}], pending=[])
    pt.poll_fills.assert_called_once()      # fills from the dark period recorded
    pt._log_event.assert_called_once()      # wake_gap_recovery event written
    notify.assert_not_called()              # normal cycle will manage the book


def test_gap_market_closed_with_positions_alerts_loudly():
    pt, notify = _run_guard(gap_min=120, market_open=False,
                            positions=[{"rec_id": "x"}], pending=[{"o": 1}])
    pt.poll_fills.assert_called_once()
    notify.assert_called_once()             # human must know NOW
    assert notify.call_args.kwargs.get("priority") == "high"
    ev = pt._log_event.call_args.args[0]
    assert ev["event"] == "wake_gap_recovery" and ev["positions_open"] == 1


def test_gap_market_closed_flat_book_stays_quiet():
    pt, notify = _run_guard(gap_min=120, market_open=False, positions=[], pending=[])
    pt.poll_fills.assert_called_once()
    notify.assert_not_called()


def test_sweep_failure_never_raises():
    pt, notify = _run_guard(gap_min=45, market_open=True, positions=[], pending=[],
                            poll_side_effect=RuntimeError("broker down"))
    # reaching here without an exception IS the assertion; loop must survive
    notify.assert_not_called()
