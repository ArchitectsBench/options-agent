#!/bin/bash
# STANDING RUNBOOK — scheduled pre-open orchestrator (ratified 2026-08-08).
#
# Executes the full runbook every market day so NO HUMAN MESSAGE IS LOAD-BEARING
# on a normal morning. On 2026-08-07 the engine sat down for 2.5h of a session
# because a start depended on a conversation that did not happen in time.
#
# EVERY GATE IS A STOP CONDITION. On any failure the engine STAYS DOWN and an
# urgent, SESSION-AWARE alert fires (O-21 machinery). Silence means it worked.
set -uo pipefail
ROOT="/Users/you/projects/OptionsAgent"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/logs/ops/standing_runbook-$(date +%F).log"
mkdir -p "$ROOT/logs/ops" "$ROOT/ops"
exec >>"$LOG" 2>&1
echo "=== STANDING RUNBOOK $(date '+%F %T %Z') ==="

# ── IBKR clientId allocation (de facto registry — keep this comment current) ─
# Each client retries cid, cid+10, cid+20, cid+30 (src/data/ibkr_orders.py:170),
# so allocations must be clear of each other's LADDERS, not just each other.
#   42 → {42,52,62,72}    trading engine (DEFAULT_CLIENT_ID) — holds 42 all session
#   61                    RVOL-EXEC lane (stock client, no ladder)
#   91 → {91,101,111,121} scripts/ops_checks.py (moved off 42 on 2026-09-29)
#   93 → {93,103,113,123} THIS runbook's read-only book checks (2026-10-05)
# WHY 93 EXISTS: gates 2 and 6 called audit_book_vs_broker_checked() with no
# client, so they connected on 42 — which the engine already holds, because the
# 06:20 restart precedes this 06:31 run. The ladder usually recovered on 52
# (hence the harmless Error 326 pairs in logs through 2026-10-02), but on
# 2026-10-05 a slow gateway let the ladder run past the worker-ready deadline:
# the check returned could_not_check, gate 2 correctly refused to call that a
# pass, and the operator got a FALSE urgent "Engine STAYS DOWN" page while the
# engine was live, flat and cycling. The gate logic was right; the clientId was.
RUNBOOK_CLIENT_ID=93

notify() {  # notify <title> <priority> <msg>
    "$ROOT/scripts/ops/ntfy_send.sh" "$1" "$2" "" "$3
UNVALIDATED - PAPER ONLY" || true
}
stop() {  # stop <gate> <detail>  — engine stays down, urgent alert
    echo "GATE FAILED [$1]: $2"
    notify "RUNBOOK STOPPED at $1" "urgent" "Engine STAYS DOWN. $2"
    exit 1
}

# ── GATE 0: HOLD file ───────────────────────────────────────────────────────
# `touch ops/HOLD` any evening keeps the next open dark without a conversation.
if [ -f "$ROOT/ops/HOLD" ]; then
    REASON=$(cat "$ROOT/ops/HOLD" 2>/dev/null | head -1)
    echo "HELD by operator: ${REASON:-no reason recorded}"
    notify "Runbook HELD by operator" "low" \
        "ops/HOLD present — engine intentionally NOT started. ${REASON:-}
Remove the file to re-arm the next open."
    exit 0
fi

# ── GATE 1: session window ──────────────────────────────────────────────────
[ "$("$PY" "$ROOT/scripts/ops/liveness.py" session_open)" = "1" ] \
    || { echo "outside the session window — nothing to do"; exit 0; }

# ── GATE 2: STEP 0 stale-book ───────────────────────────────────────────────
# COULD-NOT-CHECK IS NOT A PASS (2026-08-17). This previously called
# audit_book_vs_broker(), which returns [] both when clean and when the broker
# is unreachable — so on 08-17 it passed while the gateway sat unauthenticated
# and a phantom WMT position was in the book.
"$PY" -c "
import sys; sys.path.insert(0,'$ROOT')
from src.engine.paper_trader import audit_book_vs_broker_checked, assert_shared_books_retired
from src.data.ibkr_orders import IbkrOrderClient
assert_shared_books_retired()
r = audit_book_vs_broker_checked(IbkrOrderClient(client_id=$RUNBOOK_CLIENT_ID))
if r['status'] == 'could_not_check':
    raise SystemExit('COULD NOT VERIFY BOOK: %s' % r.get('detail'))
raise SystemExit(0 if r['status'] == 'clean' else 'book/broker divergence: %s' % r['alarms'])
" || stop "STEP-0-stale-book" "book state not VERIFIED clean — divergence or unverifiable broker"

# ── GATE 3: broker confirm ──────────────────────────────────────────────────
BROKER=$("$PY" -c "
import sys; sys.path.insert(0,'$ROOT')
from src.data.order_client import _resolve_broker; print(_resolve_broker())")
[ "$BROKER" = "ibkr_paper" ] || stop "broker-confirm" "broker is '$BROKER', expected ibkr_paper (sandbox fills are FANTASY)"

# ── GATE 4: bootstrap (bootout UNLOADS; kickstart cannot revive it) ─────────
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.optionsagent.trader.plist 2>/dev/null
sleep 5

# ── GATE 5: job-loaded + process-alive + FIRST CYCLE COMPLETED ──────────────
bash "$ROOT/scripts/ops/assert_engine_live.sh" || stop "engine-live-assertion" \
    "job/process/first-cycle assertion failed — 'start issued' is not a start"

# ── GATE 6: zero-alarm startup acceptance ───────────────────────────────────
"$PY" -c "
import sys; sys.path.insert(0,'$ROOT')
from src.engine.paper_trader import audit_book_vs_broker_checked
from src.data.ibkr_orders import IbkrOrderClient
r = audit_book_vs_broker_checked(IbkrOrderClient(client_id=$RUNBOOK_CLIENT_ID))
if r['status'] == 'could_not_check':
    raise SystemExit('COULD NOT VERIFY BOOK after start: %s' % r.get('detail'))
raise SystemExit(0 if r['status'] == 'clean' else 'post-start alarms: %s' % r['alarms'])
" || stop "startup-acceptance" "post-start book state not VERIFIED clean"

echo "RUNBOOK COMPLETE — engine live, all gates passed"
notify "Runbook OK — engine live" "low" "All gates passed; session running."
