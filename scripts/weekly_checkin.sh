#!/bin/bash
# Weekly check-in (Fridays 16:00 PT): deterministic week facts -> read-only
# Claude review -> logs/ops/weekly-review-<date>.md -> ntfy push.
# Same pattern as nightly_ops.sh; the wrapper owns all writes.
# set -euo pipefail (2026-08-04): -e abort on error, -u unset-var guard,
# -o pipefail so a failure ANYWHERE in a pipeline is the pipeline's status
# (a piped verdict silently read as success is how three exit-code misreads
# happened). Optional actions carry an explicit `|| true`.
set -euo pipefail
ROOT="/Users/you/projects/OptionsAgent"
DATE=$(date +%F)
# launchd's bare PATH misses `claude`; npm-global fallback (see nightly_ops.sh)
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.npm-global/bin/claude")}"
mkdir -p "$ROOT/logs/ops"

# 1. facts
"$ROOT/.venv/bin/python" "$ROOT/scripts/weekly_review.py" \
    > "$ROOT/logs/ops/weekly-facts-$DATE.json" 2>&1

# 1b. lessons audit (learn-from-mistakes loop): verify every lesson in
# docs/lessons.yaml still has its enforcement in place; BROKEN = exit 1,
# which the charter routes to ACTION_NEEDED.
"$ROOT/.venv/bin/python" "$ROOT/scripts/lessons_audit.py" --json \
    > "$ROOT/logs/ops/lessons-audit-$DATE.json" 2>&1 || true

# 2. read-only review
PROMPT="$(cat "$ROOT/agents/weekly-review-charter.md")

Today: $DATE. The week's facts JSON is at logs/ops/weekly-facts-$DATE.json — read it first.
The lessons audit result is at logs/ops/lessons-audit-$DATE.json; the registry is docs/lessons.yaml.
Working directory: $ROOT"

OUT=$(cd "$ROOT" && "$CLAUDE_BIN" -p "$PROMPT" \
    --allowedTools "Read,Grep,Glob" \
    2>>"$ROOT/logs/ops/agent-errors.log")

if [ -z "$OUT" ]; then
    BODY="weekly check-in agent produced no output — see logs/ops/agent-errors.log"
    echo "$BODY"
else
    echo "$OUT" | awk '/^===WEEKLY===/{f=1;next} /^===ACTION_NEEDED===/{f=0} f' \
        > "$ROOT/logs/ops/weekly-review-$DATE.md"
    ACTION=$(echo "$OUT" | awk '/^===ACTION_NEEDED===/{f=1;next} f')
    BODY="$(head -8 "$ROOT/logs/ops/weekly-review-$DATE.md")

ACTION NEEDED: $ACTION"
fi

# 3. notify (UNVALIDATED stamp; every ntfy target from .env via the shared sender)
"$ROOT/scripts/ops/ntfy_send.sh" "OptionsAgent weekly check-in - UNVALIDATED PAPER" default calendar "$BODY
UNVALIDATED - PAPER ONLY" || true
echo "weekly check-in complete: logs/ops/weekly-review-$DATE.md"
