#!/bin/bash
# Nightly ops agent: deterministic checks -> read-only Claude interpretation
# -> readiness report + journal -> ntfy push. The agent has NO write tools;
# this wrapper owns all writes.
# set -euo pipefail (2026-08-04): -e abort on error, -u unset-var guard,
# -o pipefail so a failure ANYWHERE in a pipeline is the pipeline's status
# (a piped verdict silently read as success is how three exit-code misreads
# happened). Optional actions carry an explicit `|| true`.
set -euo pipefail
ROOT="/Users/you/projects/OptionsAgent"
DATE=$(date +%F)
# launchd's bare PATH misses `claude`; fall back to the npm-global install
# (fixed 2026-07-01 — the old ~/.claude/local path was stale, which silently
# killed nightly reports from ~2026-06-12; see logs/ops/agent-errors.log)
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.npm-global/bin/claude")}"
# MODEL IS PINNED (2026-09-29), same cause as the autopilot pin on 2026-09-28: a
# headless run inherits the operator's INTERACTIVE defaults (~/.claude/settings.json
# model + advisorModel), and that pair is rejected outright ("claude-fable-5 cannot
# be used as an advisor when the request model is claude-fable-5-1"). The CLI prints
# that on STDOUT and exits non-zero, which is why logs/ops/agent-errors.log shows
# nothing after 07-31 while reports silently stopped on 09-24. Override: NIGHTLY_MODEL.
NIGHTLY_MODEL="${NIGHTLY_MODEL:-claude-opus-5}"
mkdir -p "$ROOT/logs/ops" "$ROOT/logs/journal"

# Per-step wall clock (2026-10-21) — see scripts/ops/step_timing.sh for why and
# for the fail-safe contract. Sourcing is itself optional: if the library is
# missing the chain runs exactly as before with timing off.
TIMING_FILE="$ROOT/logs/ops/nightly_timing-$DATE.jsonl"
# shellcheck source=scripts/ops/step_timing.sh
# `[ ! -r ... ] ||` IS REQUIRED, and the bare `if ! . missing` form that stood
# here until 2026-10-30 did NOT deliver the fail-safe this comment promises.
# MEASURED on this host (bash 3.2.57, arm64-darwin25): `.` on an unreadable file
# EXITS a non-interactive shell outright — even inside an `if !` condition, even
# under no `set -e`. A one-line probe printed NEITHER the fallback nor the line
# after the `fi`, and rc was 1 with stdout empty and stderr swallowed by the
# `2>/dev/null` above. So a missing step_timing.sh would have killed the entire
# nightly chain HERE, before ops_checks ran and before step_alert is even
# defined — silently, with no page and no log line. The readability test keeps
# the source call from ever being reached in that case.
if [ ! -r "$ROOT/scripts/ops/step_timing.sh" ] || ! . "$ROOT/scripts/ops/step_timing.sh" 2>/dev/null; then
    timing_now() { return 0; }
    emit_timing() { return 0; }
fi
CHAIN_START=$(timing_now || true)

# Honest completion line (2026-10-30) — see scripts/ops/report_completion.sh for
# the 10-29 false "nightly ops complete" that named two absent files. Sourcing
# is optional on the same contract as the timing library: missing file => the
# old unconditional claim, chain otherwise unchanged.
# shellcheck source=scripts/ops/report_completion.sh
# The `[ ! -r ... ] ||` guard is required for the reason measured at the
# step_timing source above — `.` on an unreadable file exits the shell.
if [ ! -r "$ROOT/scripts/ops/report_completion.sh" ] || ! . "$ROOT/scripts/ops/report_completion.sh" 2>/dev/null; then
    completion_line() { echo "nightly ops complete: $1, $2"; return 0; }
fi

# Item-0 (2026-08-03): chain-step failure must reach the phone. Each step's
# exit code is checked; failure alerts but the chain CONTINUES (degraded ops
# beat no ops). FORCE_STEP_FAIL=<name> is the Game-Day drill hook.
step_alert() {  # step_alert <step-name> <exit-code>
    "$ROOT/scripts/ops/ntfy_send.sh" "NIGHTLY CHAIN STEP FAILED: $1" urgent rotating_light \
        "step $1 exited $2 — chain continued; investigate logs/ops + logs/learning. UNVALIDATED - PAPER ONLY" || true
    echo "$(date '+%F %T') STEP-FAIL $1 (exit $2)"
}
run_step() {  # run_step <name> <command...>
    local name="$1"; shift
    if [ "${FORCE_STEP_FAIL:-}" = "$name" ]; then
        step_alert "$name" "forced(drill)"; return 0
    fi
    # `"$@" || rc=$?` NOT a bare `"$@"`: under `set -e` a bare failing command
    # aborts the script BEFORE `rc=$?` is assigned, which would silently destroy
    # this chain's whole reason for existing (degraded ops beat no ops). Verified
    # 2026-08-04: the bare form died at the first failing step with no alert.
    local rc=0 start
    start=$(timing_now || true)
    "$@" || rc=$?
    # `|| true` is load-bearing, not decoration: if the timing library is absent
    # (or run_step is lifted into a test harness without it) an undefined
    # function is rc=127, which `set -e` would turn into a dead chain.
    emit_timing "$name" "$start" "$rc" || true
    if [ $rc -ne 0 ]; then step_alert "$name" "$rc"; fi
    return 0
}

# 1. facts
run_step ops_checks sh -c "\"$ROOT/.venv/bin/python\" \"$ROOT/scripts/ops_checks.py\" > \"$ROOT/logs/ops/checks-$DATE.json\" 2> \"$ROOT/logs/ops/checks-$DATE.err\""

# 1a. Durable bars5m refresh keyed on TRADED (ticker,day) — closes the no_path
# gap at the source so the pre-gate can measure real drift (not the age fallback)
# on names outside the liquidity universes (rvolrank top-20s / backfill top-200).
# Idempotent: covered pairs skip the Polygon call. Must run BEFORE the CLL nightly
# so newly-traded names are covered when post-mortems are built.
run_step bars5m_refresh sh -c "\"$ROOT/.venv/bin/python\" \"$ROOT/scripts/bars5m_traded_refresh.py\" --write >> \"$ROOT/logs/learning/bars5m_traded-$DATE.log\" 2>&1"

# 1b. CLL Tier-1 nightly (deterministic: ingest -> classify -> ledger + metric
# integrity). Counts-only; MAY NOT propose (guard-enforced). An INTEGRITY ALARM
# sets a hold that blocks tiers 2/3 and quarantines the batch — clear only via
# scripts/learning/clear_hold.py with a logged reason.
mkdir -p "$ROOT/logs/learning"
run_step cll_nightly sh -c "\"$ROOT/.venv/bin/python\" \"$ROOT/scripts/learning/nightly.py\" >> \"$ROOT/logs/learning/nightly-$DATE.log\" 2>&1"

# 1c. Buy-zone radar (scripts/watch_buyzone.py) as its OWN chain step
# (2026-09-13). Until now the radar ran ONLY as the last line of the hourly
# travel_catchup.sh sweep, piped through `| tail -1` — its nightly summary was
# discarded and a 5-day outage (09-08..09-12, sweep aborted upstream) alarmed
# nothing. Here a failure is a STEP-FAIL page and never blocks other steps;
# full stdout persists in logs/alerts/radar-$DATE.log (the hourly sweep appends
# to the same file). Idempotent with the sweep's run. The radar's own summary
# row goes to logs/alerts/summary.jsonl (collection_expectations:
# buyzone_radar_daily). Placed before the agent interpretation; its network
# calls are all bounded (Polygon 5 tries x 30s + backoff per name, AV 20s,
# yfinance 30s, ntfy 10s), so even a full Polygon outage delays the nightly
# report by ~25 min for 8 names and never kills it. The checks JSON above does
# not see this run (18:40 chain) — the expectation's due_after accounts for
# that. A radar already running from the hourly sweep is skipped by the
# script's own flock run lock (exit 0, no page).
mkdir -p "$ROOT/logs/alerts"
run_step buyzone_radar sh -c "\"$ROOT/.venv/bin/python\" \"$ROOT/scripts/watch_buyzone.py\" >> \"$ROOT/logs/alerts/radar-$DATE.log\" 2>&1"

# 1d. Radar OUTCOME WRITER (scripts/radar_outcomes.py, 2026-09-13) — logging
# only, no rule changes. For every instance the radar opened (archive + hot
# event log, fixture excluded) it appends forward close-to-close outcomes at
# +3/+5/+10 store sessions — frozen-at-entry invalidation breach, MAE/MFE —
# to logs/alerts/outcomes.jsonl, idempotently (a row is never rewritten).
# Runs AFTER the radar step so tonight's instances are in the log, in its
# own log file; a failure is a STEP-FAIL page and never blocks the chain.
# Every row is stamped "exploratory, close-to-close, no costs — NO EDGE CLAIM".
run_step radar_outcomes sh -c "\"$ROOT/.venv/bin/python\" \"$ROOT/scripts/radar_outcomes.py\" >> \"$ROOT/logs/alerts/outcomes-$DATE.log\" 2>&1"

# 1e. ENTRY-TIMING SCAN (scripts/entry_timing_scan.py, 2026-09-13) — decision
# support for DISCRETIONARY entries, not a signal. Answers "is today a
# historically bad day to enter this name" from the strategy-chase 630-cell
# grid's measured entry-timing asymmetry (chasing strength costs ~3x what
# buying weakness earns). Pushes only when a watched name is flagged; every
# scan appends to logs/alerts/entry_timing.jsonl so it can be scored forward
# and withdrawn if it stops working. Never blocks the chain.
run_step entry_timing sh -c "\"$ROOT/.venv/bin/python\" \"$ROOT/scripts/entry_timing_scan.py\" --notify >> \"$ROOT/logs/alerts/entry_timing-$DATE.log\" 2>&1"

# 1f. SCALP-v2 MECHANICS (scripts/scalp_v2_mechanics.py, 2026-09-14) — the
# standing v1->v2 assessment the operator asked for: per-session and cumulative
# fill rate, spreads paid, attempt concentration, exit-reason mix and truth vs
# booked P&L, each beside its pre-committed prediction. Descriptive only — the
# frozen gate stays scalp_forward_check --v2. Writes logs/ops/scalp_v2_mechanics-
# $DATE.{json,md} and pushes one summary line. Runs AFTER the 17:45 enrichment
# (this chain is 18:40) so tonight's truth scores are included. Never blocks.
run_step scalp_v2_mechanics sh -c "\"$ROOT/.venv/bin/python\" \"$ROOT/scripts/scalp_v2_mechanics.py\" --write --notify >> \"$ROOT/logs/ops/scalp_v2_mechanics-run-$DATE.log\" 2>&1"

# Nightly Nasdaq Data Link mirror (SEP/TICKERS/EVENTS/SF2 + NDAQ/RTAT10) for the
# watchlist universe into data/nasdaq/. EOD/offline research data only — never a
# cycle-time call. Incremental from each table's own last date with a 7-day
# re-fetch overlap, so a skipped night self-heals and late filings are not lost
# in a seam. Each table is written as it completes, so a partial run is durable
# and one table's 403/5xx never aborts another; rc=1 reports per-table errors.
run_step nasdaq_mirror sh -c "\"$ROOT/.venv/bin/python\" \"$ROOT/scripts/nasdaq_mirror.py\" >> \"$ROOT/logs/ops/nasdaq_mirror-$DATE.log\" 2>&1"

# 2. read-only agent interpretation
PROMPT="$(cat "$ROOT/agents/nightly-ops-charter.md")

Today: $DATE. The checks JSON is at logs/ops/checks-$DATE.json — read it first.
Working directory: $ROOT"

# NON-FATAL (2026-09-29). This was a bare command substitution under `set -e`, so a
# non-zero CLI exit aborted the whole chain here — no report, no journal, no ntfy,
# no "nightly ops complete" line. That is exactly how 09-24/09-25/09-28 lost their
# reports while every run_step step had already succeeded. Capture the rc instead
# and fall through to the empty-OUT branch, which already pages a usable message.
agent_rc=0
# Timed like a run_step, but by hand: this step is deliberately NOT wrapped (it
# owns $OUT). Both timestamps are taken OUTSIDE the command substitution — an
# echo inside it would land in $OUT and corrupt the ===READINESS=== split below.
AGENT_START=$(timing_now || true)
OUT=$(cd "$ROOT" && "$CLAUDE_BIN" -p "$PROMPT" \
    --model "$NIGHTLY_MODEL" \
    --allowedTools "Read,Grep,Glob" \
    2>>"$ROOT/logs/ops/agent-errors.log") || agent_rc=$?
emit_timing agent_interpretation "$AGENT_START" "$agent_rc" || true
if [ "$agent_rc" -ne 0 ]; then
    # the CLI reports startup failures (bad model/advisor pair, auth) on STDOUT,
    # so record $OUT here or the reason is lost entirely
    { echo "=== $DATE: agent exited $agent_rc (model=$NIGHTLY_MODEL) ==="; \
      echo "$OUT"; } >> "$ROOT/logs/ops/agent-errors.log"
    step_alert agent_interpretation "$agent_rc"
    OUT=""
fi

if [ -z "$OUT" ]; then
    BODY="nightly ops agent produced no output — see logs/ops/agent-errors.log"
    echo "$BODY"
elif ! echo "$OUT" | grep -q '^===READINESS==='; then
    # marker-less output (CLI update banner, format drift) — capture it instead
    # of silently splitting into empty report/journal files (bit us 2026-07-16)
    { echo "=== $DATE: agent output missing ===READINESS=== marker ==="; \
      echo "$OUT"; } >> "$ROOT/logs/ops/agent-errors.log"
    BODY="nightly ops agent output missing READINESS marker — raw output in logs/ops/agent-errors.log"
    echo "$BODY"
else
    # 3. split + persist (wrapper owns writes)
    echo "$OUT" | awk '/^===READINESS===/{f=1;next} /^===JOURNAL===/{f=0} f' \
        > "$ROOT/logs/ops/report-$DATE.md"
    echo "$OUT" | awk '/^===JOURNAL===/{f=1;next} f' \
        > "$ROOT/logs/journal/$DATE.md"
    BODY=$(head -13 "$ROOT/logs/ops/report-$DATE.md")
fi

# 3b. Cockpit staleness self-heal (2026-10-28). The cockpit API was found
# running code older than the code it serves on 09-29, 10-02, 10-05, 10-08 and
# 10-28 — every time a human typed `launchctl kickstart`, which is why it kept
# recurring. This step is the automatic hand. Placed AFTER the report is split
# so tonight's report still states the staleness honestly, and the restart is
# what makes TOMORROW's read fresh. run_step already traps a non-zero rc and
# pages instead of aborting the chain (set -e safe).
run_step cockpit_stale sh -c "\"$ROOT/.venv/bin/python\" \"$ROOT/scripts/ops/cockpit_stale_kickstart.py\" >> \"$ROOT/logs/ops/cockpit_staleness-$DATE.log\" 2>&1"

# 4. notify (UNVALIDATED stamp; every ntfy target from .env via the shared sender)
"$ROOT/scripts/ops/ntfy_send.sh" "OptionsAgent nightly ops - UNVALIDATED PAPER" default clipboard "$BODY
UNVALIDATED - PAPER ONLY" || true
emit_timing chain_total "$CHAIN_START" 0 || true
# `|| true` is load-bearing: rc 1 here means "no report tonight", which is a
# description, not a chain failure — the failing step already paged.
completion_line "$ROOT/logs/ops/report-$DATE.md" "$ROOT/logs/journal/$DATE.md" || true
