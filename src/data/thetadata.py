"""ThetaData local-terminal client (Options Standard subscription, 2026-07-14).

The vendor's model: a local Theta Terminal (Java) authenticates with the API
key and serves REST on 127.0.0.1:25503; this module manages that process and
wraps the v3 endpoints we use. Research/data layer ONLY — nothing in the live
engine imports this.

Key: THETADATA_KEY in .env (never logged). Terminal jar: ~/ThetaTerminal/.
Plan limit: 4 concurrent requests — this client is deliberately sequential.
"""
from __future__ import annotations

import io
import logging
import os
import subprocess
import time
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BASE = "http://127.0.0.1:25503"
TERMINAL_DIR = Path.home() / "ThetaTerminal"
JAVA = "/opt/homebrew/opt/openjdk@21/bin"
ROOT = Path(__file__).resolve().parents[2]


def _env_key() -> str | None:
    envf = ROOT / ".env"
    if not envf.exists():
        return None
    for line in envf.read_text().splitlines():
        if line.startswith("THETADATA_KEY="):
            return line.split("=", 1)[1].strip()
    return None


def terminal_alive() -> bool:
    try:
        requests.get(f"{BASE}/v3/option/list/expirations",
                     params={"symbol": "SPY"}, timeout=5)
        return True
    except Exception:
        return False


def entitlement_probe(symbol: str = "SPY") -> tuple[str, str]:
    """Is the option HISTORY QUOTE endpoint actually entitled? -> (state, detail).

    `terminal_alive()` / `ensure_terminal()` only prove the JVM serves: they hit
    /v3/option/list/expirations, which the FREE tier is allowed to answer. The
    endpoint enrichment actually needs (/v3/option/history/quote) is paid, and a
    FREE key 403s it. On 2026-10-22 that gap made a nightly report read
    "prewarm succeeded, serving=True" and conclude the 89 `no_market_data` rows
    were a bug in the already-enriched predicate — while a direct probe of two
    of those exact contracts returned HTTP 403 "you only have a FREE
    subscription". Entitlement is a separate fact from liveness; measure it.

    States: 'entitled' (200/472/No-data — the endpoint answered us),
    'free_tier' (403 subscription refusal), 'unreachable' (terminal not serving
    or transport error), 'unknown' (any other non-200)."""
    exps = list_expirations(symbol)
    if not exps:
        return "unreachable", "list/expirations returned nothing"
    try:
        r = requests.get(f"{BASE}/v3/option/history/quote", timeout=30, params={
            "symbol": symbol, "expiration": exps[-1], "strike": "500",
            "right": "C", "start_date": exps[0], "end_date": exps[0],
            "interval": "1m"})
    except Exception as exc:                       # transport, not entitlement
        return "unreachable", str(exc)[:120]
    body = (r.text or "")[:200]
    if r.status_code == 403 or "subscription" in body.lower():
        return "free_tier", f"HTTP {r.status_code}: {body[:120]}"
    if r.status_code in (200, 472) or body.startswith("No data"):
        return "entitled", f"HTTP {r.status_code}"
    return "unknown", f"HTTP {r.status_code}: {body[:120]}"


def ensure_terminal(timeout_s: int = 180) -> bool:
    """Start the local terminal if it isn't running; wait until it serves.

    Timeout is 180s: on 2026-07-16 the JVM cold start took ~90s+ (terminal
    served at 17:46:29, 3s after the old 90s deadline fired), so the enrich job
    saw a false 'failed to start' and skipped the night. The prewarm launchd job
    (17:35) normally makes this a no-op by the time enrich runs at 17:45."""
    if terminal_alive():
        return True
    key = _env_key()
    if not key:
        logger.error("THETADATA_KEY missing from .env")
        return False
    env = dict(os.environ, THETADATA_API_KEY=key,
               PATH=f"{JAVA}:{os.environ.get('PATH', '')}")
    logf = (TERMINAL_DIR / "terminal.log").open("a")
    subprocess.Popen(["java", "-jar", "ThetaTerminalv3.jar"],
                     cwd=TERMINAL_DIR, env=env, stdout=logf, stderr=logf)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if terminal_alive():
            logger.info("Theta Terminal up")
            return True
        time.sleep(2)
    logger.error("Theta Terminal failed to start within %ss", timeout_s)
    return False


def _get_csv(path: str, params: dict, retries: int = 3) -> pd.DataFrame | None:
    for i in range(retries):
        try:
            r = requests.get(f"{BASE}{path}", params=params, timeout=60)
            if r.status_code == 472 or "No data" in r.text[:80]:
                return None          # empty result, not an error — no retry
            if r.status_code != 200 or not r.text or r.text.startswith("<"):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
            if r.text.startswith(("No data", "Invalid", "We have upgraded")):
                return None
            return pd.read_csv(io.StringIO(r.text))
        except Exception as exc:
            if i == retries - 1:
                logger.warning("thetadata %s failed: %s", path, str(exc)[:120])
                return None
            time.sleep(1 + i)
    return None


def option_quotes(symbol: str, expiration: str, strike: float, right: str,
                  date: str, interval: str = "1m") -> pd.DataFrame | None:
    """1-minute NBBO for one option contract on one day.
    right: 'C'|'P'; dates ISO YYYY-MM-DD; strike in points."""
    return _get_csv("/v3/option/history/quote", {
        "symbol": symbol, "expiration": expiration, "strike": f"{strike:g}",
        "right": right, "start_date": date, "end_date": date,
        "interval": interval})


def option_trades(symbol: str, expiration: str, strike: float, right: str,
                  date: str) -> pd.DataFrame | None:
    """All trade prints for one contract on one day."""
    return _get_csv("/v3/option/history/trade", {
        "symbol": symbol, "expiration": expiration, "strike": f"{strike:g}",
        "right": right, "start_date": date, "end_date": date})


def list_expirations(symbol: str) -> list[str]:
    df = _get_csv("/v3/option/list/expirations", {"symbol": symbol})
    return sorted(df["expiration"].astype(str)) if df is not None else []
