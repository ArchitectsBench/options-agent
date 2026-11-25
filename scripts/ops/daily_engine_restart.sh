#!/bin/bash
# STANDING pre-session engine restart (2026-08-22, operator-approved).
#
# WHY THIS EXISTS. The engine's long-lived IBKR client dies and never
# reconnects — zero reconnect attempts appear anywhere in agent.log. Once dead
# it rejects every entry with "qualify_failed: Not connected" while the loop
# keeps cycling normally, so the session looks healthy and books nothing. Seven
# of the eight trading days 08-12..08-21 executed ZERO orders. The single live
# day in that span, 08-18, was the one that followed an engine restart.
#
# A restart is a WORKAROUND, not the fix. The fix is reconnect logic in the
# client (tracked separately). This bounds the damage to one session in the
# meantime, and also bounds code staleness — the cockpit once served 17-day-old
# code, and the engine ran 4 days across a gateway restart cycle.
#
# WHY PRE-SESSION rather than post-close: the gateway itself restarts nightly
# at ~23:45, so a post-close restart would still leave the engine pointing at a
# gateway that disappears hours later. Restarting at 06:20 means the engine's
# client is minutes old when the session opens.
#
# NOT self-retiring. Its one-shot predecessor (post_close_restart.sh) removed
# its own plist after firing, so nothing restarted the engine for the next
# three sessions and three daily reports flagged the absence with nothing able
# to act on it.
set -euo pipefail

ROOT="/Users/you/projects/OptionsAgent"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/logs/ops/daily_engine_restart-$(date +%F).log"
exec >>"$LOG" 2>&1

notify() {  # notify <title> <priority> <msg>
    "$ROOT/scripts/ops/ntfy_send.sh" "$1" "$2" "" "$3
UNVALIDATED - PAPER ONLY" || true
}

echo "=== DAILY ENGINE RESTART $(date '+%F %T %Z') ==="

# ── GATE 1: weekday only ────────────────────────────────────────────────────
[ "$(date +%u)" -le 5 ] || { echo "  weekend — nothing to do"; exit 0; }

# ── GATE 2: operator HOLD wins ──────────────────────────────────────────────
if [ -f "$ROOT/ops/HOLD" ]; then
    echo "  ops/HOLD present — refusing to restart (operator hold)"
    exit 0
fi

# ── GATE 3: never restart into a LIVE, MANAGEABLE position ──────────────────
# DEADLOCK FIX (2026-09-12). This previously refused whenever the book was not
# flat. But the book is not flat BECAUSE the broker is dead — positions cannot
# be closed — so the guard fired exactly when a restart was most needed and the
# outage sustained itself:
#
#   broker client dies -> positions cannot close -> book not flat
#           ^                                             |
#      client stays dead <- restart REFUSES --------------+
#
# It cost 09-02, 09-03, 09-04, 09-08, 09-10 and 09-11 (five+ zero-execution
# sessions), held open by contracts that had ALREADY EXPIRED and that the
# broker no longer held. Refusing protected nothing.
#
# What actually deserves protection is a LIVE position the engine is still
# managing. So refuse only when a position is (a) not past expiry AND (b) the
# broker confirms it exists. If the broker view is UNKNOWN, or every blocking
# position is expired, restarting is the SAFE action — nothing is being managed
# either way, and the restart is what restores management.
BLOCK=$("$PY" -c "
import sys; sys.path.insert(0,'$ROOT')
from src.engine.paper_trader import restart_blocking_positions
n, why = restart_blocking_positions()
print(f'{n}|{why}')" 2>/dev/null || echo "ERR|probe failed")
N="${BLOCK%%|*}"; WHY="${BLOCK#*|}"
echo "  gate 3: blocking=$N ($WHY)"
if [ "$N" = "ERR" ]; then
    echo "  REFUSING: could not evaluate the book — not restarting blind"
    notify "Engine restart SKIPPED" "high" "Book probe failed at 06:20 — engine NOT restarted."
    exit 1
fi
if [ "$N" != "0" ]; then
    echo "  REFUSING: $N LIVE position(s)/order(s) confirmed at the broker — restart could orphan them"
    notify "Engine restart SKIPPED" "high" \
        "$N LIVE position(s) at the broker at 06:20 — engine NOT restarted. Flatten or reconcile, then restart."
    exit 1
fi

# ── GATE 3b: the engine must read the SAME v2 setting the operator set ─────
# Activation night (2026-09-12): the YAML said enabled: true and the engine
# started with v2 OFF because scalp_v2_config() swallowed a NameError. Go
# through the engine's own reader; a read failure is a refusal, not a restart.
V2=$("$PY" -c "
import sys; sys.path.insert(0,'$ROOT')
from src.engine.paper_trader import scalp_v2_config
c = scalp_v2_config(); print(f\"{int(c['enabled'])}|{int(c['read_failed'])}\")" 2>/dev/null || echo "ERR|1")
V2_ON="${V2%%|*}"; V2_RF="${V2#*|}"
echo "  gate 3b: scalp_v2 enabled=$V2_ON read_failed=$V2_RF"
if [ "$V2_RF" != "0" ]; then
    echo "  REFUSING: scalp_v2 config read FAILED — the engine would run the wrong rules"
    notify "Engine restart SKIPPED" "urgent" "scalp_v2 config read FAILED at 06:20 — engine NOT restarted; fix config/config.yaml before the open."
    exit 1
fi

# ── restart ─────────────────────────────────────────────────────────────────
OLD=$(pgrep -f 'src/scheduler.py' | head -1 || true)
echo "  old pid: ${OLD:-none}"
# UNLOADED-SERVICE FIX (2026-09-24). `kickstart` only restarts a LOADED job. On
# 09-23 the trader job was unloaded mid-incident and never reloaded; the 09-24
# 06:20 run then failed with "Could not find service" and the whole session
# executed nothing. If the job is not loaded, load it (RunAtLoad starts it).
PLIST="$HOME/Library/LaunchAgents/com.optionsagent.trader.plist"
if ! launchctl list com.optionsagent.trader >/dev/null 2>&1; then
    echo "  trader job NOT loaded — loading $PLIST"
    launchctl load "$PLIST" || {
        echo "  load FAILED"
        notify "Engine restart FAILED" "urgent" "trader job was unloaded and launchctl load failed — engine DOWN before the open."
        exit 1
    }
    sleep 5
fi
KICK_T0="$(date +%H:%M:%S)"          # only a broker activation AFTER this counts
launchctl kickstart -k "gui/$(id -u)/com.optionsagent.trader" || {
    echo "  kickstart FAILED"
    notify "Engine restart FAILED" "urgent" "launchctl kickstart returned non-zero — engine may be down before the open."
    exit 1
}
sleep 20

# ── verify: 'restart issued' is not a restart ───────────────────────────────
NEW=$(pgrep -f 'src/scheduler.py' | head -1 || true)
if [ -z "${NEW:-}" ]; then
    echo "  no scheduler process after kickstart"
    notify "Engine restart FAILED" "urgent" "No scheduler process after kickstart — the engine is DOWN before the open."
    exit 1
fi
echo "  new pid: $NEW"

# ── verify the BROKER CLIENT, which is the entire point ─────────────────────
# A restarted engine with a dead client is the failure this job exists to
# prevent, so assert the thing that actually matters, not just the pid.
# C-12.1 (2026-09-28): WAIT for it, and re-kick when the gateway becomes
# reachable. On 09-28 this block noticed the dead client, paged, and exited —
# the gateway was simply not logged in yet, nothing re-tried, and the session
# booked nothing. scripts/ops/await_broker_activation.sh pages the exact remedy
# once, re-kicks while the port is open, and gives up loudly at 06:45.
sleep 10
if SINCE_HMS="$KICK_T0" "$ROOT/scripts/ops/await_broker_activation.sh"; then
    NEW=$(pgrep -f 'src/scheduler.py' | head -1 || true)
    echo "  broker client ACTIVATED — execution path live (pid ${NEW:-?})"
    notify "Engine restarted" "low" "pid ${NEW:-?}, broker client activated, book flat. Ready for the open."
else
    echo "  NO BROKER by the deadline — session will book nothing unless the gateway is logged in and the engine re-kicked"
    echo "RESTART COMPLETE (BROKER DOWN)"
    exit 1
fi
echo "RESTART COMPLETE"
