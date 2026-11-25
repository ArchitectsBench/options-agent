"""Risk watchdog — the fast reflex loop (2026-07-03).

The 5-minute cycle is the strategy brain (signals, entries, VWAP/time exits —
they need bars). This daemon thread is the reflex arc for the two exits that
suffer from cycle latency: stop_loss and profit_target. It watches the
position book, holds a Tradier HTTP streaming session (production DATA token;
orders still go only through the sandbox paper path) subscribed to exactly
the open positions' option legs, and on every quote tick recomputes position
value — a breach submits the close within ~1s instead of up to 300s.

Discipline:
  - Idles (no stream) whenever the book is flat or every position is closing.
  - Restarts the stream when the watched symbol set changes.
  - Shares the scheduler's job lock for anything that touches the order book,
    so it can never race a cycle.
  - FAILS LOUD, DEGRADES SAFE: any stream failure logs + notifies once per
    episode and falls back to reconnect-with-backoff; the 5-minute loop
    remains the guaranteed backstop the whole time. The watchdog can only
    ever make exits faster, never disable them.
  - Kill switch does NOT block watchdog closes (exits are never blocked).

Config: agent.watchdog_enabled (default true); thresholds reuse
risk.paper_exits (stop_loss_pct / profit_target_pct).
"""

import json
import logging
import os
import threading
import time
from datetime import datetime

import requests

from src.engine.paper_trader import load_positions, submit_close_order, update_position_marks

logger = logging.getLogger("trader")

SESSION_URL = "https://api.tradier.com/v1/markets/events/session"
BOOK_POLL_S = 2.0          # how often the book / symbol set is re-checked
IDLE_POLL_S = 15.0         # poll cadence while the book is flat
MARKS_THROTTLE_S = 30.0    # min seconds between mark persists per position
BACKOFFS = (5, 15, 60)     # stream reconnect backoff, then repeat 60s


def position_value_from(position: dict, quotes: dict[str, tuple]) -> float | None:
    """Net close-out value from streamed (bid, ask) pairs; None if any leg lacks a quote."""
    total = 0.0
    for leg in position["legs"]:
        q = quotes.get(leg["symbol"])
        if not q or q[0] is None or q[1] is None:
            return None
        mid = (q[0] + q[1]) / 2
        total += mid if leg["action"] == "buy_to_open" else -mid
    return round(total, 3)


def evaluate_tick(position: dict, value: float, cfg: dict) -> str | None:
    """Value-based exits only — the ones latency hurts. VWAP/time stay with
    the 5-minute loop. Reason strings match exit_reason_code buckets."""
    pnl = value - position["entry_price"]
    if pnl <= -cfg.get("stop_loss_pct", 0.50) * position["entry_price"]:
        return f"stop_loss (watchdog: value {value} vs entry {position['entry_price']})"
    if position.get("max_profit") and \
            pnl >= cfg.get("profit_target_pct", 0.50) * position["max_profit"]:
        return f"profit_target (watchdog: value {value} vs entry {position['entry_price']})"
    if position.get("max_profit") is None and \
            pnl >= cfg.get("single_leg_target_gain_pct", 0.50) * position["entry_price"]:
        return f"profit_target (watchdog single-leg: value {value} vs entry {position['entry_price']})"
    return None


class RiskWatchdog(threading.Thread):
    def __init__(self, lock: threading.Lock, cfg: dict):
        super().__init__(name="risk-watchdog", daemon=True)
        self.lock = lock
        self.exit_cfg = (cfg.get("risk") or {}).get("paper_exits") or {}
        self.quotes: dict[str, tuple] = {}
        self.watched: set[str] = set()
        self._stream_resp = None
        self._failed_notified = False
        self._marks_at: dict[str, float] = {}
        self._retry_at: dict[str, float] = {}
        self._closed_by_me: set[str] = set()

    # ── book awareness ──
    def _open_positions(self) -> list[dict]:
        try:
            out = []
            for p in load_positions():
                if p.get("closing"):
                    continue
                if p["rec_id"] in self._closed_by_me:
                    if p.get("close_reject_count", 0) > 0:
                        # a rejected close re-armed it — un-forget so the
                        # fast-retry path can see it again
                        self._closed_by_me.discard(p["rec_id"])
                    else:
                        continue
                out.append(p)
            self._book_read_fails = 0
            return out
        except Exception:  # noqa: BLE001
            # FAIL LOUD (2026-09-12). This returned [] silently, so a failing
            # book read made the watchdog report "nothing to watch" and quietly
            # stop being a reflex arc at all — on a protective layer whose own
            # docstring promises "FAILS LOUD, DEGRADES SAFE". Degrading safe is
            # right (the 5-minute loop remains the backstop); doing it in
            # silence is not.
            self._book_read_fails = getattr(self, "_book_read_fails", 0) + 1
            logger.exception("watchdog: book read FAILED (%d consecutive) — "
                             "treating as no positions this tick; the 5-minute "
                             "loop remains the backstop",
                             self._book_read_fails)
            if self._book_read_fails in (1, 10, 100):
                try:
                    from src.engine.notify import send
                    send("OptionsAgent watchdog degraded — UNVALIDATED — PAPER ONLY",
                         f"Position-book read failed {self._book_read_fails}x in a row. "
                         f"The fast stop/target reflex is NOT running; 5-minute exits "
                         f"still apply.", tags="warning")
                except Exception:  # noqa: BLE001
                    pass
            return []

    def _symbols(self, positions) -> set[str]:
        return {leg["symbol"] for p in positions for leg in p["legs"]}

    # ── the reflex ──
    def _check_positions(self):
        for p in self._open_positions():
            value = position_value_from(p, self.quotes)
            if value is None:
                continue
            now = time.monotonic()
            if now - self._marks_at.get(p["rec_id"], 0) > MARKS_THROTTLE_S:
                self._marks_at[p["rec_id"]] = now
                with self.lock:
                    update_position_marks(p["rec_id"], value)
            reason = evaluate_tick(p, value, self.exit_cfg)
            # fast retry (2026-07-14, user go): a close the broker REJECTED
            # used to wait for the next 5-min cycle to retry — fatal near the
            # bell (Monday's rejected flatten stranded the position into the
            # sandbox's EOD purge, L20). If an exit was already decided
            # (close_reason on the position) and the order came back rejected,
            # retry here at watchdog speed. Decision rules unchanged — this is
            # retry latency on an already-decided exit, same family as
            # escalate_stale_close. Paced to >=10s; L20 breaker still caps at 3.
            if reason is None and p.get("close_reject_count", 0) > 0 \
                    and not p.get("broker_lost") and p.get("close_reason") \
                    and now - self._retry_at.get(p["rec_id"], 0) > 10:
                self._retry_at[p["rec_id"]] = now
                reason = f"fast_retry({p['close_reject_count']}): {p['close_reason']}"
            if reason is None:
                continue
            with self.lock:
                # re-read under the lock — the cycle may have closed it already
                fresh = next((q for q in load_positions()
                              if q["rec_id"] == p["rec_id"] and not q.get("closing")), None)
                if fresh is None:
                    continue
                result = submit_close_order(fresh, value, reason)
            if result:
                self._closed_by_me.add(p["rec_id"])
            logger.warning("WATCHDOG exit [%s]: %s -> submitted=%s",
                           p["ticker"], reason, bool(result))
            try:
                from src.engine.notify import send
                send(f"Watchdog exit: {p['ticker']}", reason,
                     priority="high", tags="zap")
            except Exception:
                pass

    # ── streaming plumbing ──
    def _open_stream(self, symbols: set[str]):
        token = os.environ.get("TRADIER_PROD_TOKEN")
        if not token:
            raise RuntimeError("TRADIER_PROD_TOKEN not set — watchdog cannot stream")
        s = requests.post(SESSION_URL, timeout=10,
                          headers={"Authorization": f"Bearer {token}",
                                   "Accept": "application/json"})
        s.raise_for_status()
        stream = s.json()["stream"]
        resp = requests.post(stream["url"], stream=True, timeout=(10, 90), data={
            "sessionid": stream["sessionid"],
            "symbols": ",".join(sorted(symbols)),
            "filter": "quote", "linebreak": "true",
        })
        resp.raise_for_status()
        logger.info("watchdog stream open: %d symbols (%s)", len(symbols),
                    ",".join(sorted(symbols)))
        return resp

    def _consume(self, resp):
        """Read stream lines until the symbol set changes or the book empties."""
        last_book_check = 0.0
        for line in resp.iter_lines():
            if line:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "quote" and ev.get("symbol"):
                    self.quotes[ev["symbol"]] = (ev.get("bid"), ev.get("ask"))
                    self._check_positions()
            now = time.monotonic()
            if now - last_book_check >= BOOK_POLL_S:
                last_book_check = now
                current = self._symbols(self._open_positions())
                if current != self.watched:
                    logger.info("watchdog: symbol set changed — restarting stream")
                    return  # outer loop reopens (or idles)

    def run(self):
        logger.info("risk watchdog started")
        backoff_i = 0
        while True:
            try:
                positions = self._open_positions()
                symbols = self._symbols(positions)
                if not symbols:
                    self.watched = set()
                    time.sleep(IDLE_POLL_S)
                    continue
                self.watched = symbols
                resp = self._open_stream(symbols)
                self._failed_notified = False
                backoff_i = 0
                try:
                    self._consume(resp)
                finally:
                    resp.close()
            except Exception as exc:
                wait = BACKOFFS[min(backoff_i, len(BACKOFFS) - 1)]
                backoff_i += 1
                logger.warning("watchdog stream failed (%s) — retry in %ds; "
                               "5-min cycle remains the exit backstop", exc, wait)
                if not self._failed_notified:
                    self._failed_notified = True
                    try:
                        from src.engine.notify import send
                        send("Watchdog degraded",
                             f"stream failed ({exc}) — fast exits paused, 5-min "
                             "cycle still guards positions", priority="high",
                             tags="warning")
                    except Exception:
                        pass
                time.sleep(wait)


def start_watchdog(lock: threading.Lock, cfg: dict) -> RiskWatchdog | None:
    if not (cfg.get("agent") or {}).get("watchdog_enabled", True):
        logger.info("risk watchdog disabled by config")
        return None
    wd = RiskWatchdog(lock, cfg)
    wd.start()
    return wd
