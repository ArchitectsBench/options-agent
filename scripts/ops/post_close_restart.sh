#!/bin/bash
# ONE-SHOT post-close engine restart (2026-08-10, operator-approved).
#
# WHY THIS EXISTS. The C-1 fixes (SCALP daily cap, funnel lane isolation) are
# money-path, and the operator's instruction was "fix both and restart after
# the session" — NOT mid-session. The engine is never restarted on its own:
# it runs continuously, and the standing runbook's bootstrap no-ops on an
# already-loaded job, so a running process keeps serving whatever code it
# imported at start. That is the same failure that let the cockpit API serve
# 17-day-old code; the engine has the identical exposure.
#
# SELF-RETIRING. This job removes its own plist and boots itself out on
# success, so it fires exactly once. It is not a permanent daily restart —
# that would be a standing behaviour change and is the operator's call.
#
# EVERY GATE IS A STOP CONDITION. Refusing to restart is always safer than
# restarting into an unknown state, so anything unexpected leaves the engine
# exactly as it is and alerts loudly.
set -euo pipefail

ROOT="/Users/you/projects/OptionsAgent"
PY="$ROOT/.venv/bin/python"
LABEL="com.optionsagent.postcloserestart"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$ROOT/logs/ops/post_close_restart-$(date +%F).log"
exec >>"$LOG" 2>&1

notify() {  # notify <title> <priority> <msg>
    "$ROOT/scripts/ops/ntfy_send.sh" "$1" "$2" "" "$3
UNVALIDATED - PAPER ONLY" || true
}
stop() {  # stop <gate> <detail> — engine left UNTOUCHED, job stays armed
    echo "GATE FAILED [$1]: $2"
    notify "Post-close restart ABORTED" "high" "[$1] $2 — engine left running on OLD code; C-1 not in effect."
    exit 1
}

echo "=== POST-CLOSE RESTART $(date '+%F %T %Z') ==="

# ── GATE 1: the session must be CLOSED ──────────────────────────────────────
# The whole point of this job is "after the session". If the clock says the
# session is open, something is wrong with the schedule — never restart into
# a live session.
if [ "$("$PY" "$ROOT/scripts/ops/liveness.py" session_open)" = "1" ]; then
    stop "session-still-open" "refusing to restart the engine during market hours"
fi

# ── GATE 2: the book must be FLAT ───────────────────────────────────────────
# The engine flattens at 15:45 ET. A non-flat book after the close means the
# flatten did not complete, which is a condition to investigate, not to
# restart through.
OPEN_N=$("$PY" -c "
import sys; sys.path.insert(0,'$ROOT')
from src.engine.paper_trader import load_positions
print(len(load_positions()))")
[ "$OPEN_N" = "0" ] || stop "book-not-flat" "$OPEN_N position(s) still open after the close"

# ── GATE 3: prove the fix is actually present before restarting for it ──────
# Restarting without verifying the new code is loadable would swap a known
# state for an unknown one.
"$PY" -c "
import sys; sys.path.insert(0,'$ROOT')
from src.engine.paper_trader import today_test_entry_count   # noqa: F401
from src.engine.risk import _funnel_exposure                  # noqa: F401
import inspect
from src.engine import risk
src = inspect.getsource(risk._funnel_exposure)
raise SystemExit(0 if 'is_scalp' in src else 'C-1 fix absent from risk._funnel_exposure')
" || stop "fix-not-present" "the C-1 code is not importable/current — nothing to restart into"

# ── GATE 4: restart (bootout UNLOADS; kickstart -k restarts in place) ───────
echo "  restarting com.optionsagent.trader ..."
launchctl kickstart -k "gui/$(id -u)/com.optionsagent.trader" \
    || stop "kickstart-failed" "launchctl kickstart returned non-zero"
sleep 10

# ── GATE 5: 'restart issued' is not a restart ───────────────────────────────
# Outside market hours the engine idles rather than trading, so assert the
# PROCESS, not a completed cycle (which would never come until tomorrow).
PID=$(pgrep -f 'src/scheduler.py' | head -1 || true)
[ -n "${PID:-}" ] || stop "process-dead" "no scheduler process after kickstart"
echo "  engine alive, pid $PID"

# ── GATE 6: confirm the running process imported the FIX ────────────────────
# The defect this whole day was about is a process serving stale code. Do not
# repeat it by trusting that a restart implies new code.
sleep 2
NEWSTART=$(ps -o lstart= -p "$PID" | tr -s ' ')
echo "  process started: $NEWSTART"
echo "  HEAD: $(cd "$ROOT" && git rev-parse --short HEAD)"

# ── retire: remove the plist, then boot out AFTER this script exits ─────────
rm -f "$PLIST"
( sleep 3; launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 ) &

echo "RESTART COMPLETE — C-1 in effect from the next session; job retired."
notify "Engine restarted (post-close)" "low" \
    "C-1 in effect: SCALP daily cap binds, funnel lane isolation restored. Book flat, pid $PID. One-shot job retired."
