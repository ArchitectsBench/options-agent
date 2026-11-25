#!/bin/bash
# set -euo pipefail (2026-08-04): -e abort on error, -u unset-var guard,
# -o pipefail so a failure ANYWHERE in a pipeline is the pipeline's status
# (a piped verdict silently read as success is how three exit-code misreads
# happened). Optional actions carry an explicit `|| true`.
set -euo pipefail
# Travel catch-up sweep (2026-07-22): the laptop travels and wakes
# irregularly. Every run: (1) replay any RVOL-RANK session whose selection
# file exists but whose ledger record is missing (last 14 days); (2) bring
# truth enrichment current. Idempotent; safe to run hourly.
cd /Users/you/projects/OptionsAgent || exit 1
PY=.venv/bin/python
LEDGER=logs/observation/rvolrank_sleeve.jsonl
for f in $(find logs/observation -name "rvol_shadow-*.json" -mtime -14 2>/dev/null | sort); do
  d=$(basename "$f" | sed 's/rvol_shadow-\(.*\)\.json/\1/')
  # today runs at 21:30 on its own schedule
  if [ "$d" = "$(date +%Y-%m-%d)" ]; then continue; fi
  # forward window start
  if [ "$d" \< "2026-07-20" ]; then continue; fi
  if ! grep -q "\"date\": \"$d\"" "$LEDGER" 2>/dev/null; then
    echo "[catchup] replaying missed sleeve session $d"
    # A non-replayable session (holiday / no live selection captured) is NOT a
    # sweep failure. Under set -e the Labor Day file (09-07) aborted EVERY
    # hourly run at this line 09-08..09-12, so enrichment, the exit shadow and
    # the buy-zone radar below never ran (radar state frozen at 09-07 23:42).
    $PY scripts/rvolrank_sleeve.py --date "$d" || echo "[catchup] $d not replayable (see above) — continuing"
  fi
done
$PY scripts/enrich_trades_thetadata.py --through "$(date +%Y-%m-%d)" 2>&1 | tail -1
echo "[catchup] sweep done $(date)"
.venv/bin/python scripts/exit_shadow_tracker.py 2>&1 | tail -1
# Buy-zone radar: FULL stdout to the shared per-day radar log (2026-09-13) —
# the old `| tail -1` discarded the nightly summary and hid every failure
# mode. Same file nightly_ops.sh's buyzone_radar step appends to. A radar
# failure is reported here but never aborts the sweep (idempotent hourly run).
mkdir -p logs/alerts
.venv/bin/python scripts/watch_buyzone.py >> "logs/alerts/radar-$(date +%F).log" 2>&1 \
  || echo "[catchup] buy-zone radar exited $? — see logs/alerts/radar-$(date +%F).log"
echo "[catchup] buy-zone radar run appended to logs/alerts/radar-$(date +%F).log"
