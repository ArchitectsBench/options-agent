"""Exit manager — deterministic position management for paper positions.

Runs every cycle. Exit rules, in priority order:
  1. time_flat     — day-trading mandate: flat by flat_time_et (15:45 ET)
  2. invalidation  — close beyond session VWAP against the position
                     (the level every recommendation states)
  3. stop_loss     — position value down stop_loss_pct of the debit paid
  4. profit_target — captured profit_target_pct of max profit

Marks-to-market via live Tradier option quotes (production token, data
only). Close orders go through paper_trader.submit_close_order — sandbox
only. No quotes / no positions => no-op; never breaks the cycle.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from src.data.tradier import get_client
from src.engine.paper_trader import cancel_stale_entries, load_positions, submit_close_order

logger = logging.getLogger("trader")

DEFAULT_CFG = {
    "profit_target_pct": 0.50,
    "stop_loss_pct": 0.50,
    "flat_time_et": "15:45",
    # C-9.3 (2026-09-25, operator-approved): a position that EXPIRES TODAY is
    # closed from this time, ahead of the general flatten. On 09-23 an outage at
    # the 15:45 flatten let an NVDA spread expire between its strikes and
    # exercise into 400 shares the options-only engine cannot see. 15:30 buys
    # three extra retry cycles — it is not immunity: a dead connection at 15:30
    # is as dead as at 15:45.
    "same_day_expiry_flat_et": "15:30",
    "entry_timeout_min": 30,
    # single-leg longs have no max_profit, so the spread target never armed
    # on them (gap visible live 2026-07-06: a 0DTE call ran +62% with no
    # profit-taking). Target = gain as a fraction of the debit paid.
    "single_leg_target_gain_pct": 0.50,
    # quote-outage flatten (2026-07-02): after this many consecutive
    # unpriceable cycles, stop flying blind — protective close at 75% of the
    # last known mark. 0 disables. Alerting fires at streak 2 regardless.
    "quote_outage_flatten_cycles": 4,
}
QUOTE_OUTAGE_HAIRCUT = 0.75  # accept up to 25% worse than the last known mark

ET = ZoneInfo("America/New_York")


def position_value(position: dict, quotes: dict[str, dict]) -> float | None:
    """Net value to close: sum of leg mids signed by original action."""
    total = 0.0
    for leg in position["legs"]:
        q = quotes.get(leg["symbol"])
        if q is None or q.get("bid") is None or q.get("ask") is None:
            return None
        mid = (q["bid"] + q["ask"]) / 2
        total += mid if leg["action"] == "buy_to_open" else -mid
    return round(total, 3)


def evaluate_exit(position: dict, value: float | None, signals: dict | None,
                  now_et: datetime, cfg: dict) -> str | None:
    """Pure rule check. Returns the exit reason or None to keep holding."""
    flat_h, flat_m = map(int, cfg["flat_time_et"].split(":"))
    if (now_et.hour, now_et.minute) >= (flat_h, flat_m):
        return "time_flat (day-trading mandate)"
    exp_t = cfg.get("same_day_expiry_flat_et")
    if exp_t and str(position.get("expiration") or "") == now_et.strftime("%Y-%m-%d"):
        eh, em = map(int, str(exp_t).split(":"))
        if (now_et.hour, now_et.minute) >= (eh, em):
            return "expiry_flat (expires today; closed ahead of the 15:45 flatten)"

    vwap_dist = (signals or {}).get("vwap_dist_atr")
    if vwap_dist is not None and position.get("direction") in ("long", "short"):
        if position["direction"] == "short" and vwap_dist > 0:
            return f"invalidation (reclaimed VWAP, +{vwap_dist} ATR)"
        if position["direction"] == "long" and vwap_dist < 0:
            return f"invalidation (lost VWAP, {vwap_dist} ATR)"

    if value is not None:
        pnl = value - position["entry_price"]
        if pnl <= -cfg["stop_loss_pct"] * position["entry_price"]:
            return f"stop_loss (value {value} vs entry {position['entry_price']})"
        if position.get("max_profit") and pnl >= cfg["profit_target_pct"] * position["max_profit"]:
            return f"profit_target (value {value} vs entry {position['entry_price']})"
        if position.get("max_profit") is None and \
                pnl >= cfg.get("single_leg_target_gain_pct", 0.50) * position["entry_price"]:
            return f"profit_target (single-leg +{cfg.get('single_leg_target_gain_pct', 0.50):.0%}: " \
                   f"value {value} vs entry {position['entry_price']})"
    return None


def manage_positions(signals: dict[str, dict], cfg: dict | None = None) -> list[dict]:
    """Called once per cycle. Returns a list of exit/cancel events."""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    # marketable_day (2026-09-16): the short cancel IS the fill mode — a DAY
    # limit at the natural that is not filled within entry_timeout_min is the
    # "or cancel" half of IOC, done by us because IBKR refuses IOC on combos.
    # v1's resting entries keep the 30-min default.
    from src.engine.paper_trader import scalp_v2_config
    _v2 = scalp_v2_config()
    _mode_to = ({"marketable_day": int(_v2.get("entry_timeout_min", 5))}
                if _v2.get("enabled") and _v2.get("fill_mode") == "marketable_day" else None)
    events = [{"event": "entry_canceled", **e}
              for e in cancel_stale_entries(cfg["entry_timeout_min"], mode_timeouts=_mode_to)]

    all_positions = load_positions()
    positions = [p for p in all_positions if not p.get("closing")]
    closing = [p for p in all_positions if p.get("closing")]
    if not all_positions:
        return events
    client = get_client()
    if not client.available():
        return events

    leg_symbols = sorted({leg["symbol"] for p in all_positions for leg in p["legs"]})
    quotes = client.quotes(leg_symbols) or {}
    now_et = datetime.now(ET)

    # weekend fix #4: chase unfilled close orders — flat is the mandate
    from src.engine.paper_trader import escalate_stale_close
    for position in closing:
        esc = escalate_stale_close(position, quotes, now_et)
        if esc:
            events.append(esc)

    for position in positions:
        if position.get("broker_lost"):
            # L20 circuit breaker tripped: the broker no longer holds this
            # position; retrying closes is reject spam. Awaits reconciliation
            # (scripts/reconcile_positions.py); ops position_reconcile alarms.
            continue
        value = position_value(position, quotes)
        from src.engine.paper_trader import update_position_marks, update_quote_streak
        if value is not None:
            update_position_marks(position["rec_id"], value)  # MFE/MAE recording only
            update_quote_streak(position["rec_id"], missing=False)
        else:
            # Quote outage: stop_loss/profit_target are NOT evaluable this cycle
            # (time_flat and VWAP invalidation below still are). This used to be
            # silent — the position's P&L stops were simply off. Now it's logged
            # every cycle and alerts once per outage episode at 2 misses (~10 min).
            streak = update_quote_streak(position["rec_id"], missing=True)
            logger.warning("no quotes for %s (%s) — P&L stops NOT evaluable this cycle (streak %d)",
                           position["ticker"], position["rec_id"], streak)
            if streak == 2:
                try:
                    from src.engine.notify import send
                    send("Quote outage on open position",
                         f"{position['ticker']} unpriceable {streak} cycles — stop/target "
                         "disabled; VWAP + time exits still armed", priority="high", tags="warning")
                except Exception:
                    pass
            # protective flatten: a blind position is worse than a bad fill.
            # Limit anchors to the last successfully priced mark (haircut) —
            # if submission fails (broker down too), it retries every cycle.
            flatten_after = cfg.get("quote_outage_flatten_cycles", 4)
            if flatten_after and streak >= flatten_after:
                last_v = position.get("last_value")
                anchor = last_v if last_v is not None else position["entry_price"] * 0.5
                limit = max(0.05, round(anchor * QUOTE_OUTAGE_HAIRCUT, 2))
                reason = (f"quote_outage_flatten (unpriceable {streak} cycles; "
                          f"protective limit {limit} from last mark {last_v})")
                result = submit_close_order(position, limit, reason)
                events.append({"event": "exit_submitted", "rec_id": position["rec_id"],
                               "ticker": position["ticker"], "reason": reason,
                               "mark_value": None, "entry_price": position["entry_price"],
                               "submitted": bool(result)})
                logger.warning("QUOTE-OUTAGE FLATTEN [%s]: %s -> submitted=%s",
                               position["ticker"], reason, bool(result))
                try:
                    from src.engine.notify import send
                    send("QUOTE-OUTAGE FLATTEN", f"{position['ticker']} unpriceable "
                         f"{streak} cycles — protective close @ {limit} submitted="
                         f"{bool(result)}", priority="high", tags="octagonal_sign")
                except Exception:
                    pass
                continue
        reason = evaluate_exit(position, value, signals.get(position["ticker"]), now_et, cfg)
        if reason is None:
            continue
        limit = value if value is not None else max(0.01, position["entry_price"] * 0.5)
        result = submit_close_order(position, limit, reason)
        events.append({
            "event": "exit_submitted",
            "rec_id": position["rec_id"],
            "ticker": position["ticker"],
            "reason": reason,
            "mark_value": value,
            "entry_price": position["entry_price"],
            "submitted": bool(result),
        })
        logger.info("exit submitted for %s: %s (mark %.2f vs entry %.2f)",
                    position["ticker"], reason, value or -1, position["entry_price"])
    return events
