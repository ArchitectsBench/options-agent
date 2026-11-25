#!/bin/bash
# C-12.1 (2026-09-28, operator "go") — WAIT for the engine's broker client to
# activate after the 06:20 restart, and re-kick the engine when the gateway
# becomes reachable.
#
# WHY. On 2026-09-28 IB Gateway was running but NOT LOGGED IN (weekend re-auth;
# port 4002 closed). The engine connects once at startup; that attempt failed and
# nothing re-tried, so SCALP-v3's first session ran 78 cycles with no broker. The
# restart job DID notice ("BROKER UNCONFIRMED") — then exited. A correct page
# that nobody can act on in ten minutes is not a control.
#
# WHAT. Until the deadline (default 06:45 PT):
#   * activation line present in agent.log (after this job's start)  -> exit 0
#   * gateway port CLOSED -> page ONCE with the exact remedy, keep waiting
#   * gateway port OPEN but no activation -> kickstart the engine (its startup
#     connect now succeeds), at most once per MIN_KICK_GAP_S, keep waiting
#   * deadline reached -> final urgent page, exit 1
# Re-kicking before the open is safe: the book is flat until the first entry
# (the caller's gate 3 already refused to run if a live position existed). A kick
# after 06:30 costs at most the cycles already logged; it never touches orders.
#
# HARD EXIT: launchd calendar jobs have no timeout of their own, so the loop is
# bounded by DEADLINE_HM *and* by MAX_WAIT_S.
#
# Env (tests/drills): AGENT_LOG, GATEWAY_HOST, GATEWAY_PORT, DEADLINE_HM, POLL_S,
# MAX_WAIT_S, MIN_KICK_GAP_S, SINCE_HMS, AWAIT_DRY_RUN=1 (no kickstart, no page).
# UNVALIDATED - PAPER ONLY.
set -uo pipefail

ROOT="/Users/you/projects/OptionsAgent"
AGENT_LOG="${AGENT_LOG:-$ROOT/logs/agent.log}"
HOST="${GATEWAY_HOST:-127.0.0.1}"
PORT="${GATEWAY_PORT:-4002}"
DEADLINE_HM="${DEADLINE_HM:-06:45}"
POLL_S="${POLL_S:-60}"
MAX_WAIT_S="${MAX_WAIT_S:-1800}"
MIN_KICK_GAP_S="${MIN_KICK_GAP_S:-120}"
DRY="${AWAIT_DRY_RUN:-0}"
TODAY="$(date +%F)"
SINCE_HMS="${SINCE_HMS:-$(date +%H:%M:%S)}"     # only an activation AFTER this counts

say() { echo "  [await-broker $(date +%H:%M:%S)] $*"; }
notify() {  # notify <title> <priority> <msg>
    if [ "$DRY" = "1" ]; then say "DRY-RUN page: [$2] $1 — $3"; return 0; fi
    "$ROOT/scripts/ops/ntfy_send.sh" "$1" "$2" "" "$3
UNVALIDATED - PAPER ONLY" || true
}
activated() {   # an 'activated paper account' line stamped today at/after SINCE_HMS
    [ -f "$AGENT_LOG" ] || return 1
    awk -v d="$TODAY" -v t="$SINCE_HMS" \
        '$1==d && substr($2,1,8)>=t && /activated paper account/ {f=1} END{exit f?0:1}' "$AGENT_LOG"
}
port_open() { nc -z -w 3 "$HOST" "$PORT" >/dev/null 2>&1; }
kick() {
    if [ "$DRY" = "1" ]; then say "DRY-RUN kickstart"; return 0; fi
    launchctl kickstart -k "gui/$(id -u)/com.optionsagent.trader"
}
past_deadline() { [ "$(date +%H:%M)" \> "$DEADLINE_HM" ] || [ "$(date +%H:%M)" = "$DEADLINE_HM" ]; }

START=$(date +%s); LAST_KICK=0; PAGED_CLOSED=0; KICKS=0
say "waiting for broker activation (since $SINCE_HMS, deadline $DEADLINE_HM, port $HOST:$PORT)"
while :; do
    if activated; then
        say "broker client ACTIVATED (kicks: $KICKS)"
        [ "$KICKS" -gt 0 ] && notify "Broker connected after retry" "default" \
            "The engine's broker client activated after $KICKS re-kick(s). Execution path live."
        exit 0
    fi
    NOW=$(date +%s)
    if past_deadline || [ $((NOW - START)) -ge "$MAX_WAIT_S" ]; then
        say "DEADLINE: no broker activation by $DEADLINE_HM (kicks: $KICKS, port $(port_open && echo open || echo CLOSED))"
        notify "NO BROKER at the open — engine will trade NOTHING" "urgent" \
            "No broker activation by $DEADLINE_HM. Gateway port $PORT is $(port_open && echo open || echo CLOSED). Log in to IB Gateway (paper), then run: launchctl kickstart -k gui/$(id -u)/com.optionsagent.trader"
        exit 1
    fi
    if port_open; then
        if [ $((NOW - LAST_KICK)) -ge "$MIN_KICK_GAP_S" ]; then
            say "gateway port open but no activation — re-kicking the engine"
            kick || say "kickstart returned non-zero"
            LAST_KICK=$NOW; KICKS=$((KICKS + 1))
        fi
    elif [ "$PAGED_CLOSED" -eq 0 ]; then
        say "gateway port CLOSED — paging the operator once"
        notify "LOG IN TO IB GATEWAY" "urgent" \
            "IB Gateway is not accepting connections on port $PORT (not logged in). Open IB Gateway and log in with the PAPER credentials. This job re-kicks the engine automatically until $DEADLINE_HM."
        PAGED_CLOSED=1
    fi
    sleep "$POLL_S"
done
