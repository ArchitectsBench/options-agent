#!/bin/bash
# Dead-man's switch (gap analysis 2026-07-09, item 1; hardened 2026-07-14 —
# "who watches the watchmen": the proactive-monitoring stack lives in the
# nightly ops sweep, so the sweep itself must be watched too).
#
# Runs every 10 minutes via launchd. Four layers:
#   1. AGENT alive?           (trader process from agent_state.json)
#   2. WATCHMAN fresh?        (nightly ops sweep ran within 26h on weekdays —
#                              the layer that carries collection_health,
#                              broker_canary, position_reconcile, etc.)
#   3. CYCLE advancing?       (R9: during market hours the engine's last_cycle
#                              timestamp must be recent — a hung cycle keeps the
#                              pid alive (so layer 1 passes) while positions go
#                              unmanaged all day. pid-exists is NOT enough.)
#   4. MACHINE alive?         (external healthchecks.io: silence = dead;
#                              requires DEADMAN_URL in .env — user setup,
#                              pre-travel checklist item)
#
# Failure alerts go to the phone via scripts/ops/ntfy_send.sh (ntfy.sh + the second server, both from .env)
# regardless of DEADMAN_URL, throttled to one alert per condition per 6h.
# Machine-death coverage still needs the external URL; nothing local can
# report its own host's death.
# set -euo pipefail (2026-08-04): -e abort on error, -u unset-var guard,
# -o pipefail so a failure ANYWHERE in a pipeline is the pipeline's status
# (a piped verdict silently read as success is how three exit-code misreads
# happened). Optional actions carry an explicit `|| true`.
set -euo pipefail
ROOT="/Users/you/projects/OptionsAgent"
URL=$(grep '^DEADMAN_URL=' "$ROOT/.env" 2>/dev/null | cut -d= -f2)
STATE_DIR="$ROOT/data/deadman"
mkdir -p "$STATE_DIR"

alert() {  # alert <condition-slug> <message> — SESSION-AWARE throttle (O-21)
    # O-21 (2026-08-07): "down" and "down DURING MARKET HOURS" are DIFFERENT
    # conditions. On 08-07 an agent-dead alert fired at 03:12 during legitimate
    # overnight engine-down work, and its 6h throttle then suppressed the SAME
    # condition across the 06:30 open — the engine was down for 2.5h of the
    # session in silence. A session-critical condition now gets its OWN slug
    # (-in-session) and a 30-min throttle, so an out-of-session alert can never
    # consume the window that matters.
    local cond="$1" msg="$2"
    local slug; slug=$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/liveness.py" slug "$cond" 2>/dev/null || echo "$cond")
    local thr;  thr=$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/liveness.py" throttle "$cond" 2>/dev/null || echo 21600)
    local stamp="$STATE_DIR/$slug.last"
    local now; now=$(date +%s)
    local last=0
    if [ -f "$stamp" ]; then last=$(cat "$stamp"); fi
    if [ $((now - last)) -lt "$thr" ]; then return 0; fi
    echo "$now" > "$stamp"
    echo "$(date '+%F %T') ALERT $slug: $msg"
    # Shared sender (2026-09-13): ntfy.sh AND the second server; per-target
    # HTTP codes land in this log instead of being discarded.
    "$ROOT/scripts/ops/ntfy_send.sh" "DEADMAN: $slug" urgent rotating_light "$msg" || true
}

FAIL=0

# 1. agent alive?
PID=$("$ROOT/.venv/bin/python" -c "
import json
try: print(json.load(open('$ROOT/data/agent_state.json')).get('pid') or '')
except Exception: print('')" 2>/dev/null)
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
    FAIL=1
    alert "agent-dead" "trader process (pid ${PID:-none}) not running"
fi

# 2. watchman fresh? (weekdays only; ops sweep runs 18:40 Mon-Fri). The age
# threshold is DAY-AWARE — see scripts/ops/lib_ops_sweep_stale.sh for why
# Monday tolerates 75h (Friday's run is the newest legitimate one until Monday
# 18:40) while Tue-Fri keep 26h. Before 2026-10-05 a flat 26h paged four false
# `ops-sweep-stale` alerts every Monday.
# FAIL-LOUD LIBRARY LOAD (2026-10-31). This source used to be bare, which was
# the one failure mode the whole deadman exists to prevent: this script runs
# under `set -euo pipefail` (line 27 — NOT absent, as was briefly believed
# because it is not in the first 12 lines), and measured on this host
# (bash 3.2.57, arm64-darwin25) a failed `.`/`source` under `set -e` exits the
# shell with rc=1. The plist invokes `/bin/bash deadman.sh`, so that is exactly
# production. An absent or unreadable library therefore killed the sweep HERE,
# after layer 1 only — layers 2, 2b, 3, 3e, 3f and 4 never ran, silently.
# Function presence is checked, not just readability: a truncated or zero-byte
# library sources cleanly and then every `if ops_sweep_stale ...` is false
# (127 -> false), which disables the staleness page with no trace at all.
# Deliberately fail-loud-and-CONTINUE rather than guard-with-a-fallback: a
# stubbed staleness check is worse than one that complains, but the later
# layers (cycle advancing, broker port, ephemeral ports, healthchecks ping) are
# load-bearing and must still run. alert() depends only on $ROOT
# (liveness.py + ntfy_send.sh), never on this library, so the page gets out.
# shellcheck source=scripts/ops/lib_ops_sweep_stale.sh
OPS_STALE_LIB="$ROOT/scripts/ops/lib_ops_sweep_stale.sh"
if [ -r "$OPS_STALE_LIB" ]; then
    source "$OPS_STALE_LIB" || true
fi
DOW=$(date +%u)
OPS_LOG="$ROOT/logs/launchd_nightlyops.log"
if ! declare -F ops_sweep_stale >/dev/null 2>&1 || ! declare -F ops_sweep_max_age_min >/dev/null 2>&1; then
    FAIL=1
    alert "deadman-lib-missing" "deadman could not load scripts/ops/lib_ops_sweep_stale.sh (unreadable, missing or truncated) — layer 2 (nightly-ops-sweep staleness) is DISABLED this tick and cannot page. Every other layer still ran. ACTION: restore the library from git (git checkout -- scripts/ops/lib_ops_sweep_stale.sh) and check the next deadman tick."
elif ops_sweep_stale "$OPS_LOG" "$DOW"; then
    FAIL=1
    alert "ops-sweep-stale" "nightly ops sweep (the proactive-monitoring layer) has not run in >$(( $(ops_sweep_max_age_min "$DOW") / 60 ))h"
fi

# 2b. O-21 DEAD-MAN AT OPEN+10: the session is open, the grace window has
# passed, and there is no heartbeat from THIS session. This is the check that
# would have caught 08-07 at 06:40 instead of a human noticing at 08:33.
LC=$("$ROOT/.venv/bin/python" -c "
import json
try: print(json.load(open('$ROOT/data/agent_state.json')).get('last_cycle') or '')
except Exception: print('')" 2>/dev/null)
OVERDUE=$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/liveness.py" heartbeat_overdue "$LC" 2>/dev/null || echo 0)
if [ "$OVERDUE" = "1" ]; then
    FAIL=1
    alert "no-heartbeat-at-open" "SESSION OPEN and NO trader heartbeat from this session (last_cycle=${LC:-none}). The engine is not trading."
fi

# 3. cycle advancing? (R9 intraday hung-cycle detection) — market hours only.
# A black-holed network / hanging Claude call can freeze a cycle for ~25-30 min
# with the pid still alive; layer 1 (pid-exists) would pass all day. Compare the
# engine's last_cycle timestamp against the wall clock DURING market hours and
# alarm when it stops advancing (>15 min = 3 missed 5-min cycles).
# 2026-09-30: the decision moved into scripts/ops/liveness.py (testable, see
# cycle_hang_minutes + tests/test_liveness_o21.py). The inline version measured
# the age from last_cycle alone, which only advances on an in-session cycle — so
# every trading morning between 06:30 and the first real cycle (~06:35) it paged
# the operator's phone with a ~1,050-minute "hang" that was not one.
HUNG=$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/liveness.py" cycle_hang "$LC" 2>/dev/null || echo '')
if [ -n "$HUNG" ]; then
    FAIL=1
    if [ "$HUNG" = "none" ]; then
        alert "cycle-hung" "agent running but no last_cycle timestamp at all, and the session is more than 15 min old"
    else
        alert "cycle-hung" "last cycle was ${HUNG} min ago during market hours (>15m = hung; positions unmanaged while the pid stays alive)"
    fi
fi

# 3b. BROKER EXECUTION PATH ALIVE? (2026-08-22)
# The gap that let 08-19, 08-20 and 08-21 each run a full 6h25m session with
# ZERO orders reaching the broker and NOT ONE alert. Every existing layer
# passed honestly: the pid was alive, the ops sweep ran, and cycles advanced
# normally the whole time — the engine cheerfully cycles while its long-lived
# IBKR client is dead, rejecting every entry with "qualify_failed: Not
# connected". Liveness of the LOOP is not liveness of EXECUTION, and only the
# loop was ever watched.
#
# Reads the engine's own log rather than probing the broker: a fresh probe
# connects fine (that is exactly why go_nogo kept passing), so it would prove
# nothing about the engine's client. The rejections are the observable that
# was screaming for three days.
if [ "$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/liveness.py" session_open)" = "1" ]; then
    TODAY=$(date +%Y-%m-%d)
    DEAD=$(grep "^$TODAY" "$ROOT/logs/agent.log" 2>/dev/null \
           | grep -cE "qualify_failed: Not connected|qualify failed .*Not connected|Socket disconnect" || true)
    # 3+ rejections means it is not a one-off blip: entries are being dropped.
    if [ "${DEAD:-0}" -ge 3 ]; then
        FAIL=1
        alert "broker-execution-dead" \
              "SESSION OPEN and the broker execution path is DEAD: ${DEAD} entries rejected today with 'Not connected'. Cycles keep advancing so every other check passes. The engine's long-lived IBKR client does not self-heal — it needs a RESTART (launchctl kickstart -k gui/\$(id -u)/com.optionsagent.trader)."
    fi
fi

# 3d. SCALP-v2: A SESSION OF IOC ATTEMPTS WITH ZERO FILLS (2026-09-12)
# Rule 3b only sees "Not connected". Under v2 the broker can be perfectly
# healthy and still cancel/reject every IOC all day — zero executions, nothing
# resting, nothing paged. After open+60min, >= 5 terminal zero-fill attempts
# with today_test_entry_count() == 0 is a liveness failure of the FILL path.
if [ "$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/liveness.py" session_open)" = "1" ]; then
    MIN_OPEN=$("$ROOT/.venv/bin/python" -c "
import json,subprocess
print(json.loads(subprocess.run(['$ROOT/.venv/bin/python','$ROOT/scripts/ops/liveness.py','state'],capture_output=True,text=True).stdout).get('minutes_since_open',0))" 2>/dev/null || echo 0)
    TODAY=$(date +%Y-%m-%d)
    # the ENGINE'S OWN counters, not a log grep: attempts = today_v2_attempt_count()
    # (fills + entry_ioc_unfilled), fills = today_test_entry_count()
    read -r ZF FILLS <<< "$("$ROOT/.venv/bin/python" -c "
import sys; sys.path.insert(0,'$ROOT')
from src.engine.paper_trader import today_v2_attempt_count, today_test_entry_count
print(today_v2_attempt_count(), today_test_entry_count())" 2>/dev/null || echo "0 0")"
    if [ "${MIN_OPEN%.*}" -ge 60 ] && [ "${ZF:-0}" -ge 5 ] && [ "${FILLS:-0}" -eq 0 ]; then
        FAIL=1
        alert "v2-no-fills" \
              "SESSION OPEN 60+ min: ${ZF} SCALP entries ended canceled/rejected and ZERO filled (today_test_entry_count=0). The fill path is dead or v2's price is never marketable — check quotes/aggressiveness, do not wait for the close."
    fi
fi

# 3c. CLAUDE GATE ALIVE? (2026-09-12)
# 2026-09-11: 15 x "credit balance is too low" — every funnel candidate HELD,
# the Claude-gated lane posted nothing, the batch-review job died on the same
# 400, and nothing paged: the loop cycled, the broker was fine, the deadman was
# satisfied. Same class as broker-execution-dead: the LOOP is alive, the
# CAPABILITY is dead. Same fix: read the engine's own failure lines.
if [ "$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/liveness.py" session_open)" = "1" ]; then
    TODAY=$(date +%Y-%m-%d)
    CDEAD=$(grep "^$TODAY" "$ROOT/logs/agent.log" 2>/dev/null \
            | grep -cE "Claude validation failed|credit balance is too low|invalid_request_error" || true)
    if [ "${CDEAD:-0}" -ge 3 ]; then
        FAIL=1
        alert "claude-gate-dead" \
              "SESSION OPEN and the Claude validation gate is FAILING: ${CDEAD} errors today (credits exhausted / API error). Every funnel candidate is being HELD — the funnel forward test is starving silently. SCALP is unaffected. Action: check Anthropic billing/API key."
    fi
fi

# 3e. IBKR API PORT REACHABLE DURING THE SESSION? (2026-10-12)
# The gap that lost 2026-10-09 AND 2026-10-12 in silence: the gateway was up at
# 06:35 on 10-09 and its port was dead by 10:00, with 78 cycles, 0 ERROR lines
# and not one alert. Layer 3b cannot see this — it counts 'Not connected' ENTRY
# REJECTIONS, and an idle engine makes no entries. Decision + the full rationale
# and the ownership split with await_broker_activation.sh (which owns 06:20-06:45,
# this layer owns open+30 onward) live in scripts/ops/liveness.py.
# The probe is an `if` condition, never a bare command: this script runs under
# `set -e`, and a closed port is exactly the case we are here to report — an
# unguarded `nc -z` would abort the script and silently disable layer 4.
PORT_OK=0
if nc -z -w 3 127.0.0.1 4002 >/dev/null 2>&1; then PORT_OK=1; fi
if [ "$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/liveness.py" broker_port_dead "$PORT_OK")" = "1" ]; then
    FAIL=1
    alert "broker-port-dead" \
          "SESSION OPEN and the IBKR API port 4002 is NOT LISTENING: IB Gateway is not logged in (or died mid-session). The engine keeps cycling, so every other check passes honestly — it can place NOTHING. This repeats every 30 min while the port stays down. ACTION: open IB Gateway, log in with the PAPER credentials; if the engine does not pick it up within a few minutes, kickstart it: launchctl kickstart -k gui/\$(id -u)/com.optionsagent.trader  (layer added 2026-10-12 after 10-09 and 10-12 were each lost in silence)"
fi

# 3f. EPHEMERAL PORT RANGE NEARLY CONSUMED? (2026-10-24)
# The fault that cost the 2026-10-23 evening and (undiagnosed) 2026-08-24: a
# wedged TIME_WAIT table consumes all 16,384 ephemeral ports, and from then on
# EVERY outbound connect fails with [Errno 49] — cockpit curl, the IBKR line,
# the nightly API step and ThetaData all at once. At 100% the host cannot even
# send the page about it, so the check fires at 80%, while there is headroom.
# Guarded in an `if` for the same reason as layer 3e: the script runs under
# `set -e` and the tool exits 3 on breach, which is the case we are here to
# report. Exit 1 (could not measure) deliberately does NOT alert — it is not
# evidence of a breach, and a noisy false page would get the layer ignored.
if [ "$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/ephemeral_port_check.py" >/dev/null 2>&1; echo $?)" = "3" ]; then
    FAIL=1
    EPC_LINE=$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/ephemeral_port_check.py" 2>/dev/null || true)
    alert "ephemeral-ports-exhausting" \
          "$EPC_LINE  ACTION (operator-only, autopilot has neither): widen the range as root — sudo sysctl -w net.inet.ip.portrange.first=16384 — or reboot. Both known occurrences (2026-08-24, 2026-10-23) followed ~60 days of uptime and cleared only at a reboot; current uptime: $(uptime | sed 's/.*up //; s/,.*users.*//'). BEFORE REBOOTING see the C-3 decision in docs/autopilot/QUEUE.md — a reboot destroys the plist-less post-close restart job."
fi

# 3g. AWAKE ON BATTERY during a trading-day window? (2026-10-29)
# Twice proven to cost ticks: 2026-10-22 slept at 07:53 ('Clamshell Sleep ...
# Using Batt (Charge:84%)') for 8 RTH ticks, and 2026-10-29 slept 18 times for
# 67 ticks — straight through the 06:30 open. Both days the host was unplugged,
# and `caffeinate -s` (which the engine runs under) is void on battery, so the
# assertions are listed and the host sleeps anyway. Nothing detected either
# instance: both days' pages were typed by hand by an autopilot run, and on
# 10-29 that run then died mid-response from the host sleeping.
# KNOWN LIMIT: this cannot fire while the host is ASLEEP. It catches the
# awake-on-battery state — the warning BEFORE the next sleep. Charge % is not a
# condition (the 10-22 sleep began at 84%); being unplugged is.
# Guarded in an `if` and alerting on exit 3 ONLY, for the same reasons as 3f:
# the script runs under `set -e`, and exit 1 ("cannot measure") is not a breach.
if [ "$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/power_source_check.py" >/dev/null 2>&1; echo $?)" = "3" ]; then
    FAIL=1
    PSC_LINE=$("$ROOT/.venv/bin/python" "$ROOT/scripts/ops/power_source_check.py" 2>/dev/null || true)
    alert "host-on-battery" \
          "$PSC_LINE  ACTION: PLUG THE MAC IN. Do not rely on caffeinate — it is void on battery by design, and a lid close is not an idle sleep. The lasting fix is operator-only power policy (a ratified root \`pmset -b\` change, or lid-open-on-AC); see the 2026-10-24 power item in docs/autopilot/QUEUE.md."
fi

# 4. external heartbeat (machine-death coverage) — only if configured
if [ -n "${URL:-}" ]; then
    if [ "$FAIL" = "0" ]; then
        curl -fsS -m 10 --retry 2 "$URL" >/dev/null 2>&1 || true
    else
        curl -fsS -m 10 --retry 2 "$URL/fail" >/dev/null 2>&1 || true
    fi
fi
exit 0
