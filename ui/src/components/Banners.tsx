import { useCockpit } from "../store";
import { ActionButton } from "./ActionButton";
import { useArmed } from "./useArmed";

/** Top-of-layout status strips. SimulatedBanner is the one App.tsx mounts, so it
 * also hosts the two SEV1 R7 truth banners (DISCONNECTED / KILL COMMAND FAILED)
 * — each renders only when its own condition holds; otherwise nothing shows. */
export function SimulatedBanner() {
  return (
    <>
      <DisconnectedBanner />
      <KillCommandFailedBanner />
      <SimulatedDataBanner />
    </>
  );
}

/** Unmissable notice while the UI renders mock data (no live feed wired).
 * Positions, P&L, and logs on screen are design-prototype samples, NOT the
 * real book. Removed automatically when the API feed sets simulated=false. */
export function SimulatedDataBanner() {
  const { feed } = useCockpit();
  if (!feed.simulated) return null;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 10,
        background: "rgba(var(--accent-rgb),0.10)",
        borderBottom: "1px solid rgba(var(--accent-rgb),0.35)",
        padding: "6px 14px",
      }}
    >
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: "50%",
          background: "var(--accent)",
          animation: "pulseDot 1.4s infinite",
        }}
      />
      <span
        className="mono"
        style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.18em", color: "var(--accent)" }}
      >
        SIMULATED DATA — LIVE FEED NOT CONNECTED · positions and P&L shown are design samples, not
        the real book
      </span>
    </div>
  );
}

/** SEV1 R7 — HARD, unmissable banner when the live feed has gone silent past the
 * staleness threshold. Everything on screen (positions, P&L, and any ENGAGED
 * kill state) is FROZEN and may not reflect reality until the feed resumes.
 * This is the guarantee that a stale ENGAGED can never be shown alone. */
export function DisconnectedBanner() {
  const { stale, killEngaged } = useCockpit();
  if (!stale) return null;
  return (
    <div
      role="alert"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 12,
        background: "linear-gradient(90deg, #4a0d0f, #7a1418, #4a0d0f)",
        borderBottom: "2px solid #ff4d54",
        padding: "9px 16px",
      }}
    >
      <span
        style={{
          width: 9,
          height: 9,
          borderRadius: "50%",
          background: "#ff4d54",
          animation: "pulseDot 1s infinite",
        }}
      />
      <span
        className="mono"
        style={{ fontSize: 12, fontWeight: 800, letterSpacing: "0.16em", color: "#ffd0d2" }}
      >
        ⚠ DISCONNECTED — LIVE FEED SILENT · numbers on screen are FROZEN and may be stale
        {killEngaged ? " · KILL STATE SHOWN MAY NOT REFLECT THE ENGINE" : ""}
      </span>
    </div>
  );
}

/** SEV1 R7 — the /api/kill POST did not land. The kill state is UNCHANGED (real
 * last-known); we never fake ENGAGED. Tell the operator the command failed so
 * they retry rather than trusting a phantom toggle. */
export function KillCommandFailedBanner() {
  const { killError } = useCockpit();
  if (!killError) return null;
  return (
    <div
      role="alert"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 12,
        background: "linear-gradient(90deg, #4a0d0f, #7a1418, #4a0d0f)",
        borderBottom: "2px solid #ff4d54",
        padding: "9px 16px",
      }}
    >
      <span
        style={{
          width: 9,
          height: 9,
          borderRadius: "50%",
          background: "#ff4d54",
          animation: "pulseDot 1s infinite",
        }}
      />
      <span
        className="mono"
        style={{ fontSize: 12, fontWeight: 800, letterSpacing: "0.16em", color: "#ffd0d2" }}
      >
        ⚠ KILL SWITCH COMMAND FAILED — engine state UNCHANGED · the toggle did not land, retry
      </span>
    </div>
  );
}

/** Paper-only stamp strip (or LIVE ribbon), always at the very top. */
export function StampStrip() {
  const { feed } = useCockpit();
  if (feed.mode === "live") {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 10,
          background: "#2a0e10",
          borderBottom: "1px solid #5a1a1e",
          padding: "5px 14px",
        }}
      >
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: "var(--red)",
            animation: "pulseDot 1.4s infinite",
          }}
        />
        <span
          className="mono"
          style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.22em", color: "#ff8b8f" }}
        >
          ● LIVE MODE — REAL CAPITAL AT RISK · CONFIRMATIONS ARE BINDING
        </span>
      </div>
    );
  }
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 10,
        background:
          "repeating-linear-gradient(135deg, #161207, #161207 12px, #1a1608 12px, #1a1608 24px)",
        borderBottom: "1px solid #2c2410",
        padding: "5px 14px",
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: 1, background: "var(--amb)" }} />
      <span
        className="mono"
        style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: "0.22em", color: "#e0b256" }}
      >
        UNVALIDATED — PAPER ONLY · NO REAL CAPITAL AT RISK · ALL PERFORMANCE PROVISIONAL
      </span>
    </div>
  );
}

/** Full-width red banner while the kill switch is engaged. */
export function KillBanner() {
  const { killEngaged, toggleKill } = useCockpit();
  const { armed, arm, disarm } = useArmed();
  if (!killEngaged) return null;
  // two-step confirm to disengage (this banner only shows while engaged)
  const onDisengage = () => {
    if (!armed) { arm(); return; }
    disarm();
    return toggleKill();
  };
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 16,
        background: "linear-gradient(90deg, #3a0d0f, #5a1418, #3a0d0f)",
        borderBottom: "1px solid #7a1c22",
        padding: "11px 20px",
      }}
    >
      <span
        className="mono"
        style={{ fontWeight: 700, fontSize: 14, letterSpacing: "0.14em", color: "#ff9a9e" }}
      >
        ⏻ KILL SWITCH ENGAGED
      </span>
      <span style={{ fontSize: 12.5, color: "#ffc9cb" }}>
        No new orders will be placed. <b style={{ color: "#fff" }}>Exit management remains active</b>{" "}
        — open positions are still protected.
      </span>
      <ActionButton
        onAct={onDisengage}
        // No cooldownMs (fixed 2026-07-31): same arm-then-confirm race as the TopBar
        // kill button — a cooldown disables the button during the "CONFIRM?" window
        // and swallows the confirm click. Two-step arm + in-flight disable suffice.
        aria-label={armed ? "Confirm disengage kill switch" : "Disengage kill switch (two-step)"}
        title="Disengage the kill switch (two-step confirm)"
        className="mono"
        style={{
          marginLeft: "auto",
          padding: "6px 16px",
          borderRadius: 6,
          background: armed ? "#ffb000" : "rgba(255,255,255,0.08)",
          border: `1px solid ${armed ? "#ffd060" : "rgba(255,255,255,0.25)"}`,
          fontSize: 11,
          fontWeight: 600,
          letterSpacing: "0.08em",
          color: armed ? "#1a1206" : "#fff",
        }}
      >
        {armed ? "CONFIRM DISENGAGE" : "DISENGAGE"}
      </ActionButton>
    </div>
  );
}
