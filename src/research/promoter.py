"""Stage 6 — Acceptance & Promotion.

Writes candidates that clear the FULL acceptance gate (Sharpe, OOS, OOS-vs-IS,
maxDD, MC 5th-pct, the per-regime acid test, and trade count) to
config/validated_strategies.yaml with parameters FROZEN, plus full provenance
(metrics, the gate result, run id, data window, timestamp).

Invariants (CLAUDE.md — these do not move):
  * promote() RE-CHECKS the gate and REFUSES to write anything that fails even one
    criterion. Nothing reaches this file that wasn't validated end to end.
  * The live agent (Track B) loads ONLY from this file. Empty/absent => observation
    mode only; live (real-money) mode is impossible without a passing entry here.

  load_validated_strategies()  -> list[dict]   (single source of truth for readers)
  promote(family, ticker, report, run_id, data_window) -> dict (the written entry)
"""
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from src.research.monte_carlo import MonteCarloReport, acceptance_gate

ROOT = Path(__file__).resolve().parents[2]
VALIDATED_FILE = ROOT / "config" / "validated_strategies.yaml"


def _r(x, nd=4):
    """Round, turning NaN/None into None so the YAML stays clean."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    return round(float(x), nd) if isinstance(x, (int, float)) else x


def entry_id(family: str, ticker: str) -> str:
    return f"{family}:{ticker}"


def load_validated_strategies() -> list[dict]:
    """Frozen, promoted strategies. [] when the file is absent or empty — which is
    exactly the observation-mode condition the live agent checks."""
    if not VALIDATED_FILE.exists():
        return []
    data = yaml.safe_load(VALIDATED_FILE.read_text()) or {}
    return data.get("strategies", []) or []


def build_entry(family: str, ticker: str, report: MonteCarloReport,
                run_id: str, data_window: str | None = None) -> dict:
    gate = acceptance_gate(report)
    return {
        "id": entry_id(family, ticker),
        "family": family,
        "ticker": ticker,
        "status": "validated",
        "params": {k: _r(v) for k, v in report.params.items()},  # FROZEN
        "promoted_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "data_window": data_window,
        "metrics": {
            "full_sharpe": _r(report.full_sharpe),
            "is_sharpe": _r(report.is_sharpe),
            "oos_sharpe": _r(report.oos_sharpe),
            "mc_p05_sharpe": _r(report.mc_p05_sharpe),
            "worst_year_sharpe": _r(report.worst_year_sharpe),
            "max_drawdown": _r(report.full_max_dd),
            "trades": int(report.full_trades),
            "regime_by_year": {int(y): _r(s) for y, s in (report.regime_by_year or {}).items()},
        },
        "gate": [{"criterion": c, "passed": bool(ok), "actual": a} for c, ok, a in gate],
    }


def promote(family: str, ticker: str, report: MonteCarloReport,
            run_id: str, data_window: str | None = None) -> dict:
    """Validate-then-write. HARD GUARD: re-runs the acceptance gate and raises if any
    criterion fails — so a buggy caller can never sneak an unvalidated strategy into
    the live agent's only source of truth. Replaces any existing same-id entry
    (re-promotion with frozen params); appends otherwise."""
    gate = acceptance_gate(report)
    failed = [c for c, ok, _ in gate if not ok]
    if failed:
        raise ValueError(f"refusing to promote {entry_id(family, ticker)} — "
                         f"failed acceptance criteria: {failed}")

    entry = build_entry(family, ticker, report, run_id, data_window)
    strategies = [e for e in load_validated_strategies() if e.get("id") != entry["id"]]
    strategies.append(entry)
    VALIDATED_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Stage-6 validated strategies — written ONLY by src/research/promoter.py\n"
              "# after a candidate clears the FULL acceptance gate. Params are frozen.\n"
              "# The live agent loads ONLY from here; empty/absent => observation mode.\n")
    VALIDATED_FILE.write_text(header + yaml.safe_dump({"strategies": strategies}, sort_keys=False))
    return entry
