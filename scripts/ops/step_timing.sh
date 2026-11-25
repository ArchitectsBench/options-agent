# Per-step wall-clock timing for the nightly chain (2026-10-21).
#
# WHY: nightly_ops.sh took ~57 min on 2026-10-20 vs ~9 min on 10-19, and the
# overrun was only visible to a live `pgrep` — once the chain exits, nothing on
# disk says how long any step took, so a 6x regression is invisible and the slow
# STEP cannot be named. This library gives every step a queryable duration.
#
# APPEND-PER-STEP, NEVER BUFFERED: each line is written the moment its step
# finishes, so a PARTIAL file is readable while the chain is still running. That
# is the case that matters — on an overrun night the chain is still going at the
# 19:10 evening checkpoint, and the grinding step is then "the first step with no
# line yet". A write-at-chain-end design would say nothing on exactly the night
# it was built for.
#
# FAIL-SAFE BY CONSTRUCTION: the chain is load-bearing (degraded ops beat no
# ops), so timing may never change a step's exit code or abort the script under
# `set -euo pipefail`. Every write is `|| true`, every input is numerically
# validated before arithmetic (a non-numeric start would otherwise be a fatal
# arithmetic error under `set -e`), and every function returns 0.
#
# Sourced by scripts/nightly_ops.sh; covered by tests/test_step_timing.sh.

timing_now() {  # epoch seconds, or empty if date is unavailable
    date +%s 2>/dev/null || true
}

emit_timing() {  # emit_timing <step-name> <start-epoch> <rc>
    local name="${1:-unknown}" start="${2:-}" rc="${3:-}" end dur stamp

    # No destination configured -> timing is simply off, not an error.
    [ -n "${TIMING_FILE:-}" ] || return 0

    end=$(date +%s 2>/dev/null || true)

    # Both bounds must be plain integers before any arithmetic happens.
    case "$start" in ''|*[!0-9]*) return 0 ;; esac
    case "$end"   in ''|*[!0-9]*) return 0 ;; esac

    dur=$(( end - start ))
    stamp=$(date '+%FT%T' 2>/dev/null || true)

    printf '{"step":"%s","seconds":%s,"rc":"%s","finished_at":"%s"}\n' \
        "$name" "$dur" "$rc" "$stamp" >> "$TIMING_FILE" 2>/dev/null || true
    return 0
}
