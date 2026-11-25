#!/bin/bash
# Merge the RVOL-EXEC paper lane into main — Monday 2026-09-14 AFTER 13:00 PT.
#
# Written as a script (2026-09-13) because the scheduled wake-up that was going
# to do this lives only in a Claude session, and the operator is restarting the
# IDE. This survives; the cron did not.
#
# It is SAFE to run unattended: it merges only if everything is green, and
# aborts back to the pre-merge commit otherwise. It loads no launchd job,
# connects to no broker and places no order — merging alone cannot trade,
# because the lane's plist is a file on disk that nobody has loaded.
#
#   bash scripts/ops/merge_rvol_exec_lane.sh
#
set -euo pipefail
ROOT="/Users/you/projects/OptionsAgent"
PY="$ROOT/.venv/bin/python"
BRANCH="rvol-exec-lane"
cd "$ROOT"

say() { echo "$(date '+%F %T') $*"; }
fail() { say "STOP: $*"; exit 1; }

# ---- gate 1: the session must be over -------------------------------------
HHMM=$(date +%H%M)
if [ "$(date +%u)" -le 5 ] && [ "$HHMM" -lt 1300 ]; then
    fail "market session not over (local $HHMM). The options engine trades until 13:00 PT."
fi

# ---- gate 2: clean tree, branch present ------------------------------------
[ -z "$(git status --porcelain)" ] || fail "working tree is dirty — commit or stash first"
git rev-parse --verify "$BRANCH" >/dev/null 2>&1 || fail "branch $BRANCH not found"
PRE=$(git rev-parse HEAD)
say "pre-merge HEAD $PRE"

# ---- gate 3: main must be green BEFORE we touch it -------------------------
say "running the suite on main…"
"$PY" -m pytest -q >/tmp/mrg_pre.txt 2>&1 || { tail -5 /tmp/mrg_pre.txt; fail "main is RED before the merge — fix that first"; }
say "main green: $(tail -1 /tmp/mrg_pre.txt)"

# ---- merge -----------------------------------------------------------------
say "merging $BRANCH --no-ff…"
git merge --no-ff "$BRANCH" -m "merge(rvol-exec): paper execution lane onto main (not loaded, not connected)

Merged after the 2026-09-14 close with the full suite and both session
simulators green. The launchd plist remains UNLOADED: merging cannot trade.
Next: Tuesday is a SHADOW day (data/lane_rvol/SHADOW forces dry-run),
Wednesday is the earliest first order session." \
    || fail "merge produced conflicts — resolve by hand, nothing was left half-merged"

# ---- gate 4: the merged tree must be green, incl. BOTH simulators ----------
say "running the suite on the merged tree…"
if ! "$PY" -m pytest -q >/tmp/mrg_post.txt 2>&1; then
    tail -8 /tmp/mrg_post.txt
    say "merged tree is RED — reverting to $PRE"
    git reset --hard "$PRE"
    fail "merge reverted; branch left intact for inspection"
fi
say "merged tree green: $(tail -1 /tmp/mrg_post.txt)"

for SIM in tests/test_v2_session_sim.py tests/rvol_exec/test_session_sim.py; do
    [ -f "$SIM" ] || { say "NOTE: $SIM not present on the merged tree"; continue; }
    say "simulator: $SIM"
    "$PY" -m pytest -q "$SIM" >/tmp/mrg_sim.txt 2>&1 || {
        tail -8 /tmp/mrg_sim.txt
        say "simulator RED — reverting to $PRE"
        git reset --hard "$PRE"
        fail "merge reverted on a failing simulator"
    }
done

# ---- gate 5: isolation must still hold ------------------------------------
grep -q 'LANES = ("funnel", "scalp")' src/engine/paper_trader.py \
    || { git reset --hard "$PRE"; fail "LANES changed — the options money path must not gain a lane; reverted"; }
[ ! -d data/lane/rvol ] || { git reset --hard "$PRE"; fail "data/lane/rvol exists — it would be globbed by the options go/no-go; reverted"; }
if launchctl list 2>/dev/null | grep -q rvolexec; then
    fail "an rvolexec launchd job is LOADED — it must not be, before the shadow day"
fi

# ---- arm the shadow day ----------------------------------------------------
mkdir -p data/lane_rvol
printf 'SHADOW — created %s by merge_rvol_exec_lane.sh.\nWhile this file exists the daemon ranks and logs would-be orders and places NOTHING.\nDelete it only for the first real paper session (Wed 2026-09-16 earliest).\n' \
    "$(date '+%F %T')" > data/lane_rvol/SHADOW
say "shadow file armed: data/lane_rvol/SHADOW"

say "MERGE COMPLETE. Plist still unloaded; Tuesday is SHADOW-only."
say "Remaining by hand: load the plist only when you want Wednesday's first orders,"
say "and run the go/no-go probes with --i-mean-it before that."
