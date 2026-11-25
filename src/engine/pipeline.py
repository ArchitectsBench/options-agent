"""Cycle pipeline (Track B). Step 7 scope: regime + per-ticker signal scan +
applicability filter, all OBSERVATION-ONLY (no recommendations — LLM stages
are steps 8-9). Every cycle is logged as structured jsonl so future Track A
generations can use it as a forward-test dataset."""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import yaml

from src.control.state import update_state
from src.indicators.applicability import evaluate_strategies, load_applicability_map
from src.indicators.regime_live import build_regime, format_regime
from src.indicators.stock_signals import LINEAGE, scan_watchlist

ROOT = Path(__file__).resolve().parents[2]
LOG_FILE = ROOT / "logs" / "agent.log"
OBS_DIR = ROOT / "logs" / "observation"

logger = logging.getLogger("trader")
# don't attach the production log file under the test runner — unit tests
# emit through this logger and were polluting agent.log's safety-event
# counts (27 phantom breaker trips as of 2026-07-02)
import sys as _sys
if not logger.handlers and "unittest" not in _sys.modules:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _watchlist(state: dict) -> list[str]:
    if state.get("watchlist_override"):
        return state["watchlist_override"]
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    return cfg["watchlist"]["default"]


def _fmt(v, width=6):
    return f"{'-' if v is None else v:>{width}}"


def _print_signal_table(signals: dict, applicability: dict):
    print(f"\n  {'TICKER':<7}{'last':>9}{'sq5m':>12}{'sq15m':>12}{'sq30m':>12}"
          f"{'EMA 5/15/30':>14}{'MACD':>10}{'VWAP(ATR)':>10}{'vol':>6}{'RS30m':>7}{'OR':>7}")
    print("  " + "-" * 104)
    abbrev = {"bullish": "B", "bearish": "S", "insufficient_data": "?", "unavailable": "-",
              "fired_long": "FIRE^", "fired_short": "FIREv", "on": "ON", "off": "off"}
    for t, s in signals.items():
        if "error" in s:
            print(f"  {t:<7} ERROR: {s['error']}")
            continue
        stack = "/".join(abbrev.get(s["ema_stack"][tf], "?") for tf in ("5m", "15m", "30m"))
        macd = "-" if not s["macd_5m"] else f"{s['macd_5m']['sign'][:3]}/{s['macd_5m']['direction'][:4]}"
        orp = "-" if not s["opening_range"] else s["opening_range"]["position"]
        print(f"  {t:<7}{s['last']:>9}{abbrev.get(s['squeeze']['5m'], '?'):>12}"
              f"{abbrev.get(s['squeeze']['15m'], '?'):>12}{abbrev.get(s['squeeze']['30m'], '?'):>12}"
              f"{stack:>14}{macd:>10}{_fmt(s['vwap_dist_atr'], 10)}{_fmt(s['volume_ratio'])}"
              f"{_fmt(s['rs_vs_spy_30min_pct'], 7)}{orp:>7}")

    # applicability rollup: count of tickers per verdict per strategy
    print(f"\n  Applicability ({len(signals)} tickers): ", end="")
    rollup = {}
    for t, per_strategy in applicability.items():
        for strat, res in per_strategy.items():
            rollup.setdefault(strat, {"viable": 0, "blocked": 0, "unevaluable": 0})
            rollup[strat][res["verdict"]] += 1
    print(" | ".join(f"{s}: {c['viable']}V/{c['blocked']}B/{c['unevaluable']}U" for s, c in rollup.items()))
    print("  (V=viable B=blocked U=unevaluable — U means a required feed (GEX/TICK/IV) has no live source yet)")


def _log_stale_block(ticker, lane, strat, direction, fr):
    """6c: countable record of a suppressed entry (stale signal bar). Written to
    logs/observation/blocked_stale_bars.jsonl so blocks are visible, not silent."""
    import json
    p = OBS_DIR / "blocked_stale_bars.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            "ticker": ticker, "lane": lane, "bar_age_min": fr.get("age_min"),
                            "session": fr.get("session"), "reason": fr.get("reason"),
                            "would_be": {"strategy": strat, "direction": direction}}) + "\n")
    tag = "STALE-BAR BLOCK" if fr.get("reason") == "stale_bar" else "no-fresh-bar"
    print(f"  [{tag}] {ticker} {lane} {strat}/{direction}: bar {fr.get('age_min')}min old, "
          f"session={fr.get('session')} ({fr.get('reason')}) — trade suppressed", flush=True)


def entry_vwap_margin_atr() -> float:
    """Provisional margin (2026-08-06). See _vwap_entry_ok."""
    try:
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
        return float((cfg.get("agent") or {}).get("entry_vwap_margin_atr", 0.25))
    except Exception:  # noqa: BLE001
        return 0.25


def _vwap_entry_ok(ticker, sig, lane, strat, direction) -> bool:
    """RULE-CONSISTENCY GATE (2026-08-06).

    The exit manager closes a long the moment vwap_dist_atr < 0, and a short the
    moment it is > 0 — with no buffer. Entry had no matching requirement, so
    positions were opened ALREADY INSIDE the exit condition: on 2026-08-06,
    10 of 21 entries (longs as far as -2.38 ATR below VWAP, shorts +0.82 above)
    were born invalid and cut on the very next cycle. Result: 18 round trips,
    0 winners, median hold 5 minutes = one cycle, -$1,651 — the system paid the
    spread 18 times without ever giving a thesis room to work.

    This is precisely the Track A Stage 2 requirement ("entry and exit rules are
    unambiguous and non-contradictory") violated in the live engine.

    The gate makes entry obey what exit demands, plus a margin:
        long  requires vwap_dist >= +margin
        short requires vwap_dist <= -margin
    THE EXIT RULE IS UNTOUCHED. The margin is PROVISIONAL and CHOSEN, not
    derived — 0.25 ATR, dated 2026-08-06, so a position is not opened a hair
    from its own invalidation. Blocked entries are logged (never silently
    dropped) so we accumulate evidence on what the gate excludes.
    """
    vd = (sig or {}).get("vwap_dist_atr")
    if vd is None or direction not in ("long", "short"):
        return True                      # unknown VWAP: gate cannot judge
    margin = entry_vwap_margin_atr()
    ok = (vd >= margin) if direction == "long" else (vd <= -margin)
    if not ok:
        evt = {"ts": datetime.now().isoformat(timespec="seconds"),
               "event": "entry_blocked_vwap_consistency",
               "ticker": ticker, "lane": lane, "strategy": strat,
               "direction": direction, "vwap_dist_atr": vd,
               "required_margin_atr": margin,
               "reason": ("entry would open INSIDE the exit condition "
                          "(exit closes long at vwap_dist<0 / short at >0)"),
               "stamp": "UNVALIDATED — PAPER ONLY"}
        try:
            from src.engine.paper_trader import _log_event
            _log_event(evt)
        except Exception:  # noqa: BLE001
            pass
        print(f"  [VWAP-GATE BLOCK] {ticker} {lane} {strat}/{direction}: "
              f"vwap_dist={vd:+.2f} ATR, needs {'>=+' if direction=='long' else '<=-'}"
              f"{margin} — would be born in the exit condition", flush=True)
    return ok


def _freshness_ok(ticker, sig, lane, strat, direction, sess) -> bool:
    """6b: a trade may only fire when the signal bar is fresh for its lane
    (SCALP 3 min / funnel 20 min feed-guard floors). Session-aware — a stale bar
    outside the session (pre-open / weekend / half-day) blocks quietly; a stale
    bar DURING the session is a feed defect (logged as stale_bar).

    R8: the OLDER of the (5m, 30m) frames governs. The 5m primary keeps its
    lane floor; the slower 30m squeeze/EMA frame is ALSO checked (its own
    interval-aware floor) so a ~6h-stale 30m frame can't slip a trade through
    the 5m-only guard."""
    from src.data import freshness
    sig = sig or {}
    fr = freshness.assess(ticker, sig.get("bar_time"), lane, sess=sess)
    if not fr["allow"]:
        _log_stale_block(ticker, lane, strat, direction, fr)
        return False
    fr30 = freshness.assess_slow_frame(ticker, sig.get("bar_time_30m"), "30m", sess=sess)
    if not fr30["allow"]:
        _log_stale_block(ticker, lane, strat, direction, fr30)
        return False
    return True


def _paper_execution_test(signals, applicability, cycle_count, cfg, record,
                          submit_paper_order, has_open_exposure):
    """Sandbox-only paper-EXECUTION test: fire the order path on ONE viable setup
    per cycle to validate fills/exits/sizing end-to-end. NOT an edge claim —
    bypasses the alpha funnel on purpose, tags orders EXECUTION-TEST, capped."""
    from src.engine.options_selector import build_trade
    from src.engine.paper_trader import open_paper_count, today_test_entry_count
    from src.engine.risk import circuit_breaker, gate_new_entry, risk_config
    acfg = cfg.get("agent", {})
    if not acfg.get("paper_test_mode"):
        return
    from src.engine.live_gate import kill_switch_engaged
    ks = kill_switch_engaged()
    if ks:
        print(f"  [PAPER-TEST] kill switch engaged — no orders ({ks})", flush=True)
        return
    if circuit_breaker(risk_config(cfg))["tripped"]:
        print("  [PAPER-TEST] daily-loss breaker tripped — no new entries today", flush=True)
        return
    # SCALP-v2 (2026-09-12): when enabled, the pre-registered candidate's own
    # cap and fill mode apply; when off, nothing here changes.
    from src.engine.paper_trader import scalp_v2_config
    _v2 = scalp_v2_config(cfg)
    if _v2.get("read_failed"):
        # A torn/malformed v2 config must not quietly trade v1 rules: place NOTHING.
        print("  [SCALP-v2] config read FAILED — placing no orders this cycle (defect, not a setting)", flush=True)
        return
    # SCALP-v3 (2026-09-25, docs/scalp_v3_prereg.md): a FILTER LAYER on v2 —
    # SPY/QQQ/IWM, 0DTE single-leg only, 09:35-11:30 ET, volume >= 0.8x, IOC.
    # v2's caps, floor, exits and sizing still apply. Off => v2 byte-identical.
    from src.engine.paper_trader import scalp_v3_config
    _v3 = scalp_v3_config(cfg)
    if _v3.get("read_failed"):
        print("  [SCALP-v3] config read FAILED — placing no orders this cycle (defect, not a setting)", flush=True)
        return
    if _v3["enabled"] and not _v2["enabled"]:
        print("  [SCALP-v3] enabled without SCALP-v2 (its base layer) — placing no orders (defect, not a setting)", flush=True)
        return
    _fill_mode = ((_v3["fill_mode"] if _v3["enabled"] else _v2["fill_mode"])
                  if _v2["enabled"] else "resting_mid")
    if _v3["enabled"]:
        # module-level `datetime` on purpose (session simulator patches it)
        from zoneinfo import ZoneInfo as _Z3
        _n3 = datetime.now(_Z3("America/New_York"))
        _m3 = _n3.hour * 60 + _n3.minute
        _w0, _w1 = _v3["entry_window_min"]
        if not (_w0 <= _m3 < _w1):
            print(f"  [SCALP-v3] outside entry window {_v3['entry_window_et'][0]}-"
                  f"{_v3['entry_window_et'][1]} ET — no new entries", flush=True)
            return
    _cap = _v2["max_per_day"] if _v2["enabled"] else acfg.get("paper_test_max_per_day", 3)
    if _v2["enabled"]:
        # LATE-ENTRY CUTOFF (2026-09-12): v2 fills INSTANTLY. An entry at
        # 12:50-12:55 PT leaves no cycle before the 15:45 ET flatten, so the
        # contracts go home unmanaged overnight. v1 rested at mid and rarely
        # filled that late; 5 of ~65 sessions placed a 12:5x entry. No new v2
        # entries inside the last 15 minutes before flat_time_et.
        # module-level `datetime` on purpose: the session simulator patches
        # src.engine.pipeline.datetime — a local import would escape its clock
        from zoneinfo import ZoneInfo as _Z
        _flat = str(((cfg.get("risk") or {}).get("paper_exits") or {}).get("flat_time_et", "15:45"))
        _fh, _fm = (int(x) for x in _flat.split(":"))
        _now = datetime.now(_Z("America/New_York"))
        if (_now.hour * 60 + _now.minute) >= (_fh * 60 + _fm - 15):
            print("  [SCALP-v2] inside the 15-min pre-flatten cutoff — no new entries", flush=True)
            return
        from src.engine.paper_trader import today_v2_attempt_count
        if today_v2_attempt_count() >= _v2["max_attempts_per_day"]:
            print("  [SCALP-v2] daily IOC ATTEMPT cap reached — done for today", flush=True)
            return
    if today_test_entry_count() >= _cap:
        print("  [PAPER-TEST] daily entry cap reached — done for today", flush=True)
        return
    if _v2["enabled"]:
        from src.engine.paper_trader import max_open_for_lane
        # lane-scoped: funnel positions must not consume SCALP-v2's slots
        if open_paper_count(lane="scalp") >= max_open_for_lane("scalp", cfg):
            print("  [SCALP-v2] concurrent cap reached (scalp lane) — skipping", flush=True)
            return
    elif open_paper_count() >= acfg.get("paper_test_max_open", 3):
        print("  [PAPER-TEST] concurrent cap reached — skipping", flush=True)
        return

    from src.data import freshness
    _sess = freshness.current_session_state()   # once per cycle (6b session-aware)
    # candidate order: applicability-viable setups first, then a liquid fallback
    # (SPY/QQQ/IWM debit spread) so the fill/exit/sizing path gets exercised even
    # when no viable setup builds. All sandbox, tagged EXECUTION-TEST.
    cands = []
    for ticker, verdicts in applicability.items():
        sig = signals.get(ticker)
        if sig is None:
            continue
        for strat, v in verdicts.items():
            if v.get("verdict") == "viable":
                direction = "long" if strat == "orb_breakout" else (
                    "long" if (sig.get("ema_stack") or {}).get("15m") == "bullish" else "short")
                cands.append((ticker, strat, direction, False))
    for fb in ("SPY", "QQQ", "IWM"):
        if fb in signals:
            cands.append((fb, "debit_spread", "long", True))
    if _v3["enabled"]:
        # v3 universe: index ETFs, 0DTE single-leg only; no debit-spread fallback
        cands = [c for c in cands if not c[3] and c[0] in _v3["tickers"]
                 and c[1] in _v3["strategies"]]

    for ticker, strat, direction, is_fb in cands:
        if has_open_exposure(ticker, strat, direction, lane="scalp"):
            continue
        if _v2["enabled"] and _fill_mode == "marketable_day":
            # a cancelled DAY entry frees the setup immediately; without this a
            # persistent signal re-fires it every cycle (2026-09-16)
            from src.engine.paper_trader import recent_entry_expiry
            _cd = recent_entry_expiry(ticker, strat, direction,
                                      _v2.get("reattempt_cooldown_min", 30))
            if _cd:
                print(f"  [SCALP-v2] {_cd} — skipped", flush=True)
                continue
        from src.engine.paper_trader import book_underlying_risk_usd
        _cap = (cfg.get("risk") or {}).get("max_book_risk_per_underlying_usd", 2000)
        if book_underlying_risk_usd(ticker) >= _cap:
            print(f"  [PAPER-TEST] book_underlying_cap [{ticker}]: combined risk "
                  f"${book_underlying_risk_usd(ticker):.0f} >= ${_cap} — skipped")
            continue
        if not _vwap_entry_ok(ticker, signals.get(ticker), "SCALP", strat, direction):
            continue
        if not _freshness_ok(ticker, signals.get(ticker), "SCALP", strat, direction, _sess):
            continue                          # 6b: no trade on a stale signal bar
        if _v3["enabled"]:
            _vr = (signals.get(ticker) or {}).get("volume_ratio")
            if _vr is None or float(_vr) < _v3["min_volume_ratio"]:
                from src.engine.paper_trader import _log_event
                _log_event({"ts": datetime.now().isoformat(timespec="seconds"),
                            "event": "entry_blocked_v3_volume", "ticker": ticker,
                            "strategy": strat, "volume_ratio": _vr,
                            "floor": _v3["min_volume_ratio"],
                            "stamp": "UNVALIDATED — PAPER ONLY"})
                continue
        # z-overlay on EXECUTION-TEST orders only — measured burn-in; the
        # funnel keeps its frozen entry rules (clock-reset discipline)
        trade, _ = build_trade(ticker, strat, direction, signals[ticker]["last"],
                               z_overlay=True)
        if trade is None:
            continue
        if _v2["enabled"] and float(trade.get("net_debit") or 0) < _v2["min_net_debit"]:
            # v2 price floor: sub-$1 contracts pay ~11% of premium to cross the
            # spread vs ~4% above $3 (docs/trade_postmortem_20260822.md §1)
            from src.engine.paper_trader import _log_event
            _log_event({"ts": datetime.now().isoformat(timespec="seconds"),
                        "event": "entry_blocked_v2_price_floor", "ticker": ticker,
                        "strategy": strat, "net_debit": trade.get("net_debit"),
                        "floor": _v2["min_net_debit"], "stamp": "UNVALIDATED — PAPER ONLY"})
            continue
        from src.engine.earnings import earnings_veto
        eveto = earnings_veto(ticker, trade.get("expiration"))
        if eveto:  # the hard gate applies to execution-tests too
            print(f"  [PAPER-TEST] earnings veto {ticker}: {eveto}", flush=True)
            continue
        gate = gate_new_entry(trade, "test", cfg)  # sizing burn-in on test orders too
        if not gate["allowed"]:
            print(f"  [PAPER-TEST] risk gate blocked {ticker} {strat}: {gate['reason']}", flush=True)
            continue
        tag = "EXECUTION-TEST-FALLBACK" if is_fb else "EXECUTION-TEST"
        # FORWARD-ONLY rename (2026-08-08): new ids carry -SCALP-; historical ids are
        # never rewritten. scalp_marker_for() returns the legacy marker before the
        # cutover so replaying an old date still mints the era-correct id.
        from src.engine.paper_trader import scalp_marker_for
        _mk = scalp_marker_for()
        rec = {"rec_id": f"{datetime.now():%Y%m%d}{_mk}{cycle_count}-{ticker}-{strat}",
               "ticker": ticker, "strategy": strat, "trade": trade,
               "quantity": gate["quantity"], "risk": gate.get("sizing"),
               "mode": tag, "cycle": cycle_count,
               "fill_mode": _fill_mode,
               "scalp_version": "v3" if _v3["enabled"] else ("v2" if _v2["enabled"] else "v1"),
               "ts": datetime.now().isoformat(timespec="seconds")}
        if _v3["enabled"]:
            # shadow-only (prereg §3): the faster direction frame, recorded for a
            # future v4 prereg; NEVER used to choose direction here
            _fr = _v3["shadow_direction_frame"]
            rec["shadow_direction"] = {"frame": _fr, "ema_stack": (
                (signals.get(ticker) or {}).get("ema_stack") or {}).get(_fr),
                "traded_direction": direction}
        rec["paper_order"] = submit_paper_order(rec, tag=tag, quantity=gate["quantity"],
                                                fill_mode=rec["fill_mode"])
        # rec-native entry snapshot for the post-mortem engine (2026-07-23):
        # SCALP trades previously had no recommendations.jsonl row; cycle-log
        # assembly is backfill-only, native snapshots from here forward.
        try:
            from src.engine.recommendation import REC_LOG
            from src.engine.entry_features import build_entry_features
            snap_rec = {"rec_id": rec["rec_id"], "ticker": ticker,
                        "generated": rec["ts"], "cycle": cycle_count,
                        "snapshot_source": "scalp_native_v1",
                        "stamp": "UNVALIDATED — PAPER ONLY",
                        "indicator_snapshot": signals.get(ticker),
                        "regime_summary": record.get("regime"),
                        "trade": trade, "quantity": gate["quantity"],
                        "risk": gate.get("sizing"),
                        # instrumentation only (2026-08-03); recorded, never evaluated
                        "entry_features": build_entry_features(
                            ticker, signals, record.get("regime"), trade, rec["ts"]),
                        "local_confidence": None,
                        "validation_chain": {"summary": tag},
                        "expiry_invalidation": None}
            with REC_LOG.open("a") as _f:
                _f.write(json.dumps(snap_rec, default=str) + "\n")
        except Exception as _ex:  # never let telemetry break the trade path
            print(f"  [PAPER-TEST] snapshot write failed: {_ex}", flush=True)
        record.setdefault("paper_execution_tests", []).append(rec)
        print(f"  [PAPER-TEST] {tag}: {ticker} {strat} {direction} -> {rec['paper_order']}", flush=True)
        return  # one execution-test order per cycle
    print("  [PAPER-TEST] no buildable setup this cycle (viable or fallback)", flush=True)


def _load_cfg() -> dict:
    """Per-cycle config load — a seam so tests can pin behavior flags
    (confirm_mode etc.) independent of the operator's live config."""
    return yaml.safe_load((ROOT / "config" / "config.yaml").read_text())


class _CycleDeadline:
    """R9 per-cycle WALL-CLOCK budget (hung-cycle blindness fix). A black-holed
    network / a hanging Claude SDK call could otherwise stretch a cycle to
    ~25-30 min while the watchdog exits sit BEHIND the cycle lock and every
    health check still passes. This is a cooperative deadline: checked at each
    stage boundary (and before every remaining candidate / Claude call) so the
    cycle stops taking on new unbounded work and RETURNS CONTROL rather than
    running away. It does not forcibly interrupt a call already blocked — the
    Tradier failure-breaker bounds the data side, and deadman.sh catches a
    total freeze; see the scheduler-wiring note in the handoff for a hard
    thread-join timeout around run_cycle."""

    def __init__(self, budget_sec: float):
        self.budget_sec = budget_sec
        self.start = time.monotonic()
        self.tripped_flag = False

    def remaining(self) -> float:
        return self.budget_sec - (time.monotonic() - self.start)

    def tripped(self) -> bool:
        if self.remaining() <= 0:
            self.tripped_flag = True
        return self.tripped_flag


def _cycle_budget_sec() -> float:
    """Wall-clock budget for one cycle. Default 240s — comfortably under the
    5-min cycle interval so a hung cycle yields before the next tick, yet ample
    for a healthy cycle's fetch + Claude round-trips. Overridable via
    config agent.cycle_budget_sec."""
    try:
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
        return float((cfg.get("agent") or {}).get("cycle_budget_sec", 240))
    except Exception:
        return 240.0


def _resolve_cycle_mode(cfg) -> str:
    """Per-cycle mode via the live gate (non-strict): a mid-run config flip to
    'live' stays observation with a loud warning — changing modes requires a
    restart through the strict startup gate."""
    from src.engine.live_gate import resolve_mode
    return resolve_mode(cfg, strict=False)["mode"]


def run_cycle(cycle_count: int, state: dict | None = None) -> dict:
    """One full observation-mode cycle. Returns the cycle record."""
    from src.control.state import load_state
    from src.research.promoter import load_validated_strategies
    state = state or load_state()
    watchlist = _watchlist(state)

    # R9: start the per-cycle wall-clock budget. Checked at each stage boundary
    # below so a hang returns control instead of running unbounded.
    deadline = _CycleDeadline(_cycle_budget_sec())

    regime = build_regime()
    print(format_regime(regime), flush=True)

    signals, failed = scan_watchlist(watchlist)
    app_map = load_applicability_map()
    applicability = {
        t: evaluate_strategies(s, regime, app_map) for t, s in signals.items() if "error" not in s
    }
    _print_signal_table(signals, applicability)
    if failed:
        print(f"  Failed tickers (degraded): {failed}")
    print("\n  MODE: OBSERVATION — UNVALIDATED, PAPER ONLY. No recommendations generated.", flush=True)

    cfg = _load_cfg()
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "cycle": cycle_count,
        "mode": _resolve_cycle_mode(cfg),
        "stamp": "UNVALIDATED — PAPER ONLY",
        "validated_strategies": [s.get("id") for s in load_validated_strategies()],
        "signal_lineage": LINEAGE,
        "regime": regime,
        "signals": signals,
        "applicability": applicability,
        "failed_tickers": failed,
    }

    # ── step 8: deterministic candidate generation (replaced Ollama synthesis
    # 2026-06-19 — a weak 8B model added latency + a failure surface for low
    # decision value; ranking is now explicit/auditable. Claude still validates.) ──
    from src.engine.candidates import synthesize_deterministic
    llm_out = synthesize_deterministic(record)
    record["local_llm"] = llm_out          # same key/shape for downstream consumers
    s = llm_out["synthesis"]
    print(f"\n  CANDIDATES (deterministic): {s['cycle_assessment']}")
    for c in s.get("candidates", []):
        print(f"    {c['ticker']} {c['strategy']} conf={c['final_confidence']}: {c['reasoning']}")

    # ── step 9: options selection + Claude validation + recommendation ──
    from src.engine.options_selector import build_trade
    from src.engine.paper_trader import has_open_exposure, poll_fills, submit_paper_order
    from src.engine.recommendation import (build_recommendation, format_recommendation,
                                           log_recommendation, recent_rec_exists)
    from src.llm.claude import check_gate, resolve_validation

    from src.engine.exit_manager import manage_positions
    from src.engine.notify import notify_exit, notify_fill, notify_recommendation, track_health

    # C-12.2: make sure there IS a broker before the cycle relies on one
    try:
        from src.engine.paper_trader import ensure_broker
        _bev = ensure_broker(cfg)
        if _bev:
            record["broker_event"] = _bev
            print(f"  BROKER: {_bev['event']}", flush=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_broker hook failed: %s: %s", type(exc).__name__, exc)
    fills = poll_fills(regime)  # item 1 (2026-07-19): regime_tags on paper_fill records
    if fills:
        record["paper_fills"] = fills
        # DISPATCH ON EVENT TYPE (2026-08-18). This was `paper_exit` vs
        # "everything else is an entry fill" — but poll_fills also returns
        # partial_fill_booked (no 'slippage') and partial_remainder_cancelled
        # (no 'fill_price' either). Reaching those, the else-branch raised
        # KeyError('slippage') and killed the WHOLE CYCLE: no exit management,
        # no candidate evaluation, nothing. It fired five times for real
        # (08-06 09:25, 08-10 07:35 / 09:00 / 09:20) — every one a partial
        # fill, the exact case the partial-fill work exists to handle. Seven
        # daily reports called it "unexercised"; the tracebacks say otherwise.
        # An unknown event must never again be assumed to be an entry fill.
        for f in fills:
            ev = f.get("event")
            if ev == "paper_exit":
                print(f"  Paper EXIT [{f['ticker']}]: {f['close_reason']} P&L ${f['pnl_per_contract']}")
                notify_exit(f)
            elif ev == "paper_fill":
                print(f"  Paper fill [{f['ticker']}]: @ {f['fill_price']} (slip {f['slippage']:+.2f})")
                notify_fill(f)
            elif ev == "partial_fill_booked":
                print(f"  PARTIAL fill [{f['ticker']}]: {f['quantity']}/{f.get('requested')} "
                      f"@ {f['fill_price']} — booked, remainder working")
                notify_fill(f)
            elif ev == "partial_remainder_cancelled":
                print(f"  PARTIAL remainder cancelled [{f['ticker']}]: "
                      f"filled {f['filled']}/{f.get('requested')}, rest cancelled")
            else:
                # Never raise on an unrecognised event — the cycle is worth
                # more than the log line.
                print(f"  Fill event [{f.get('ticker', '?')}]: {ev}")

    # C-9.1: reconcile the book with the broker BEFORE exits are evaluated, so a
    # position the broker no longer holds is flagged (and its close suppressed)
    # in the same cycle, and an adopted quantity is what the close is sized to.
    try:
        from src.engine.paper_trader import apply_drift_in_loop
        drift_events = apply_drift_in_loop(cfg)
    except Exception as exc:  # noqa: BLE001 — reconciliation must never kill the cycle
        logger.warning("reconcile-in-loop failed: %s: %s", type(exc).__name__, exc)
        drift_events = []
    if drift_events:
        record["reconcile_events"] = drift_events
        for e in drift_events:
            print(f"  RECONCILE [{e.get('ticker')}]: {e['event']} — {e.get('reason', '')[:70]}", flush=True)
    exit_events = manage_positions(signals, cfg.get("risk", {}).get("paper_exits"))
    if exit_events:
        record["exit_events"] = exit_events
        for e in exit_events:
            print(f"  EXIT MGR [{e.get('ticker')}]: {e['event']} — {e.get('reason', 'signal expired')}")

    # recommend-and-confirm queue: expire stale asks, execute human-confirmed
    # ones (risk re-gated at execution) — BEFORE any new candidates this cycle
    from src.engine.confirm import process_queue
    conf_events = process_queue(cfg, submit_paper_order, mode=record["mode"])
    if conf_events:
        record["confirmations"] = conf_events
        for e in conf_events:
            print(f"  CONFIRM QUEUE: {e['event']} — {e['rec_id']}"
                  + (f" x{e['quantity']}" if e.get("quantity") else ""), flush=True)

    claude_cfg = cfg.get("llm", {}).get("claude", {})
    threshold = claude_cfg.get("invoke_threshold", 0.75)
    record["recommendations"] = []
    synthesis = (llm_out.get("synthesis") or {})
    # kill switch: NO new orders of any kind while engaged (checked every
    # cycle so it works mid-session); exits/manage_positions are never blocked
    from src.engine.live_gate import kill_switch_engaged
    ks = kill_switch_engaged()
    record["kill_switch"] = ks
    if ks:
        print(f"  KILL SWITCH ENGAGED — no new orders this cycle ({ks}); exits still managed",
              flush=True)
    from src.data import freshness as _freshness
    _sess_funnel = _freshness.current_session_state()   # once per cycle (6b)
    for cand in ([] if ks else synthesis.get("candidates", [])):
        # R9: bail out of the (Claude-invoking) candidate loop the moment the
        # wall-clock budget is spent, rather than issuing more calls that could
        # each hang. Exits/manage_positions already ran above and are untouched.
        if deadline.tripped():
            record["deadline_tripped"] = True
            print("  [CYCLE DEADLINE] wall-clock budget exceeded — skipping remaining "
                  "candidates this cycle (positions still managed)", flush=True)
            break
        ticker, strategy = cand.get("ticker"), cand.get("strategy")
        if ticker not in signals or cand.get("final_confidence", 0) < threshold:
            continue
        verdicts = applicability.get(ticker, {})
        if verdicts.get(strategy, {}).get("verdict") != "viable":
            continue
        sig = signals[ticker]
        # ORB is long-side only (gen-4a); other families take direction from the stack
        direction = "long" if strategy == "orb_breakout" else \
            ("long" if sig["ema_stack"].get("15m") == "bullish" else "short")
        current = {"direction": direction, "spot": sig["last"],
                   "atr": sig.get("atr"), "regime_bias": regime["bias"]}

        # one position per ticker+strategy+direction, period
        meta = {"direction": direction, "spot": sig["last"],
                "local_confidence": cand.get("final_confidence")}

        existing = has_open_exposure(ticker, strategy, direction, lane="funnel") \
            or recent_rec_exists(ticker, strategy, direction)
        if existing:
            note = {"ticker": ticker, "strategy": strategy, "skipped": "position_exists",
                    "skip_detail": {"cause": "position_exists", "detail": existing}, **meta}
            record["recommendations"].append(note)
            print(f"  REC SKIPPED [{ticker}]: position_exists — {existing}")
            continue
        # book-level concentration bound (dated amendment 2026-08-03): lanes are
        # independent, but combined per-name exposure across BOTH stays capped
        from src.engine.paper_trader import book_underlying_risk_usd
        _cap = (cfg.get("risk") or {}).get("max_book_risk_per_underlying_usd", 2000)
        if book_underlying_risk_usd(ticker) >= _cap:
            note = {"ticker": ticker, "strategy": strategy, "skipped": "book_underlying_cap",
                    "skip_detail": {"cause": "book_underlying_cap",
                                    "combined_risk_usd": book_underlying_risk_usd(ticker),
                                    "cap_usd": _cap}, **meta}
            record["recommendations"].append(note)
            print(f"  REC SKIPPED [{ticker}]: book_underlying_cap — combined "
                  f"${book_underlying_risk_usd(ticker):.0f} >= ${_cap}")
            continue

        # cheap gate BEFORE chain calls: active veto / cooldown hold / follow-on
        gated = check_gate(ticker, strategy, current, claude_cfg)
        if gated is not None and gated["status"] in ("veto_active", "held"):
            note = {"ticker": ticker, "strategy": strategy, "skipped": gated["status"],
                    "skip_detail": {"cause": gated["status"], "detail": gated["reason"]}, **meta}
            record["recommendations"].append(note)
            print(f"  REC {'REJECTED' if gated['status'] == 'veto_active' else 'HELD'} "
                  f"[{ticker}]: {gated['reason']}")
            continue

        if not _vwap_entry_ok(ticker, sig, "funnel", strategy, direction):
            continue
        if not _freshness_ok(ticker, sig, "funnel", strategy, direction, _sess_funnel):
            note = {"ticker": ticker, "strategy": strategy, "skipped": "stale_bar",
                    "skip_detail": {"cause": "stale_signal_bar"}, **meta}
            record["recommendations"].append(note)
            continue                          # 6b: no rec on a stale signal bar
        trade, skip_detail = build_trade(ticker, strategy, direction, sig["last"])
        if trade is None:
            note = {"ticker": ticker, "strategy": strategy,
                    "skipped": skip_detail["cause"], "skip_detail": skip_detail, **meta}
            record["recommendations"].append(note)
            print(f"  REC SKIPPED [{ticker}]: {skip_detail['cause']} — {skip_detail['detail']}")
            continue

        # earnings hard veto (deterministic, before Claude): an earnings date
        # inside [today, expiration] — or an unverifiable calendar — blocks
        from src.engine.earnings import earnings_veto
        eveto = earnings_veto(ticker, trade.get("expiration"))
        if eveto:
            note = {"ticker": ticker, "strategy": strategy, "skipped": "earnings_veto",
                    "skip_detail": {"cause": "earnings_veto", "detail": eveto}, **meta}
            record["recommendations"].append(note)
            print(f"  REC SKIPPED [{ticker}]: earnings_veto — {eveto}")
            continue

        # risk kernel gate BEFORE the Claude call: breaker, max open/day caps,
        # position sizing — a risk-blocked candidate must not burn budget
        from src.engine.risk import gate_new_entry
        risk_gate = gate_new_entry(trade, "funnel", cfg)
        if not risk_gate["allowed"]:
            note = {"ticker": ticker, "strategy": strategy, "skipped": "risk_gate",
                    "skip_detail": {"cause": "risk_gate", "detail": risk_gate["reason"]}, **meta}
            record["recommendations"].append(note)
            print(f"  REC SKIPPED [{ticker}]: risk_gate — {risk_gate['reason']}")
            continue

        context = {"regime": {k: v for k, v in regime.items() if k not in ("spy", "qqq")},
                   "ticker": ticker, "signals": sig, "applicability": verdicts,
                   "local_candidate": cand, "proposed_trade": trade,
                   "mode": "observation — UNVALIDATED, PAPER ONLY"}
        claude_result = resolve_validation(context, ticker, strategy, current, claude_cfg)

        if claude_result["status"] == "vetoed":
            record["recommendations"].append({"ticker": ticker, "strategy": strategy,
                                              "vetoed": claude_result["verdict"],
                                              "trade": trade, **meta})
            print(f"  VETOED [{ticker}]: {claude_result['verdict'].get('veto_reason')}")
            continue
        if claude_result["status"] != "approved":
            # conf >= threshold REQUIRES Claude approval — no local-only fallthrough
            note = {"ticker": ticker, "strategy": strategy, "skipped": claude_result["status"],
                    "skip_detail": {"cause": claude_result["status"],
                                    "detail": claude_result["reason"]}, **meta}
            record["recommendations"].append(note)
            print(f"  REC HELD [{ticker}]: {claude_result['reason']}")
            continue

        rec = build_recommendation(ticker, cand, trade, sig, regime,
                                   llm_out.get("perf"), claude_result, cycle_count)
        rec["quantity"] = risk_gate["quantity"]
        rec["risk"] = risk_gate.get("sizing")
        acfg_confirm = cfg.get("agent", {})
        if acfg_confirm.get("confirm_mode"):
            # recommend-and-confirm: queue for the human instead of submitting.
            # Live mode ALWAYS takes this path; in observation it's the
            # opt-in drill (default off — forward-test throughput must not
            # depend on human availability).
            from src.engine.confirm import queue_recommendation
            entry = queue_recommendation(rec, risk_gate["quantity"],
                                         acfg_confirm.get("confirm_expiry_min", 15))
            rec["paper_order"] = {"queued_for_confirmation": True,
                                  "expires_at": entry["expires_at"]}
            print(f"  REC QUEUED FOR CONFIRMATION — `trader confirm {rec['rec_id']}` "
                  f"(expires {entry['expires_at'][11:16]})", flush=True)
        else:
            rec["paper_order"] = submit_paper_order(rec, quantity=risk_gate["quantity"])
        log_recommendation(rec)
        record["recommendations"].append(rec)
        print(format_recommendation(rec))
        notify_recommendation(rec)  # only approved recs reach this point
        if claude_result.get("budget_used"):
            print(f"    Claude budget: {claude_result['budget_used']} calls today")

    # ── paper-EXECUTION test mode (sandbox only; NOT an edge claim) ──
    # Deliberately exercise the order/fill/exit pipeline on a structurally-valid
    # (applicability=viable) setup, bypassing the strict alpha funnel. Tagged
    # EXECUTION-TEST, capped. Live trading is unaffected (separate gate).
    # R9: skip the extra order-path work once the cycle budget is spent.
    if deadline.tripped():
        record["deadline_tripped"] = True
        print("  [CYCLE DEADLINE] budget exceeded — skipping execution-test this cycle",
              flush=True)
    else:
        _paper_execution_test(signals, applicability, cycle_count, cfg, record,
                              submit_paper_order, has_open_exposure)

    OBS_DIR.mkdir(parents=True, exist_ok=True)
    obs_file = OBS_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    with open(obs_file, "a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    # 5-minute bar log for the cockpit TRADE page (2026-09-14): the bars these
    # indicators came from. Logging only; append_cycle_bars never raises.
    from src.engine.bars_log import append_cycle_bars
    from src.indicators.stock_signals import LAST_BARS
    append_cycle_bars(LAST_BARS, cycle_count, OBS_DIR)

    # health tracking: persistent degradation only. Fix (2026-06-15):
    # (a) exclude permanently-unavailable feeds (TICK/put_call) from the
    #     alert text — they're known-missing by design, not "degraded";
    # (b) grace-period the first ~15 min of the session, where today-bar
    #     sparsity (SPY_today/QQQ_today/GEX) is expected and self-recovers.
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    from src.indicators.regime_live import UNAVAILABLE_FEEDS
    mh = cfg["agent"]["market_hours"]
    now_mkt = _dt.now(_ZI(mh["tz"]))
    sh, sm = map(int, mh["start"].split(":"))
    mins_into_session = (now_mkt.hour * 60 + now_mkt.minute) - (sh * 60 + sm)
    in_open_grace = 0 <= mins_into_session < 15

    problems = []
    real_missing = [m for m in regime.get("missing", []) if m not in UNAVAILABLE_FEEDS]
    if regime.get("degraded") and real_missing and not in_open_grace:
        problems.append(f"degraded feeds: {real_missing}")
    if len(failed) >= 3:
        problems.append(f"{len(failed)} tickers failed: {failed[:5]}")
    if llm_out.get("error"):
        problems.append(f"local LLM: {llm_out['error']}")
    track_health(bool(problems), "; ".join(problems))

    logger.info("cycle #%d bias=%s trend=%s vix=%s tickers=%d failed=%d -> %s",
                cycle_count, regime["bias"], regime["trend"], regime["vix"],
                len(signals), len(failed), obs_file.name)
    update_state(
        cycle_count=cycle_count,
        last_cycle=datetime.now().isoformat(timespec="seconds"),
        last_regime={k: v for k, v in regime.items() if k not in ("spy", "qqq")} | {
            "spy_vs_vwap": regime["spy"]["vs_vwap"] if regime["spy"] else None,
            "qqq_vs_vwap": regime["qqq"]["vs_vwap"] if regime["qqq"] else None,
        },
    )
    return record
