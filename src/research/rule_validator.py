"""Stage 2 — Rule Validation.

Validates a strategy BEFORE it is allowed near the backtester:
  1. Spec completeness: entry/exit rules and execution timing are declared.
  2. Unambiguity: entry and exit rules are distinct; entry and exit never
     fire on the same bar; long and short entries never fire together.
  3. Computability: signals compute over the available data without error,
     boolean signals are bool dtype, level series are numeric, all aligned
     to the input index.
  4. No lookahead bias (empirical): ALL emitted series — boolean signals
     AND stop/target levels — are recomputed on truncated copies of the
     data at multiple cut points; previously emitted values must be
     identical. A rule that peeks at future bars changes its past when the
     future is removed.

Strategies that fail are rejected and logged. Invalid rules are never
backtested.
"""

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from src.research.signals import Signals, as_signals

REQUIRED_FIELDS = ["direction", "timeframe", "entry", "exit", "execution"]

# SignalFunc: df -> Signals (or legacy (entry, exit) tuple)
SignalFunc = Callable[[pd.DataFrame], object]


@dataclass
class ValidationReport:
    strategy: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = ""):
        self.checks.append((name, passed, detail))

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def summary(self) -> str:
        lines = [f"Stage 2 — Rule Validation: {self.strategy}"]
        for name, ok, detail in self.checks:
            mark = "PASS" if ok else "FAIL"
            lines.append(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
        lines.append(f"  => {'VALID — eligible for backtest' if self.passed else 'REJECTED — will not be backtested'}")
        return "\n".join(lines)


def validate_strategy(
    name: str,
    spec: dict,
    signal_func: SignalFunc,
    df: pd.DataFrame,
    lookahead_cuts: int = 5,
) -> ValidationReport:
    report = ValidationReport(strategy=name)

    # 1. Spec completeness
    missing = [f for f in REQUIRED_FIELDS if f not in spec]
    report.record(
        "spec completeness",
        not missing,
        "all required fields present" if not missing else f"missing: {missing}",
    )

    # 2a. Entry and exit rules are distinct
    distinct = spec.get("entry") != spec.get("exit")
    report.record(
        "entry/exit distinct",
        distinct,
        f"entry={spec.get('entry')!r}, exit={spec.get('exit')!r}",
    )

    # 3. Computability
    try:
        sigs = as_signals(signal_func(df))
        named = sigs.named_series()
        problems = []
        for fname, s in named.items():
            if not s.index.equals(df.index):
                problems.append(f"{fname}: index misaligned")
            elif fname in Signals.BOOL_FIELDS and s.dtype != bool:
                problems.append(f"{fname}: dtype {s.dtype}, expected bool")
            elif fname in Signals.LEVEL_FIELDS and not pd.api.types.is_numeric_dtype(s):
                problems.append(f"{fname}: dtype {s.dtype}, expected numeric")
        counts = ", ".join(
            f"{int(named[f].sum())} {f}" for f in Signals.BOOL_FIELDS if f in named
        )
        report.record(
            "computable from available data",
            not problems,
            f"{counts} over {len(df)} bars" if not problems else "; ".join(problems),
        )
    except Exception as exc:
        report.record("computable from available data", False, f"signal computation raised: {exc}")
        return report  # remaining checks need signals

    # 2b. Unambiguity: contradictory same-bar combinations
    conflicts = []
    for a, b in [("entry_long", "exit_long"), ("entry_short", "exit_short"), ("entry_long", "entry_short")]:
        if a in named and b in named:
            both = int((named[a] & named[b]).sum())
            if both:
                conflicts.append(f"{a}&{b} on {both} bars")
    report.record(
        "non-contradictory (no conflicting same-bar signals)",
        not conflicts,
        "; ".join(conflicts) if conflicts else "0 conflicting bars",
    )

    # 4. Empirical lookahead test over every emitted series
    n = len(df)
    cut_points = [int(n * frac) for frac in [0.4, 0.55, 0.7, 0.85, 0.95]][:lookahead_cuts]
    leaks = []
    for cut in cut_points:
        cut_named = as_signals(signal_func(df.iloc[:cut])).named_series()
        for fname, s in named.items():
            if fname not in cut_named or not cut_named[fname].equals(s.iloc[:cut]):
                leaks.append(f"{fname}@bar{cut}")
    report.record(
        "no lookahead bias (truncation test)",
        not leaks,
        f"all series stable across {len(cut_points)} truncation points"
        if not leaks
        else f"changed when future removed: {leaks[:6]} — rule uses future data",
    )

    # Execution timing must not be same-bar
    execution = spec.get("execution", "")
    report.record(
        "execution timing declared (next-bar)",
        execution.startswith("next_bar"),
        f"execution={execution!r}",
    )

    return report
