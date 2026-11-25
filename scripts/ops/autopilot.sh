#!/bin/bash
# AUTOPILOT OFFICER wrapper (2026-09-14). Operator: "Create a scheduled agent to
# track, follow up and resolve issues ... I don't want to keep checking."
#
# launchd (com.optionsagent.autopilot) runs this at fixed checkpoints. It starts
# a headless Claude Code session with docs/autopilot/CHARTER.md as its standing
# instructions and docs/autopilot/QUEUE.md as its work list. The charter holds
# the authority and the hard limits; this wrapper holds the mechanics:
#   * checkpoint derived from the clock (pre_open / post_open / mid_session /
#     post_close / evening / weekend); AUTOPILOT_CHECKPOINT overrides
#   * kill switch: ops/AUTOPILOT_OFF present -> skip, logged
#   * single flight: a lock dir; a lock older than 45 min is stale and broken
#   * hard wall-clock cap (AUTOPILOT_TIMEOUT_S, default 1800 s) and a per-run
#     spend cap (AUTOPILOT_BUDGET_USD, default 5)
#   * git push / sudo are disallowed at the tool layer, not just in prose
#   * every run is recorded in logs/autopilot/runs.jsonl; a run that crashes,
#     times out or ends without the ===AUTOPILOT-DONE=== line pages the operator
#
#   scripts/ops/autopilot.sh            # launchd
#   AUTOPILOT_CHECKPOINT=evening scripts/ops/autopilot.sh
set -uo pipefail
ROOT="${AUTOPILOT_ROOT:-/Users/you/projects/OptionsAgent}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.npm-global/bin/claude")}"
# MODEL IS PINNED (2026-09-28). The first five live runs all died in <1 s with
# "claude-fable-5 cannot be used as an advisor when the request model is
# claude-fable-5-1": a headless run inherits the operator's INTERACTIVE defaults
# (~/.claude/settings.json model + advisorModel), and that pair is rejected by the
# API. An unattended job must not depend on whatever the operator last picked in
# the IDE. Override with AUTOPILOT_MODEL if needed.
SEND="${AUTOPILOT_NTFY:-$ROOT/scripts/ops/ntfy_send.sh}"
LOGDIR="$ROOT/logs/autopilot"
mkdir -p "$LOGDIR" "$ROOT/data"
DATE=$(date +%F); NOW=$(date +%H%M); DOW=$(date +%u)

CP="${AUTOPILOT_CHECKPOINT:-}"
if [ -z "$CP" ]; then
    if [ "$DOW" -ge 6 ]; then CP=weekend
    elif [ "$NOW" -lt 0625 ]; then CP=pre_open
    elif [ "$NOW" -lt 0930 ]; then CP=post_open
    elif [ "$NOW" -lt 1300 ]; then CP=mid_session
    elif [ "$NOW" -lt 1700 ]; then CP=post_close
    else CP=evening
    fi
fi
LOG="$LOGDIR/$DATE-$CP.md"
record() {  # record <rc> <status>
    printf '{"date":"%s","time":"%s","checkpoint":"%s","rc":%s,"status":"%s","log":"%s"}\n' \
        "$DATE" "$(date +%H:%M:%S)" "$CP" "$1" "$2" "$LOG" >> "$LOGDIR/runs.jsonl"
}

if [ -f "$ROOT/ops/AUTOPILOT_OFF" ]; then
    record 0 "skipped_off"; exit 0
fi

LOCK="$ROOT/data/autopilot.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +45 2>/dev/null)" ]; then
        rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || { record 0 "skipped_locked"; exit 0; }
    else
        record 0 "skipped_locked"; exit 0
    fi
fi
trap 'rm -rf "$LOCK"' EXIT

PROMPT="$(cat "$ROOT/docs/autopilot/CHARTER.md")

---
CHECKPOINT: $CP
NOW: $(date '+%a %F %H:%M %Z')
Work list: docs/autopilot/QUEUE.md
Write this run's log to: $LOG"

cd "$ROOT" || exit 1
# A leftover .md from an EARLIER run at this same date+checkpoint is moved aside before the
# agent starts. That makes "$LOG exists" by itself proof that THIS run wrote it, so the
# classification below needs no timestamp comparison -- an mtime test against the lock dir
# was tried first and silently failed every time, because the lock and the log land in the
# same second and `-nt` is strictly-newer.
[ -f "$LOG" ] && mv -f "$LOG" "$LOG.prev"
# perl alarm = portable wall-clock cap (macOS has no GNU timeout)
perl -e 'alarm shift; exec @ARGV' "${AUTOPILOT_TIMEOUT_S:-1800}" \
    "$CLAUDE_BIN" -p "$PROMPT" \
    --model "${AUTOPILOT_MODEL:-claude-opus-5}" \
    --permission-mode acceptEdits \
    --allowedTools "Bash" "Read" "Edit" "Write" "Grep" "Glob" \
    --disallowedTools "Bash(git push:*)" "Bash(sudo:*)" \
    --max-budget-usd "${AUTOPILOT_BUDGET_USD:-5}" \
    > "$LOG.out" 2>&1
RC=$?

# Classification is THREE-way (2026-10-01). It used to be two: DONE-line-in-stdout = ok,
# everything else = urgent page "Nothing it was doing is confirmed". That misfired twice
# (09-29 pre_open, 09-30 evening): when --max-budget-usd is hit the CLI REPLACES $LOG.out
# with the single line "Error: Exceeded USD budget (N)", so the transcript -- and the
# completion line with it -- is gone even though the agent had already written its .md log
# and (09-30) delivered the digest two minutes earlier. Two urgent pages out of ten runs for
# work that was in fact done is how an alert channel gets ignored.
#   1. DONE line in the transcript OR in the run's own .md log -> ok, silent.
#   2. budget cap hit, log written, no DONE line                -> budget_exhausted, default page.
#   3. no log at all                                            -> failed, urgent page (unchanged).
# "$LOG exists" is trustworthy because a stale log was rotated to $LOG.prev before launch.
DONE_LINE=""
if grep -q "===AUTOPILOT-DONE===" "$LOG.out" 2>/dev/null; then
    DONE_LINE=$(grep -o "===AUTOPILOT-DONE=== .*" "$LOG.out" | tail -1)
elif [ -f "$LOG" ] && grep -q "===AUTOPILOT-DONE===" "$LOG" 2>/dev/null; then
    DONE_LINE=$(grep -o "===AUTOPILOT-DONE=== .*" "$LOG" | tail -1)
fi

if [ -n "$DONE_LINE" ]; then
    STATUS=$(printf '%s' "$DONE_LINE" | awk '{print $3}')
    record 0 "${STATUS:-ok}"
    exit 0
fi

if grep -q "Exceeded USD budget" "$LOG.out" 2>/dev/null && [ -f "$LOG" ]; then
    record "$RC" "budget_exhausted"
    "$SEND" "AUTOPILOT BUDGET CAP: $CP" default warning \
        "The $CP autopilot run hit its spend cap (\$${AUTOPILOT_BUDGET_USD:-5}) before signing off.
It DID write its log, so part of the work stands -- but it may be incomplete: $LOG
UNVALIDATED - PAPER ONLY" || true
    exit 1
fi

WHY="exit $RC"; [ $RC -eq 142 ] && WHY="timed out after ${AUTOPILOT_TIMEOUT_S:-1800}s"
grep -q "Exceeded USD budget" "$LOG.out" 2>/dev/null && WHY="$WHY, spend cap hit"
WHY="$WHY, no completion line"
record "$RC" "failed"
"$SEND" "AUTOPILOT RUN FAILED: $CP" urgent warning \
    "The $CP autopilot run did not complete ($WHY). Nothing it was doing is confirmed. See $LOG.out
UNVALIDATED - PAPER ONLY" || true
exit 1
