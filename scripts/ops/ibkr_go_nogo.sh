#!/bin/bash
# DAILY PREFLIGHT (was pre-session GO/NO-GO for the shadow path, which the
# 2026-08-04 full flip to ibkr_paper superseded). Runs 06:34 PT weekdays in
# ADVISORY mode: probe green (both feeds), gateway up, D-account confirmed,
# kill state present and readable, B1 sentinel. Reports only.
#
# FAILURE SEMANTICS (frozen 2026-08-04): exit non-zero AND block — but block
# THE SHADOW ONLY. On any failed check:
#   * data/SHADOW_DISABLE is written with the failing check as the reason, so
#     the shadow path is DISARMED for the day (surgical, file-based).
#   * The LIVE session proceeds normally. It is never gated on this script.
#   * A phone alert states WHICH check failed.
# Live never waits on shadow; shadow never runs on a broken precondition.
#
# Checks:
#   a  probe green, BOTH feeds (stock + OPRA)
#   b  gateway connected + D-account (paper) confirmed
#   c  readonly/shadow mode flag matches today's declared intent
#   d  shadow-book file isolation intact (no shadow rec_id prefix in live files)
#   e  kill-switch state readable + reported; shadow honours it (runtime check)
#   f  yesterday's shadow reconcile clean (no orphans overnight)
#   g  B1 regression sentinel (get_orders = broker truth or None, never silent [])
#
# Usage: ibkr_go_nogo.sh [--intent readonly|shadow|none]
# set -euo pipefail (2026-08-04): -e abort on error, -u unset-var guard,
# -o pipefail so a failure ANYWHERE in a pipeline is the pipeline's status
# (a piped verdict silently read as success is how three exit-code misreads
# happened). Optional actions carry an explicit `|| true`.
set -euo pipefail
ROOT="/Users/you/projects/OptionsAgent"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/logs/ops/go_nogo-$(date +%F).log"
INTENT="readonly"
ADVISORY=0
# ADVISORY mode (2026-08-04): log all seven verdicts daily, NO phone alert, NO
# arming authority, SHADOW_DISABLE untouched. Check (d) — shadow-file isolation
# — thereby becomes a standing daily tripwire immediately, before the shadow
# path exists. Flip to GATING (alerts + arming) as a commissioning step when
# the shadow path arms.
for a in "$@"; do
    case "$a" in
        --advisory) ADVISORY=1 ;;
        readonly|shadow|none) INTENT="$a" ;;
    esac
done
if [ "${1:-}" = "--intent" ]; then INTENT="${2:-readonly}"; fi
mkdir -p "$ROOT/logs/ops" "$ROOT/data"

FAILED=""
H_DETAIL=""
say() { echo "$1" | tee -a "$LOG"; }
record() {  # record <id> <status> <detail>
    say "  [$2] ($1) $3"
    if [ "$2" = "FAIL" ]; then FAILED="${FAILED}${FAILED:+, }$1"; fi
    # (h)'s detail is needed at paging time to tell a real DIVERGENCE from a
    # book that merely COULD NOT BE CHECKED (2026-10-12) — different message,
    # and the unverifiable case must not shout "divergence" at the operator.
    if [ "$1" = "h" ]; then H_DETAIL="$3"; fi
    return 0
}

say "=== IBKR PRE-SESSION GO/NO-GO $(date '+%F %T %Z') — declared intent: $INTENT ==="

# ── a. probe green, both feeds ───────────────────────────────────────────────
# `|| PRC=$?` — a bare assignment would abort under `set -e` on a probe FAIL,
# losing every subsequent check and the verdict itself.
PRC=0
POUT=$("$PY" "$ROOT/scripts/ibkr_entitlement_check.py" 2>&1) || PRC=$?
PLAST=$(echo "$POUT" | tail -1)
if [ $PRC -eq 0 ]; then
    case "$PLAST" in
        *"BOTH FEEDS REAL-TIME"*) record a PASS "$PLAST" ;;
        *"tentative"*)            record a FAIL "probe only TENTATIVE (liveness unverified): $PLAST" ;;
        *)                        record a FAIL "probe green but unrecognized verdict: $PLAST" ;;
    esac
else
    record a FAIL "probe exit $PRC: $PLAST"
fi

# ── c. declared-intent flag matches reality ──────────────────────────────────
SD="$ROOT/data/SHADOW_DISABLE"
case "$INTENT" in
    shadow)
        if [ -f "$SD" ]; then
            record c FAIL "intent=shadow but SHADOW_DISABLE is already set: $(head -c 120 "$SD")"
        else
            record c PASS "intent=shadow; SHADOW_DISABLE clear"
        fi ;;
    readonly|none)
        record c PASS "intent=$INTENT; no shadow submits expected today" ;;
    *)
        record c FAIL "unknown intent '$INTENT' (expected readonly|shadow|none)" ;;
esac

# ── b, d, e, f, g (python) ───────────────────────────────────────────────────
while IFS='|' read -r cid status detail; do
    if [ -z "${cid:-}" ]; then continue; fi
    record "$cid" "$status" "$detail"
done < <("$PY" "$ROOT/scripts/ops/ibkr_go_nogo_checks.py" 2>/dev/null)

# ── verdict ──────────────────────────────────────────────────────────────────
if [ "$ADVISORY" -eq 1 ]; then
    if [ -n "$FAILED" ]; then
        say "VERDICT (ADVISORY): NO-GO — failed: $FAILED (intent=$INTENT)"
    else
        say "VERDICT (ADVISORY): GO — all checks passed (intent=$INTENT)"
    fi
    # C-9.2 (2026-09-25, operator-approved): a book/broker DIVERGENCE pages even in
    # advisory mode. On 09-23 check (h) caught 3 phantom positions + an AMZN
    # quantity mismatch at 06:34 and this branch told no one; the engine then
    # tried to close positions the broker did not hold. Advisory still writes no
    # HOLD and arms nothing — it just stops being silent about the one check
    # whose failure means the exit manager is flying on a wrong book.
    case ", $FAILED," in
        *", h,"*)
            # 2026-10-12: (h) now FAILS when the broker cannot be read at all
            # (it used to print a false PASS). Two very different failures share
            # this branch, so split them:
            #   CANNOT VERIFY + gateway already failed (a/b) -> one cause, and
            #     the gateway is paged by the probe, the open check, the runbook
            #     and the 30-min deadman already. A 12th page saying
            #     "DIVERGENCE" for a dead port is both wrong and the way pages
            #     get ignored. Log it, do not page.
            #   CANNOT VERIFY with the gateway otherwise fine -> page, but say
            #     UNVERIFIABLE, not DIVERGENCE.
            #   a real divergence -> unchanged, urgent (C-9.2).
            case "$H_DETAIL" in
                *"CANNOT VERIFY"*)
                    case ", $FAILED," in
                        *", a,"*|*", b,"*)
                            say "ADVISORY MODE: (h) could not be checked because the gateway is down (a/b also failed) — NOT paged separately; the gateway itself is paged by the probe and the deadman." ;;
                        *)
                            "$ROOT/scripts/ops/ntfy_send.sh" "BOOK/BROKER UNVERIFIABLE (advisory go/no-go)" urgent rotating_light \
                                "check (h) could NOT verify the book at $(date '+%H:%M %Z') — the broker position read failed while the gateway looked otherwise healthy, so book staleness is UNKNOWN. Details: $ROOT/logs/ops/go_nogo-$(date +%F).log
No HOLD written (advisory). Verify the book before the open.
UNVALIDATED - PAPER ONLY" || true
                            say "ADVISORY MODE: (h) UNVERIFIABLE paged; SHADOW_DISABLE untouched, no arming authority." ;;
                    esac ;;
                *)
                    "$ROOT/scripts/ops/ntfy_send.sh" "BOOK/BROKER DIVERGENCE (advisory go/no-go)" urgent rotating_light \
                        "check (h) FAILED at $(date '+%H:%M %Z'): the engine's book does not match IBKR. Details: $ROOT/logs/ops/go_nogo-$(date +%F).log
No HOLD written (advisory). Verify the book before the open.
UNVALIDATED - PAPER ONLY" || true
                    say "ADVISORY MODE: divergence PAGED (check h); SHADOW_DISABLE untouched, no arming authority." ;;
            esac
            ;;
        *)
            say "ADVISORY MODE: no alert sent, SHADOW_DISABLE untouched, no arming authority."
            ;;
    esac
    exit 0
fi

if [ -n "$FAILED" ]; then
    REASON="GO/NO-GO failed: $FAILED (intent=$INTENT)"
    say "VERDICT: NO-GO — $REASON"
    say "ACTION : SHADOW DISARMED for today. LIVE SESSION PROCEEDS NORMALLY."
    "$PY" - "$REASON" <<'PYEOF' 2>/dev/null
import sys
sys.path.insert(0, "/Users/you/projects/OptionsAgent")
from src.engine.shadow_control import disable_shadow
print(disable_shadow(sys.argv[1], source="go_nogo"))
PYEOF
    "$ROOT/scripts/ops/ntfy_send.sh" "IBKR GO/NO-GO: NO-GO — shadow disarmed" high warning "$REASON
Shadow is DISARMED for today. The live session proceeds normally.
UNVALIDATED - PAPER ONLY" || true
    exit 1
fi

say "VERDICT: GO — all checks passed (intent=$INTENT)"
"$ROOT/scripts/ops/ntfy_send.sh" "IBKR GO/NO-GO: GO ($INTENT)" low white_check_mark \
    "All pre-session checks passed (intent=$INTENT).
UNVALIDATED - PAPER ONLY" || true
exit 0
