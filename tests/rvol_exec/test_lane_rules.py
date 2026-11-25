"""Known-answer unit tests for every RVOL-EXEC rule (spec §16.2): rules,
snapshot T_ready + two-stage rank, prep formulas + freshness, ledger,
post_result copy, state transitions, liveness rules, constants pins.
Hermetic — paths redirected by the conftests; no socket, no API.
UNVALIDATED — PAPER ONLY.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

import src.rvol_exec.paths as paths
from src.rvol_exec import causal, ledger as ledger_mod, liveness_rules as lr, prep, rules, snapshot
from src.rvol_exec.clock import FakeClock, at_et
from src.rvol_exec.constants import (CLIENT_ID, ET, GUARD_FRAC, HOLIDAYS_2026, LOSS_HALT_USD,
                                     OPTIONS_ENGINE_TAG, PART_CAP, RISK_USD, SLOTS, STAMP,
                                     STOP_ATR_FRAC, T_ENTRY_END, T_NO_ENTRY_AFTER, TAG)
from src.rvol_exec.post_result import AMBIGUOUS, DEFINITIVE_REJECT, OK, classify_post_result
from src.rvol_exec.state import (CANDIDATE, HELD, INTENT, RESTING, SKIPPED, STOPPED,
                                 IllegalTransition, NameState, SessionState)

ROOT = Path(__file__).resolve().parents[2]
DAY = "2026-09-14"


# ── constants (§1) ───────────────────────────────────────────────────────────
def test_constants_pinned_and_holidays_equal_config():
    assert TAG == "UNVALIDATED-RVOL-PAPER" and TAG != OPTIONS_ENGINE_TAG
    assert STAMP == "UNVALIDATED — PAPER ONLY"
    assert CLIENT_ID == 61 and SLOTS == 10 and RISK_USD == 50.0 and STOP_ATR_FRAC == 0.20
    assert GUARD_FRAC == 0.10 and PART_CAP == 0.01 and LOSS_HALT_USD == -300.0
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())     # read-only
    assert tuple(cfg["agent"]["holidays_2026"]) == HOLIDAYS_2026


def test_kill_switch_path_is_the_shared_file_before_redirect():
    # the conftest redirects paths.KILL_SWITCH_FILE under tmp; its source
    # definition must be the shared data/KILL_SWITCH the options engine honours
    src = (ROOT / "src" / "rvol_exec" / "paths.py").read_text()
    assert 'KILL_SWITCH_FILE = ROOT / "data" / "KILL_SWITCH"' in src
    from src.engine.live_gate import KILL_SWITCH_FILE as LG
    assert LG.name == "KILL_SWITCH" and LG.parent.name == "data"


def test_lane_dir_is_a_sibling_of_data_lane():
    src = (ROOT / "src" / "rvol_exec" / "paths.py").read_text()
    assert 'LANE_DIR = ROOT / "data" / "lane_rvol"' in src
    assert not (ROOT / "data" / "lane_rvol").resolve().is_relative_to((ROOT / "data" / "lane").resolve())


# ── rules (§7) ───────────────────────────────────────────────────────────────
def test_doji_filter_known_answers():
    assert rules.doji_ok(10.0, 10.5, 9.9, 10.4)                    # body 0.4 / rng 0.6
    assert not rules.doji_ok(10.0, 10.5, 9.9, 10.05)               # body 0.05 < 0.06
    assert not rules.doji_ok(10.0, 10.0, 10.0, 10.0)               # rng 0
    assert rules.doji_ok(10.0, 10.6, 10.0, 10.06)                  # body exactly 10% -> ok


def test_side_level_stop_limit_known_answers():
    assert rules.side_of(10.0, 10.4) == "long" and rules.side_of(10.0, 9.6) == "short"
    assert rules.side_of(10.0, 10.0) == "short"                    # c > o strictly
    assert rules.level_of("long", 10.5, 9.9) == 10.51
    assert rules.level_of("short", 10.5, 9.9) == 9.89
    assert rules.stop_dist_of(2.0) == pytest.approx(0.4)
    assert rules.limit_of("long", 10.51, 0.4) == 10.55
    assert rules.limit_of("short", 9.89, 0.4) == 9.85
    assert rules.stop_provisional_of("long", 10.51, 0.4) == 10.11
    assert rules.stop_provisional_of("short", 9.89, 0.4) == 10.29
    assert rules.stop_final_of("long", 10.53, 0.4) == 10.13


def test_shares_floor_and_at_least_one():
    assert rules.shares_live(0.4, 200_000) == 125                  # 50/0.4 = 125 exactly
    assert rules.shares_sim(0.4, 200_000) == 125.0
    assert rules.shares_live(0.4, 5_000) == 50                     # 1% of v930 binds
    assert rules.shares_sim(0.4, 5_000) == 50.0
    assert rules.shares_live(0.3, 200_000) == 166                  # floor(166.67)
    assert rules.shares_sim(0.3, 200_000) == 166.7                 # sim keeps the fraction
    assert rules.shares_live(60.0, 200_000) == 0                   # 50/60 < 1 -> skipped{size}
    assert rules.plan_name(10.0, 10.5, 9.9, 10.4, 200_000, 300.0)["reason"] == "size"


def test_notional_cap_known_answer():
    assert rules.notional_ok(100, 250.0)                           # 25,000 exactly
    assert not rules.notional_ok(100, 250.01)
    p = rules.plan_name(1000.0, 1050.0, 990.0, 1040.0, 5_000_000, 5.0)   # sd=1 -> 50 sh * 1050 > cap
    assert p["status"] == "skipped" and p["reason"] == "cap"


def test_plan_name_full_answer():
    p = rules.plan_name(10.0, 10.5, 9.9, 10.4, 200_000, 2.0)
    assert p["status"] == "ok" and p["side"] == "long" and p["level"] == 10.51
    assert p["stop_dist"] == pytest.approx(0.4) and p["limit_px"] == 10.55
    assert p["stop_provisional"] == 10.11 and p["shares_live"] == 125 and p["shares_sim"] == 125.0
    q = rules.plan_name(10.0, 10.5, 9.9, 9.6, 200_000, 2.0)
    assert q["side"] == "short" and q["level"] == 9.89 and q["limit_px"] == 9.85 and q["stop_provisional"] == 10.29


def test_stale_trigger_guard_boundary():
    # long: limit 10.55 — exactly at the limit places, one tick beyond skips
    assert rules.stale_trigger_ok("long", 10.55, 10.55)
    assert not rules.stale_trigger_ok("long", 10.56, 10.55)
    assert rules.stale_trigger_ok("long", 10.20, 10.55)
    assert rules.stale_trigger_ok("short", 9.85, 9.85)
    assert not rules.stale_trigger_ok("short", 9.84, 9.85)
    assert rules.stale_trigger_ok("long", None, 10.55)              # no reference -> inert


def test_breach_rule_only_beyond_limit():
    assert rules.breached("long", 10.56, 10.55) and not rules.breached("long", 10.55, 10.55)
    assert rules.breached("short", 9.84, 9.85) and not rules.breached("short", 9.85, 9.85)
    assert not rules.breached("long", None, 10.55)


def test_may_place_caps():
    ok, _ = rules.may_place(at_et(DAY, 10, 24, 59), T_ENTRY_END, T_NO_ENTRY_AFTER, 0, 0, False)
    assert ok
    assert rules.may_place(at_et(DAY, 10, 25, 0), T_ENTRY_END, T_NO_ENTRY_AFTER, 0, 0, False) == (False, "window_closed")
    assert rules.may_place(at_et(DAY, 9, 40), T_ENTRY_END, T_NO_ENTRY_AFTER, 20, 0, False)[1] == "max_entries"
    assert rules.may_place(at_et(DAY, 9, 40), T_ENTRY_END, T_NO_ENTRY_AFTER, 0, 10, False)[1] == "slots_full"
    assert rules.may_place(at_et(DAY, 9, 40), T_ENTRY_END, T_NO_ENTRY_AFTER, 0, 0, True)[1] == "entries_closed"


def test_entry_start_alignment_rule():
    assert rules.entry_start_hm(at_et(DAY, 9, 35, 5)) == "09:35"     # within 30 s of the bar open
    assert rules.entry_start_hm(at_et(DAY, 9, 35, 30)) == "09:35"    # boundary inclusive
    assert rules.entry_start_hm(at_et(DAY, 9, 35, 31)) == "09:40"    # else ceil
    assert rules.entry_start_hm(at_et(DAY, 9, 50, 0)) == "09:50"
    assert rules.entry_start_hm(at_et(DAY, 9, 52, 10)) == "09:55"


def test_slip_and_r_booked():
    assert rules.slip_bps("long", 10.02, 10.00) == pytest.approx(20.0)
    assert rules.slip_bps("short", 10.02, 10.00) == pytest.approx(-20.0)
    assert rules.r_booked("long", 10.0, 10.4, 0.4) == 1.0
    assert rules.r_booked("short", 10.0, 10.4, 0.4) == -1.0
    assert rules.oca_group_for("20260914", "ABCD") == "RVX-20260914-ABCD"


# ── snapshot (§5, §6) ────────────────────────────────────────────────────────
def _ms(dt): return int(dt.timestamp() * 1000)
def _ns(dt): return int(dt.timestamp() * 1e9)


def _snap(now_et, ref_min_t, updated_at, n=6000, prep_syms=("AAA", "BBB"), day_o=True):
    tk = []
    for i in range(n):
        sym = f"T{i:05d}"
        tk.append({"ticker": sym, "updated": _ns(updated_at), "day": {"o": 10.0, "v": 100.0},
                   "min": {"t": _ms(ref_min_t), "av": 100.0, "c": 10.0}})
    for s in ("SPY", "QQQ"):
        tk.append({"ticker": s, "updated": _ns(updated_at), "day": {"o": 500.0, "v": 1e6},
                   "min": {"t": _ms(ref_min_t), "av": 1e6, "c": 500.0}})
    for s in prep_syms:
        tk.append({"ticker": s, "updated": _ns(updated_at), "day": ({"o": 20.0, "v": 5e5} if day_o else {}),
                   "min": {"t": _ms(ref_min_t), "av": 5e5, "c": 20.5}})
    return {"tickers": tk}


def test_snapshot_units_ns_and_ms_with_literal_stamps():
    # 2026-09-14 09:34:00 ET == 13:34:00 UTC == 1789392840 s
    t = at_et(DAY, 9, 34)
    assert snapshot.ms_to_et(1789392840000).isoformat() == t.isoformat()
    assert snapshot.ns_to_et(1789392840_000_000_000).isoformat() == t.isoformat()


def test_parse_snapshot_realtime_ready_and_delayed():
    now = at_et(DAY, 9, 35, 5)
    m = snapshot.parse_snapshot(_snap(now, at_et(DAY, 9, 34), now - timedelta(seconds=3)), ["AAA", "BBB"],
                                now.astimezone(timezone.utc))
    assert m["realtime"] and m["ready"] and not m["contaminated"] and m["ok"] and m["coverage"] == 1.0
    m2 = snapshot.parse_snapshot(_snap(now, at_et(DAY, 9, 20), now - timedelta(seconds=900)), ["AAA"],
                                 now.astimezone(timezone.utc))
    assert not m2["realtime"] and not m2["ready"] and m2["delay_s"] == pytest.approx(900, abs=1)
    m3 = snapshot.parse_snapshot(_snap(now, at_et(DAY, 9, 36), now), ["AAA"], now.astimezone(timezone.utc))
    assert m3["ready"] and m3["contaminated"]


def test_parse_snapshot_prior_day_min_t_excluded_and_floors():
    now = at_et(DAY, 9, 35, 5)
    m = snapshot.parse_snapshot(_snap(now, at_et(DAY, 15, 59) - timedelta(days=1), now), ["AAA"],
                                now.astimezone(timezone.utc))
    assert m["ref_min_t_et"] is None and not m["ready"]
    m2 = snapshot.parse_snapshot(_snap(now, at_et(DAY, 9, 34), now, n=100), ["AAA"], now.astimezone(timezone.utc))
    assert m2["ready"] and not m2["ok"]                               # < 5,000 tickers
    m3 = snapshot.parse_snapshot(_snap(now, at_et(DAY, 9, 34), now, day_o=False), ["AAA", "BBB"],
                                 now.astimezone(timezone.utc))
    assert m3["coverage"] == 0.0 and not m3["ok"]


class _Poly:
    serial = True

    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = 0

    def snapshot_all(self):
        self.calls += 1
        item = self.queue.pop(0) if self.queue else None
        return item() if callable(item) else item

    def bars_5m(self, t, day):
        return None

    def last_price(self, t):
        return None


def _ledger(clock):
    return ledger_mod.Ledger(DAY, clock, sha="t")


def test_poller_errors_429_retry_after_empty_body_then_ready():
    clock = FakeClock(at_et(DAY, 9, 35, 0))
    ok = lambda: _snap(clock.now_et(), at_et(DAY, 9, 34), clock.now_et())  # noqa: E731
    poly = _Poly([{"error": "HTTP 503"}, {"error": "HTTP 429 Too Many", "retry_after": 12}, None, ok])
    led = _ledger(clock)
    p = snapshot.TReadyPoller(clock, poly, led, ["AAA"], None)
    assert p.poll()[0] == "wait"                       # 503 -> wait 30 s
    clock.sleep(29); assert p.poll()[0] == "wait" and poly.calls == 1
    clock.sleep(1); assert p.poll()[0] == "wait" and poly.calls == 2       # 429 -> Retry-After 12
    clock.sleep(11); assert poly.calls == 2 and p.poll()[0] == "wait"
    clock.sleep(1); assert p.poll()[0] == "wait" and poly.calls == 3       # empty body -> wait 30
    clock.sleep(30)
    status, snap, meta = p.poll()
    assert status == "ready" and meta["mode"] == "realtime" and poly.calls == 4
    errs = [r for r in led.records if r["event"] == "snapshot_error"]
    assert [e["retry_in_s"] for e in errs] == [30.0, 12.0, 30.0]
    assert any(r["event"] == "t_ready" for r in led.records)


def test_poller_delayed_polls_until_0934_then_contamination_and_deadline():
    clock = FakeClock(at_et(DAY, 9, 35, 0))
    delay = timedelta(seconds=900)

    def delayed():
        vis = clock.now_et() - delay
        ref = vis.replace(second=0, microsecond=0) - timedelta(minutes=1)
        return _snap(clock.now_et(), ref, vis)
    poly = _Poly([delayed] * 200)
    led = _ledger(clock)
    p = snapshot.TReadyPoller(clock, poly, led, ["AAA"], None)
    status = "wait"
    while status == "wait":
        status, snap, meta = p.poll()
        if status == "wait":
            clock.sleep(30)
    assert status == "ready" and meta["mode"] == "delayed"
    assert meta["t_ready_et"] == at_et(DAY, 9, 50, 0).isoformat(timespec="seconds")
    assert not meta["contaminated"]
    # deadline: nothing ever ready -> timeout at 10:10
    clock2 = FakeClock(at_et(DAY, 9, 35, 0))
    poly2 = _Poly([lambda: _snap(clock2.now_et(), at_et(DAY, 9, 20), clock2.now_et())] * 200)
    p2 = snapshot.TReadyPoller(clock2, poly2, _ledger(clock2), ["AAA"], None)
    st = "wait"
    while st == "wait":
        st = p2.poll()[0]
        clock2.sleep(30)
    assert st == "timeout" and clock2.now_et() >= at_et(DAY, 10, 10)
    # late start: first view already past the bar -> contaminated with contamination_s
    clock3 = FakeClock(at_et(DAY, 9, 41, 0))
    poly3 = _Poly([lambda: _snap(clock3.now_et(), at_et(DAY, 9, 40), clock3.now_et())])
    p3 = snapshot.TReadyPoller(clock3, poly3, _ledger(clock3), ["AAA"], None)
    st, _, meta3 = p3.poll()
    assert st == "ready" and meta3["contaminated"] and meta3["contamination_s"] == 360.0


class _Poly2(_Poly):
    def __init__(self, snap, bars):
        super().__init__([snap])
        self.bars = bars
        self.bar_calls = []

    def bars_5m(self, t, day):
        self.bar_calls.append(t)
        return self.bars.get(t)


def test_two_stage_rank_equals_exact_bar_rank_with_premarket_inflated_min_av():
    now = at_et(DAY, 9, 35, 5)
    base = _snap(now, at_et(DAY, 9, 34), now, n=5000, prep_syms=())
    t930 = _ms(at_et(DAY, 9, 30))

    def bar(o, h, l, c, v):
        return [{"t": t930, "o": o, "h": h, "l": l, "c": c, "v": v}]
    # NEWS has pre-market volume inflating min.av (4e5) but a small 09:30 bar (1e5);
    # CLEAN has min.av == its 09:30 bar (3e5). Same baseline -> exact rank puts CLEAN first.
    base["tickers"] += [
        {"ticker": "NEWS", "updated": _ns(now), "day": {"o": 20.0, "h": 21, "l": 19.5, "c": 20.8, "v": 4e5},
         "min": {"t": _ms(at_et(DAY, 9, 34)), "av": 4e5, "c": 20.8}},
        {"ticker": "CLEAN", "updated": _ns(now), "day": {"o": 30.0, "h": 31, "l": 29.5, "c": 30.8, "v": 3e5},
         "min": {"t": _ms(at_et(DAY, 9, 34)), "av": 3e5, "c": 30.8}},
        {"ticker": "CHEAP", "updated": _ns(now), "day": {"o": 4.0, "h": 4.1, "l": 3.9, "c": 4.05, "v": 9e5},
         "min": {"t": _ms(at_et(DAY, 9, 34)), "av": 9e5, "c": 4.05}},
    ]
    prep_rows = [{"ticker": s, "atr14": 1.0, "avgvol14": 2e6, "baseline14": 1e5, "n_baseline": 14, "prev_close": 1}
                 for s in ("NEWS", "CLEAN", "CHEAP")]
    prep_rows.append({"ticker": "SHORTBASE", "atr14": 1.0, "avgvol14": 2e6, "baseline14": 1e5, "n_baseline": 13, "prev_close": 1})
    poly = _Poly2(base, {"NEWS": bar(20.0, 21, 19.5, 20.8, 1e5), "CLEAN": bar(30.0, 31, 29.5, 30.8, 3e5)})
    res = snapshot.rank_universe(base, prep_rows, poly, DAY, contaminated=False, stage2=True)
    names = [r["ticker"] for r in res["top20"]]
    assert names[:2] == ["CLEAN", "NEWS"], names                    # exact bar restores the sim's input
    assert "CHEAP" not in names                                     # open < 5 filtered
    assert res["top20"][0]["orb_source"] == "exact" and res["top20"][0]["v930_used"] == 3e5
    assert set(poly.bar_calls) == {"NEWS", "CLEAN"}
    # stage 2 off: snapshot volumes rank NEWS first
    res2 = snapshot.rank_universe(base, prep_rows, _Poly2(base, {}), DAY, contaminated=False, stage2=False)
    assert [r["ticker"] for r in res2["top20"]][:2] == ["NEWS", "CLEAN"]
    assert all(r["orb_source"] == "snapshot" for r in res2["top20"])
    # contaminated + missing exact bar -> orb_unavailable
    res3 = snapshot.rank_universe(base, prep_rows, _Poly2(base, {"CLEAN": bar(30.0, 31, 29.5, 30.8, 3e5)}), DAY,
                                  contaminated=True, stage2=True)
    assert [r["ticker"] for r in res3["top20"]] == ["CLEAN"]
    assert {"ticker": "NEWS", "reason": "orb_unavailable"} in res3["excluded"]


def test_rank_ties_stable_by_ticker():
    now = at_et(DAY, 9, 35, 5)
    base = _snap(now, at_et(DAY, 9, 34), now, n=5000, prep_syms=())
    for s in ("BBB", "AAA", "CCC"):
        base["tickers"].append({"ticker": s, "updated": _ns(now), "day": {"o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100},
                                "min": {"t": _ms(at_et(DAY, 9, 34)), "av": 100, "c": 10.5}})
    prep_rows = [{"ticker": s, "atr14": 1.0, "avgvol14": 2e6, "baseline14": 10, "n_baseline": 14} for s in ("BBB", "AAA", "CCC")]
    res = snapshot.rank_universe(base, prep_rows, _Poly2(base, {}), DAY, stage2=False)
    assert [r["ticker"] for r in res["top20"]] == ["AAA", "BBB", "CCC"]


# ── prep (§14) ───────────────────────────────────────────────────────────────
def _grouped_fixture(tmp_path, dates, tickers=("AAA", "BBB")):
    d = tmp_path / "grouped"
    d.mkdir()
    for i, ds in enumerate(dates):
        rows = []
        for j, t in enumerate(tickers):
            px = 10 + i * 0.5 + j
            rows.append({"ticker": t, "open": px, "high": px + 1 + 0.1 * i, "low": px - 1, "close": px + 0.2,
                         "volume": 1_500_000 + i * 1000 + j})
        pd.DataFrame(rows).to_parquet(d / f"{ds}.parquet")
    return d


def _sessions(n, end="2026-09-10"):
    out, d = [], date.fromisoformat(end)
    while len(out) < n:
        if prep.is_trading_day(d):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def test_prep_features_equal_the_shadow_scan_formula(tmp_path):
    dates = _sessions(18)
    gdir = _grouped_fixture(tmp_path, dates)
    d_prev, d_today = dates[-1], "2026-09-11"
    ours = prep.daily_features(gdir, d_prev, d_today).set_index("ticker")
    # the shadow scan at D_today's close: last 16 files INCLUDING today, shift(1)
    hist = prep.load_grouped(gdir, d_prev, 15)
    today_rows = hist[hist["date"] == d_prev].copy()
    today_rows["date"] = d_today
    today_rows[["open", "high", "low", "close"]] = 999.0                # today's values must not matter
    shadow = prep.shadow_features(pd.concat([hist, today_rows]), d_today).set_index("ticker")
    for t in ("AAA", "BBB"):
        assert ours.loc[t, "atr14"] == pytest.approx(shadow.loc[t, "atr14"])
        assert ours.loc[t, "avgvol14"] == pytest.approx(shadow.loc[t, "avgvol14"])
    assert ours.loc["AAA", "prev_close"] == pytest.approx(hist[(hist.ticker == "AAA") & (hist.date == d_prev)]["close"].iloc[0])


def test_baseline14_needs_exactly_14_sessions_and_excludes_today():
    v = {d: 100.0 + i for i, d in enumerate(_sessions(20))}
    b, n = prep.baseline14(v, "2026-09-10")
    assert n == 14 and b == pytest.approx(sum(sorted(v.values())[-14:]) / 14)
    b2, n2 = prep.baseline14({d: 1.0 for d in _sessions(13)}, "2026-09-10")
    assert b2 is None and n2 == 13
    v3 = dict(v); v3["2026-09-11"] = 1e9                              # today never in its own baseline
    assert prep.baseline14(v3, "2026-09-10")[0] == b
    assert prep.baseline14({d: 0.0 for d in _sessions(14)}, "2026-09-10")[0] is None


class _Api:
    def __init__(self):
        self.calls = []

    def bars_5m_range(self, t, s, e):
        self.calls.append(t)
        rows = []
        for d in _sessions(16):
            ts = at_et(d, 9, 30)
            rows.append({"t": int(ts.timestamp() * 1000), "o": 1, "h": 1, "l": 1, "c": 1, "v": 5000})
        return rows


def test_build_prep_never_calls_api_without_allow_api_and_sources(tmp_path):
    dates = _sessions(18)
    gdir = _grouped_fixture(tmp_path, dates, tickers=("AAA", "BBB", "CCC"))
    fresh = tmp_path / "fresh"
    for d in _sessions(14):
        (fresh / d).mkdir(parents=True)
        pd.DataFrame([{"hm": "09:30", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1000}]).to_parquet(fresh / d / "AAA.parquet")
    store = tmp_path / "store"
    store.mkdir()
    pd.DataFrame([{"date": d, "hm": "09:30", "o": 1, "h": 1, "l": 1, "c": 1, "v": 2000} for d in _sessions(14)]).to_parquet(store / "BBB.parquet")
    api = _Api()
    df, meta = build = prep.build_prep("2026-09-11", gdir, store, fresh, tmp_path / "cache.parquet",
                                       tmp_path / "prep", api=api, allow_api=False)
    assert api.calls == []
    r = df.set_index("ticker")
    assert r.loc["AAA", "baseline14"] == 1000 and r.loc["BBB", "baseline14"] == 2000
    assert pd.isna(r.loc["CCC", "baseline14"]) and r.loc["CCC", "n_baseline"] == 0
    assert meta["sources"] == {"cache": 0, "fresh": 1, "store": 1, "api": 0}
    assert meta["coverage"]["without_baseline"] == 1 and meta["built_from_through"] == "2026-09-10"
    # with the flag: only CCC is pulled; cache now serves everything
    df2, meta2 = prep.build_prep("2026-09-11", gdir, store, fresh, tmp_path / "cache.parquet",
                                 tmp_path / "prep", api=api, allow_api=True)
    assert api.calls == ["CCC"] and df2.set_index("ticker").loc["CCC", "baseline14"] == 5000
    df3, meta3 = prep.build_prep("2026-09-11", gdir, store, fresh, tmp_path / "cache.parquet",
                                 tmp_path / "prep", api=api, allow_api=False)
    assert meta3["sources"]["cache"] == 3 and api.calls == ["CCC"]
    assert (tmp_path / "prep" / "2026-09-11.meta.json").exists()


def test_freshness_across_weekend_and_holiday():
    good = lambda pf, thr: {"prep_for": pf, "built_from_through": thr,  # noqa: E731
                            "coverage": {"candidates": 900, "with_baseline14": 850}}
    assert prep.freshness(good("2026-09-14", "2026-09-11"), "2026-09-14")[0]          # Friday -> Monday
    assert prep.freshness(good("2026-09-08", "2026-09-04"), "2026-09-08")[0]          # across Labor Day 09-07
    assert not prep.freshness(good("2026-09-14", "2026-09-10"), "2026-09-14")[0]      # two sessions old
    assert not prep.freshness(good("2026-09-11", "2026-09-10"), "2026-09-14")[0]      # wrong day
    assert not prep.freshness(None, "2026-09-14")[0]
    assert not prep.freshness({"prep_for": "2026-09-14", "built_from_through": "2026-09-11",
                               "coverage": {"candidates": 400, "with_baseline14": 400}}, "2026-09-14")[0]
    assert not prep.freshness({"prep_for": "2026-09-14", "built_from_through": "2026-09-11",
                               "coverage": {"candidates": 900, "with_baseline14": 700}}, "2026-09-14")[0]
    assert prep.previous_trading_day(date(2026, 9, 8)) == date(2026, 9, 4)


# ── ledger (§15) ─────────────────────────────────────────────────────────────
def test_ledger_required_fields_stamp_seq_and_restart_continuation():
    clock = FakeClock(at_et(DAY, 9, 33))
    led = ledger_mod.Ledger(DAY, clock, sha="abc")
    r = led.emit("session_start", x=1)
    for k in ("ts_et", "date", "seq", "pid", "sha", "run_id", "lane", "event", "stamp", "criteria"):
        assert k in r
    assert r["stamp"] == STAMP and r["seq"] == 1 and r["lane"] == "rvol_exec"
    led.emit("connected")
    with pytest.raises(ValueError):
        led.emit("not_an_event")
    # torn last line tolerated; a new process continues the seq
    with open(paths.LEDGER, "a") as fh:
        fh.write('{"date": "' + DAY + '", "seq": 99, "lane": "rvol_exec", "torn')
    led2 = ledger_mod.Ledger(DAY, clock, sha="abc")
    assert led2.emit("connected")["seq"] == 3 and led2.run_id != led.run_id
    assert len(led2.read_today()) == 3
    assert str(paths.LEDGER).endswith("logs/observation/rvol_exec_paper.jsonl")


# ── post_result copy (D1) ────────────────────────────────────────────────────
def test_post_result_copy_equals_original():
    from src.data.order_client import classify_post_result as original    # test-side only
    table = [None, "x", {"transport_error": True, "error": "timeout"},
             {"order": {"status": "rejected"}, "reason": "bp"}, {"error": "qualify_failed"},
             {"order": {"id": 5, "status": "open"}}, {"order": {}}, {"order": {"id": 0, "status": "pending"}}]
    for env in table:
        assert classify_post_result(env) == original(env)
    assert classify_post_result({"order": {"id": 5}})["status"] == OK
    assert classify_post_result({"transport_error": True})["status"] == AMBIGUOUS
    assert classify_post_result(None)["status"] == DEFINITIVE_REJECT


# ── state machine (§9) ───────────────────────────────────────────────────────
def test_state_transitions_legal_and_illegal():
    n = NameState("ABCD")
    n.transition(INTENT); n.transition(RESTING); n.transition(HELD); n.transition(STOPPED)
    with pytest.raises(IllegalTransition):
        n.transition(RESTING)                       # terminal
    m = NameState("EFGH")
    with pytest.raises(IllegalTransition):
        m.transition(HELD)                          # candidate -> held is not legal
    m.transition(SKIPPED)
    assert m.is_terminal


def test_session_state_roundtrip(tmp_path):
    s = SessionState(date=DAY, phase="manage", slots_used=3)
    n = NameState("ABCD", rank=2, state=HELD, held_qty=10, exec_ids=["e1"])
    s.names["ABCD"] = n
    s.save(tmp_path / "state.json")
    s2 = SessionState.load(tmp_path / "state.json")
    assert s2.phase == "manage" and s2.slots_used == 3
    assert isinstance(s2.names["ABCD"], NameState) and s2.names["ABCD"].held_qty == 10
    (tmp_path / "bad.json").write_text("{not json")
    assert SessionState.load(tmp_path / "bad.json") is None


# ── causal_first10 (§3) ──────────────────────────────────────────────────────
def _bars_with_cross(level_cross_hm, o=10.0, h=10.5, l=9.9, c=10.4, v=100_000, gap=False):
    from tests.rvol_exec.session_sim import make_bars
    path = {}
    if level_cross_hm:
        px = 10.75 if gap else 10.45
        path[level_cross_hm] = (px, 10.9, px - 0.05, 10.8)
        path["15:55"] = (10.9, 10.95, 10.85, 10.92)
    return make_bars((o, h, l, c, v), path)


def test_causal_first10_orders_by_crossing_bar_tie_rank_and_cuts():
    bars = {f"N{i}": _bars_with_cross(hm) for i, hm in enumerate(
        ["09:45", "09:40", "09:40", "09:50", "09:35", "10:00", "09:55", "10:05", "10:10", "10:15", "10:20", "10:25"])}
    rank_rows = [{"ticker": f"N{i}", "rank": i + 1} for i in range(12)]
    atr = {t: 2.0 for t in bars}
    taken, cancelled = causal.replay_causal_first10(rank_rows, bars, atr, skips=["N4"])
    assert [t["ticker"] for t in taken] == ["N1", "N2", "N0", "N3", "N6", "N5", "N7", "N8", "N9", "N10"]
    assert [c[0] for c in cancelled] == []                              # N11 crosses AT 10:25 -> excluded (f)
    assert all(t["exit_open"] == 10.9 for t in taken)                    # 15:55 bar OPEN
    assert taken[0]["r_tiers"]["stress_2x"] < taken[0]["r_tiers"]["base"]
    # 11 crossings -> the 11th is cancelled at the 10th crossing's bar; N4 (rank 5)
    # and N10 (rank 11) both cross at 10:20 -> tie -> rank: N4 takes the slot
    bars["N4"] = _bars_with_cross("10:20")
    taken2, cancelled2 = causal.replay_causal_first10(rank_rows, bars, atr, skips=[])
    assert len(taken2) == 10 and cancelled2 == [("N10", "cancelled_slots", "10:20")]
    assert [t["ticker"] for t in taken2][-1] == "N4"
    # guardrail: the crossing bar opens beyond level + 0.1*sd -> never taken
    bars["G"] = _bars_with_cross("09:40", gap=True)
    rank_rows.append({"ticker": "G", "rank": 0})
    atr["G"] = 2.0
    taken3, _ = causal.replay_causal_first10(rank_rows, bars, atr, skips=[])
    assert "G" not in [t["ticker"] for t in taken3]


# ── liveness rules (§15.4) ───────────────────────────────────────────────────
def test_liveness_decision_functions():
    now = at_et(DAY, 11, 0)
    hb = {f: 0 for f in lr.HEARTBEAT_FIELDS}
    assert lr.heartbeat_ok(hb)[0] and not lr.heartbeat_ok({})[0]
    fresh = dict(hb, ts_et=(now - timedelta(seconds=30)).isoformat())
    stale = dict(hb, ts_et=(now - timedelta(seconds=200)).isoformat())
    assert not lr.rvol_lane_dead_in_session(fresh, now)
    assert lr.rvol_lane_dead_in_session(stale, now) and lr.rvol_lane_dead_in_session(None, now)
    assert not lr.rvol_lane_dead_in_session(None, at_et(DAY, 8, 0))            # outside the session
    assert not lr.rvol_lane_dead_in_session(None, at_et("2026-09-13", 11, 0))  # Sunday
    assert lr.rvol_lane_stop_missing({"positions_without_live_stop": 1}, {"positions_without_live_stop": 1})
    assert not lr.rvol_lane_stop_missing({"positions_without_live_stop": 0}, {"positions_without_live_stop": 1})
    assert lr.rvol_lane_unflattened(True) and not lr.rvol_lane_unflattened(False)
    hist = [(now - timedelta(minutes=6), {"broker": "unknown", "held": 2}),
            (now - timedelta(minutes=3), {"broker": "unknown", "held": 2})]
    assert lr.rvol_lane_broker_unknown(hist, now)
    assert not lr.rvol_lane_broker_unknown([(now - timedelta(minutes=6), {"broker": "unknown", "held": 0})], now)
    assert not lr.rvol_lane_broker_unknown(hist + [(now, {"broker": "connected", "held": 2})], now)


# ── comparator fidelity vs the frozen replay_k18 (finding 2026-09-13: slipped hard stop) ──
def _load_sleeve():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rvolrank_sleeve_ro", ROOT / "scripts" / "rvolrank_sleeve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # offline: reads the criteria yaml only, calls no API
    return mod


def test_trigger_only_reproduces_replay_k18_on_the_fixture():
    """With the window end moved to the frozen lanes' INCLUSIVE 10:25 bar
    (`entry_end_exclusive="10:30"`), `replay_k18_trigger_only` must reproduce
    `scripts/rvolrank_sleeve.py::replay_k18`'s `reason` and raw exit for every
    name of the vendored 2026-09-11 day, both entry windows — the hard stop
    hangs off the BASE-slipped fill in both (a 5 bps difference moves stop
    bars on ~9% of recorded trades)."""
    from tests.rvol_exec.session_sim import fixture_day
    sleeve = _load_sleeve()
    bars, prep, _ = fixture_day("2026-09-11")               # the vendored day (DAY here is 09-14)
    atr = dict(zip(prep["ticker"], prep["atr14"]))
    compared = 0
    for sym, df in bars.items():
        a = float(atr.get(sym) or 0)
        if a <= 0:
            continue
        for es in ("09:35", "09:45"):
            ref = sleeve.replay_k18(df, a, entry_start=es)
            got = causal.replay_k18_trigger_only(df, a, entry_start=es, entry_end_exclusive="10:30")
            assert (ref is None) == (got is None), (sym, es)
            if ref is None:
                continue
            compared += 1
            assert got["fill_t"] == ref["fill_t"] and got["reason"] == ref["reason"], (sym, es)
            assert got["side"] == ref["side"] and got["stop_dist"] == pytest.approx(ref["stop_dist"])
            # replay_k18 exits eod at the 15:55 bar CLOSE; stops at the same price in both
            # replay_k18's `r` = base_gross / stop_dist rounded to 4 dp; base_gross = raw_exit*(1-SLIP) - raw_fill*(1+SLIP)
            slip = sleeve.SLIP
            raw_fill = got["raw_fill"]
            raw_exit = got["exit_close"]
            base_gross = (raw_exit * (1 - slip) - raw_fill * (1 + slip)) if ref["side"] == "L" \
                else (raw_fill * (1 - slip) - raw_exit * (1 + slip))
            assert round(base_gross / got["stop_dist"], 4) == pytest.approx(ref["r"], abs=1.5e-4), (sym, es, ref["reason"])
            for tier in ("base", "stress_2x", "stress_3x"):
                assert causal.net_r(ref["side"] == "L", raw_fill, raw_exit, got["stop_dist"], tier) == \
                    pytest.approx(ref["r_tiers"][tier], abs=1.5e-4), (sym, es, tier)
    assert compared >= 10


def test_trigger_only_hard_stop_is_the_base_slipped_fill():
    from tests.rvol_exec.session_sim import make_bars
    # long: level 10.51, fill at the bar open 10.60 (guardrail 0.04 -> open beyond limit cancels), so use open 10.52
    b = make_bars((10.0, 10.5, 9.9, 10.4, 100_000), {"09:40": (10.52, 10.9, 10.50, 10.8)})
    tr = causal.replay_k18_trigger_only(b, 2.0)
    assert tr["raw_fill"] == 10.52 and tr["fill_base"] == pytest.approx(10.52 * 1.0005)
    assert tr["hard"] == pytest.approx(round(10.52 * 1.0005 - 0.4, 4))
    # short mirror
    b2 = make_bars((10.4, 10.5, 9.9, 10.0, 100_000), {"09:40": (9.88, 9.90, 9.5, 9.6)})
    tr2 = causal.replay_k18_trigger_only(b2, 2.0)
    assert tr2["side"] == "S" and tr2["raw_fill"] == 9.88
    assert tr2["hard"] == pytest.approx(round(9.88 * 0.9995 + 0.4, 4))


# ── stage-2 bounded retry (finding: contaminated miss must not exclude on one attempt) ──
class _PolyFlaky(_Poly2):
    """Per-ticker exact-bar endpoint that raises the first `fail_first[t]` times."""

    def __init__(self, snap, bars, fail_first: dict[str, int]):
        super().__init__(snap, bars)
        self.fail_first = dict(fail_first)

    def bars_5m(self, t, day):
        self.bar_calls.append(t)
        if self.fail_first.get(t, 0) > 0:
            self.fail_first[t] -= 1
            raise RuntimeError("transient")
        return self.bars.get(t)


def test_stage2_retry_is_bounded_and_rescues_a_transient_miss():
    now = at_et(DAY, 9, 35, 5)
    base = _snap(now, at_et(DAY, 9, 35), now, n=5000, prep_syms=())         # ref min.t 09:35 -> contaminated
    t930 = _ms(at_et(DAY, 9, 30))
    bars = {}
    for i, s in enumerate(("T0", "T1", "T2", "T3", "T4")):
        v = 1e5 * (i + 1)
        base["tickers"].append({"ticker": s, "updated": _ns(now), "day": {"o": 20.0, "h": 21, "l": 19.5, "c": 20.8, "v": v},
                                "min": {"t": _ms(at_et(DAY, 9, 35)), "av": v, "c": 20.8}})
        bars[s] = [{"t": t930, "o": 20.0, "h": 21, "l": 19.5, "c": 20.8, "v": v}]
    prep_rows = [{"ticker": s, "atr14": 1.0, "avgvol14": 2e6, "baseline14": 1e5, "n_baseline": 14} for s in bars]
    poly = _PolyFlaky(base, bars, {"T4": 1, "T3": 5})          # T4 rescued on retry 1; T3 never
    slept = []
    res = snapshot.rank_universe(base, prep_rows, poly, DAY, contaminated=True, stage2=True,
                                 retry_sleep=lambda s: slept.append(s))
    st = res["stage2"]
    assert st["retries"] == 2 and st["missing_after_retry"] == ["T3"]
    assert slept == [15, 15]                                   # STAGE2_RETRY_WAIT_S between contaminated retries
    assert poly.bar_calls.count("T4") == 2 and poly.bar_calls.count("T3") == 3    # 1 + STAGE2_RETRIES, never more
    names = [r["ticker"] for r in res["top20"]]
    assert "T4" in names and "T3" not in names
    assert {"ticker": "T3", "reason": "orb_unavailable"} in res["excluded"]
    # a CLEAN snapshot retries back-to-back (no sleep) and never excludes: the miss falls back to snapshot day.*
    poly2 = _PolyFlaky(base, bars, {"T3": 5})
    slept2 = []
    res2 = snapshot.rank_universe(base, prep_rows, poly2, DAY, contaminated=False, stage2=True,
                                  retry_sleep=lambda s: slept2.append(s))
    assert slept2 == [] and res2["stage2"]["retries"] == 2
    assert [r for r in res2["top20"] if r["ticker"] == "T3"][0]["orb_source"] == "snapshot"


def test_baseline14_is_capped_to_the_shadow_scan_30_day_span():
    # 14 sessions with a 09:30 bar, but the oldest 4 lie beyond 30 calendar days -> not rankable
    from datetime import date, timedelta
    d = date(2026, 9, 10)
    recent = [(d - timedelta(days=i)).isoformat() for i in (0, 1, 2, 3, 4, 7, 8, 9, 10, 11)]     # 10 within 12 days
    old = [(d - timedelta(days=i)).isoformat() for i in (35, 36, 37, 38)]                        # 4 beyond 30 days
    v = {x: 100.0 for x in recent + old}
    b, n = prep.baseline14(v, d.isoformat())
    assert b is None and n == 10
    v2 = dict(v, **{(d - timedelta(days=i)).isoformat(): 100.0 for i in (14, 15, 16, 17)})
    assert prep.baseline14(v2, d.isoformat()) == (100.0, 14)
