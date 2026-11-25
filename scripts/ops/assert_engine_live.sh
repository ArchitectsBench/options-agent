#!/bin/bash
# O-21(a) — runbook STEP 3 does not end at "start command issued".
# It ASSERTS: job loaded + process alive + FIRST TRADING CYCLE COMPLETED.
# On 2026-08-07 `launchctl bootout` had UNLOADED the job, so nothing would have
# restarted it; a process-alive check alone would still have passed nothing.
set -euo pipefail
ROOT="/Users/you/projects/OptionsAgent"
LABEL="com.optionsagent.trader"
WAIT_S="${1:-420}"          # allow one 5-min cycle + margin

fail() { echo "STEP 3 ASSERTION FAILED: $1"; exit 1; }

# 1. job LOADED (not merely "a process exists")
launchctl list | grep -q "$LABEL" || fail "launchd job $LABEL is NOT LOADED (bootout unloads it; use bootstrap)"
PID=$(launchctl list | awk -v l="$LABEL" '$3==l{print $1}')
echo "  [1/3] job loaded (pid $PID)"

# 2. process ALIVE
[ -n "$PID" ] && [ "$PID" != "-" ] && kill -0 "$PID" 2>/dev/null \
  || fail "job loaded but process not running (pid=${PID:-none})"
echo "  [2/3] process alive"

# 3. FIRST TRADING CYCLE COMPLETED (a heartbeat from THIS session)
echo "  [3/3] waiting up to ${WAIT_S}s for the first cycle..."
DEADLINE=$(( $(date +%s) + WAIT_S ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if [ "$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/liveness.py" heartbeat_overdue \
            "$("$ROOT/.venv/bin/python" -c "
import json
try: print(json.load(open('$ROOT/data/agent_state.json')).get('last_cycle') or '')
except Exception: print('')" 2>/dev/null)" 2>/dev/null)" = "0" ]; then
        echo "  [3/3] first cycle completed — ENGINE LIVE"
        exit 0
    fi
    sleep 15
done
fail "no trading cycle completed within ${WAIT_S}s — engine started but is not trading"
