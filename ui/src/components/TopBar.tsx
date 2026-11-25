import { useCockpit, mmss } from "../store";
import { ActionButton } from "./ActionButton";
import { useArmed } from "./useArmed";

const cell: React.CSSProperties = {
  background: "var(--bg-rail)",
  padding: "9px 16px",
  display: "flex",
  flexDirection: "column",
  justifyContent: "center",
  gap: 3,
};

// GEX display helpers (context only, never scored)
const humanGex = (n?: number | null): string | null => {
  if (n == null) return null;
  const a = Math.abs(n);
  const s = n >= 0 ? "+" : "−";
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(0)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(0)}k`;
  return `${s}$${a.toFixed(0)}`;
};
const GEX_WORD: Record<string, string> = {
  amplifying: "amplify",
  suppressive: "pin",
  neutral: "flip",
};

export function TopBar() {
  const { feed, killEngaged, cycleSecs, toggleKill } = useCockpit();
  const { armed, arm, disarm } = useArmed();

  // Kill = single-click-instant engage. Release = two-step confirm (arm, then
  // a second click within the timeout actually releases). Returns the POST
  // promise (or nothing when only arming) so ActionButton disables in-flight.
  const onKill = () => {
    if (!killEngaged) return toggleKill();      // engage: instant
    if (!armed) { arm(); return; }              // release step 1: arm
    disarm();                                   // release step 2: confirm
    return toggleKill();
  };

  // status cell (derivations mirror the prototype's renderVals)
  let statusColor = "var(--grn)";
  let statusLabel = "RUNNING · cycle 5m";
  let statusAnim: React.CSSProperties = {};
  if (killEngaged) {
    statusColor = "var(--amb)";
    statusLabel = "RUNNING · ORDERS BLOCKED";
  }
  if (!feed.marketOpen) {
    statusColor = "var(--accent)";
    statusLabel = "HEARTBEAT · MKT CLOSED";
  }
  if (feed.agent === "dead") {
    statusColor = "var(--red)";
    statusLabel = "PROCESS DEAD";
    statusAnim = { animation: "pulseDot 1s infinite" };
  }

  const live = feed.mode === "live";
  const cycleCaption = feed.marketOpen ? "NEXT CYCLE" : "NEXT SESSION";
  const cycleValue =
    feed.agent === "dead" ? "—— STALE" : feed.marketOpen ? mmss(cycleSecs) : "06:30 PT";
  const cycleColor =
    feed.agent === "dead" ? "var(--red)" : feed.marketOpen ? "var(--text-1)" : "var(--text-7)";

  return (
    <div
      style={{
        display: "flex",
        alignItems: "stretch",
        flexWrap: "wrap",
        gap: 1,
        background: "var(--border-rail)",
        borderBottom: "1px solid var(--border-rail)",
      }}
    >
      {/* agent status */}
      <div style={{ ...cell, flexDirection: "row", alignItems: "center", gap: 11, minWidth: 210 }}>
        <span
          style={{
            width: 9,
            height: 9,
            borderRadius: "50%",
            background: statusColor,
            boxShadow: `0 0 8px ${statusColor}`,
            ...statusAnim,
          }}
        />
        <div>
          <div style={{ fontWeight: 700, fontSize: 13, letterSpacing: "-0.01em" }}>OptionsAgent</div>
          <div
            className="mono"
            style={{ fontSize: 10, color: statusColor, fontWeight: 600, letterSpacing: "0.04em" }}
          >
            {statusLabel}
          </div>
        </div>
      </div>

      {/* mode badge */}
      <div style={cell}>
        <div className="caption">MODE</div>
        <div
          className="mono"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            padding: "3px 9px",
            borderRadius: 5,
            background: live ? "#2a0e10" : "rgba(var(--accent-rgb), 0.12)",
            border: `1px solid ${live ? "#7a1c22" : "rgba(var(--accent-rgb), 0.35)"}`,
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: "0.1em",
            color: live ? "#ff8b8f" : "var(--accent)",
          }}
        >
          {live ? "● LIVE" : "OBSERVATION"}
        </div>
      </div>

      {/* market */}
      <div style={{ ...cell, minWidth: 120 }}>
        <div className="caption">MARKET</div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: feed.marketOpen ? "var(--grn)" : "var(--text-7)",
            }}
          />
          <span
            className="mono"
            style={{
              fontSize: 12,
              fontWeight: 600,
              color: feed.marketOpen ? "var(--grn)" : "var(--text-7)",
            }}
          >
            {feed.marketOpen ? "OPEN" : "CLOSED"}
          </span>
        </div>
      </div>

      {/* next cycle countdown */}
      <div style={{ ...cell, minWidth: 132 }}>
        <div className="caption">{cycleCaption}</div>
        <div
          className="mono"
          style={{ fontSize: 15, fontWeight: 600, letterSpacing: "0.02em", color: cycleColor }}
        >
          {cycleValue}
        </div>
      </div>

      {/* regime one-liner */}
      <div style={{ ...cell, gap: 4, flex: 1, minWidth: 300 }}>
        <div className="caption">REGIME</div>
        <div
          className="mono"
          style={{ display: "flex", alignItems: "center", gap: 12, fontSize: 11.5, whiteSpace: "nowrap" }}
        >
          <span>
            bias <b style={{ color: "var(--dir)", fontWeight: 700 }}>{feed.regime.bias}</b>
          </span>
          <span style={{ color: "var(--hairline)" }}>·</span>
          <span>
            trend <b style={{ color: "var(--dir)", fontWeight: 700 }}>{feed.regime.trend}</b>
          </span>
          <span style={{ color: "var(--hairline)" }}>·</span>
          <span>
            VIX <b style={{ color: "var(--text-1)", fontWeight: 700 }}>{feed.regime.vix}</b>
          </span>
          <span style={{ color: "var(--hairline)" }}>·</span>
          {(() => {
            const r = feed.regime;
            const color =
              r.gex === "NEG" ? "var(--amb)" : r.gex === "POS" ? "var(--accent)" : "var(--text-6)";
            const net = humanGex(r.gexNet);
            const word = r.gexRegime ? GEX_WORD[r.gexRegime] ?? r.gexRegime : null;
            const tip = [
              r.gexFlip != null ? `zero-gamma flip ${r.gexFlip}` : null,
              r.gexSpot != null ? `spot ${r.gexSpot}` : null,
              r.gexEstimated ? "chain-estimated" : null,
              "context only — not used to score trades",
            ]
              .filter(Boolean)
              .join(" · ");
            return (
              <span title={tip} style={{ display: "inline-flex", alignItems: "baseline", gap: 5 }}>
                GEX <b style={{ color, fontWeight: 700 }}>{r.gex}</b>
                {net && <span style={{ color: "var(--text-5)" }}>{net}</span>}
                {word && <span style={{ color, fontSize: 9.5, opacity: 0.85 }}>{word}</span>}
              </span>
            );
          })()}
        </div>
      </div>

      {/* AI validation budget */}
      <div style={{ ...cell, gap: 4, minWidth: 126 }}>
        <div className="caption">AI VALIDATION</div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="mono" style={{ fontSize: 12, fontWeight: 600 }}>
            {feed.claude.used}
            <span style={{ color: "var(--text-9)" }}>/{feed.claude.max}</span>
          </span>
          <div
            style={{
              flex: 1,
              height: 5,
              borderRadius: 3,
              background: "var(--bg-track)",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${Math.round((feed.claude.used / feed.claude.max) * 100)}%`,
                background: "var(--accent)",
                borderRadius: 3,
              }}
            />
          </div>
        </div>
      </div>

      {/* kill switch — engage is single-click-instant; release is two-step confirm */}
      <div className="control-region" style={{ background: "var(--bg-rail)", padding: "8px 12px", display: "flex", alignItems: "center" }}>
        <ActionButton
          onAct={onKill}
          // No cooldownMs (fixed 2026-07-31): the arming click flips the label to
          // "CONFIRM?" and invites an immediate 2nd click, but a post-action
          // cooldown disables the button during exactly that window — swallowing
          // the confirm ("clicked and nothing happened" on the kill switch).
          // Flip-flop is already prevented by the two-step arm + async in-flight disable.
          aria-pressed={killEngaged}
          aria-label={killEngaged ? (armed ? "Confirm release of kill switch" : "Release kill switch (two-step)") : "Engage kill switch"}
          title={killEngaged ? "Kill switch engaged — click to release (two-step confirm)" : "Engage kill switch — blocks new orders"}
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 3,
            padding: "6px 14px",
            borderRadius: 7,
            background: armed ? "#ffb000" : killEngaged ? "var(--red)" : "rgba(255, 90, 95, 0.06)",
            border: `1.5px solid ${armed ? "#ffd060" : killEngaged ? "#ff8b8f" : "#5a1e22"}`,
          }}
        >
          <div
            className="mono"
            style={{
              fontSize: 12,
              fontWeight: 700,
              letterSpacing: "0.08em",
              color: armed ? "#1a1206" : killEngaged ? "#1a0405" : "#ff8b8f",
            }}
          >
            {armed ? "⏻ CONFIRM?" : "⏻ KILL"}
          </div>
          <div
            style={{
              fontSize: 8,
              letterSpacing: "0.1em",
              fontWeight: 600,
              color: armed ? "#1a1206" : killEngaged ? "#1a0405" : "#ff8b8f",
            }}
          >
            {armed ? "CLICK TO RELEASE" : killEngaged ? "● ENGAGED" : "ARMED"}
          </div>
        </ActionButton>
      </div>
    </div>
  );
}
