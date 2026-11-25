import { useCockpit } from "../store";
import { ActionButton } from "./ActionButton";

const cur = (n: number) =>
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
const pnlColor = (n: number) => (n > 0 ? "var(--grn)" : n < 0 ? "var(--red)" : "var(--text-4)");

const tile: React.CSSProperties = {
  flex: 1,
  padding: "13px 15px",
  borderRadius: 9,
  background: "var(--bg-panel)",
  border: "1px solid var(--border-panel)",
};
const tileCaption: React.CSSProperties = {
  fontSize: 10,
  letterSpacing: "0.1em",
  color: "var(--text-8)",
  fontWeight: 600,
};

export function TodayStrip() {
  const { feed, blended, setBlended } = useCockpit();
  const t = feed.today;
  const set = blended ? t.blended : t.funnel;
  const winRate = set.roundTrips ? Math.round((set.wins / set.roundTrips) * 100) : 0;

  // breaker derivation (mirrors prototype renderVals)
  const tripped = t.breakerTripped;
  const frac = tripped
    ? 1
    : Math.min(1, Math.max(0, set.realized < 0 ? set.realized / t.breakerLimit : 0));
  const breakerColor = tripped || frac > 0.66 ? "var(--red)" : frac > 0.33 ? "var(--amb)" : "var(--grn)";
  const headroom = tripped
    ? "Limit breached — trading halted for the session."
    : set.realized >= 0
      ? "$1,500 headroom · no realized loss"
      : `$${Math.abs(t.breakerLimit - Math.min(0, set.realized)).toLocaleString()} headroom before halt`;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 9,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.14em", color: "var(--text-5)" }}>
            TODAY
          </span>
          <span className="mono" style={{ fontSize: 10, color: "var(--text-9)" }}>
            session P&L · {t.sessionDate}
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 10, color: "var(--text-7)" }}>view</span>
          <div
            className="mono control-region"
            style={{
              display: "flex",
              background: "var(--bg-tile)",
              border: "1px solid var(--border-tile)",
              borderRadius: 6,
              overflow: "hidden",
              fontSize: 10,
              fontWeight: 600,
            }}
          >
            <ActionButton
              allowRapid
              onAct={() => setBlended(false)}
              aria-pressed={!blended}
              style={{
                padding: "5px 11px",
                background: blended ? "transparent" : "rgba(var(--accent-rgb),0.16)",
                color: blended ? "var(--text-7)" : "var(--accent)",
              }}
            >
              FUNNEL ONLY
            </ActionButton>
            <ActionButton
              allowRapid
              onAct={() => setBlended(true)}
              aria-pressed={blended}
              style={{
                padding: "5px 11px",
                background: blended ? "rgba(240,178,58,0.16)" : "transparent",
                color: blended ? "var(--amb)" : "var(--text-7)",
              }}
            >
              BLENDED
            </ActionButton>
          </div>
        </div>
      </div>

      {blended && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginBottom: 9,
            padding: "6px 11px",
            borderRadius: 6,
            background: "#231803",
            border: "1px solid #4a3a10",
          }}
        >
          <span style={{ fontSize: 11 }}>⚠</span>
          <span className="mono" style={{ fontSize: 11, color: "#e0c98a" }}>
            TWO STREAMS MERGED — funnel + SCALP candidate combined for a whole-book view.
            Each stream is scored against its own frozen gate; never read this merge as
            either strategy's performance.
          </span>
        </div>
      )}

      <div style={{ display: "flex", gap: 12 }}>
        <div style={tile}>
          <div style={tileCaption}>REALIZED P&L</div>
          <div
            className="mono"
            style={{ fontSize: 23, fontWeight: 700, marginTop: 5, color: pnlColor(set.realized) }}
          >
            {cur(set.realized)}
          </div>
          <div className="mono" style={{ fontSize: 10, color: "var(--text-9)", marginTop: 2 }}>
            {blended ? "incl. execution-test" : "funnel-validated only"}
          </div>
          {/* DUAL RULER (2026-08-07). The headline is BOOKED — identical to the
              daily-loss breaker's input. Market truth is the SCORING basis and
              is shown separately; their difference is what execution cost. */}
          {set.marketTruth !== undefined && set.marketTruth !== null && (
            <div className="mono" style={{ fontSize: 11, marginTop: 4, color: "var(--text-9)" }}>
              market-truth (mid): {cur(set.marketTruth)}
              {set.executionCost !== undefined && set.executionCost !== null && (
                <span style={{ color: set.executionCost < 0 ? "var(--red)" : "var(--green)" }}>
                  {"  ·  execution cost: "}{cur(set.executionCost)}
                </span>
              )}
            </div>
          )}
          {/* market-truth strip REMOVED 2026-08-06 (objective O-11): display only.
              The enrichment PIPELINE it read from is load-bearing and is KEPT —
              enriched_trades_v1.jsonl still feeds gate_learning, post_mortem_engine,
              daily_cohort_report and ops_checks. Removing the readout does not
              remove the evidence. */}
        </div>

        <div style={tile}>
          <div style={tileCaption}>UNREALIZED P&L</div>
          <div
            className="mono"
            style={{
              fontSize: 23,
              fontWeight: 700,
              marginTop: 5,
              color: pnlColor(set.unrealized ?? t.unrealized),
            }}
          >
            {cur(set.unrealized ?? t.unrealized)}
          </div>
          <div className="mono" style={{ fontSize: 10, color: "var(--text-9)", marginTop: 2 }}>
            {/* fallback must respect the view filter (2026-07-31): the global
                positions.length said "1 open" in FUNNEL view while the filtered
                table showed none — count-vs-table mismatch the user hit */}
            {set.openPositions ??
              (blended
                ? feed.positions.length
                : feed.positions.filter((p) => p.sleeve !== "test").length)}{" "}
            open · mark age {t.markAge}
          </div>
        </div>

        <div style={tile}>
          <div style={tileCaption}>ROUND TRIPS</div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginTop: 5 }}>
            <span className="mono" style={{ fontSize: 23, fontWeight: 700 }}>
              {set.roundTrips}
            </span>
            <span className="mono" style={{ fontSize: 12, color: "var(--grn)" }}>
              {set.wins}W
            </span>
            <span className="mono" style={{ fontSize: 12, color: "var(--red)" }}>
              {set.losses}L
            </span>
          </div>
          <div className="mono" style={{ fontSize: 10, color: "var(--text-9)", marginTop: 2 }}>
            {/* "0% win" with 0W 0L just means NOT SCORED YET (truth enrichment
                pending) — say that instead of a misleading zero (2026-07-31) */}
            {set.wins + set.losses > 0 ? (
              <>{winRate}% win · </>
            ) : set.roundTrips > 0 ? (
              <>truth pending · </>
            ) : null}
            <span style={{ color: "var(--text-5)" }}>n={set.roundTrips}</span>
          </div>
        </div>

        <div
          style={{
            ...tile,
            flex: 1.3,
            ...(tripped
              ? {
                  background: "#1e0a0b",
                  border: "1px solid #7a1c22",
                  animation: "ringPulse 1.6s infinite",
                }
              : {}),
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={tileCaption}>DAILY-LOSS BREAKER</span>
            <span className="mono" style={{ fontSize: 10, fontWeight: 700, color: breakerColor }}>
              {tripped ? "● TRIPPED" : "SAFE"}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 6, marginTop: 5 }}>
            <span className="mono" style={{ fontSize: 18, fontWeight: 700, color: breakerColor }}>
              {cur(set.realized)}
            </span>
            <span className="mono" style={{ fontSize: 11, color: "var(--text-9)" }}>
              / −$1,500 limit
            </span>
          </div>
          <div
            style={{
              height: 8,
              borderRadius: 4,
              background: "var(--bg-track)",
              overflow: "hidden",
              marginTop: 9,
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${Math.round(frac * 100)}%`,
                background: breakerColor,
                borderRadius: 4,
              }}
            />
          </div>
          <div className="mono" style={{ fontSize: 9.5, color: "var(--text-9)", marginTop: 5 }}>
            {headroom}
          </div>
        </div>
      </div>
    </div>
  );
}
