import { useCockpit } from "../store";
import type { ClockChip, ClockRing } from "../feed/types";

const R = 38;
const CIRC = 2 * Math.PI * R;

function RingGauge({ ring }: { ring: ClockRing }) {
  const frac = ring.done / ring.goal;
  return (
    <div
      style={{
        flex: 1,
        minWidth: 190,
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: "14px 16px",
        borderRadius: 10,
        background: "var(--bg-inset)",
        border: "1px solid var(--border-inset)",
      }}
    >
      <div style={{ position: "relative", width: 88, height: 88, flex: "0 0 88px" }}>
        <svg width="88" height="88" viewBox="0 0 88 88" style={{ transform: "rotate(-90deg)" }}>
          <circle cx="44" cy="44" r={R} fill="none" stroke="var(--border-inset)" strokeWidth="9" />
          <circle
            cx="44"
            cy="44"
            r={R}
            fill="none"
            stroke="var(--accent)"
            strokeWidth="9"
            strokeLinecap="round"
            strokeDasharray={`${(CIRC * frac).toFixed(1)} ${CIRC.toFixed(1)}`}
          />
        </svg>
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <span className="mono" style={{ fontSize: 19, fontWeight: 700, lineHeight: 1 }}>
            {ring.done}
          </span>
          <span className="mono" style={{ fontSize: 10, color: "var(--text-9)" }}>
            / {ring.goal}
          </span>
        </div>
      </div>
      <div>
        <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-2)" }}>{ring.title}</div>
        <div className="mono" style={{ fontSize: 10.5, color: "var(--text-7)", marginTop: 3 }}>
          {ring.sub}
        </div>
        <div className="mono" style={{ fontSize: 11, fontWeight: 700, color: "var(--accent)", marginTop: 8 }}>
          {Math.round(frac * 100)}% complete
        </div>
      </div>
    </div>
  );
}

const CHIP_STYLE = {
  pass: { color: "var(--grn)", icon: "✓", bg: "rgba(53,208,127,0.07)", border: "rgba(53,208,127,0.28)" },
  fail: { color: "var(--red)", icon: "✕", bg: "rgba(255,90,95,0.07)", border: "rgba(255,90,95,0.28)" },
  wait: { color: "var(--text-7)", icon: "○", bg: "var(--bg-tile)", border: "var(--border-tile)" },
} as const;

function CriterionChip({ chip }: { chip: ClockChip }) {
  const s = CHIP_STYLE[chip.state];
  return (
    <div
      style={{
        flex: 1,
        minWidth: 118,
        display: "flex",
        flexDirection: "column",
        gap: 5,
        padding: "10px 12px",
        borderRadius: 8,
        background: s.bg,
        border: `1px solid ${s.border}`,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ fontSize: 10.5, fontWeight: 600, color: "var(--text-3)" }}>{chip.label}</span>
        <span style={{ fontSize: 12, color: s.color }}>{s.icon}</span>
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
        <span className="mono" style={{ fontSize: 14, fontWeight: 700, color: s.color }}>
          {chip.value}
        </span>
        <span className="mono" style={{ fontSize: 9.5, color: "var(--text-9)" }}>
          {chip.n}
        </span>
      </div>
    </div>
  );
}

/** The hero: progress toward the frozen live-money criteria. Deliberately
 * out-weighs today's P&L — it governs the system. */
export function ForwardClock() {
  const { forwardClock: fc } = useCockpit((s) => s.feed);
  return (
    <div
      style={{
        border: "1px solid var(--hero-border)",
        borderRadius: 12,
        background: "var(--hero-bg)",
        boxShadow: "0 0 0 1px rgba(var(--accent-rgb),0.06), 0 8px 30px rgba(0,0,0,0.4)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          height: 3,
          background: "linear-gradient(90deg, var(--accent), var(--accent-dark) 40%, transparent)",
        }}
      />
      <div style={{ padding: "16px 20px 18px" }}>
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "space-between",
            marginBottom: 16,
          }}
        >
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <h2 style={{ margin: 0, fontSize: 16, fontWeight: 700, letterSpacing: "0.02em" }}>
                Forward-Acceptance Clock
              </h2>
              <span
                className="mono"
                style={{
                  fontSize: 9.5,
                  fontWeight: 700,
                  letterSpacing: "0.12em",
                  padding: "2px 7px",
                  borderRadius: 4,
                  background: "var(--bg-track)",
                  color: "var(--text-5)",
                }}
              >
                FROZEN CRITERIA
              </span>
            </div>
            <div style={{ fontSize: 11.5, color: "var(--text-6)", marginTop: 3 }}>
              Every gate must pass before live-money authorization. This governs the system — not
              today's P&L.
            </div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 7,
                padding: "5px 12px",
                borderRadius: 6,
                background: "rgba(240,178,58,0.08)",
                border: "1px solid rgba(240,178,58,0.3)",
              }}
            >
              <span style={{ fontSize: 13 }}>{fc.gate.icon}</span>
              <span
                className="mono"
                style={{ fontSize: 12, fontWeight: 700, letterSpacing: "0.1em", color: "var(--amb)" }}
              >
                {fc.gate.label}
              </span>
            </div>
            <div className="mono" style={{ fontSize: 10, color: "var(--text-9)" }}>
              est. ready ~ {fc.gate.eta}
            </div>
          </div>
        </div>

        <div style={{ display: "flex", gap: 16, alignItems: "stretch", flexWrap: "wrap" }}>
          {fc.rings.map((r) => (
            <RingGauge key={r.title} ring={r} />
          ))}
          <div
            style={{
              flex: "0 0 168px",
              display: "flex",
              flexDirection: "column",
              justifyContent: "center",
              gap: 8,
              padding: "14px 16px",
              borderRadius: 10,
              background: "var(--bg-inset)",
              border: "1px solid var(--border-inset)",
            }}
          >
            <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-2)" }}>
              Adverse-regime week
            </div>
            <div className="mono" style={{ fontSize: 10, color: "var(--text-7)" }}>
              Survived a hostile tape?
            </div>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                marginTop: 4,
                padding: "5px 10px",
                borderRadius: 6,
                background: fc.adverse.met ? "rgba(53,208,127,0.07)" : "var(--bg-tile)",
                border: `1px solid ${fc.adverse.met ? "rgba(53,208,127,0.28)" : "var(--border-tile)"}`,
                alignSelf: "flex-start",
              }}
            >
              <span style={{ fontSize: 12, color: fc.adverse.met ? "var(--grn)" : "var(--text-7)" }}>
                {fc.adverse.met ? "✓" : "○"}
              </span>
              <span
                className="mono"
                style={{
                  fontSize: 12,
                  fontWeight: 700,
                  letterSpacing: "0.06em",
                  color: fc.adverse.met ? "var(--grn)" : "var(--text-7)",
                }}
              >
                {fc.adverse.label}
              </span>
            </div>
          </div>
        </div>

        <div style={{ display: "flex", gap: 9, marginTop: 14, flexWrap: "wrap" }}>
          {fc.chips.map((c) => (
            <CriterionChip key={c.label} chip={c} />
          ))}
        </div>
      </div>
    </div>
  );
}
