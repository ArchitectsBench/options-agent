#!/bin/bash
# Honest completion line for the nightly chain (2026-10-30).
#
# WHY THIS EXISTS: scripts/nightly_ops.sh ended with an UNCONDITIONAL
#   echo "nightly ops complete: logs/ops/report-$DATE.md, logs/journal/$DATE.md"
# so on 2026-10-29 — when the agent step exited 1 ("Your computer went to sleep
# mid-response") and the empty-$OUT branch wrote NEITHER file — launchd's log
# read:
#   20:39:42 STEP-FAIL agent_interpretation (exit 1)
#   nightly ops agent produced no output — see logs/ops/agent-errors.log
#   nightly ops complete: logs/ops/report-2026-10-29.md, logs/journal/2026-10-29.md
# naming two files that do not exist. The urgent STEP-FAIL page DID fire, so no
# alert was lost; what was lost is the ability to read the chain's own last line
# as evidence a report was written. This library makes that line a MEASUREMENT
# of the files rather than a claim about them.
#
# Deliberately NOT a new alert: the failing step already pages via step_alert,
# and a second page for the same cause is noise. Reporting, not a new gate.
#
# Contract (mirrors scripts/ops/step_timing.sh): sourcing is optional. If this
# file is missing, nightly_ops.sh defines a fallback and the chain runs exactly
# as before. completion_line is safe under `set -euo pipefail` and returns
# non-zero ONLY to describe the outcome — the caller must `|| true` it so the
# chain's own exit status is unchanged.

# completion_line <report-path> <journal-path>
# Prints the honest one-line outcome. rc 0 = both files written and non-empty,
# rc 1 = at least one is missing or empty.
completion_line() {
    local report="$1" journal="$2" missing=""

    # -s not -e: the 2026-07-16 marker-less-output bug wrote two EMPTY files,
    # which an existence check would have reported as a complete night.
    [ -s "$report" ] || missing="report=$report"
    if [ ! -s "$journal" ]; then
        [ -n "$missing" ] && missing="$missing "
        missing="${missing}journal=$journal"
    fi

    if [ -z "$missing" ]; then
        echo "nightly ops complete: $report, $journal"
        return 0
    fi
    echo "NIGHTLY OPS INCOMPLETE — not written (empty or absent): $missing — see logs/ops/agent-errors.log"
    return 1
}
