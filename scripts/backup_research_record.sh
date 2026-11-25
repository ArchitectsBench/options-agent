#!/bin/bash
# Nightly backup of the IRREPLACEABLE research record (gap item 2, 2026-07-09).
#
# TWO-STAGE (2026-07-10 fix): macOS TCC denies launchd jobs write access to
# ~/Library/CloudStorage (the first scheduled run failed with 'Operation not
# permitted' on every path, while interactive runs succeed). So:
#   STAGE 1  rsync -> ~/Backups/OptionsAgent   (local staging; TCC-free, always works)
#   STAGE 2  rsync staging -> Google Drive     (best-effort; succeeds interactively,
#            denied under launchd until EITHER the staging folder is added to
#            Google Drive for desktop's synced folders (preferred, one-time,
#            Preferences -> My Computer -> Add folder) OR bash/rsync get Full
#            Disk Access (broader; not recommended).
# Off-machine durability comes from stage 2 / the Drive app syncing the staging
# folder. ops_checks.backup_freshness alarms on staging failure/staleness and
# reports cloud state separately.
#
# Scope: logs/ (evidence ledgers), config/ docs/ agents/, data state *.json,
# trendbook/, iv_history/ dailies. NEVER .env (secrets), never the big
# re-downloadable stores. No --delete (local corruption must not propagate).
# set -euo pipefail (2026-08-04): -e abort on error, -u unset-var guard,
# -o pipefail so a failure ANYWHERE in a pipeline is the pipeline's status
# (a piped verdict silently read as success is how three exit-code misreads
# happened). Optional actions carry an explicit `|| true`.
set -euo pipefail
ROOT="/Users/you/projects/OptionsAgent"
STAGING="$HOME/Backups/OptionsAgent"
CLOUD="$HOME/Library/CloudStorage/GoogleDrive-you@example.com/My Drive/OptionsAgent-backup"
STAMP=$(date "+%Y-%m-%dT%H:%M:%S")

# ---- STAGE 1: local staging (must succeed) ----
S1=ok
mkdir -p "$STAGING/data" || S1=FAILED
rsync -a --exclude '.DS_Store' "$ROOT/logs/"   "$STAGING/logs/"   || S1=FAILED
rsync -a "$ROOT/config/" "$STAGING/config/" || S1=FAILED
rsync -a "$ROOT/docs/"   "$STAGING/docs/"   || S1=FAILED
rsync -a "$ROOT/agents/" "$STAGING/agents/" || S1=FAILED
rsync -a --include '*.json' --exclude '*' "$ROOT/data/" "$STAGING/data/" || S1=FAILED
rsync -a "$ROOT/data/trendbook/"  "$STAGING/data/trendbook/"  || S1=FAILED
rsync -a "$ROOT/data/iv_history/" "$STAGING/data/iv_history/" || S1=FAILED

# ---- CODE (added 2026-09-13) ----
# This script backed up docs, config, logs and evidence but NEVER the code.
# That was survivable only while `git push` worked; on 2026-09-13 the remote
# was found to be unreachable ("Repository not found") with 92 unpushed
# commits, so every line of src/, scripts/ and tests/ — and the whole git
# history — existed on exactly one disk.
# A git BUNDLE is a single file containing the complete history and restores
# with `git clone <file>`, so it rides the same rsync to the cloud.
git -C "$ROOT" bundle create "$STAGING/optionsagent-repo.bundle" --all >/dev/null 2>&1 \
    || S1=FAILED
# the exec-lane worktree lives on its own branch inside the same repo, so the
# --all bundle already carries it; belt-and-braces for the working tree itself:
rsync -a --exclude '__pycache__' "$ROOT/src/"     "$STAGING/src/"     || S1=FAILED
rsync -a --exclude '__pycache__' "$ROOT/scripts/" "$STAGING/scripts/" || S1=FAILED
rsync -a --exclude '__pycache__' "$ROOT/tests/"   "$STAGING/tests/"   || S1=FAILED

# ---- STAGE 2: cloud (best-effort; TCC may deny under launchd) ----
# 2026-10-20: the cloud leg reported FAILED for the first time in 15 days and the
# reason was UNDIAGNOSABLE, because the only copy of it went to /dev/null. Found
# by hand: ONE Drive-side locked file — `data/trendbook/state.json`, which refuses
# every writer with `Resource deadlock avoided` (openrsync AND plain `cp`, so it is
# a Google Drive mount lock, not an rsync flag) — and openrsync does not continue
# past a per-file error: it aborts the whole transfer with `unexpected end of file`,
# stranding every file after it in the list while the receipt said only "FAILED".
# So: keep the stderr (dated, so tomorrow's diagnosis is a file read), and on
# failure retry once excluding the deadlocked paths, reporting PARTIAL — one stuck
# file must not cost the night's whole offsite copy.
S2=FAILED
NSIDE=0   # initialised OUTSIDE the retry branch: `set -u` makes a reference to
          # an unset NSIDE in the receipt below an abort, and the branch that
          # increments it only runs on a deadlock night.
CLOUD_ERR="$ROOT/logs/ops/backup_cloud-$(date +%Y-%m-%d).err"
if [ "$S1" = "ok" ] && mkdir -p "$CLOUD" 2>/dev/null; then
    if rsync -a "$STAGING/" "$CLOUD/" 2>"$CLOUD_ERR"; then
        S2=ok
    else
        # openrsync prints: `rsync(123): error: <path>: mmap: Resource deadlock avoided`
        EXARGS=()
        STUCKS=()
        NSTUCK=0   # count files, NOT ${#EXARGS[@]} — that is 2 per file (flag + path)
        while IFS= read -r stuck; do
            [ -n "$stuck" ] && { EXARGS+=(--exclude "$stuck"); STUCKS+=("$stuck"); NSTUCK=$((NSTUCK + 1)); }
        done < <(sed -n 's/^rsync(\{0,1\}[0-9]*)\{0,1\}: *error: \(.*\): mmap: Resource deadlock avoided$/\1/p' \
                     "$CLOUD_ERR" | sort -u)
        if [ "$NSTUCK" -gt 0 ] \
           && rsync -a "${EXARGS[@]}" "$STAGING/" "$CLOUD/" 2>>"$CLOUD_ERR"; then
            S2="PARTIAL($NSTUCK locked)"
        fi
        # 2026-11-02: the deadlock is per DESTINATION FILE, not the directory —
        # proven by probe: `cp staging/state.json "$CLOUD/.../state.json.probe"`
        # SUCCEEDS while overwriting `state.json` itself still fails. So the
        # night's CONTENT can always reach offsite under a sidecar name even
        # though the canonical name is stuck (14 nights for
        # data/trendbook/state.json, cloud copy frozen at 2026-10-19). Additive
        # only: the locked original is never deleted or moved, and a dated name
        # is used if the sidecar name is itself locked.
        # Guarded on NSTUCK because `"${STUCKS[@]}"` on an EMPTY array under
        # `set -u` aborts on macOS bash 3.2 — and rsync can fail for reasons
        # that are not deadlocks, which is exactly the NSTUCK=0 case.
        if [ "$NSTUCK" -gt 0 ]; then
            for stuck in "${STUCKS[@]}"; do
                [ -f "$STAGING/$stuck" ] || continue
                mkdir -p "$(dirname "$CLOUD/$stuck")" 2>/dev/null || true
                DATED="$stuck.sidecar-$(date +%Y-%m-%d)"
                if cp "$STAGING/$stuck" "$CLOUD/$stuck.sidecar" 2>>"$CLOUD_ERR"; then
                    echo "sidecar: $stuck -> $stuck.sidecar ok" >>"$CLOUD_ERR"
                    NSIDE=$((NSIDE + 1))
                elif cp "$STAGING/$stuck" "$CLOUD/$DATED" 2>>"$CLOUD_ERR"; then
                    echo "sidecar: $stuck -> $DATED ok" >>"$CLOUD_ERR"
                    NSIDE=$((NSIDE + 1))
                else
                    echo "sidecar: $stuck FAILED (both names locked)" >>"$CLOUD_ERR"
                fi
            done
        fi
    fi
fi

# `sidecars` is an ADDED key, not a change to `cloud`: ops_checks computes
# cloud_ok from the cloud last_backup.json mtime and never parses this string,
# and persistent_cloud_locks() reads the dated .err files — so nothing
# downstream breaks, and a consumer that wants "the content did reach offsite"
# now has a number for it.
RECEIPT="{\"ts\": \"$STAMP\", \"staging\": \"$S1\", \"cloud\": \"$S2\", \"sidecars\": $NSIDE, \"staging_dir\": \"$STAGING\", \"cloud_dir\": \"$CLOUD\"}"
echo "$RECEIPT" > "$ROOT/logs/ops/last_backup.json"
echo "$RECEIPT" > "$STAGING/last_backup.json" 2>/dev/null || true
echo "$STAMP backup staging=$S1 cloud=$S2"
[ "$S1" = "ok" ]
