#!/bin/bash
# ONE-SHOT post-close engine restart for C-3 (2026-08-28, operator-approved).
#
# Same pattern as the C-1 restart (scripts/ops/post_close_restart.sh, retired):
# the C-3 fix (partial CLOSE fills book the executed portion, re-arm the
# remainder, and count R5 passes so the exit loop breaker can fire) is
# money-path, applied after operator approval, and takes effect only when the
# engine process restarts onto the new code — after the session, never during.
#
# SELF-RETIRING: removes its own plist and boots itself out on success.
# EVERY GATE IS A STOP CONDITION: refusing to restart is always safer.
set -euo pipefail

ROOT="/Users/you/projects/OptionsAgent"
PY="$ROOT/.venv/bin/python"
LABEL="com.optionsagent.postcloserestart.c3"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$ROOT/logs/ops/post_close_restart_c3-$(date +%F).log"
exec >>"$LOG" 2>&1

notify() {  # notify <title> <priority> <msg>
    "$ROOT/scripts/ops/ntfy_send.sh" "$1" "$2" "" "$3
UNVALIDATED - PAPER ONLY" || true
}
stop() {  # stop <gate> <detail> — engine left UNTOUCHED, job stays armed
    echo "GATE FAILED [$1]: $2"
    notify "Post-close restart ABORTED" "high" "[$1] $2 — engine left running on OLD code; C-3 not in effect."
    exit 1
}

echo "=== POST-CLOSE RESTART (C-3) $(date '+%F %T %Z') ==="

# ── GATE 1: the session must be CLOSED ──────────────────────────────────────
if [ "$("$PY" "$ROOT/scripts/ops/liveness.py" session_open)" = "1" ]; then
    stop "session-still-open" "refusing to restart the engine during market hours"
fi

# ── GATE 2: the book must be FLAT ───────────────────────────────────────────
OPEN_N=$("$PY" -c "
import sys; sys.path.insert(0,'$ROOT')
from src.engine.paper_trader import load_positions
print(len(load_positions()))")
[ "$OPEN_N" = "0" ] || stop "book-not-flat" "$OPEN_N position(s) still open after the close"

# ── GATE 3: prove the C-3 fix is actually present before restarting for it ──
"$PY" -c "
import sys; sys.path.insert(0,'$ROOT')
import inspect
from src.engine import paper_trader as pt
assert hasattr(pt, '_book_close_fill'), 'C-3 booking helper absent'
src = inspect.getsource(pt.escalate_stale_close)
raise SystemExit(0 if 'exit_partial_during_cancel' in src else 'C-3 fix absent from escalate_stale_close')
" || stop "fix-not-present" "the C-3 code is not importable/current — nothing to restart into"

# ── GATE 4: restart (kickstart -k restarts in place; bootout would UNLOAD) ──
echo "  restarting com.optionsagent.trader ..."
launchctl kickstart -k "gui/$(id -u)/com.optionsagent.trader" \
    || stop "kickstart-failed" "launchctl kickstart returned non-zero"
sleep 10

# ── GATE 5: 'restart issued' is not a restart ───────────────────────────────
PID=$(pgrep -f 'src/scheduler.py' | head -1 || true)
[ -n "${PID:-}" ] || stop "process-dead" "no scheduler process after kickstart"
echo "  engine alive, pid $PID"

# ── GATE 6: record what the process restarted onto ──────────────────────────
sleep 2
NEWSTART=$(ps -o lstart= -p "$PID" | tr -s ' ')
echo "  process started: $NEWSTART"
echo "  HEAD: $(cd "$ROOT" && git rev-parse --short HEAD)"

# ── retire: remove the plist, then boot out AFTER this script exits ─────────
rm -f "$PLIST"
( sleep 3; launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 ) &

echo "RESTART COMPLETE — C-3 in effect from the next session; job retired."
notify "Engine restarted (post-close)" "low" \
    "C-3 in effect: partial CLOSE fills book the executed portion and re-arm the remainder; the exit loop breaker counts R5 passes. Book flat, pid $PID. One-shot job retired."
