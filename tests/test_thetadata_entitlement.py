"""The ThetaData entitlement probe distinguishes liveness from subscription.

Why this exists (2026-10-22): the nightly chain read `prewarm serving=True` and
concluded the 89 `no_market_data` enrichment rows had to be a predicate bug,
because nothing in the chain measured entitlement. The liveness check hits
/v3/option/list/expirations (FREE-tier allowed); the endpoint enrichment needs
is /v3/option/history/quote, which a FREE key 403s. Hermetic — no terminal, no
network: `requests.get` is replaced per case.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.data import thetadata as td


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def exps(monkeypatch):
    monkeypatch.setattr(td, "list_expirations", lambda symbol="SPY": ["2026-10-23"])


def _answer(monkeypatch, resp):
    def fake_get(url, **kw):
        assert "/v3/option/history/quote" in url
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(td.requests, "get", fake_get)


def test_free_tier_403_is_not_reported_as_working(monkeypatch, exps):
    _answer(monkeypatch, _Resp(403, "Requesting an option endpoint requiring a "
                                    "value subscription, but you only have a "
                                    "FREE subscription."))
    state, detail = td.entitlement_probe()
    assert state == "free_tier"
    assert "403" in detail


def test_subscription_refusal_on_a_200_still_reads_free_tier(monkeypatch, exps):
    # The terminal has answered 200 with an upgrade notice body before.
    _answer(monkeypatch, _Resp(200, "We have upgraded your subscription tier"))
    assert td.entitlement_probe()[0] == "free_tier"


def test_entitled_on_data(monkeypatch, exps):
    _answer(monkeypatch, _Resp(200, "ms_of_day,bid,ask\n1,1.0,1.1\n"))
    assert td.entitlement_probe()[0] == "entitled"


def test_entitled_on_empty_result(monkeypatch, exps):
    # 472 / "No data" means the endpoint answered us — entitlement is fine, the
    # contract simply has no prints. Must NOT read as a subscription problem.
    _answer(monkeypatch, _Resp(472, "No data for the specified request"))
    assert td.entitlement_probe()[0] == "entitled"


def test_transport_error_is_unreachable_not_free_tier(monkeypatch, exps):
    _answer(monkeypatch, RuntimeError("connection refused"))
    assert td.entitlement_probe()[0] == "unreachable"


def test_dead_terminal_is_unreachable(monkeypatch):
    monkeypatch.setattr(td, "list_expirations", lambda symbol="SPY": [])
    state, detail = td.entitlement_probe()
    assert state == "unreachable"
    assert "expirations" in detail


def test_other_status_is_unknown(monkeypatch, exps):
    _answer(monkeypatch, _Resp(500, "internal error"))
    assert td.entitlement_probe()[0] == "unknown"


def test_probe_never_touches_the_paid_endpoint_when_dead(monkeypatch):
    """A dead terminal must short-circuit before any quote request."""
    monkeypatch.setattr(td, "list_expirations", lambda symbol="SPY": [])
    called = []
    monkeypatch.setattr(td.requests, "get",
                        lambda url, **kw: called.append(url) or _Resp(200, ""))
    td.entitlement_probe()
    assert called == []
    assert pd is not None  # the module's pandas import stays intact
