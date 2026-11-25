#!/usr/bin/env bash
# Shared phone-push sender for every shell job (2026-09-13).
#
# Usage: ntfy_send.sh <title> <priority> <tags> <body>
#
# Posts to EVERY configured ntfy target:
#   primary  $NTFY_SERVER (default https://ntfy.sh) / $NTFY_TOPIC
#   second   $NTFY_ALT_SERVER / $NTFY_ALT_TOPIC
# Why two servers: ntfy.sh's free quota is per visitor IP (429 code 42908,
# "daily message quota reached"). When it is exhausted every topic on ntfy.sh
# fails, so a second ntfy.sh topic is not a second channel — a second server is.
#
# Delivery is REPORTED, not assumed: one "ntfy target=<host> http=<code>" line
# per target on stdout and in logs/ops/notify_delivery.log. Exit 0 if at least
# one target returned 2xx, 1 otherwise. Callers keep their own `|| true`.
#
# A 000 — no HTTP response at all (connect refused, DNS, timeout) — is RETRIED
# up to NTFY_ATTEMPTS times (2026-10-19). Both targets returned 000 in the same
# second that day and one DEADMAN page was lost outright: a second ntfy SERVER
# is not independent of this host's own network stack, and nothing re-sent. The
# 30-min deadman cadence happened to cover it; a one-shot page would have gone
# with no trace. Retried attempts carry an " attempt=N/M" suffix, so count
# PAGES not LINES when reading the log (filter out the attempt= rows).
#
# ONLY 000 is retried, deliberately: a 429 is ntfy.sh's daily per-IP quota
# (re-posting cannot help — the second server is the answer) and a 4xx/5xx came
# from a server that did answer, so the code is the truth, not a lost page.
#
# Values come from .env (NTFY_SEND_ENV overrides the path — used by tests).

set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${NTFY_SEND_ENV:-$ROOT/.env}"
LOG="${NTFY_SEND_LOG:-$ROOT/logs/ops/notify_delivery.log}"

envval() { grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }

title="${1:-OptionsAgent}"; prio="${2:-default}"; tags="${3:-}"; body="${4:-}"
# HTTP headers are latin-1; keep the title ASCII (em dash -> hyphen, drop the rest).
title=$(printf '%s' "$title" | sed 's/—/-/g' | LC_ALL=C tr -cd '\11\40-\176')

primary_server=$(envval NTFY_SERVER); primary_server="${primary_server:-https://ntfy.sh}"
primary_topic=$(envval NTFY_TOPIC)
alt_server=$(envval NTFY_ALT_SERVER); alt_topic=$(envval NTFY_ALT_TOPIC)

targets=()
[ -n "$primary_topic" ] && targets+=("${primary_server%/}/$primary_topic")
[ -n "$alt_server" ] && [ -n "$alt_topic" ] && targets+=("${alt_server%/}/$alt_topic")

mkdir -p "$(dirname "$LOG")" 2>/dev/null
ok=1
for url in ${targets[@]+"${targets[@]}"}; do
    host=$(printf '%s' "$url" | awk -F/ '{print $3}')
    args=(-sS -m 10 -o /dev/null -w '%{http_code}' -H "Title: $title" -H "Priority: $prio")
    [ -n "$tags" ] && args+=(-H "Tags: $tags")
    attempts="${NTFY_ATTEMPTS:-3}"
    for try in $(seq 1 "$attempts"); do
        code=$(curl "${args[@]}" --data-binary "$body" "$url" 2>/dev/null) || code="${code:-000}"
        line="ntfy target=$host http=$code"
        [ "$try" -gt 1 ] && line="$line attempt=$try/$attempts"
        echo "$line"
        echo "$(date '+%F %T') $line title=\"$title\"" >> "$LOG" 2>/dev/null
        [ "$code" = "000" ] || break
        [ "$try" -lt "$attempts" ] && sleep "${NTFY_RETRY_SLEEP:-2}"
    done
    case "$code" in 2??) ok=0 ;; esac
done
[ ${#targets[@]} -eq 0 ] && echo "ntfy: no targets configured"
exit $ok
