"""Local LLM layer (Ollama) — every-cycle signal synthesis (Track B step 8).

Contract:
  - Input: the observation-record snapshot (regime + signals + applicability
    verdicts). Never raw bars.
  - Output: strict JSON per prompts/local_synthesis.md. format="json" +
    temperature 0.1 enforce structure; one retry on parse failure, then a
    malformed-output event is logged and the cycle proceeds WITHOUT
    synthesis (the loop never crashes on a bad generation).
  - Model selection: primary from config, fallback if the primary exceeds
    max_latency_seconds (measured, per call).
"""

import json
import logging
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT_FILE = ROOT / "src" / "llm" / "prompts" / "local_synthesis.md"
OLLAMA_URL = "http://localhost:11434/api/chat"

logger = logging.getLogger("trader")

DEFAULT_CFG = {
    "model": "llama3.1:8b",   # keep in sync with config.yaml (qwen2.5:32b
    "fallback": "llama3.1:8b",  # benchmarked unusable on this machine)
    "max_latency_seconds": 30,
}


MAX_TICKERS_TO_LLM = 10
_FIRED = ("fired_long", "fired_short")


def _interest_score(ticker: str, sig: dict, applicability: dict) -> float:
    """Deterministic pre-rank (weekend fix #1): the model only needs the
    interesting snapshots, not all 30 — 30 full snapshots pushed synthesis
    to 48-78s and periodic timeouts on 6/12."""
    if "error" in sig:
        return -1
    score = 0.0
    verdicts = applicability.get(ticker, {})
    score += 3.0 * sum(1 for v in verdicts.values() if v.get("verdict") == "viable")
    sq = sig.get("squeeze", {})
    score += 2.0 * sum(1 for tf in sq.values() if tf in _FIRED)
    score += 0.5 * sum(1 for tf in sq.values() if tf == "on")
    rs = sig.get("rs_vs_spy_30min_pct")
    score += min(abs(rs), 2.0) if rs is not None else 0
    vol = sig.get("volume_ratio")
    score += 0.5 if vol is not None and vol >= 1.5 else 0
    return score


def _strip_for_llm(record: dict) -> dict:
    """The snapshot the model sees: regime + the TOP-N interesting tickers'
    signals + verdicts, minus internal bookkeeping. Lineage stays — the
    model should know nothing here is validated."""
    regime = {k: v for k, v in record["regime"].items() if k != "ts"}
    applicability = record.get("applicability", {})
    ranked = sorted(record["signals"],
                    key=lambda t: _interest_score(t, record["signals"][t], applicability),
                    reverse=True)[:MAX_TICKERS_TO_LLM]
    return {
        "mode": record["mode"],
        "regime": regime,
        "note": f"signals pre-filtered to the {len(ranked)} most active of "
                f"{len(record['signals'])} scanned tickers",
        "signals": {t: record["signals"][t] for t in ranked},
        "applicability": {t: applicability[t] for t in ranked if t in applicability},
        "signal_lineage": record["signal_lineage"],
    }


def _call_ollama(model: str, system: str, payload: str, timeout: int = 120) -> tuple[str, dict]:
    """Returns (content, perf) where perf has latency/token stats."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": payload},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.1},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read())
    latency = time.time() - t0
    eval_count = out.get("eval_count", 0)
    eval_s = out.get("eval_duration", 0) / 1e9
    perf = {
        "model": model,
        "latency_s": round(latency, 1),
        "output_tokens": eval_count,
        "tokens_per_s": round(eval_count / eval_s, 1) if eval_s > 0 else None,
        "prompt_tokens": out.get("prompt_eval_count"),
    }
    return out["message"]["content"], perf


def _validate(parsed: dict) -> bool:
    if not isinstance(parsed, dict) or "candidates" not in parsed or "cycle_assessment" not in parsed:
        return False
    return all(
        isinstance(c, dict) and {"ticker", "strategy", "final_confidence", "reasoning"} <= set(c)
        for c in parsed["candidates"]
    )


def synthesize(record: dict, cfg: dict | None = None) -> dict:
    """Run local synthesis on a cycle record. Returns
    {"synthesis": parsed_json | None, "perf": {...}, "error": str | None}."""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    system = PROMPT_FILE.read_text()
    payload = json.dumps(_strip_for_llm(record), default=str)

    model = cfg["model"]
    for attempt in range(2):  # parse-or-retry once
        try:
            content, perf = _call_ollama(model, system, payload)
        except Exception as exc:
            if model != cfg["fallback"]:
                logger.warning("local LLM %s failed (%s) — falling back to %s", model, exc, cfg["fallback"])
                model = cfg["fallback"]
                continue
            logger.error("local LLM unavailable: %s — cycle proceeds without synthesis", exc)
            return {"synthesis": None, "perf": None, "error": f"ollama unavailable: {exc}"}

        if perf["latency_s"] > cfg["max_latency_seconds"] and model != cfg["fallback"]:
            logger.warning("local LLM %s took %.1fs > %ds budget — switching to fallback %s",
                           model, perf["latency_s"], cfg["max_latency_seconds"], cfg["fallback"])
            model = cfg["fallback"]
            continue

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None and _validate(parsed):
            return {"synthesis": parsed, "perf": perf, "error": None}
        logger.warning("local LLM malformed output (attempt %d, model %s)", attempt + 1, model)

    logger.error("local LLM produced malformed JSON twice — skipping synthesis this cycle")
    return {"synthesis": None, "perf": perf, "error": "malformed JSON after retry"}
