"""LIVE order client — Phase L step 3 scaffold (2026-07-02). NOT IN USE.

The mirror image of tradier_orders.py's sandbox lock: this module can reach
the PRODUCTION Tradier endpoint, so it is unusable unless the FULL live gate
passes — which today it structurally cannot (live_gate.LIVE_EXECUTION_BUILT
is False, no validated strategy, no forward-acceptance pass, no live creds).

Safety rails, all fail-closed:
  1. Construction runs live_gate.resolve_mode(cfg, strict=True) and requires
     the resolved mode to be 'live' — every gate condition, every time.
  2. Every request re-asserts: kill switch clear, host is api.tradier.com,
     live account id present and distinct from the sandbox account id.
  3. Orders are tagged OPTIONSAGENT-LIVE for tag-scoped reconciliation.

Nothing imports this module yet. Wiring it into the order lifecycle
(fills/positions/exits against a SEPARATE live book) is the remaining work
before live_gate.LIVE_EXECUTION_BUILT may flip — a deliberate, reviewed step
once forward acceptance is within reach, not before.
"""

import os
import urllib.parse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROD_BASE = "https://api.tradier.com/v1"
LIVE_ORDER_TAG = "OPTIONSAGENT-LIVE"


class LiveSafetyError(RuntimeError):
    pass


def assert_live_safety():
    """Every condition re-checked; any failure refuses. Fail-closed."""
    from src.engine.live_gate import kill_switch_engaged, resolve_mode, LiveModeRefused
    ks = kill_switch_engaged()
    if ks:
        raise LiveSafetyError(f"kill switch engaged: {ks}")
    host = urllib.parse.urlparse(PROD_BASE).hostname or ""
    if host != "api.tradier.com":
        raise LiveSafetyError(f"live endpoint {PROD_BASE!r} is not production Tradier")
    account = os.environ.get("TRADIER_LIVE_ACCOUNT_ID")
    if not os.environ.get("TRADIER_LIVE_TOKEN") or not account:
        raise LiveSafetyError("live credentials not set")
    if account == os.environ.get("TRADIER_SANDBOX_ACCOUNT_ID"):
        raise LiveSafetyError("live account id equals sandbox account id")
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    try:
        mode = resolve_mode(cfg, strict=True)["mode"]
    except LiveModeRefused as exc:
        raise LiveSafetyError(f"live gate refused: {exc}") from exc
    if mode != "live":
        raise LiveSafetyError(f"resolved mode is {mode!r}, not live")


class LiveOrderClient:
    """Real-money option orders. Constructor refuses unless the full live
    gate passes — today it cannot, by design."""

    def __init__(self):
        assert_live_safety()
        self.base = PROD_BASE
        self.token = os.environ["TRADIER_LIVE_TOKEN"]
        self.account = os.environ["TRADIER_LIVE_ACCOUNT_ID"]

    def _request(self, method: str, path: str, data: dict | None = None):
        assert_live_safety()  # belt and suspenders on EVERY call
        raise NotImplementedError(
            "live request path deliberately unimplemented — the live order-book "
            "lifecycle (fills/positions/exits) must ship and be reviewed before "
            "LIVE_EXECUTION_BUILT flips (live_gate.py)")
