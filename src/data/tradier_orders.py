"""Paper order execution — SANDBOX ONLY. Hard safety rails:

1. The base URL is fixed to the sandbox at module level; the constructor
   re-asserts it and refuses anything else. There is no parameter that can
   point this module at api.tradier.com.
2. `assert_paper_safety()` runs at agent startup and raises if the trading
   configuration could reach a production endpoint.
3. Orders are tagged UNVALIDATED-PAPER-ONLY (Tradier tag field).

The production token never appears in this module — it reads ONLY
TRADIER_SANDBOX_TOKEN / TRADIER_SANDBOX_ACCOUNT_ID.
"""

import json as jsonlib
import os
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

SANDBOX_BASE = "https://sandbox.tradier.com/v1"
ORDER_TAG = "UNVALIDATED-PAPER-ONLY"


class PaperSafetyError(RuntimeError):
    pass


def assert_paper_safety(base_url: str = SANDBOX_BASE):
    """Startup assertion: refuse to run if the trading endpoint is not the
    Tradier sandbox. Called by the agent loop before any cycle."""
    host = urllib.parse.urlparse(base_url).hostname or ""
    if host != "sandbox.tradier.com":
        raise PaperSafetyError(
            f"trading endpoint {base_url!r} is not the Tradier sandbox — "
            "REFUSING to start. The production token must never place orders.")


class PaperOrderClient:
    """Option paper orders against the Tradier sandbox."""

    def __init__(self):
        assert_paper_safety(SANDBOX_BASE)
        self.base = SANDBOX_BASE
        self.token = os.environ.get("TRADIER_SANDBOX_TOKEN") or None
        self.account = os.environ.get("TRADIER_SANDBOX_ACCOUNT_ID") or None

    def available(self) -> bool:
        return bool(self.token and self.account)

    def _request(self, method: str, path: str, data: dict | None = None) -> dict | None:
        assert_paper_safety(self.base)  # belt and suspenders on every call
        url = f"{self.base}{path}"
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return jsonlib.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # The broker RESPONDED with an error status — a definitive answer.
            # For a POST this means the order was NOT accepted (no live order).
            try:
                return {"error": exc.read().decode()[:500], "status": exc.code}
            except Exception:
                return {"error": str(exc), "status": exc.code}
        except Exception as exc:
            # NO response was received (timeout / connection reset / DNS / etc).
            # This is TRANSPORT-level and AMBIGUOUS: for a POST the order may or
            # may not have reached the broker. `transport_error` flags that so the
            # engine never treats it as a definitive reject (R1) and GET readers
            # never mistake it for "broker says none" (R2).
            return {"error": str(exc), "transport_error": True}

    def place_option_order(self, underlying: str, option_symbol: str, side: str,
                           quantity: int, limit_price: float, tif: str = "DAY") -> dict | None:
        if tif == "IOC":
            # Tradier sandbox has no immediate-or-cancel. Silently resting a
            # DAY limit would trade v1 rules under v2's label (2026-09-12).
            return {"error": "ioc_unsupported: tradier cannot honour IOC", "rejected": True}
        """side: buy_to_open | sell_to_open | buy_to_close | sell_to_close."""
        if not self.available():
            return None
        return self._request("POST", f"/accounts/{self.account}/orders", {
            "class": "option", "symbol": underlying, "option_symbol": option_symbol,
            "side": side, "quantity": quantity, "type": "limit",
            "price": f"{limit_price:.2f}", "duration": "day", "tag": ORDER_TAG,
        })

    def place_spread_order(self, underlying: str, legs: list[dict],
                           limit_price: float, order_type: str = "debit", tif: str = "DAY") -> dict | None:
        if tif == "IOC":
            # Tradier sandbox has no immediate-or-cancel. Silently resting a
            # DAY limit would trade v1 rules under v2's label (2026-09-12).
            return {"error": "ioc_unsupported: tradier cannot honour IOC", "rejected": True}
        """legs: [{option_symbol, side, quantity}]. Multileg limit order;
        order_type 'debit' to open, 'credit' to close debit structures."""
        if not self.available():
            return None
        data = {"class": "multileg", "symbol": underlying, "type": order_type,
                "duration": "day", "price": f"{limit_price:.2f}", "tag": ORDER_TAG}
        for i, leg in enumerate(legs):
            data[f"option_symbol[{i}]"] = leg["option_symbol"]
            data[f"side[{i}]"] = leg["side"]
            data[f"quantity[{i}]"] = leg["quantity"]
        return self._request("POST", f"/accounts/{self.account}/orders", data)

    def get_order(self, order_id: int) -> dict | None:
        if not self.available():
            return None
        out = self._request("GET", f"/accounts/{self.account}/orders/{order_id}")
        return (out or {}).get("order")

    def cancel_order(self, order_id: int) -> dict | None:
        if not self.available():
            return None
        return self._request("DELETE", f"/accounts/{self.account}/orders/{order_id}")

    def get_orders(self) -> list[dict] | None:
        """All of today's orders (any status).

        R2 contract: `[]` means the broker AUTHORITATIVELY reports no orders;
        `None` means the read FAILED (transport error or broker error status) —
        the broker view is unknown. Callers on recovery/reconcile paths MUST
        treat None as abort-and-retry, never as "empty" (dropping intents on a
        flaky broker is exactly the crash-restart failure R2 guards against)."""
        if not self.available():
            return []  # not configured (recovery paths guard available()) — not an error
        out = self._request("GET", f"/accounts/{self.account}/orders")
        if out is None or "error" in out:  # transport OR broker error — not "empty"
            return None
        orders = (out.get("orders") or {})
        if orders in ("null", None):
            return []
        items = orders.get("order", [])
        return [items] if isinstance(items, dict) else items

    def get_positions(self) -> list[dict] | None:
        """Current account positions (per option leg).

        R2 contract: `[]` = broker authoritatively reports none; `None` = read
        FAILED (unknown broker view). See get_orders."""
        if not self.available():
            return []  # not configured (recovery paths guard available()) — not an error
        out = self._request("GET", f"/accounts/{self.account}/positions")
        if out is None or "error" in out:  # transport OR broker error — not "empty"
            return None
        positions = (out.get("positions") or {})
        if positions in ("null", None):
            return []
        items = positions.get("position", [])
        return [items] if isinstance(items, dict) else items
