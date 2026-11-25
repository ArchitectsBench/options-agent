"""RVOL nightly evidence producers — hardening (2026-09-13).

WHY: the Polygon adapter had NO 429/5xx handling, so a throttled or flaky
request came back as {"error": ...} and every caller treated it as "no
data": rvol_shadow_scan silently dropped 591 of 1,780 candidates on
2026-07-29 (ranked on 67% of the universe, no alarm) and the sleeve's lost
nights (08-25, 09-02 'grouped daily unavailable') paged nobody.

Covers:
  1. src/data/polygon_data._get — exponential backoff with jitter, Retry-After
     honoured, non-retryable 4xx immediate, and a DISTINGUISHABLE error shape
     {"error", "http_status", "retries"} — never a silent empty result.
  2. scripts/rvol_shadow_scan.py — additive coverage fields, the pre-declared
     ranking_incomplete flag (A2 draft rule: scan_errors > 10% of candidates),
     ntfy on incomplete; ranking/filter/top-20 math untouched.
  3. scripts/rvolrank_sleeve.py — additive stamping of the shadow's coverage
     evidence, ONE ntfy page per lost night (quiet on holidays/weekends and on
     repeats), skip reasons carrying the http status.
  4. BEHAVIOUR LOCK — the frozen K18 construction (replay_k18, both entry
     windows; run_lane rank-priority allocation, slot cap, cost tiers) must
     reproduce, byte-for-byte, outputs captured from the code AS IT WAS before
     this change (LOCK_EXPECTED generated from git HEAD 183a8a3 on 2026-09-13
     with scripts/ops-free scratch generator; do not regenerate from new code).

O-20: every production path constant in both scripts is redirected to
tmp_path STRUCTURALLY — _load itself re-points FRESH/OBS_DIR/LEDGER/ALERT_LOG/
OUT_DIR/RAW under tmp_path and asserts none is under the repo (O-20 layer 2
does not intercept Path.open('a') or pyarrow to_parquet, so the redirect is
the guard); an autouse fixture no-ops both notification channels at the
channel seam (NTFY_TOPIC is live in the pytest process and _macos_notify runs
osascript unconditionally) while leaving notify.send intact for the spy; the
fake Polygon client raises on any URL it does not recognise, so no test can
reach the network.
"""
from __future__ import annotations

import email.message
import importlib.util
import io
import json
import re
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.data.polygon_data as pdmod  # noqa: E402
import src.engine.notify as notify  # noqa: E402


_TMP: Path | None = None            # set by the autouse fixture below
_PROD_PATHS = ("FRESH", "OBS_DIR", "LEDGER", "ALERT_LOG", "OUT_DIR", "RAW")


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # STRUCTURAL redirect, not per-test discipline: every production path
    # constant of a script loaded here points under tmp_path BEFORE the module
    # reaches any test (the sleeve fixture's explicit redirects are then a
    # second, narrower layer). Asserted, so a new constant that slips this
    # list still cannot point at the repo silently.
    assert _TMP is not None, "_load called outside a test (autouse seal not active)"
    for attr in _PROD_PATHS:
        if hasattr(m, attr):
            setattr(m, attr, _TMP / name / attr.lower())
            assert not str(getattr(m, attr)).startswith(str(ROOT)), attr
    return m


@pytest.fixture(autouse=True)
def _seal_notifications_and_paths(tmp_path, monkeypatch):
    """Module-scoped seal (deliberately NOT in conftest.py: tests/test_ntfy_
    commands.py sets a fake NTFY_TOPIC and needs the real _ntfy_notify).
    (1) No notification can leave the pytest process: both channels are
    no-op'd at the channel seam; notify.send stays intact so _ntfy_spy's
    override keeps working and any UNspied send is inert. (2) Provide the tmp
    root that _load redirects production paths under."""
    global _TMP
    monkeypatch.setattr(notify, "_ntfy_notify", lambda *a, **k: None)
    monkeypatch.setattr(notify, "_macos_notify", lambda *a, **k: None)
    _TMP = tmp_path
    yield
    _TMP = None


# ═══════════════════════════════════════════════════════════════════════════
# Synthetic 5m-bar fixture (shared by the lock, the sleeve tests and the
# generator that captured LOCK_EXPECTED). Column layout == pull_5m output.
# ═══════════════════════════════════════════════════════════════════════════
HM = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(0, 60, 5)
      if (h, m) >= (9, 30)]                       # 09:30 .. 15:55 -> 78 bars
DAY = "2026-09-11"

# (name, kind, base price) in RANK order — 11 tradeable names so the SLOTS=10
# cap bites (rank-16 S4 must be dropped), plus every skip/None path.
LOCK_NAMES = [
    ("L1", "long_eod", 100.0), ("S1", "short_eod", 50.0), ("NB", "no_bars", 0.0),
    ("L2", "long_stop", 80.0), ("NA", "no_atr", 120.0), ("S2", "short_stop", 30.0),
    ("L3", "long_gap_stop", 200.0), ("NBODY", "no_body", 60.0),
    ("LC", "late_cross", 90.0), ("CAP", "cap", 40.0), ("NC", "no_cross", 70.0),
    ("CX", "cancel", 110.0), ("L4", "long_eod", 150.0), ("S3", "short_eod", 25.0),
    ("L5", "long_eod", 300.0), ("S4", "short_eod", 15.0),
]
_KIND = {n: k for n, k, _ in LOCK_NAMES}
_BASE = {n: b for n, _, b in LOCK_NAMES}


def atr_for(name: str) -> float | None:
    if _KIND[name] == "no_atr":
        return None
    i = [n for n, _, _ in LOCK_NAMES].index(name)
    return round(1.6 + 0.1 * i, 2)


def make_bars(kind: str, base: float) -> pd.DataFrame | None:
    """Deterministic RTH day. Long kinds break the 09:30 high at 09:40 (lane
    09:35 fills there) and again at 09:45 with a 0.01 gap (lane 09:45 fills
    there — the two lanes get DIFFERENT fills). Short kinds mirror."""
    if kind == "no_bars":
        return None
    s = -1.0 if kind.startswith("short") else 1.0
    rows: list[dict] = []

    def bar(hm, o, c, a, b, v=100_000):
        rows.append({"hm": hm, "o": round(o, 2), "h": round(max(a, b), 2),
                     "l": round(min(a, b), 2), "c": round(c, 2), "v": v})

    v930 = 5_000 if kind == "cap" else 100_000
    if kind == "no_body":
        bar("09:30", base, base + 0.02, base + 0.60, base - 0.10, v930)  # body < 10% range
    else:
        bar("09:30", base, base + s * 0.50, base + (0.60 if s > 0 else 0.10),
            base - (0.10 if s > 0 else 0.60), v930)

    def quiet(hm):
        bar(hm, base + s * 0.45, base + s * 0.50, base + s * 0.55, base + s * 0.40)

    def cross(hm):        # crosses level=base±0.61, opens BELOW it -> raw == level
        bar(hm, base + s * 0.58, base + s * 0.65, base + s * 0.55, base + s * 0.70)

    def cross_gap(hm):    # opens 0.01 past the level (inside the 10%-of-stop guardrail)
        bar(hm, base + s * 0.62, base + s * 0.70, base + s * 0.60, base + s * 0.75)

    def cancel(hm):       # opens 0.14 past the level -> cancel guardrail
        bar(hm, base + s * 0.75, base + s * 0.80, base + s * 0.72, base + s * 0.90)

    def stop(hm, gap):    # trades through the hard stop; gap=True opens beyond it
        o = base + s * (0.05 if gap else 0.45)
        bar(hm, o, base, base + s * 0.50, base - s * 0.10)

    k = 0
    for hm in HM[1:]:
        if hm == "15:55":
            bar(hm, base + s * 1.15, base + s * 1.20, base + s * 1.10, base + s * 1.25)
        elif kind in ("no_cross", "no_body"):
            quiet(hm)
        elif kind == "cancel":
            cancel(hm) if hm in ("09:40", "09:45") else quiet(hm)
        elif kind == "late_cross":
            if hm < "10:20":
                quiet(hm)
            elif hm == "10:20":
                cross(hm)
            else:
                o = base + s * (0.70 + 0.01 * k); k += 1
                bar(hm, o, o + s * 0.02, o + 0.05, o - 0.05)
        else:
            if hm == "09:35":
                quiet(hm)
            elif hm == "09:40":
                cross(hm)
            elif hm == "09:45":
                cross_gap(hm)
            elif hm == "10:30" and kind in ("long_stop", "short_stop"):
                stop(hm, gap=False)
            elif hm == "10:30" and kind == "long_gap_stop":
                stop(hm, gap=True)
            else:
                o = base + s * (0.70 + 0.01 * k); k += 1
                bar(hm, o, o + s * 0.02, o + 0.05, o - 0.05)
    return pd.DataFrame(rows, columns=["hm", "o", "h", "l", "c", "v"])


def bars_for(name: str) -> pd.DataFrame | None:
    return make_bars(_KIND[name], _BASE[name])


def polygon_rows(bars: pd.DataFrame, day: str) -> list[dict]:
    """Raw Polygon aggregate rows (t in ms, US/Eastern session) for pull_5m."""
    out = []
    for r in bars.to_dict("records"):
        t = pd.Timestamp(f"{day} {r['hm']}", tz="US/Eastern")
        out.append({"t": int(t.value // 10 ** 6), "o": r["o"], "h": r["h"],
                    "l": r["l"], "c": r["c"], "v": r["v"]})
    return out


def daily_rows(base: float, atr: float, day: str, n: int = 20) -> list[dict]:
    """n daily bars ending the day before `day` with TR == atr each -> ATR(14) == atr."""
    d = pd.Timestamp(day) - pd.Timedelta(days=1)
    out = []
    for i in range(n):
        ts = d - pd.Timedelta(days=n - 1 - i)
        out.append({"t": int(ts.value // 10 ** 6), "o": base, "h": base + atr / 2,
                    "l": base - atr / 2, "c": base, "v": 1_000_000})
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════════════
class _Resp:
    def __init__(self, body: bytes):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _http_error(code: int, retry_after: str | None = None, body: bytes = b'{"status":"ERROR"}'):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://api.polygon.io/x", code, "err", hdrs, io.BytesIO(body))


class _Sequence:
    """urlopen stand-in: pops one scripted outcome per call (Exception -> raised)."""
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return _Resp(o)


class FakePolygon:
    """Serves grouped / 5m / daily URLs from fixtures; raises on anything else.
    `errors[ticker]` = error shape returned for that ticker's 5m pull."""
    def __init__(self, grouped=None, bars=None, atrs=None, errors=None, bases=None):
        self.grouped = grouped or []
        self.bars = bars or {}
        self.atrs = atrs or {}
        self.errors = errors or {}
        self.bases = bases or {}
        self.urls: list[str] = []

    def available(self):
        return True

    def _get(self, url):
        self.urls.append(url)
        if "/aggs/grouped/" in url:
            return {"results": self.grouped}
        m = re.search(r"/ticker/([A-Za-z0-9.]+)/range/(\d+)/(minute|day)/", url)
        if not m:
            raise AssertionError(f"unexpected network call: {url}")
        t, span = m.group(1), m.group(3)
        if span == "minute":
            if t in self.errors:
                return dict(self.errors[t])
            b = self.bars.get(t)
            return {"results": polygon_rows(b, DAY) if b is not None else []}
        a = self.atrs.get(t)
        if a is None:
            return {"results": []}
        return {"results": daily_rows(self.bases.get(t, 100.0), a, DAY)}


# ═══════════════════════════════════════════════════════════════════════════
# 1. polygon_data._get — backoff + distinguishable error shape
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pdmod, "_sleep", lambda s: None)
    c = pdmod.PolygonData()
    c.key = "test-key"
    return c


def _record_sleeps(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(pdmod, "_sleep", slept.append)
    return slept


def test_get_429_then_200_retries_once_and_honours_retry_after(client, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    seq = _Sequence([_http_error(429, retry_after="2"), b'{"results": [1, 2]}'])
    monkeypatch.setattr(pdmod, "_urlopen", seq)
    out = client._get("https://api.polygon.io/v2/aggs/x")
    assert out == {"results": [1, 2]}
    assert seq.calls == 2
    assert len(slept) == 1 and 2.0 <= slept[0] <= 2.0 * (1 + pdmod.BACKOFF_JITTER_FRAC)


def test_get_five_429s_returns_distinguishable_error_shape(client, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    seq = _Sequence([_http_error(429) for _ in range(pdmod.BACKOFF_TRIES)])
    monkeypatch.setattr(pdmod, "_urlopen", seq)
    out = client._get("https://api.polygon.io/v2/aggs/x")
    assert seq.calls == pdmod.BACKOFF_TRIES == 5
    assert out["http_status"] == 429
    assert out["retries"] == pdmod.BACKOFF_TRIES - 1
    assert out["attempts"] == pdmod.BACKOFF_TRIES
    assert out["error"].startswith("HTTP 429")          # sleeve's "429" match preserved
    assert "results" not in out                           # never a silent empty result
    # 1, 2, 4, 8 (+ jitter) — no sleep after the final attempt
    assert len(slept) == 4
    for k, s in enumerate(slept):
        base = pdmod.BACKOFF_BASE_S * 2 ** k
        assert base <= s <= base * (1 + pdmod.BACKOFF_JITTER_FRAC)


def test_get_5xx_is_retried_and_other_4xx_is_immediate(client, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    seq = _Sequence([_http_error(503), _http_error(502), b'{"results": []}'])
    monkeypatch.setattr(pdmod, "_urlopen", seq)
    assert client._get("https://api.polygon.io/v2/aggs/x") == {"results": []}
    assert seq.calls == 3 and len(slept) == 2

    slept.clear()
    seq = _Sequence([_http_error(403, body=b"not entitled")])
    monkeypatch.setattr(pdmod, "_urlopen", seq)
    out = client._get("https://api.polygon.io/vX/reference/financials")
    assert seq.calls == 1 and slept == []
    assert out["http_status"] == 403 and out["retries"] == 0
    assert "not entitled" in out["error"]


def test_get_network_error_is_retried_then_reported_with_status_none(client, monkeypatch):
    slept = _record_sleeps(monkeypatch)
    seq = _Sequence([urllib.error.URLError("conn reset"), b'{"ok": 1}'])
    monkeypatch.setattr(pdmod, "_urlopen", seq)
    assert client._get("https://api.polygon.io/v2/aggs/x") == {"ok": 1}
    assert len(slept) == 1

    seq = _Sequence([TimeoutError("timed out")] * pdmod.BACKOFF_TRIES)
    monkeypatch.setattr(pdmod, "_urlopen", seq)
    out = client._get("https://api.polygon.io/v2/aggs/x")
    assert out["http_status"] is None and out["retries"] == pdmod.BACKOFF_TRIES - 1
    assert "timed out" in out["error"]


def test_get_garbled_body_returns_error_shape_not_exception(client, monkeypatch):
    monkeypatch.setattr(pdmod, "_urlopen", _Sequence([b"<html>oops</html>"]))
    out = client._get("https://api.polygon.io/v2/aggs/x")
    assert "error" in out and out["http_status"] is None


def test_retry_after_parsing_and_backoff_delay():
    h = email.message.Message()
    assert pdmod._retry_after_seconds(h) is None
    h["Retry-After"] = "7"
    assert pdmod._retry_after_seconds(h) == 7.0
    h.replace_header("Retry-After", "Wed, 21 Oct 2026 07:28:00 GMT")
    assert pdmod._retry_after_seconds(h) is None
    h.replace_header("Retry-After", "-3")
    assert pdmod._retry_after_seconds(h) is None
    h.replace_header("Retry-After", "99999")
    assert pdmod._retry_after_seconds(h) == pdmod.RETRY_AFTER_CAP_S
    assert pdmod._retry_after_seconds(None) is None
    # computed delay caps at BACKOFF_CAP_S (+ jitter)
    d = pdmod._backoff_delay(10, None)
    assert pdmod.BACKOFF_CAP_S <= d <= pdmod.BACKOFF_CAP_S * (1 + pdmod.BACKOFF_JITTER_FRAC)


def test_aggregates_stops_on_error_shape_without_raising(client, monkeypatch):
    monkeypatch.setattr(pdmod, "_urlopen", _Sequence([_http_error(404)]))
    assert client.aggregates("SPY", 1, "day", "2026-09-01", "2026-09-02") == []


# ═══════════════════════════════════════════════════════════════════════════
# 2. rvol_shadow_scan — additive coverage fields, ranking_incomplete, ntfy
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def scan(monkeypatch):
    m = _load("rvol_shadow_scan_ut", "scripts/rvol_shadow_scan.py")
    monkeypatch.setattr(m, "REQ_SLEEP", 0.0)
    return m


def _ntfy_spy(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: calls.append((a, k)))
    return calls


def test_scan_pacing_constants_are_declared_and_small():
    real = _load("rvol_shadow_scan_real", "scripts/rvol_shadow_scan.py")   # not the zeroed fixture
    assert 0 < real.REQ_SLEEP <= 1.0, "13s-class pacing is stale: the 5 req/min limit is gone"
    assert real.COVERAGE_MIN_PCT == 90.0           # pre-declared, A2 draft rule


def test_rank_candidates_counts_errors_with_status_histogram(scan):
    names = ["L1", "S1", "L4", "L5", "NB"]
    bars = {n: bars_for(n) for n in names if _KIND[n] != "no_bars"}
    errors = {"L4": {"error": "HTTP 429: x", "http_status": 429, "retries": 4},
              "L5": {"error": "timed out", "http_status": None, "retries": 4}}
    fake = FakePolygon(bars=bars, errors=errors)
    results, stats = scan.rank_candidates(fake, names, DAY, "2026-08-12")
    # no prior 14 same-slot bars in this one-day fixture -> nobody ranks, but the
    # error accounting is what this test pins
    assert results == []
    assert stats == {"scan_errors": 3, "scan_errors_http": 2,
                     "scan_error_statuses": {"429": 1, "None": 1}}
    assert len(fake.urls) == 5


def test_rank_candidates_ranking_math_is_unchanged(scan):
    """Positive lock on step 3: rvol = today's 09:30 volume / mean of the prior
    14 same-slot volumes (rounded 2dp), sorted descending; a name with fewer
    than 14 prior sessions or no 09:30 bar today is NOT ranked and NOT an error."""
    def rows(vols_by_day: dict) -> list[dict]:
        out = []
        for d, v in vols_by_day.items():
            t = pd.Timestamp(f"{d} 09:30", tz="US/Eastern")
            out.append({"t": int(t.value // 10 ** 6), "o": 1, "h": 1, "l": 1, "c": 1, "v": v})
            t2 = pd.Timestamp(f"{d} 09:35", tz="US/Eastern")     # non-slot bar: ignored
            out.append({"t": int(t2.value // 10 ** 6), "o": 1, "h": 1, "l": 1, "c": 1, "v": 10 ** 7})
        return out
    days = [(pd.Timestamp(DAY) - pd.Timedelta(days=k)).strftime("%Y-%m-%d") for k in range(20, 0, -1)]
    prior14 = days[-14:]

    class _Fake(FakePolygon):
        def __init__(self):
            super().__init__()
            self.by = {
                "AAA": rows({**{d: 1000 for d in days}, DAY: 5500}),           # 5500/1000 = 5.5
                "BBB": rows({**{d: 1000 for d in days[:6]},                     # older 6 sessions are
                             **{d: 2000 + 100 * i for i, d in enumerate(prior14)},  # outside the 14-window
                             DAY: 4000}),                                       # 4000/mean(2000..3300)=1.51
                "SHORT": rows({**{d: 1000 for d in days[-10:]}, DAY: 9000}),  # only 10 prior -> unranked
                "NOTODAY": rows({d: 1000 for d in days}),                      # no 09:30 bar today
            }

        def _get(self, url):
            self.urls.append(url)
            t = re.search(r"/ticker/([A-Z]+)/range/5/minute/", url).group(1)
            return {"results": self.by[t]}

    fake = _Fake()
    results, stats = scan.rank_candidates(fake, ["BBB", "NOTODAY", "AAA", "SHORT"], DAY, days[0])
    assert results == [{"ticker": "AAA", "rvol": 5.5, "v930": 5500},
                       {"ticker": "BBB", "rvol": 1.51, "v930": 4000}]
    assert stats == {"scan_errors": 0, "scan_errors_http": 0, "scan_error_statuses": {}}
    assert len(fake.urls) == 4


def test_payload_additive_fields_and_incomplete_flag(scan, monkeypatch):
    results = [{"ticker": "AAA", "rvol": 5.0, "v930": 10}, {"ticker": "SPY", "rvol": 2.0, "v930": 5}]
    stats = {"scan_errors": 591, "scan_errors_http": 500, "scan_error_statuses": {"429": 500}}
    p = scan.build_payload(DAY, 1780, results, stats, ["SPY"])
    # frozen keys untouched
    assert p["candidates_scanned"] == 1780 and p["scan_errors"] == 591
    assert p["top20"] == results and p["watchlist_in_top20"] == ["SPY"]
    assert p["watchlist_rvols"] == {"SPY": 2.0}
    # additive
    assert p["candidates_total"] == 1780 and p["ranked_total"] == 2
    assert p["coverage_pct"] == 66.8                        # 1 - 591/1780 — the 07-29 case
    assert p["scan_errors_http"] == 500 and p["scan_error_statuses"] == {"429": 500}
    assert p["ranking_incomplete"] is True
    json.dumps(p)                                            # single-line-json safe

    ok = scan.build_payload(DAY, 1780, results, {"scan_errors": 100, "scan_errors_http": 0,
                                                 "scan_error_statuses": {}}, ["SPY"])
    assert ok["coverage_pct"] == 94.4 and "ranking_incomplete" not in ok
    edge = scan.build_payload(DAY, 1000, results, {"scan_errors": 100, "scan_errors_http": 0,
                                                   "scan_error_statuses": {}}, [])
    assert edge["coverage_pct"] == 90.0 and "ranking_incomplete" not in edge   # < 90 flags, == 90 does not
    # 178/1779 = 10.006% errors: the 1-dp stored value rounds to 90.0 but the
    # A2 rule (> 10% of candidates) is decided on the UNROUNDED share -> flagged
    over = scan.build_payload(DAY, 1779, results, {"scan_errors": 178, "scan_errors_http": 0,
                                                   "scan_error_statuses": {}}, [])
    assert over["coverage_pct"] == 90.0 and over["ranking_incomplete"] is True
    zero = scan.build_payload(DAY, 0, [], {"scan_errors": 0, "scan_errors_http": 0,
                                           "scan_error_statuses": {}}, [])
    assert zero["coverage_pct"] == 0.0 and zero["ranking_incomplete"] is True


def test_incomplete_ranking_sends_one_ntfy_line(scan, monkeypatch):
    calls = _ntfy_spy(monkeypatch)
    stats = {"scan_errors": 591, "scan_errors_http": 500, "scan_error_statuses": {"429": 500}}
    p = scan.build_payload(DAY, 1780, [], stats, [])
    assert scan.alert_if_incomplete(p) is True
    assert len(calls) == 1
    (title, body), kw = calls[0]
    assert DAY in body and "66.8" in body and "591" in body and "429" in body
    assert kw.get("priority") == "high"
    ok = scan.build_payload(DAY, 1780, [], {"scan_errors": 0, "scan_errors_http": 0,
                                            "scan_error_statuses": {}}, [])
    assert scan.alert_if_incomplete(ok) is False and len(calls) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 3. rvolrank_sleeve — stamping, lost-night paging, visible skip reasons
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def sleeve(tmp_path, monkeypatch):
    m = _load("rvolrank_sleeve_ut", "scripts/rvolrank_sleeve.py")
    monkeypatch.setattr(m, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(m, "FRESH", tmp_path / "fresh")
    monkeypatch.setattr(m, "OBS_DIR", tmp_path / "obs")
    monkeypatch.setattr(m, "ALERT_LOG", tmp_path / "alerts.jsonl")
    monkeypatch.setattr(m, "REQ_SLEEP", 0.0)
    monkeypatch.setattr(m, "OUTER_RETRY_SLEEP_S", 0.0)
    (tmp_path / "obs").mkdir()
    # default client: any network call is a test bug
    monkeypatch.setattr(m, "get_client", lambda: FakePolygon())
    m.PULL_ERRORS.clear()
    return m


def _run_main(m, monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["rvolrank_sleeve.py", *argv])
    return m.main()


def _write_shadow(m, day: str, payload: dict):
    (m.OBS_DIR / f"rvol_shadow-{day}.json").write_text(json.dumps(payload))


def test_sleeve_req_sleep_is_small():
    real = _load("rvolrank_sleeve_real", "scripts/rvolrank_sleeve.py")   # not the zeroed fixture
    assert 0 < real.REQ_SLEEP <= 1.0, "13s-class pacing is stale: the 5 req/min limit is gone"
    assert real.OUTER_RETRY_SLEEP_S < 65.0                                # the old 65s 429 sleep is gone


def test_stamp_shadow_quality_is_additive(sleeve):
    rec = {"date": DAY}
    assert sleeve.stamp_shadow_quality(rec, {"top20": [1]}) == {"date": DAY}
    rec = sleeve.stamp_shadow_quality({"date": DAY}, {"top20": [1], "coverage_pct": 96.0})
    assert rec == {"date": DAY, "coverage_pct": 96.0}
    rec = sleeve.stamp_shadow_quality({"date": DAY},
                                      {"top20": [1], "coverage_pct": 66.8, "ranking_incomplete": True})
    assert rec == {"date": DAY, "coverage_pct": 66.8, "ranking_incomplete": True}


def test_record_replays_and_is_stamped_when_shadow_incomplete(sleeve, monkeypatch):
    calls = _ntfy_spy(monkeypatch)
    _write_shadow(sleeve, DAY, {"date": DAY, "top20": [{"ticker": "L1", "rvol": 6.1}],
                                "coverage_pct": 66.8, "ranking_incomplete": True})
    fake = FakePolygon(grouped=[{"T": "S1", "c": 50.0, "v": 9_000_000}],
                       bars={"L1": bars_for("L1"), "S1": bars_for("S1")},
                       atrs={"L1": atr_for("L1"), "S1": atr_for("S1")},
                       bases={"L1": 100.0, "S1": 50.0})
    monkeypatch.setattr(sleeve, "get_client", lambda: fake)
    # measurement-only metadata reads production parquet + a cross-repo store;
    # keep the test disk-independent (its output is not under test here)
    monkeypatch.setattr(sleeve, "name_metadata", lambda *a, **k: {"_stubbed": True})
    assert _run_main(sleeve, monkeypatch, "--date", DAY) == 0
    rows = [json.loads(l) for l in sleeve.LEDGER.read_text().splitlines()]
    assert len(rows) == 1
    rec = rows[0]
    # frozen record schema keys still present
    for k in ("date", "stamp", "generated", "criteria", "gate_primary_lane", "selection",
              "control", "selection_exec", "control_exec", "metadata", "binding_constraint"):
        assert k in rec, k
    assert rec["stamp"] == "UNVALIDATED — PAPER ONLY"
    # replay happened (hygiene is applied at evaluation, not here)
    assert rec["selection"]["n"] == 1 and rec["selection_exec"]["n"] == 1
    assert rec["control"]["n"] == 1 and rec["selection"]["trades"][0]["ticker"] == "L1"
    assert rec["selection"]["trades"][0]["fill_t"] == "09:40"
    assert rec["selection_exec"]["trades"][0]["fill_t"] == "09:45"
    # additive stamping
    assert rec["ranking_incomplete"] is True and rec["coverage_pct"] == 66.8
    assert calls == []                                 # a replayed night does not page
    # fresh bars persisted under the (redirected) FRESH root
    assert (sleeve.FRESH / DAY / "L1.parquet").exists()


def test_record_without_shadow_flag_has_no_stamp(sleeve, monkeypatch):
    _write_shadow(sleeve, DAY, {"date": DAY, "top20": [{"ticker": "L1", "rvol": 6.1}]})
    fake = FakePolygon(grouped=[{"T": "L1", "c": 100.0, "v": 9_000_000}],
                       bars={"L1": bars_for("L1")}, atrs={"L1": atr_for("L1")})
    monkeypatch.setattr(sleeve, "get_client", lambda: fake)
    monkeypatch.setattr(sleeve, "name_metadata", lambda *a, **k: {"_stubbed": True})
    assert _run_main(sleeve, monkeypatch, "--date", DAY) == 0
    rec = json.loads(sleeve.LEDGER.read_text().splitlines()[0])
    assert "ranking_incomplete" not in rec and "coverage_pct" not in rec


@pytest.mark.parametrize("setup, cause, code", [
    ("no_file", "no_shadow_file", 1),
    ("scan_failed", "no_top20", 1),
    ("holiday_marker_on_trading_day", "no_top20", 1),
    ("grouped_unavailable", "grouped_unavailable", 1),
])
def test_lost_night_pages_exactly_once(sleeve, monkeypatch, setup, cause, code):
    calls = _ntfy_spy(monkeypatch)
    if setup == "scan_failed":
        _write_shadow(sleeve, DAY, {"date": DAY, "scan_failed": True, "attempts": 3})
    elif setup == "holiday_marker_on_trading_day":
        _write_shadow(sleeve, DAY, {"date": DAY, "holiday_or_no_data": True})
    elif setup == "grouped_unavailable":
        _write_shadow(sleeve, DAY, {"date": DAY, "top20": [{"ticker": "L1", "rvol": 6.1}]})
        monkeypatch.setattr(sleeve, "get_client", lambda: FakePolygon(grouped=[]))
    assert _run_main(sleeve, monkeypatch, "--date", DAY) == code   # exit codes unchanged
    assert len(calls) == 1
    (title, body), kw = calls[0]
    assert DAY in body and cause in body
    assert kw.get("priority") == "high"
    # the hourly catch-up sweep re-runs the same date: no second page
    assert _run_main(sleeve, monkeypatch, "--date", DAY) == code
    assert len(calls) == 1
    marks = [json.loads(l) for l in sleeve.ALERT_LOG.read_text().splitlines()]
    assert marks == [{**marks[0]}] and marks[0]["date"] == DAY and marks[0]["cause"] == cause


def test_lost_night_is_quiet_on_holidays_and_weekends(sleeve, monkeypatch):
    calls = _ntfy_spy(monkeypatch)
    import src.trendbook.executor as ex
    monkeypatch.setattr(ex, "_holidays", lambda: {"2026-09-07"})
    # Labor Day marker — the 09-08..09-12 hourly-sweep case: exit 1, no page
    _write_shadow(sleeve, "2026-09-07", {"date": "2026-09-07", "holiday_or_no_data": True})
    assert _run_main(sleeve, monkeypatch, "--date", "2026-09-07") == 1
    # Saturday, no file at all
    assert _run_main(sleeve, monkeypatch, "--date", "2026-09-12") == 1
    assert calls == []
    assert not sleeve.ALERT_LOG.exists()


def test_dry_run_never_pages_nor_writes_the_dedup_marker(sleeve, monkeypatch):
    """A human dry-running a lost weekday must not fire a real page, and must
    not poison ALERT_LOG so the real 21:30 run stays 'already paged — quiet'."""
    calls = _ntfy_spy(monkeypatch)
    _write_shadow(sleeve, DAY, {"date": DAY, "scan_failed": True, "attempts": 3})
    assert _run_main(sleeve, monkeypatch, "--dry-run", "--date", DAY) == 1
    assert calls == [] and not sleeve.ALERT_LOG.exists()
    # the real run afterwards still pages exactly once
    assert _run_main(sleeve, monkeypatch, "--date", DAY) == 1
    assert len(calls) == 1 and sleeve.ALERT_LOG.exists()


def test_run_lane_skip_reason_carries_http_status(sleeve, monkeypatch):
    fake = FakePolygon(bars={"L1": bars_for("L1")}, atrs={"L1": atr_for("L1")},
                       errors={"L2": {"error": "HTTP 429: throttled", "http_status": 429, "retries": 4},
                               "L3": {"error": "timed out", "http_status": None, "retries": 4}})
    cache: dict = {}
    out, skips = sleeve.run_lane(fake, DAY, "selection", ["L1", "L2", "L3", "NB"], cache)
    assert [t["ticker"] for t in out["09:35"]] == ["L1"]
    assert skips == [("L2", "no_bars:http_429"), ("L3", "no_bars:net_error"), ("NB", "no_bars")]
    # the 429 name was re-attempted by the outer wrapper (transient), the 404-class is not
    n429 = sum("/ticker/L2/" in u for u in fake.urls)
    assert n429 == 3
    fake2 = FakePolygon(errors={"L2": {"error": "HTTP 404: nope", "http_status": 404, "retries": 0}})
    _, skips2 = sleeve.run_lane(fake2, DAY, "control", ["L2"], {})
    assert skips2 == [("L2", "no_bars:http_404")]
    assert sum("/ticker/L2/" in u for u in fake2.urls) == 1


def test_run_lane_no_atr_skip_reason_carries_status(sleeve, monkeypatch):
    class _AtrErr(FakePolygon):
        def _get(self, url):
            if "/range/1/day/" in url:
                self.urls.append(url)
                return {"error": "HTTP 503: x", "http_status": 503, "retries": 4}
            return super()._get(url)
    fake = _AtrErr(bars={"L1": bars_for("L1")})
    _, skips = sleeve.run_lane(fake, DAY, "selection", ["L1"], {})
    assert skips == [("L1", "no_atr:http_503")]


# ═══════════════════════════════════════════════════════════════════════════
# 4. BEHAVIOUR LOCK — frozen construction unchanged
# ═══════════════════════════════════════════════════════════════════════════
def lock_outputs(m) -> dict:
    """Run the fixture through replay_k18 (both entry windows) and run_lane
    (rank-priority allocation, SLOTS cap) with pull/atr faked at the function
    seam so ONLY the frozen construction is exercised."""
    replay = {}
    for name, kind, base in LOCK_NAMES:
        b = bars_for(name)
        a = atr_for(name)
        if b is None or a is None:
            continue
        replay[name] = {es: m.replay_k18(b, a, entry_start=es) for es in ("09:35", "09:45")}
    m.pull_5m = lambda client, t, day: bars_for(t)
    m.atr14 = lambda client, t, day: atr_for(t)
    out, skips = m.run_lane(None, DAY, "selection", [n for n, _, _ in LOCK_NAMES], {})
    return {"replay": replay, "lane": out, "skips": skips}


# Captured 2026-09-13 from the PRE-CHANGE sleeve (git HEAD 183a8a3) via a scratch
# generator that imported this file's fixture helpers. Two runs were byte-identical.
LOCK_EXPECTED = {'lane': {'09:35': [{'entry_window': '09:35',
                     'fill_t': '09:40',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 74.86,
                     'pnl_per_share': 0.4791,
                     'r': 1.5284,
                     'r_tiers': {'base': 1.4972, 'stress_2x': 1.1506, 'stress_3x': 0.804},
                     'rank': 1,
                     'reason': 'eod',
                     'shares': 156.2,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.32000000000000006,
                     'ticker': 'L1'},
                    {'entry_window': '09:35',
                     'fill_t': '09:40',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 78.07,
                     'pnl_per_share': 0.5309,
                     'r': 1.5909,
                     'r_tiers': {'base': 1.5615, 'stress_2x': 1.3877, 'stress_3x': 1.2139},
                     'rank': 2,
                     'reason': 'eod',
                     'shares': 147.1,
                     'side': 'S',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.34,
                     'ticker': 'S1'},
                    {'entry_window': '09:35',
                     'fill_t': '09:40',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': -56.59,
                     'pnl_per_share': -0.4301,
                     'r': -1.1056,
                     'r_tiers': {'base': -1.1319, 'stress_2x': -1.3699, 'stress_3x': -1.6079},
                     'rank': 4,
                     'reason': 'stop',
                     'shares': 131.6,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.38,
                     'ticker': 'L2'},
                    {'entry_window': '09:35',
                     'fill_t': '09:40',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': -52.96,
                     'pnl_per_share': -0.4449,
                     'r': -1.0355,
                     'r_tiers': {'base': -1.0593, 'stress_2x': -1.1535, 'stress_3x': -1.2478},
                     'rank': 6,
                     'reason': 'stop',
                     'shares': 119.0,
                     'side': 'S',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.42000000000000004,
                     'ticker': 'S2'},
                    {'entry_window': '09:35',
                     'fill_t': '09:40',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': -87.53,
                     'pnl_per_share': -0.7703,
                     'r': -1.728,
                     'r_tiers': {'base': -1.7507, 'stress_2x': -2.2288, 'stress_3x': -2.7068},
                     'rank': 7,
                     'reason': 'stop',
                     'shares': 113.6,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.44000000000000006,
                     'ticker': 'L3'},
                    {'entry_window': '09:35',
                     'fill_t': '10:20',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 50.95,
                     'pnl_per_share': 0.4891,
                     'r': 1.0398,
                     'r_tiers': {'base': 1.0189, 'stress_2x': 0.8087, 'stress_3x': 0.5985},
                     'rank': 9,
                     'reason': 'eod',
                     'shares': 104.2,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.48,
                     'ticker': 'LC'},
                    {'entry_window': '09:35',
                     'fill_t': '09:40',
                     'lane': 'selection',
                     'open_vol': 5000.0,
                     'pnl': 26.96,
                     'pnl_per_share': 0.5391,
                     'r': 1.0982,
                     'r_tiers': {'base': 1.0782, 'stress_2x': 0.9764, 'stress_3x': 0.8746},
                     'rank': 10,
                     'reason': 'eod',
                     'shares': 50.0,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.5,
                     'ticker': 'CAP'},
                    {'entry_window': '09:35',
                     'fill_t': '09:40',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 38.31,
                     'pnl_per_share': 0.4291,
                     'r': 0.7841,
                     'r_tiers': {'base': 0.7662, 'stress_2x': 0.4789, 'stress_3x': 0.1916},
                     'rank': 13,
                     'reason': 'eod',
                     'shares': 89.3,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.5599999999999999,
                     'ticker': 'L4'},
                    {'entry_window': '09:35',
                     'fill_t': '09:40',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 47.92,
                     'pnl_per_share': 0.5559,
                     'r': 0.9757,
                     'r_tiers': {'base': 0.9585, 'stress_2x': 0.8997, 'stress_3x': 0.8409},
                     'rank': 14,
                     'reason': 'eod',
                     'shares': 86.2,
                     'side': 'S',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.58,
                     'ticker': 'S3'},
                    {'entry_window': '09:35',
                     'fill_t': '09:40',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 23.26,
                     'pnl_per_share': 0.2791,
                     'r': 0.4818,
                     'r_tiers': {'base': 0.4652, 'stress_2x': -0.053, 'stress_3x': -0.5712},
                     'rank': 15,
                     'reason': 'eod',
                     'shares': 83.3,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.6000000000000001,
                     'ticker': 'L5'}],
          '09:45': [{'entry_window': '09:45',
                     'fill_t': '09:45',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 73.3,
                     'pnl_per_share': 0.4691,
                     'r': 1.4972,
                     'r_tiers': {'base': 1.4659, 'stress_2x': 1.1193, 'stress_3x': 0.7727},
                     'rank': 1,
                     'reason': 'eod',
                     'shares': 156.2,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.32000000000000006,
                     'ticker': 'L1'},
                    {'entry_window': '09:45',
                     'fill_t': '09:45',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 76.6,
                     'pnl_per_share': 0.5209,
                     'r': 1.5615,
                     'r_tiers': {'base': 1.5321, 'stress_2x': 1.3583, 'stress_3x': 1.1845},
                     'rank': 2,
                     'reason': 'eod',
                     'shares': 147.1,
                     'side': 'S',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.34,
                     'ticker': 'S1'},
                    {'entry_window': '09:45',
                     'fill_t': '09:45',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': -56.59,
                     'pnl_per_share': -0.4301,
                     'r': -1.1056,
                     'r_tiers': {'base': -1.1319, 'stress_2x': -1.37, 'stress_3x': -1.608},
                     'rank': 4,
                     'reason': 'stop',
                     'shares': 131.6,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.38,
                     'ticker': 'L2'},
                    {'entry_window': '09:45',
                     'fill_t': '09:45',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': -52.96,
                     'pnl_per_share': -0.4449,
                     'r': -1.0355,
                     'r_tiers': {'base': -1.0593, 'stress_2x': -1.1535, 'stress_3x': -1.2478},
                     'rank': 6,
                     'reason': 'stop',
                     'shares': 119.0,
                     'side': 'S',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.42000000000000004,
                     'ticker': 'S2'},
                    {'entry_window': '09:45',
                     'fill_t': '09:45',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': -88.67,
                     'pnl_per_share': -0.7803,
                     'r': -1.7508,
                     'r_tiers': {'base': -1.7735, 'stress_2x': -2.2515, 'stress_3x': -2.7296},
                     'rank': 7,
                     'reason': 'stop',
                     'shares': 113.6,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.44000000000000006,
                     'ticker': 'L3'},
                    {'entry_window': '09:45',
                     'fill_t': '10:20',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 50.95,
                     'pnl_per_share': 0.4891,
                     'r': 1.0398,
                     'r_tiers': {'base': 1.0189, 'stress_2x': 0.8087, 'stress_3x': 0.5985},
                     'rank': 9,
                     'reason': 'eod',
                     'shares': 104.2,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.48,
                     'ticker': 'LC'},
                    {'entry_window': '09:45',
                     'fill_t': '09:45',
                     'lane': 'selection',
                     'open_vol': 5000.0,
                     'pnl': 26.46,
                     'pnl_per_share': 0.5291,
                     'r': 1.0782,
                     'r_tiers': {'base': 1.0582, 'stress_2x': 0.9564, 'stress_3x': 0.8545},
                     'rank': 10,
                     'reason': 'eod',
                     'shares': 50.0,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.5,
                     'ticker': 'CAP'},
                    {'entry_window': '09:45',
                     'fill_t': '09:45',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 37.42,
                     'pnl_per_share': 0.4191,
                     'r': 0.7662,
                     'r_tiers': {'base': 0.7484, 'stress_2x': 0.461, 'stress_3x': 0.1737},
                     'rank': 13,
                     'reason': 'eod',
                     'shares': 89.3,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.5599999999999999,
                     'ticker': 'L4'},
                    {'entry_window': '09:45',
                     'fill_t': '09:45',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 47.06,
                     'pnl_per_share': 0.5459,
                     'r': 0.9585,
                     'r_tiers': {'base': 0.9412, 'stress_2x': 0.8824, 'stress_3x': 0.8237},
                     'rank': 14,
                     'reason': 'eod',
                     'shares': 86.2,
                     'side': 'S',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.58,
                     'ticker': 'S3'},
                    {'entry_window': '09:45',
                     'fill_t': '09:45',
                     'lane': 'selection',
                     'open_vol': 100000.0,
                     'pnl': 22.42,
                     'pnl_per_share': 0.2691,
                     'r': 0.4651,
                     'r_tiers': {'base': 0.4485, 'stress_2x': -0.0697, 'stress_3x': -0.5879},
                     'rank': 15,
                     'reason': 'eod',
                     'shares': 83.3,
                     'side': 'L',
                     'spread_proxy': 0.15,
                     'stop_dist': 0.6000000000000001,
                     'ticker': 'L5'}]},
 'replay': {'CAP': {'09:35': {'fill_t': '09:40',
                              'open_vol': 5000.0,
                              'pnl_per_share': 0.5391,
                              'r': 1.0982,
                              'r_tiers': {'base': 1.0782, 'stress_2x': 0.9764, 'stress_3x': 0.8746},
                              'reason': 'eod',
                              'side': 'L',
                              'spread_proxy': 0.15,
                              'stop_dist': 0.5},
                    '09:45': {'fill_t': '09:45',
                              'open_vol': 5000.0,
                              'pnl_per_share': 0.5291,
                              'r': 1.0782,
                              'r_tiers': {'base': 1.0582, 'stress_2x': 0.9564, 'stress_3x': 0.8545},
                              'reason': 'eod',
                              'side': 'L',
                              'spread_proxy': 0.15,
                              'stop_dist': 0.5}},
            'CX': {'09:35': None, '09:45': None},
            'L1': {'09:35': {'fill_t': '09:40',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.4791,
                             'r': 1.5284,
                             'r_tiers': {'base': 1.4972, 'stress_2x': 1.1506, 'stress_3x': 0.804},
                             'reason': 'eod',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.32000000000000006},
                   '09:45': {'fill_t': '09:45',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.4691,
                             'r': 1.4972,
                             'r_tiers': {'base': 1.4659, 'stress_2x': 1.1193, 'stress_3x': 0.7727},
                             'reason': 'eod',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.32000000000000006}},
            'L2': {'09:35': {'fill_t': '09:40',
                             'open_vol': 100000.0,
                             'pnl_per_share': -0.4301,
                             'r': -1.1056,
                             'r_tiers': {'base': -1.1319, 'stress_2x': -1.3699, 'stress_3x': -1.6079},
                             'reason': 'stop',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.38},
                   '09:45': {'fill_t': '09:45',
                             'open_vol': 100000.0,
                             'pnl_per_share': -0.4301,
                             'r': -1.1056,
                             'r_tiers': {'base': -1.1319, 'stress_2x': -1.37, 'stress_3x': -1.608},
                             'reason': 'stop',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.38}},
            'L3': {'09:35': {'fill_t': '09:40',
                             'open_vol': 100000.0,
                             'pnl_per_share': -0.7703,
                             'r': -1.728,
                             'r_tiers': {'base': -1.7507, 'stress_2x': -2.2288, 'stress_3x': -2.7068},
                             'reason': 'stop',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.44000000000000006},
                   '09:45': {'fill_t': '09:45',
                             'open_vol': 100000.0,
                             'pnl_per_share': -0.7803,
                             'r': -1.7508,
                             'r_tiers': {'base': -1.7735, 'stress_2x': -2.2515, 'stress_3x': -2.7296},
                             'reason': 'stop',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.44000000000000006}},
            'L4': {'09:35': {'fill_t': '09:40',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.4291,
                             'r': 0.7841,
                             'r_tiers': {'base': 0.7662, 'stress_2x': 0.4789, 'stress_3x': 0.1916},
                             'reason': 'eod',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.5599999999999999},
                   '09:45': {'fill_t': '09:45',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.4191,
                             'r': 0.7662,
                             'r_tiers': {'base': 0.7484, 'stress_2x': 0.461, 'stress_3x': 0.1737},
                             'reason': 'eod',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.5599999999999999}},
            'L5': {'09:35': {'fill_t': '09:40',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.2791,
                             'r': 0.4818,
                             'r_tiers': {'base': 0.4652, 'stress_2x': -0.053, 'stress_3x': -0.5712},
                             'reason': 'eod',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.6000000000000001},
                   '09:45': {'fill_t': '09:45',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.2691,
                             'r': 0.4651,
                             'r_tiers': {'base': 0.4485, 'stress_2x': -0.0697, 'stress_3x': -0.5879},
                             'reason': 'eod',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.6000000000000001}},
            'LC': {'09:35': {'fill_t': '10:20',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.4891,
                             'r': 1.0398,
                             'r_tiers': {'base': 1.0189, 'stress_2x': 0.8087, 'stress_3x': 0.5985},
                             'reason': 'eod',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.48},
                   '09:45': {'fill_t': '10:20',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.4891,
                             'r': 1.0398,
                             'r_tiers': {'base': 1.0189, 'stress_2x': 0.8087, 'stress_3x': 0.5985},
                             'reason': 'eod',
                             'side': 'L',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.48}},
            'NBODY': {'09:35': None, '09:45': None},
            'NC': {'09:35': None, '09:45': None},
            'S1': {'09:35': {'fill_t': '09:40',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.5309,
                             'r': 1.5909,
                             'r_tiers': {'base': 1.5615, 'stress_2x': 1.3877, 'stress_3x': 1.2139},
                             'reason': 'eod',
                             'side': 'S',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.34},
                   '09:45': {'fill_t': '09:45',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.5209,
                             'r': 1.5615,
                             'r_tiers': {'base': 1.5321, 'stress_2x': 1.3583, 'stress_3x': 1.1845},
                             'reason': 'eod',
                             'side': 'S',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.34}},
            'S2': {'09:35': {'fill_t': '09:40',
                             'open_vol': 100000.0,
                             'pnl_per_share': -0.4449,
                             'r': -1.0355,
                             'r_tiers': {'base': -1.0593, 'stress_2x': -1.1535, 'stress_3x': -1.2478},
                             'reason': 'stop',
                             'side': 'S',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.42000000000000004},
                   '09:45': {'fill_t': '09:45',
                             'open_vol': 100000.0,
                             'pnl_per_share': -0.4449,
                             'r': -1.0355,
                             'r_tiers': {'base': -1.0593, 'stress_2x': -1.1535, 'stress_3x': -1.2478},
                             'reason': 'stop',
                             'side': 'S',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.42000000000000004}},
            'S3': {'09:35': {'fill_t': '09:40',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.5559,
                             'r': 0.9757,
                             'r_tiers': {'base': 0.9585, 'stress_2x': 0.8997, 'stress_3x': 0.8409},
                             'reason': 'eod',
                             'side': 'S',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.58},
                   '09:45': {'fill_t': '09:45',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.5459,
                             'r': 0.9585,
                             'r_tiers': {'base': 0.9412, 'stress_2x': 0.8824, 'stress_3x': 0.8237},
                             'reason': 'eod',
                             'side': 'S',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.58}},
            'S4': {'09:35': {'fill_t': '09:40',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.5659,
                             'r': 0.9289,
                             'r_tiers': {'base': 0.9128, 'stress_2x': 0.8739, 'stress_3x': 0.835},
                             'reason': 'eod',
                             'side': 'S',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.6200000000000001},
                   '09:45': {'fill_t': '09:45',
                             'open_vol': 100000.0,
                             'pnl_per_share': 0.5559,
                             'r': 0.9128,
                             'r_tiers': {'base': 0.8966, 'stress_2x': 0.8578, 'stress_3x': 0.8189},
                             'reason': 'eod',
                             'side': 'S',
                             'spread_proxy': 0.15,
                             'stop_dist': 0.6200000000000001}}},
 'skips': [('NB', 'no_bars'), ('NA', 'no_atr')]}


def test_frozen_construction_behaviour_lock(sleeve):
    assert LOCK_EXPECTED is not None, "generate LOCK_EXPECTED from the pre-change code first"
    got = lock_outputs(sleeve)
    assert got["replay"] == LOCK_EXPECTED["replay"]
    assert got["lane"] == LOCK_EXPECTED["lane"]
    assert got["skips"] == LOCK_EXPECTED["skips"]
    # sanity on what the lock covers: both lanes fill differently, the slot
    # cap drops the rank-16 name, and every skip/None path is present
    assert got["replay"]["L1"]["09:35"]["fill_t"] == "09:40"
    assert got["replay"]["L1"]["09:45"]["fill_t"] == "09:45"
    assert got["replay"]["L2"]["09:35"]["reason"] == "stop"
    assert got["replay"]["NC"] == {"09:35": None, "09:45": None}
    assert got["replay"]["CX"] == {"09:35": None, "09:45": None}
    assert got["replay"]["NBODY"] == {"09:35": None, "09:45": None}
    assert len(got["lane"]["09:35"]) == len(got["lane"]["09:45"]) == 10
    assert "S4" not in {t["ticker"] for t in got["lane"]["09:35"]}
    assert got["lane"]["09:35"][9]["ticker"] == "L5"
    assert got["skips"] == [("NB", "no_bars"), ("NA", "no_atr")]
