"""Stage 2 rule-validator tests (src/research/rule_validator.py).

Covers the four checks validate_strategy() runs, each with a signal function
built to trip exactly one of them:
  * a clean, causal strategy PASSES end to end;
  * a signal that peeks at the NEXT bar is REJECTED by the truncation-based
    lookahead test (acceptance criterion #2 — a biased strategy must be
    provably rejected);
  * entry and exit firing on the same bar trip the non-contradictory check;
  * a spec missing required fields trips spec-completeness.

Expected values are reasoned from the rules, not read back from the code.

Run: .venv/bin/python -m unittest tests.test_rule_validator -v
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.rule_validator import validate_strategy  # noqa: E402
from src.research.signals import Signals  # noqa: E402


def _df(n=12):
    idx = pd.date_range("2022-01-03 09:30", periods=n, freq="1h")
    close = pd.Series(
        [10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18][:n], index=idx, dtype=float)
    return pd.DataFrame(
        {"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close},
        index=idx)


def _clean(df):
    """Causal: rolling(2) mean uses only current+prior bars -> truncation-stable."""
    c = df["Close"]
    ma = c.rolling(2).mean()
    return Signals(entry_long=(c > ma).fillna(False), exit_long=(c < ma).fillna(False))


def _lookahead(df):
    """Entry peeks at the NEXT bar (c.shift(-1)) -> changes its past when the
    future is truncated away, which the validator must catch."""
    c = df["Close"]
    ma = c.rolling(2).mean()
    return Signals(entry_long=(c < c.shift(-1)).fillna(False),
                   exit_long=(c < ma).fillna(False))


def _conflicting(df):
    """entry_long and exit_long both fire on every bar -> same-bar contradiction."""
    c = df["Close"]
    both = (c > 0).fillna(False)
    return Signals(entry_long=both, exit_long=both)


CLEAN_SPEC = {"direction": "long", "timeframe": "1h", "entry": "close>ma2",
              "exit": "close<ma2", "execution": "next_bar_open"}


def _check(report, keyword):
    """The passed-flag of the first check whose name contains keyword."""
    return next(ok for name, ok, _ in report.checks if keyword in name)


class TestRuleValidator(unittest.TestCase):
    def test_clean_strategy_passes(self):
        report = validate_strategy("clean", CLEAN_SPEC, _clean, _df())
        self.assertTrue(report.passed, report.summary())

    def test_lookahead_strategy_rejected(self):
        spec = {**CLEAN_SPEC, "entry": "close<next_close"}
        report = validate_strategy("biased", spec, _lookahead, _df())
        self.assertFalse(report.passed)
        # the failure must be the lookahead check specifically
        self.assertFalse(_check(report, "lookahead"))
        self.assertTrue(_check(report, "spec completeness"))

    def test_same_bar_conflict_rejected(self):
        report = validate_strategy("conflict", CLEAN_SPEC, _conflicting, _df())
        self.assertFalse(report.passed)
        self.assertFalse(_check(report, "non-contradictory"))

    def test_missing_required_fields_rejected(self):
        spec = {"direction": "long", "timeframe": "1h", "entry": "close>ma2"}
        report = validate_strategy("incomplete", spec, _clean, _df())
        self.assertFalse(report.passed)
        self.assertFalse(_check(report, "spec completeness"))


if __name__ == "__main__":
    unittest.main()
