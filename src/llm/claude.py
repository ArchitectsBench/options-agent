"""Claude API validation layer (Track B step 9) — the senior trader.

SAFETY ARCHITECTURE (post-session fix 2026-06-11):
The rule is: local confidence >= invoke_threshold REQUIRES Claude approval
to proceed. There is no local-only approval path. resolve_validation() is
the single gate and returns exactly one of:

  approved     — Claude approved now, OR a prior approval within the
                 cooldown window applies as a follow-on (same direction,
                 |spot drift| < 0.5 ATR, regime bias unchanged)
  vetoed       — Claude vetoed now
  veto_active  — a prior VETO for this ticker+strategy is still in its
                 cooldown window: candidate REJECTED without an API call
  held         — Claude cannot be consulted (cooldown without a usable
                 verdict, budget exhausted, no key, API error): candidate
                 logged, NO order

Only "approved" may produce an order. Verdicts are persisted per
ticker+strategy in data/claude_budget.json with the snapshot needed for
the follow-on check.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

PROMPT_FILE = ROOT / "src" / "llm" / "prompts" / "claude_validation.md"
BUDGET_FILE = ROOT / "data" / "claude_budget.json"

logger = logging.getLogger("trader")

DEFAULT_CFG = {
    "enabled": True,            # 2026-09-29: operator off-switch (config.yaml governs, see O-33)
    "model": "claude-opus-5",   # 2026-09-14: 4.8 -> 5, same price, drop-in (config.yaml governs)
    "invoke_threshold": 0.75,
    "max_calls_per_day": 20,
    "ticker_cooldown_minutes": 90,
}

MAX_FOLLOW_ON_DRIFT_ATR = 0.5
DISABLED_REASON = "candidate_held (validator disabled by operator 2026-09-29)"


class ClaudeVerdict(BaseModel):
    approve: bool
    confidence: float
    veto_reason: str | None = None
    adjustments: str | None = None
    narrative: str = ""        # default so a missing narrative doesn't fail validation


class BudgetGuard:
    def __init__(self, max_calls: int, cooldown_minutes: int):
        self.max_calls = max_calls
        self.cooldown_s = cooldown_minutes * 60
        self.state = self._load()

    def _load(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        if BUDGET_FILE.exists():
            try:
                state = json.loads(BUDGET_FILE.read_text())
                if state.get("date") == today:
                    state.setdefault("verdicts", {})
                    state.setdefault("ticker_last", {})
                    return state
            except json.JSONDecodeError:
                pass
        return {"date": today, "calls": 0, "ticker_last": {}, "verdicts": {}}

    def _save(self):
        BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
        BUDGET_FILE.write_text(json.dumps(self.state, default=str))

    def allow(self, ticker: str) -> tuple[bool, str]:
        if self.state["calls"] >= self.max_calls:
            return False, f"daily budget exhausted ({self.state['calls']}/{self.max_calls})"
        last = self.state["ticker_last"].get(ticker)
        if last:
            elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
            if elapsed < self.cooldown_s:
                return False, f"{ticker} cooldown ({elapsed / 60:.0f}m of {self.cooldown_s / 60:.0f}m)"
        return True, ""

    def record_call(self, ticker: str):
        self.state["calls"] += 1
        self.state["ticker_last"][ticker] = datetime.now().isoformat()
        self._save()

    def record_verdict(self, ticker: str, strategy: str, status: str,
                       snapshot: dict, verdict: dict | None):
        self.state["verdicts"][f"{ticker}:{strategy}"] = {
            "status": status,
            "ts": datetime.now().isoformat(),
            "snapshot": snapshot,
            "verdict": verdict,
        }
        self._save()

    def verdict_within_cooldown(self, ticker: str, strategy: str) -> dict | None:
        v = self.state["verdicts"].get(f"{ticker}:{strategy}")
        if not v:
            return None
        age = (datetime.now() - datetime.fromisoformat(v["ts"])).total_seconds()
        if age >= self.cooldown_s:
            return None
        return {**v, "remaining_min": (self.cooldown_s - age) / 60}

    @property
    def used(self) -> str:
        return f"{self.state['calls']}/{self.max_calls}"


def follow_on_ok(prior_snapshot: dict, current: dict) -> tuple[bool, str]:
    """A prior APPROVAL extends to a follow-on candidate only if the
    structure is identical and conditions haven't degraded."""
    if prior_snapshot.get("direction") != current.get("direction"):
        return False, "direction changed"
    if prior_snapshot.get("regime_bias") != current.get("regime_bias"):
        return False, "regime bias changed"
    atr, spot, prior_spot = current.get("atr"), current.get("spot"), prior_snapshot.get("spot")
    if not atr or spot is None or prior_spot is None:
        return False, "insufficient data for drift check"
    drift = abs(spot - prior_spot)
    if drift >= MAX_FOLLOW_ON_DRIFT_ATR * atr:
        return False, f"price drift {drift:.2f} >= 0.5 ATR ({atr:.2f})"
    return True, f"direction/regime unchanged, drift {drift:.2f} < 0.5 ATR"


def check_gate(ticker: str, strategy: str, current: dict, cfg: dict | None = None) -> dict | None:
    """Cheap pre-check (no API call, no chain calls needed). Returns a
    terminal result (veto_active / held / follow-on approved) or None,
    meaning: proceed to build the trade and call resolve_validation."""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    guard = BudgetGuard(cfg["max_calls_per_day"], cfg["ticker_cooldown_minutes"])

    # OPERATOR OFF-SWITCH (2026-09-29, "keep everything except claude veto
    # turning off"). Checked FIRST: a stored approval inside its cooldown would
    # otherwise pass a follow-on with no API call, and "off" must mean the
    # funnel books nothing unvalidated — HOLD, never local-only approval.
    if not cfg.get("enabled", True):
        return {"status": "held", "reason": DISABLED_REASON,
                "verdict": None, "budget_used": guard.used}

    v = guard.verdict_within_cooldown(ticker, strategy)
    if v and v["status"] == "vetoed":
        return {"status": "veto_active",
                "reason": f"claude_veto_active ({v['remaining_min']:.0f}m remaining)",
                "verdict": v.get("verdict"), "budget_used": guard.used}
    if v and v["status"] == "approved":
        ok, why = follow_on_ok(v.get("snapshot", {}), current)
        if ok:
            return {"status": "approved", "follow_on": True,
                    "reason": f"follow-on within cooldown: {why}",
                    "verdict": v.get("verdict"), "budget_used": guard.used}
        return {"status": "held",
                "reason": f"candidate_held (prior approval in cooldown but {why})",
                "verdict": None, "budget_used": guard.used}

    ok, reason = guard.allow(ticker)
    if not ok:
        # cooldown/budget with no usable verdict: conf >= threshold requires
        # Claude — NEVER fall through to local-only approval.
        return {"status": "held", "reason": f"candidate_held ({reason})",
                "verdict": None, "budget_used": guard.used}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"status": "held", "reason": "candidate_held (no ANTHROPIC_API_KEY)",
                "verdict": None, "budget_used": guard.used}
    return None  # callable — proceed


def resolve_validation(context: dict, ticker: str, strategy: str, current: dict,
                       cfg: dict | None = None) -> dict:
    """The single validation gate. Re-runs the cheap gate, then calls the
    API; records the verdict for cooldown-window enforcement."""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    gated = check_gate(ticker, strategy, current, cfg)
    if gated is not None:
        return gated

    guard = BudgetGuard(cfg["max_calls_per_day"], cfg["ticker_cooldown_minutes"])
    if not cfg.get("enabled", True):   # belt and braces: no API client is ever built
        return {"status": "held", "reason": DISABLED_REASON,
                "verdict": None, "budget_used": guard.used}
    import anthropic

    client = anthropic.Anthropic()
    try:
        response = client.messages.parse(
            model=cfg["model"],
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=PROMPT_FILE.read_text(),
            messages=[{"role": "user", "content": json.dumps(context, default=str)}],
            output_format=ClaudeVerdict,
        )
        guard.record_call(ticker)
        if response.stop_reason == "refusal":
            return {"status": "held", "reason": "candidate_held (model refused the request)",
                    "verdict": None, "budget_used": guard.used}
        verdict: ClaudeVerdict = response.parsed_output
        status = "approved" if verdict.approve else "vetoed"
        guard.record_verdict(ticker, strategy, status, current, verdict.model_dump())
        return {"status": status, "verdict": verdict.model_dump(), "reason": None,
                "budget_used": guard.used, "model": cfg["model"]}
    except anthropic.APIError as exc:
        logger.error("Claude validation failed: %s", exc)
        return {"status": "held", "reason": f"candidate_held (API error: {exc})",
                "verdict": None, "budget_used": guard.used}
    except Exception as exc:
        # malformed verdict / parse / validation error -> HELD (veto-default safe),
        # never crash the cycle (fixes 2026-06-18 ClaudeVerdict validation crash)
        logger.error("Claude verdict parse/validation failed: %s", exc)
        return {"status": "held", "reason": f"candidate_held (verdict parse error: {exc})",
                "verdict": None, "budget_used": guard.used}
