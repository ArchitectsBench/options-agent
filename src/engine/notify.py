"""Notification layer. Fires on:
  - Claude-APPROVED recommendations only (not every signal)
  - paper order fills (with slippage vs recommendation price)
  - agent errors / degraded feeds persisting > 2 consecutive cycles

Channels: macOS native (osascript) + phone push to TWO ntfy servers: ntfy.sh
(NTFY_TOPIC) and a second server (NTFY_ALT_SERVER + NTFY_ALT_TOPIC, 2026-09-13
— ntfy.sh's quota is per IP, so one server is one point of failure). Every
message carries the UNVALIDATED — PAPER ONLY stamp and stays to a few lines —
the full spec lives in the logs.

Graceful degradation: a notification failure is logged and swallowed; it
never breaks the cycle.
"""

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

logger = logging.getLogger("trader")

STAMP = "UNVALIDATED — PAPER ONLY"
HEALTH_THRESHOLD = 3  # notify when a problem persists > 2 cycles


def _macos_notify(title: str, body: str):
    script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def _ascii_header(s: str) -> str:
    """HTTP headers are latin-1; ntfy titles must be ASCII-safe."""
    return s.replace("—", "-").encode("ascii", "replace").decode()


_sleep = time.sleep          # injectable, so the retry tests never really wait


def _ntfy_attempts() -> int:
    """Tries per target before a page is given up on (shell: NTFY_ATTEMPTS)."""
    try:
        return max(1, int(os.environ.get("NTFY_ATTEMPTS", "3")))
    except ValueError:
        return 3


def _ntfy_retry_sleep() -> float:
    try:
        return float(os.environ.get("NTFY_RETRY_SLEEP", "2"))
    except ValueError:
        return 2.0


def _ntfy_targets() -> list[tuple[str, str]]:
    """(host, url) for every configured ntfy target.

    Primary is ntfy.sh/NTFY_TOPIC. The second channel (NTFY_ALT_SERVER +
    NTFY_ALT_TOPIC, added 2026-09-13) is a different SERVER, not a different
    topic: ntfy.sh's free quota is per visitor IP, so when it is exhausted
    every ntfy.sh topic returns 429 at once."""
    targets = []
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        targets.append((server.split("//")[-1], f"{server}/{topic}"))
    alt_server = os.environ.get("NTFY_ALT_SERVER", "").rstrip("/")
    alt_topic = os.environ.get("NTFY_ALT_TOPIC")
    if alt_server and alt_topic:
        targets.append((alt_server.split("//")[-1], f"{alt_server}/{alt_topic}"))
    return targets


def _log_delivery(host: str, code: int, title: str, attempt: tuple[int, int] | None = None):
    """Append one per-target row to the SAME log scripts/ops/ntfy_send.sh writes,
    byte-for-byte in its format, so engine pages stop being invisible there
    (2026-10-14: that log was ops-scripts-only, and the autopilot health sweep
    read it as a complete record of every page).

    Wrapped entirely — this file is the paging backbone during an outage, so
    bookkeeping must never be able to block or break a page.
    """
    try:
        path = os.environ.get("NTFY_SEND_LOG")          # resolved at CALL time
        path = Path(path) if path else ROOT / "logs" / "ops" / "notify_delivery.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        # LOCAL time, never UTC: the shell writes PT, and a UTC row after 17:00
        # PT carries tomorrow's date and breaks every date-anchored grep.
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # The suffix appears ONLY on tries > 1, exactly as the shell writes it:
        # a first-attempt row must stay suffix-free or the 2026-10-19 page count
        # (`grep "http=000" | grep -v " attempt="`) over-counts lost pages.
        suffix = f" attempt={attempt[0]}/{attempt[1]}" if attempt and attempt[0] > 1 else ""
        with open(path, "a") as fh:
            fh.write(f'{stamp} ntfy target={host} http={code:03d}{suffix} title="{title}"\n')
    except Exception:
        pass


def _ntfy_notify(title: str, body: str, priority: str = "default",
                 tags: str = "chart_with_upwards_trend", actions: list[str] | None = None):
    """Post to every target. Each target's failure is logged on its own; raises
    only when targets exist and NONE accepted the message."""
    targets = _ntfy_targets()
    if not targets:
        return
    headers = {"Title": _ascii_header(title), "Priority": priority, "Tags": tags}
    if actions:  # ntfy action buttons, e.g. one-tap confirm (spec strings, ';'-joined)
        headers["Actions"] = _ascii_header("; ".join(actions))
    delivered = 0
    attempts = _ntfy_attempts()
    for host, url in targets:
        # Each target retries independently, as the shell helper does.
        for try_n in range(1, attempts + 1):
            req = urllib.request.Request(
                url,
                data=body.encode(),  # body is UTF-8 — only headers need ASCII
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    delivered += 1
                    code = getattr(resp, "status", 200) or 200
                _log_delivery(host, code, title, (try_n, attempts))
                break
            except urllib.error.HTTPError as exc:
                # A code came back, so a server ANSWERED: the code is the truth,
                # not a lost page. Never retried — a 429 is the daily per-IP
                # quota (the second SERVER is the answer to that, not a re-post).
                logger.warning("ntfy target %s failed: %s", host, exc)
                _log_delivery(host, exc.code, title, (try_n, attempts))
                break
            except Exception as exc:
                # No HTTP response at all — logged 000, as curl -w writes on
                # connect failure — so the page is simply LOST unless re-sent.
                # On 2026-10-19 both targets returned 000 in the same second and
                # one DEADMAN page went with no trace: a second ntfy server is
                # not independent of this host's own network stack.
                logger.warning("ntfy target %s failed: %s", host, exc)
                _log_delivery(host, 0, title, (try_n, attempts))
                if try_n < attempts:
                    _sleep(_ntfy_retry_sleep())
    if not delivered:
        raise RuntimeError(f"all {len(targets)} ntfy targets failed")


def send(title: str, body: str, priority: str = "default",
         tags: str = "chart_with_upwards_trend", actions: list[str] | None = None):
    """Fire both channels; failures are logged, never raised."""
    for name, fn in (("macos", lambda: _macos_notify(title, body)),
                     ("ntfy", lambda: _ntfy_notify(title, body, priority, tags, actions))):
        try:
            fn()
        except Exception as exc:
            logger.warning("notification channel %s failed: %s", name, exc)


def notify_recommendation(rec: dict):
    """Claude-approved recommendations only — caller enforces."""
    t = rec["trade"]
    strikes = "/".join(str(leg["strike"]) for leg in t["legs"])
    body = (f"{rec['ticker']} {t['structure']} {strikes} exp {t['expiration']}\n"
            f"debit {t['net_debit']} | entry: next bar, invalid beyond VWAP\n"
            f"{STAMP}")
    send(f"OptionsAgent — {STAMP}", body, priority="high", tags="rotating_light")


def notify_fill(fill: dict):
    # PARTIAL FILLS reach here too (2026-08-18) and carry no 'rec_price' /
    # 'slippage'. Indexing them raised KeyError inside the cycle. Report what
    # the event actually has — a notification must never be able to kill the
    # caller.
    qty = fill.get("quantity")
    req = fill.get("requested")
    partial = fill.get("event") == "partial_fill_booked"
    head = f"{fill.get('ticker', '?')} paper {'PARTIALLY ' if partial else ''}FILLED"
    body = f"{head} @ {fill.get('fill_price')}"
    if partial and qty is not None:
        body += f" ({qty}/{req} contracts, remainder working)"
    if fill.get("rec_price") is not None:
        body += f" (rec {fill['rec_price']}"
        if fill.get("slippage") is not None:
            body += f", slip {fill['slippage']:+.2f}"
        body += ")"
    send(f"OptionsAgent fill — {STAMP}", f"{body}\n{STAMP}", tags="white_check_mark")


def notify_exit(exit_event: dict):
    pnl = exit_event["pnl_per_contract"]
    body = (f"{exit_event['ticker']} paper EXIT @ {exit_event['exit_price']} "
            f"(entry {exit_event['entry_price']}) P&L ${pnl:+.2f}/contract\n"
            f"reason: {exit_event['close_reason']}\n{STAMP}")
    send(f"OptionsAgent exit — {STAMP}", body,
         tags="moneybag" if pnl >= 0 else "small_red_triangle_down")


def track_health(problem: bool, detail: str = ""):
    """Call once per cycle. Notifies when a problem persists > 2 consecutive
    cycles, once per streak (re-arms after recovery)."""
    from src.control.state import load_state, update_state
    state = load_state()
    streak = state.get("health_fail_streak", 0)
    notified = state.get("health_notified", False)
    if problem:
        streak += 1
        if streak >= HEALTH_THRESHOLD and not notified:
            send(f"OptionsAgent DEGRADED — {STAMP}",
                 f"problem persisting {streak} cycles: {detail}\n{STAMP}",
                 priority="high", tags="warning")
            notified = True
    else:
        streak, notified = 0, False
    update_state(health_fail_streak=streak, health_notified=notified)
