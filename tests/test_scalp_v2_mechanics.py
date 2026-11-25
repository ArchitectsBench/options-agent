"""SCALP-v2 mechanics telemetry (scripts/scalp_v2_mechanics.py, 2026-09-14).

Pins what the standing v1->v2 assessment must get right on a fixture ledger:
fill rate counts unfilled IOCs in the denominator, the attempt cap's binding
time is recorded, per-ticker concentration is visible, spreads paid over the
at-fill mid are derived from the fill row, exits are classified by reason,
truth is scored by the injected gate scorer with pending enrichment COUNTED,
and v1-mode fills never leak in. Also: ops_checks must actually return the v2
tracker it computes (it computed and dropped it until 09-14).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scalp_v2_mechanics as m  # noqa: E402

CRIT = {"status": "FROZEN", "window_start": "2026-09-12T22:55:27-07:00",
        "scope": {"ledger": "fills.jsonl", "rec_id_tag": "-SCALP-", "fill_mode": "marketable_ioc"},
        "rules_frozen": {"max_attempts_per_day": 3}}


def _unfilled(ts, tk):
    return {"ts": ts, "event": "entry_ioc_unfilled", "rec_id": f"20260914-SCALP-1-{tk}-0dte_directional",
            "ticker": tk, "fill_mode": "marketable_ioc"}


def _fill(ts, rec, mode="marketable_ioc", cap_vs_mid=-0.115, spread=0.07):
    return {"ts": ts, "event": "paper_fill", "rec_id": rec, "ticker": rec.split("-")[3],
            "fill_mode": mode, "capture_vs_mid": cap_vs_mid, "spread_at_fill": spread}


def _exit(ts, rec, pnl, reason):
    return {"ts": ts, "event": "paper_exit", "rec_id": rec, "ticker": rec.split("-")[3],
            "pnl_total": pnl, "exit_reason": reason, "round_trip": {"entry_filled_at": ts}}


def _ledger(tmp_path, rows):
    p = tmp_path / "fills.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_fill_rate_concentration_cap_and_spread(tmp_path):
    p = _ledger(tmp_path, [
        _unfilled("2026-09-14T07:00:00", "TSLA"),
        _unfilled("2026-09-14T07:05:00", "TSLA"),
        _fill("2026-09-14T07:10:00", "20260914-SCALP-2-MSFT-0dte_directional"),   # 3rd attempt = cap
        _unfilled("2026-09-14T07:15:00", "NFLX"),
    ])
    r = m.mechanics(p, CRIT, lambda e: (None, "pending_enrichment"), today="2026-09-14")
    d = r["days"][0]
    assert (d["attempts"], d["fills"], d["fill_rate"]) == (4, 1, 0.25)
    assert (d["top_ticker"], d["top_ticker_attempts"]) == ("TSLA", 2)
    assert d["cap_bound_at"] == "07:10"
    assert d["spreads_over_mid_mean"] == 1.64          # -(-0.115)/0.07
    assert r["cumulative"]["attempts_by_ticker"]["TSLA"] == 2


def test_truth_scored_by_injected_scorer_and_pending_counted(tmp_path):
    rec_a, rec_b = "20260914-SCALP-2-MSFT-0dte_directional", "20260914-SCALP-3-AMD-debit_spread"
    p = _ledger(tmp_path, [
        _fill("2026-09-14T07:10:00", rec_a), _exit("2026-09-14T07:40:00", rec_a, 770.0, "target_50"),
        _fill("2026-09-14T08:10:00", rec_b), _exit("2026-09-14T08:40:00", rec_b, -90.0, "vwap_invalidation"),
    ])
    truth = {rec_a: (610.0, "ok"), rec_b: (None, "pending_enrichment")}
    r = m.mechanics(p, CRIT, lambda e: truth[e["rec_id"]], today="2026-09-15")
    c = r["cumulative"]
    assert c["round_trips"] == 2 and c["booked_total"] == 680.0
    assert (c["truth_scored"], c["truth_total"], c["truth_win_rate"]) == (1, 610.0, 1.0)
    assert c["pending_enrichment"] == 1
    assert c["stale_pending_enrichment"] == 1            # 09-14 trade still unscored on 09-15
    assert c["vwap_invalidation_exit_share"] == 0.5
    assert "pred -20.8" in m.summary_line(r) and "pending" not in m.summary_line(r).split("|")[2]


def test_v1_mode_fills_and_pre_window_rows_never_count(tmp_path):
    p = _ledger(tmp_path, [
        _fill("2026-09-11T07:10:00", "20260911-SCALP-9-SPY-debit_spread"),          # before window
        _fill("2026-09-14T07:10:00", "20260914-SCALP-8-SPY-debit_spread", mode="resting_mid"),
        _exit("2026-09-14T07:40:00", "20260914-SCALP-8-SPY-debit_spread", 50.0, "target_50"),
        _fill("2026-09-14T07:12:00", "20260914-FUNNEL-1-QQQ-debit_spread"),          # not the lane
    ])
    r = m.mechanics(p, CRIT, lambda e: (1.0, "ok"), today="2026-09-14")
    assert r["cumulative"]["attempts"] == 0 and r["cumulative"]["round_trips"] == 0
    assert r["cumulative"]["fill_rate"] is None
    assert "no v2 activity" in m.summary_line(r)


def test_broker_rejects_are_split_out_and_the_frozen_rate_is_unchanged(tmp_path):
    # 2026-09-15: IBKR refused every v2 combo IOC (error 201). The recorded
    # (frozen) rate keeps them; the disclosure rate excludes them. A row logged
    # before the broker_reject field existed is back-filled from IB's own log.
    new = _unfilled("2026-09-14T07:00:00", "TSLA")
    new["broker_reject"] = {"code": 201, "text": "Inter exchange combo order should be DAY/GTC order."}
    old = _unfilled("2026-09-14T07:02:00", "TSLA")
    old["order_id"] = 1663
    p = _ledger(tmp_path, [new, old, _unfilled("2026-09-14T07:05:00", "AMD"),
                           _fill("2026-09-14T07:10:00", "20260914-SCALP-2-MSFT-0dte_directional")])
    r = m.mechanics(p, CRIT, lambda e: (None, "pending_enrichment"), today="2026-09-14",
                    reject_ids={1663})
    c, d = r["cumulative"], r["days"][0]
    assert (c["attempts"], c["fills"], c["fill_rate"]) == (4, 1, 0.25)      # frozen, unchanged
    assert (c["broker_rejects"], c["market_attempts"], c["fill_rate_market"]) == (2, 2, 0.5)
    assert (d["broker_rejects"], d["fill_rate_market"]) == (2, 0.5)
    line = m.summary_line(r)
    assert "2 broker rejects" in line and "50% (1/2)" in line
    assert "reached the market" in m.render_md(r)


def test_reject_ids_come_from_ib_refusals_only(tmp_path):
    import scalp_forward_check as sfc
    log = tmp_path / "agent_stdout.log"
    log.write_text("Error 201, reqId 1663: Order rejected - reason:Inter exchange combo order "
                   "should be DAY/GTC order.\n"
                   "Error 202, reqId 1700: Order Canceled - reason:\n")    # a cancel is not a refusal
    assert sfc.broker_reject_order_ids(log) == {1663}
    assert sfc.broker_reject_order_ids(tmp_path / "missing.log") == set()
    assert sfc.is_broker_reject({"order_id": "1663"}, {1663})
    assert not sfc.is_broker_reject({"order_id": None}, {1663})


def test_ops_checks_returns_the_v2_tracker(monkeypatch):
    import ops_checks as oc
    import scalp_forward_check as sfc

    def fake_eval(v2=False):
        return {"window_start": "w2" if v2 else "w1", "n_trades": 1 if v2 else 330,
                "total_pnl": 0, "excluded": {}, "all_pass": False,
                "checks": {"ioc_fill_rate": {"value": "6% (1/18)", "pass": False}} if v2 else {}}
    monkeypatch.setattr(sfc, "evaluate", fake_eval)
    monkeypatch.setattr(oc, "_v2_mechanics_summary", lambda: {"summary": "stub"})
    out = oc.scalp_forward()
    assert out["n_trades"] == 330                                   # v1 unchanged
    assert out["v2"]["n_trades"] == 1
    assert out["v2"]["checks"]["ioc_fill_rate"]["value"] == "6% (1/18)"
    assert out["v2_mechanics"] == {"summary": "stub"}


# --- the "today" field must not report a stale day as today ---------------
# Queue item 2026-11-07 weekend: ops_checks `v2_mechanics.today` was days[-1],
# the latest day the ledger MOVED, so it read 2026-10-08 every night from
# 10-08 to 11-04. summary_line had the same bug in a worse place: it printed
# "today 1 att / 1 fill" with no date into the nightly ntfy and the digest.

DAY_OLD = {"date": "2026-10-08", "attempts": 1, "fills": 1, "fill_rate": 1.0,
           "cap_bound_at": None, "top_ticker": "IWM", "top_ticker_attempts": 1}
DAY_NOW = {"date": "2026-11-04", "attempts": 2, "fills": 0, "fill_rate": 0.0,
           "cap_bound_at": None, "top_ticker": "SPY", "top_ticker_attempts": 2}


def test_pick_day_separates_today_from_latest_active():
    import ops_checks as oc
    # today inactive: `today` is None, the stale row survives under its own name
    assert oc._pick_day([DAY_OLD], "2026-11-04") == (None, DAY_OLD)
    # today active: both resolve, and `today` is today's row
    assert oc._pick_day([DAY_OLD, DAY_NOW], "2026-11-04") == (DAY_NOW, DAY_NOW)
    # empty ledger: neither invented
    assert oc._pick_day([], "2026-11-04") == (None, None)


def _mech(days, as_of):
    return {"as_of": as_of, "days": days, "candidate": "v3",
            "predictions": m.PREDICTIONS,
            "cumulative": {"sessions": len(days), "attempts": 1, "fills": 1,
                           "fill_rate": 1.0, "fill_rate_floor": 0.6,
                           "broker_rejects": 0, "fill_rate_market": 1.0,
                           "market_attempts": 1, "truth_scored": 0,
                           "pending_enrichment": 1, "booked_total": 0.0,
                           "vwap_invalidation_exit_share": None}}


def test_summary_line_names_the_date_when_today_had_no_activity():
    line = m.summary_line(_mech([DAY_OLD], "2026-11-04"))
    assert "no activity today (2026-11-04)" in line
    assert "last active 2026-10-08 1 att / 1 fill" in line
    assert "today 1 att" not in line          # the exact misleading phrasing

def test_summary_line_still_says_today_on_an_active_day():
    line = m.summary_line(_mech([DAY_OLD, DAY_NOW], "2026-11-04"))
    assert "today 2 att / 0 fill" in line
    assert "no activity today" not in line

def test_summary_line_handles_an_empty_ledger_and_a_missing_as_of():
    assert "no v3 activity" in m.summary_line(_mech([], "2026-11-04"))
    # as_of absent (older on-disk json): fall back to the legacy phrasing
    legacy = _mech([DAY_OLD], "2026-11-04"); legacy.pop("as_of")
    assert "today 1 att / 1 fill" in m.summary_line(legacy)
