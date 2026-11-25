"""Weekly forward-acceptance progress push (roadmap workstream 2, 2026-07-02).

Runs Fridays after the close (launchd com.optionsagent.weeklyfwd): evaluates
progress against the FROZEN criteria in config/forward_acceptance.yaml and
pushes a compact summary via ntfy + macOS notification, so the forward clock
is visible without asking. Read-only; exit 0 always.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.forward_acceptance_check import evaluate  # noqa: E402
from src.engine.notify import send  # noqa: E402


def main():
    try:
        r = evaluate(ROOT)
    except Exception as exc:  # report, never crash the schedule
        send("Forward-acceptance weekly", f"evaluator failed: {exc}", priority="high")
        print(f"evaluator failed: {exc}")
        return
    p = r.get("progress", {})
    o = r.get("overall", {})

    def mark(key):
        return "MET" if (p.get(key) or {}).get("met") else "not met"

    dd = p.get("max_drawdown", {})
    lines = [
        o.get("summary", "no summary"),
        f"P&L {r.get('total_pnl', 0):+.0f} | Sharpe {(p.get('sharpe_ann') or {}).get('value', '?')}"
        f" ({mark('sharpe_ann')}) | PF {(p.get('profit_factor') or {}).get('value', '?')}"
        f" ({mark('profit_factor')})",
        f"DD {dd.get('pct_of_risk_capital', '?')}% of risk capital ({mark('max_drawdown')}) | "
        f"sleeves {mark('per_sleeve')} | concentration {mark('concentration')}",
        f"adverse-regime week: {mark('adverse_regime')} | criteria "
        f"{o.get('criteria_met', '?')}/{o.get('criteria_total', '?')}",
    ]
    body = "\n".join(lines)
    print(body)
    send("Forward-acceptance weekly progress", body, tags="chart_with_upwards_trend")


if __name__ == "__main__":
    main()
