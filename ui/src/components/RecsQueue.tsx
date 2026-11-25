import { useCockpit, mmss } from "../store";
import { ActionButton } from "./ActionButton";
import type { Recommendation } from "../feed/types";

const STATUS_STYLE = {
  approved: { bg: "rgba(53,208,127,0.1)", color: "var(--grn)", border: "rgba(53,208,127,0.3)" },
  pending: { bg: "rgba(var(--accent-rgb),0.12)", color: "var(--accent)", border: "rgba(var(--accent-rgb),0.35)" },
  vetoed: { bg: "rgba(255,90,95,0.1)", color: "var(--red)", border: "rgba(255,90,95,0.3)" },
  skipped: { bg: "#161d26", color: "var(--text-7)", border: "#2a323d" },
} as const;

const VERDICT_COLOR = {
  approve: "var(--grn)",
  veto: "var(--red)",
  skip: "var(--amb)",
} as const;

const MATH_COLOR = { neutral: "var(--text-1)", loss: "var(--red)", profit: "var(--grn)" } as const;

function RecCard({ rec }: { rec: Recommendation }) {
  const { decisions, decide, confirmSecs, feed } = useCockpit();
  const live = feed.mode === "live";
  const decision = decisions[rec.id] ?? rec.decidedKind;
  const decidedLabel =
    decisions[rec.id] === "confirmed"
      ? "✓ CONFIRMED — order routed to sandbox"
      : decisions[rec.id] === "declined"
        ? "✕ DECLINED — recommendation dismissed"
        : rec.decidedLabel;
  const pending = rec.status === "pending" && !decision;
  const vetoCard = rec.status === "vetoed";
  const cardBorder = vetoCard ? "#2a1416" : "var(--border-panel)";
  const s = STATUS_STYLE[rec.status];
  const long = rec.dir === "LONG";

  return (
    <div
      style={{
        border: `1px solid ${cardBorder}`,
        borderRadius: 10,
        background: vetoCard ? "#130d0e" : rec.status === "skipped" ? "var(--bg-rail)" : "var(--bg-panel)",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div style={{ padding: "11px 13px 10px" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="mono" style={{ fontWeight: 700, fontSize: 15 }}>
                {rec.ticker}
              </span>
              <span
                className="mono"
                style={{
                  fontSize: 10,
                  fontWeight: 600,
                  padding: "2px 6px",
                  borderRadius: 4,
                  background: long ? "rgba(53,208,127,0.12)" : "rgba(255,138,106,0.12)",
                  color: long ? "var(--grn)" : "var(--dir)",
                }}
              >
                {rec.dir}
              </span>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-5)", marginTop: 2 }}>{rec.structure}</div>
          </div>
          <span
            className="mono"
            style={{
              fontSize: 9.5,
              fontWeight: 700,
              letterSpacing: "0.06em",
              padding: "3px 8px",
              borderRadius: 5,
              background: s.bg,
              color: s.color,
              border: `1px solid ${s.border}`,
              whiteSpace: "nowrap",
            }}
          >
            {rec.statusLabel}
          </span>
        </div>

        <div
          className="mono"
          style={{
            fontSize: 11,
            color: "var(--text-3)",
            marginTop: 9,
            padding: "8px 10px",
            borderRadius: 6,
            background: "rgba(0,0,0,0.22)",
            lineHeight: 1.65,
            whiteSpace: "pre-line",
          }}
        >
          {rec.legs}
        </div>

        {rec.math && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr 1fr",
              gap: 1,
              marginTop: 9,
              background: "#161d26",
              borderRadius: 6,
              overflow: "hidden",
            }}
          >
            {rec.math.map((m) => (
              <div key={m.k} style={{ padding: "7px 9px", background: "var(--bg-inset)" }}>
                <div style={{ fontSize: 8.5, letterSpacing: "0.06em", color: "var(--text-9)", fontWeight: 600 }}>
                  {m.k}
                </div>
                <div className="mono" style={{ fontSize: 12, fontWeight: 600, color: MATH_COLOR[m.kind], marginTop: 2 }}>
                  {m.v}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={{ padding: "9px 13px", borderTop: `1px solid ${cardBorder}`, background: "rgba(0,0,0,0.15)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 11 }}>{rec.verdictIcon}</span>
          <span
            className="mono"
            style={{
              fontSize: 9.5,
              fontWeight: 700,
              letterSpacing: "0.08em",
              color: VERDICT_COLOR[rec.verdictKind],
            }}
          >
            AI REVIEWER · {rec.verdict}
          </span>
        </div>
        <div style={{ fontSize: 11, color: "var(--text-4)", marginTop: 4, lineHeight: 1.5 }}>{rec.reasoning}</div>
      </div>

      {pending && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 13px",
            borderTop: `1px solid ${cardBorder}`,
            background: "rgba(var(--accent-rgb),0.05)",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column" }}>
            <span style={{ fontSize: 9, letterSpacing: "0.08em", color: "var(--text-5)", fontWeight: 600 }}>
              EXPIRES IN
            </span>
            <span
              className="mono"
              style={{ fontSize: 14, fontWeight: 700, color: confirmSecs < 30 ? "var(--amb)" : "var(--accent)" }}
            >
              {mmss(confirmSecs)}
            </span>
          </div>
          <ActionButton
            onAct={() => decide(rec.id, "declined")}
            aria-label={`Decline recommendation ${rec.ticker}`}
            className="mono"
            style={{
              marginLeft: "auto",
              padding: "8px 14px",
              borderRadius: 6,
              background: "rgba(255,255,255,0.04)",
              border: "1px solid var(--hairline)",
              fontSize: 11,
              fontWeight: 600,
              color: "var(--text-3)",
            }}
          >
            DECLINE
          </ActionButton>
          <ActionButton
            onAct={() => decide(rec.id, "confirmed")}
            aria-label={`Confirm recommendation ${rec.ticker}`}
            className="mono"
            style={{
              padding: "8px 18px",
              borderRadius: 6,
              background: live ? "var(--grn)" : "rgba(53,208,127,0.14)",
              border: `1px solid ${live ? "var(--grn)" : "rgba(53,208,127,0.4)"}`,
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.04em",
              color: live ? "#04140b" : "var(--grn)",
              boxShadow: live ? "0 0 14px rgba(53,208,127,0.45)" : "none",
            }}
          >
            CONFIRM ▸
          </ActionButton>
        </div>
      )}

      {decision && decidedLabel && (
        <div
          className="mono"
          style={{
            padding: "9px 13px",
            borderTop: `1px solid ${cardBorder}`,
            background: "rgba(0,0,0,0.2)",
            fontSize: 10.5,
            fontWeight: 600,
            color: decision === "confirmed" ? "var(--grn)" : "var(--text-7)",
          }}
        >
          {decidedLabel}
        </div>
      )}
    </div>
  );
}

export function RecsQueue() {
  const { feed } = useCockpit();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.14em", color: "var(--text-5)" }}>
          RECOMMENDATIONS & CONFIRMATION QUEUE
        </span>
        <span className="mono" style={{ fontSize: 10, color: "var(--text-9)" }}>
          cycle {feed.cycleId}
        </span>
      </div>
      {feed.recs.length > 0 ? (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
            gap: 11,
          }}
        >
          {feed.recs.map((r) => (
            <RecCard key={r.id} rec={r} />
          ))}
        </div>
      ) : (
        <div
          style={{
            padding: "30px 20px",
            textAlign: "center",
            border: "1px dashed var(--border-tile)",
            borderRadius: 10,
            background: "var(--bg-rail)",
          }}
        >
          <div style={{ fontSize: 12, color: "var(--text-6)", fontWeight: 600 }}>No pending confirmations</div>
          <div className="mono" style={{ fontSize: 10.5, color: "var(--text-9)", marginTop: 4 }}>
            {feed.noRecsReason}
          </div>
        </div>
      )}
    </div>
  );
}
