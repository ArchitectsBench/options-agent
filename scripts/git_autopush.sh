#!/bin/bash
# Daily GitHub sync — keeps origin/main current with the working tree.
# Installed as launchd job com.optionsagent.gitautopush (daily 19:00 PT).
# Safety: .env / logs/ / data/ are gitignored, so `git add -A` can only stage
# code, config, and docs. Refuses to act mid-merge/rebase or off main.
# set -euo pipefail (2026-08-04): -e abort on error, -u unset-var guard,
# -o pipefail so a failure ANYWHERE in a pipeline is the pipeline's status
# (a piped verdict silently read as success is how three exit-code misreads
# happened). Optional actions carry an explicit `|| true`.
set -euo pipefail
REPO="/Users/you/projects/OptionsAgent"
cd "$REPO" || exit 1

echo "=== git_autopush $(date '+%Y-%m-%d %H:%M:%S') ==="

# Never fight a manual git operation in progress.
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ] || [ -f .git/MERGE_HEAD ]; then
    echo "SKIP: merge/rebase in progress"
    exit 0
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "main" ]; then
    echo "SKIP: on branch '$BRANCH', not main"
    exit 0
fi

# Session-sentinel guard (2026-08-03 — stability-audit R-class config-drift fix,
# after 0f2a50d auto-committed a mid-audit working tree): refuse to AUTO-COMMIT a
# possibly-mid-edit tree while a Claude Code session is active. The sentinel is
# created by a SessionStart hook and removed on a clean SessionEnd; a crashed
# session leaves it behind — fail-safe (skip an auto-commit rather than commit
# half-done work). Existing clean commits are still PUSHED below. Deliberate WIP
# checkpoint override: AUTOPUSH_FORCE=1 ./scripts/git_autopush.sh  (or --force).
SENTINEL="$REPO/.claude/SESSION_ACTIVE"
FORCE=0
if [ "${AUTOPUSH_FORCE:-}" = "1" ]; then FORCE=1; fi
if [ "${1:-}" = "--force" ]; then FORCE=1; fi

if [ -n "$(git status --porcelain)" ]; then
    if [ -f "$SENTINEL" ] && [ "$FORCE" -ne 1 ]; then
        echo "SKIP COMMIT: Claude Code session active ($SENTINEL present) — not"
        echo "  auto-committing a possibly-mid-edit tree. Override: AUTOPUSH_FORCE=1 or --force."
    else
        if [ "$FORCE" -eq 1 ]; then echo "FORCE: committing despite session sentinel (deliberate WIP checkpoint)"; fi
        git add -A
        git commit -m "Auto-sync $(date '+%Y-%m-%d'): daily repo checkpoint

Unattended commit by scripts/git_autopush.sh (com.optionsagent.gitautopush)."
        echo "committed: $(git log -1 --format='%h %s')"
    fi
else
    echo "working tree clean"
fi

# Push anything unpushed (from this run or earlier manual commits).
if [ -n "$(git log origin/main..HEAD --oneline 2>/dev/null)" ]; then
    if git push origin main; then
        echo "pushed: origin/main now $(git rev-parse --short HEAD)"
    else
        echo "PUSH FAILED (offline or auth) — will retry on next run"
        exit 1
    fi
else
    echo "origin/main already up to date"
fi
