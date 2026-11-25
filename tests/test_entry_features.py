"""Entry-feature instrumentation (2026-08-03) — recorded, never evaluated.

Confirms the helper is fail-open, computes the documented definitions, and
handles both the SCALP (full {ticker: snap} map) and funnel (per-ticker snap)
signal shapes.
"""
from src.engine.entry_features import build_entry_features, SCHEMA_VERSION

_SIG = {"last": 310.0, "atr": 5.0,
        "opening_range": {"high": 312.0, "low": 302.0, "position": "inside"}}
_REG = {"tick_proxy": {"net_adv_pct": 44.0, "source": "breadth_proxy"}, "breadth": 0.4}
_TRADE = {"legs": [{"symbol": "SPY260710C00740000", "action": "buy_to_open"},
                   {"symbol": "SPY260710C00745000", "action": "sell_to_open"}]}
_TS = "2026-08-03T07:00:00"


def test_full_map_shape_scalp():
    ef = build_entry_features("SPY", {"SPY": _SIG}, _REG, _TRADE, _TS)
    assert ef["schema_version"] == SCHEMA_VERSION
    assert ef["time_from_open_min"] == 30.0          # 07:00 - 06:30
    assert ef["opening_range"]["pos_pct"] == 0.8     # (310-302)/(312-302)
    assert ef["spy_breadth_proxy"]["net_adv_pct"] == 44.0
    assert ef["spread_width_strikes"] == 5.0         # 745 - 740


def test_per_ticker_shape_funnel():
    # funnel passes an already-per-ticker snapshot, not a {ticker: snap} map
    ef = build_entry_features("SPY", _SIG, _REG, _TRADE, _TS)
    assert ef["opening_range"]["pos_pct"] == 0.8
    assert ef["time_from_open_min"] == 30.0


def test_fail_open_on_garbage():
    # never raises, whatever the inputs
    ef = build_entry_features("X", None, None, None, "not-a-timestamp")
    assert ef["schema_version"] == SCHEMA_VERSION
    assert ef["time_from_open_min"] is None
    assert ef["opening_range"] is None
    assert ef["spread_width_strikes"] is None


def test_single_leg_has_no_strike_width():
    ef = build_entry_features("SPY", {"SPY": _SIG}, _REG,
                              {"legs": [{"symbol": "SPY260710C00740000", "action": "buy_to_open"}]}, _TS)
    assert ef["spread_width_strikes"] is None        # single leg -> no width


def test_or_breakout_pos_pct_exceeds_one():
    sig = dict(_SIG, last=315.0)                       # above OR high 312
    ef = build_entry_features("SPY", {"SPY": sig}, _REG, _TRADE, _TS)
    assert ef["opening_range"]["pos_pct"] == 1.3      # (315-302)/10
