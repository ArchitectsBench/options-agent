#!/bin/bash
# Daily EVIDENCE SNAPSHOT — the surfaces that constitute gate evidence.
# Origin (2026-08-07): a probe of a protective layer truncated
# logs/recommendations.jsonl. It was recoverable ONLY because an unrelated
# staging backup happened to exist. Evidence integrity must not depend on luck.
set -euo pipefail
ROOT="/Users/you/projects/OptionsAgent"
DEST="$ROOT/data/snapshots"
KEEP=14
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/$STAMP"
mkdir -p "$OUT"

for f in logs/recommendations.jsonl \
         logs/observation/fills.jsonl \
         logs/observation/enriched_trades_v1.jsonl \
         logs/learning/classified_ledger.jsonl; do
    [ -f "$ROOT/$f" ] && install -m 444 "$ROOT/$f" "$OUT/$(basename "$f")" || true
done
for lane in funnel scalp; do
    for k in positions orders; do
        src="$ROOT/data/lane/$lane/$k.json"
        [ -f "$src" ] && install -m 444 "$src" "$OUT/lane-$lane-$k.json" || true
    done
done
( cd "$OUT" && shasum * > MANIFEST.sha256 2>/dev/null || true )
echo "snapshot -> $OUT ($(ls "$OUT" | wc -l | tr -d ' ') files)"

# VERIFY PRIOR SNAPSHOT (2026-08-07): a backup nobody checks is a hope, not a
# regime. Re-verify the previous snapshot's manifest before rotating anything.
PREV=$(ls -1d "$DEST"/*/ 2>/dev/null | sort | tail -2 | head -1)
if [ -n "${PREV:-}" ] && [ -f "$PREV/MANIFEST.sha256" ]; then
    if ( cd "$PREV" && shasum -c MANIFEST.sha256 >/dev/null 2>&1 ); then
        echo "prior snapshot VERIFIED: $(basename "$PREV")"
    else
        echo "PRIOR SNAPSHOT CORRUPT: $(basename "$PREV") — manifest mismatch"
        "$ROOT/scripts/ops/ntfy_send.sh" "EVIDENCE SNAPSHOT CORRUPT" urgent "" \
            "manifest mismatch in $(basename "$PREV") — evidence backup integrity broken" || true
    fi
fi

# rotation: keep the newest $KEEP
# portable rotation: BSD head has no `-n -N`, so drop the newest $KEEP with sed
ls -1d "$DEST"/*/ 2>/dev/null | sort -r | sed -n "$((KEEP+1)),\$p" | while read -r old; do
    chmod -R u+w "$old" && rm -rf "$old" && echo "rotated out: $(basename "$old")"
done
