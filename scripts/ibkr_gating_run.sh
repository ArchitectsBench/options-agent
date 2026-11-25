#!/bin/bash
# One-shot GATING evidence run (parallel-run step 1, 2026-08-05 06:32 PT).
# Runs the FIXED probe during the session and records ITS verdict as the
# durable gating evidence for the parallel run. Exit code is preserved.
# set -euo pipefail (2026-08-04): -e abort on error, -u unset-var guard,
# -o pipefail so a failure ANYWHERE in a pipeline is the pipeline's status
# (a piped verdict silently read as success is how three exit-code misreads
# happened). Optional actions carry an explicit `|| true`.
set -euo pipefail
ROOT="/Users/you/projects/OptionsAgent"
EV="$ROOT/logs/ops/parallel_run_gating_evidence.log"
mkdir -p "$ROOT/logs/ops"
# `|| RC=$?` NOT a bare assignment: under `set -e`, VAR=$(failing_cmd) aborts
# the script — which would mean a NOT-GREEN probe records NO gating evidence,
# precisely when the evidence matters most.
RC=0
OUT=$("$ROOT/.venv/bin/python" "$ROOT/scripts/ibkr_entitlement_check.py" --notify 2>&1) || RC=$?
{
  echo "=== GATING RUN $(date '+%F %T %Z') (exit $RC) ==="
  echo "$OUT"
  if [ $RC -eq 0 ]; then
    echo "VERDICT: GREEN — gating evidence recorded; parallel run may proceed per docs/parallel_run_plan.md"
  else
    echo "VERDICT: NOT GREEN (exit $RC) — this is a FINDING, not a formality. Parallel run does NOT start."
  fi
  echo
} >> "$EV"
echo "$OUT"; exit $RC
