import { useState } from "react";
import { useCockpit } from "../store";
import { ActionButton } from "./ActionButton";
import { PositionDetail } from "./PositionDetail";
import type { Position, WatchdogState } from "../feed/types";

const GRID = "150px 58px 60px 1fr 132px 74px 96px 74px";

const WD_STYLE: Record<WatchdogState, { icon: string; label: string; color: string; bg: string; border: string }> = {
  stream: { icon: "⚡", label: "STREAM", color: "var(--grn)", bg: "rgba(53,208,127,0.1)", border: "rgba(53,208,127,0.3)" },
  idle: { icon: "○", label: "IDLE", color: "var(--text-7)", bg: "var(--bg-tile)", border: "var(--border-tile)" },
  degraded: { icon: "▲", label: "5-MIN", color: "var(--amb)", bg: "rgba(240,178,58,0.1)", border: "rgba(240,178,58,0.3)" },
};

function Row({ p, expanded, onToggle }: { p: Position; expanded: boolean; onToggle: () => void }) {
  const long = p.dir === "LONG";
  const wd = WD_STYLE[p.watchdog];
  return (
    // Row click toggles the detail (restored 2026-07-31 per user — the
    // chevron-only expansion introduced in the Phase 2 refactor was a workflow
    // regression). dblclick-preventDefault still blocks the double-click
    // word-select that feeds the OS/Outlook data-detector; single-click drag to
    // select cell text still works (a drag is not a click).
    // Keyboard access to expand is the focusable chevron ActionButton (with
    // aria-expanded); the row onClick is a mouse convenience, so the row itself
    // needs no key handler.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events
    <div
      onClick={onToggle}
      onDoubleClick={(e) => e.preventDefault()}
      style={{
        display: "grid",
        gridTemplateColumns: GRID,
        gap: 10,
        padding: "11px 15px",
        borderBottom: expanded ? "none" : "1px solid var(--border-rail)",
        alignItems: "center",
        background: expanded ? "rgba(var(--accent-rgb),0.04)" : "transparent",
        cursor: "pointer",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 5 }}>
        <ActionButton
          allowRapid
          onAct={(e) => { e.stopPropagation(); onToggle(); }}
          aria-expanded={expanded}
          aria-label={`${expanded ? "Collapse" : "Expand"} ${p.ticker} detail (payoff · price · GEX)`}
          title={expanded ? "collapse detail" : "payoff · price · GEX detail"}
          style={{ color: expanded ? "var(--accent)" : "var(--text-9)", fontSize: 9, padding: "3px 2px", lineHeight: 1 }}
        >
          {expanded ? "▾" : "▸"}
        </ActionButton>
        <div>
          <div className="mono" style={{ fontWeight: 700, fontSize: 13 }}>{p.ticker}</div>
          <div style={{ fontSize: 10, color: "var(--text-6)" }}>{p.structure}</div>
        </div>
      </div>
      <div>
        <span
          className="mono"
          style={{
            fontSize: 10,
            fontWeight: 700,
            padding: "2px 6px",
            borderRadius: 4,
            background: long ? "rgba(53,208,127,0.12)" : "rgba(255,138,106,0.12)",
            color: long ? "var(--grn)" : "var(--dir)",
          }}
        >
          {p.dir}
        </span>
      </div>
      <div className="mono" style={{ fontSize: 13, textAlign: "right" }}>
        ×{p.qty}
      </div>

      {/* MFE / MAE excursion bar */}
      <div>
        <div
          style={{
            height: 8,
            borderRadius: 4,
            background:
              "linear-gradient(90deg, rgba(255,90,95,0.18), rgba(255,90,95,0.05) 48%, rgba(53,208,127,0.05) 52%, rgba(53,208,127,0.18))",
            position: "relative",
          }}
        >
          <div
            style={{
              position: "absolute",
              top: -2,
              bottom: -2,
              width: 2.5,
              borderRadius: 2,
              background: "var(--text-1)",
              left: `${p.markerL}%`,
              boxShadow: "0 0 5px rgba(0,0,0,0.6)",
            }}
          />
          <div
            style={{
              position: "absolute",
              top: "50%",
              left: "50%",
              width: 1,
              height: 12,
              transform: "translate(-50%,-50%)",
              background: "var(--hairline)",
            }}
          />
        </div>
        <div
          className="mono"
          style={{ display: "flex", justifyContent: "space-between", fontSize: 9, marginTop: 3 }}
        >
          <span style={{ color: "#ff7a7e" }}>{p.mae}</span>
          <span style={{ color: "var(--text-9)" }}>entry</span>
          <span style={{ color: "#5adb9a" }}>{p.mfe}</span>
        </div>
      </div>

      <div className="mono" style={{ textAlign: "right", fontSize: 11.5 }}>
        <div style={{ color: "var(--text-4)" }}>{p.entry}</div>
        <div style={{ color: "var(--text-1)", fontWeight: 600 }}>{p.mark}</div>
      </div>

      <div style={{ textAlign: "right" }}>
        <div
          className="mono"
          style={{
            fontSize: 13,
            fontWeight: 700,
            color: p.unrealN > 0 ? "var(--grn)" : p.unrealN < 0 ? "var(--red)" : "var(--text-4)",
          }}
        >
          {p.unreal}
        </div>
        <div className="mono" style={{ fontSize: 9, color: "var(--text-9)" }}>
          ml {p.maxLoss} · {p.age}
        </div>
      </div>

      {/* exit chips V/S/T/F */}
      <div style={{ display: "flex", gap: 3, justifyContent: "center" }}>
        {p.exits.map((e) => (
          <span
            key={e.k}
            title={e.title}
            className="mono"
            style={{
              width: 19,
              height: 19,
              borderRadius: 4,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 9.5,
              fontWeight: 700,
              background: e.armed ? "rgba(var(--accent-rgb),0.14)" : "var(--bg-tile)",
              color: e.armed ? "var(--accent)" : "#3f4854",
              border: `1px solid ${e.armed ? "rgba(var(--accent-rgb),0.3)" : "var(--bg-track)"}`,
            }}
          >
            {e.k}
          </span>
        ))}
      </div>

      {/* watchdog badge */}
      <div style={{ textAlign: "center" }}>
        <span
          className="mono"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            fontSize: 9.5,
            fontWeight: 600,
            padding: "3px 7px",
            borderRadius: 5,
            background: wd.bg,
            color: wd.color,
            border: `1px solid ${wd.border}`,
          }}
        >
          {wd.icon} {wd.label}
        </span>
      </div>
    </div>
  );
}

type Reconcile = {
  ok: boolean;
  error?: string;
  reconciliation?: { confirmed: string[]; stale_tracked: string[]; manual: string[] };
  counts?: { broker_legs: number };
};

export function PositionsTable() {
  const { feed, blended } = useCockpit();
  const positions = blended
    ? feed.positions
    : feed.positions.filter((p) => p.sleeve !== "test");
  const totalUnreal = blended
    ? feed.today.unrealized
    : (feed.today.funnel.unrealized ?? feed.today.unrealized);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [rec, setRec] = useState<Reconcile | null>(null);
  const [recBusy, setRecBusy] = useState(false);
  const [recAt, setRecAt] = useState<string | null>(null);

  const reconcile = () => {
    setRecBusy(true);
    return fetch("/api/reconcile-positions")
      .then((r) => r.json())
      .then((d: Reconcile) => {
        setRec(d);
        setRecAt(new Date().toLocaleTimeString("en-US", { hour12: false, timeZone: "America/Los_Angeles" }));
      })
      .catch((e) => setRec({ ok: false, error: String(e) }))
      .finally(() => setRecBusy(false));
  };
  return (
    <div
      style={{
        border: "1px solid var(--border-panel)",
        borderRadius: 10,
        background: "var(--bg-panel)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "11px 15px",
          borderBottom: "1px solid var(--border-rail)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.14em", color: "var(--text-2)" }}>
            OPEN POSITIONS
          </span>
          <span className="mono" style={{ fontSize: 10, color: "var(--text-9)" }}>
            {positions.length} live
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span className="mono" style={{ fontSize: 11, color: "var(--text-6)" }}>
            total unrealized{" "}
            <b style={{ color: totalUnreal > 0 ? "var(--grn)" : totalUnreal < 0 ? "var(--red)" : "var(--text-4)" }}>
              {`${totalUnreal >= 0 ? "+" : "−"}$${Math.abs(totalUnreal)}`}
            </b>
          </span>
          <ActionButton
            onAct={reconcile}
            disabled={recBusy}
            aria-label="Reconcile open positions against the broker now"
            title="Live Tradier query — refresh against the broker now (bypasses the local cache)"
            className="mono"
            style={{
              fontSize: 9.5, fontWeight: 700, letterSpacing: "0.06em",
              padding: "4px 9px", borderRadius: 5,
              background: recBusy ? "var(--bg-tile)" : "rgba(var(--accent-rgb),0.12)",
              color: recBusy ? "var(--text-7)" : "var(--accent)",
              border: `1px solid ${recBusy ? "var(--border-tile)" : "rgba(var(--accent-rgb),0.3)"}`,
            }}
          >
            {recBusy ? "RECONCILING…" : "⟳ RECONCILE NOW"}
          </ActionButton>
        </div>
      </div>

      {rec && (
        <div
          className="mono"
          style={{
            display: "flex", flexWrap: "wrap", gap: "4px 16px", alignItems: "baseline",
            padding: "8px 15px", borderBottom: "1px solid var(--border-rail)",
            fontSize: 10.5, background: "var(--bg-tile)",
            userSelect: "none", WebkitUserSelect: "none",
          }}
        >
          {!rec.ok ? (
            <span style={{ color: "var(--amb)" }}>reconcile unavailable: {rec.error}</span>
          ) : (
            <>
              <span style={{ color: "var(--text-9)" }}>live broker · {recAt} PT · {rec.counts?.broker_legs ?? 0} legs</span>
              <span style={{ color: "var(--grn)" }}>
                ✓ confirmed {rec.reconciliation?.confirmed.length ?? 0}
                {rec.reconciliation?.confirmed.length ? ` (${rec.reconciliation.confirmed.join(", ")})` : ""}
              </span>
              {(rec.reconciliation?.stale_tracked.length ?? 0) > 0 ? (
                <span style={{ color: "var(--amb)" }}>
                  ⚠ gone at broker, cache pending: {rec.reconciliation!.stale_tracked.join(", ")}
                  {" "}
                  <span style={{ color: "var(--text-8)" }}>
                    — normal for ≤5 min (book syncs each cycle); persists longer → real desync,
                    run reconcile_positions.py
                  </span>
                </span>
              ) : (
                <span style={{ color: "var(--text-8)" }}>no stale cache rows</span>
              )}
              {(rec.reconciliation?.manual.length ?? 0) > 0 && (
                <span style={{ color: "var(--text-7)" }}>
                  manual · not agent-managed: {rec.reconciliation!.manual.join(", ")}
                </span>
              )}
            </>
          )}
        </div>
      )}
      {positions.length > 0 ? (
        <div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: GRID,
              gap: 10,
              padding: "7px 15px",
              borderBottom: "1px solid var(--border-rail)",
              fontSize: 9.5,
              letterSpacing: "0.08em",
              color: "var(--text-9)",
              fontWeight: 600,
            }}
          >
            <div>TICKER · STRUCTURE</div>
            <div>DIR</div>
            <div style={{ textAlign: "right" }}>QTY</div>
            <div>MFE / MAE</div>
            <div style={{ textAlign: "right" }}>ENTRY → MARK</div>
            <div style={{ textAlign: "right" }}>UNREAL</div>
            <div style={{ textAlign: "center" }}>EXITS</div>
            <div style={{ textAlign: "center" }}>WATCHDOG</div>
          </div>
          {positions.map((p) => (
            <div key={p.recId}>
              <Row
                p={p}
                expanded={expanded === p.recId}
                onToggle={() => setExpanded(expanded === p.recId ? null : p.recId)}
              />
              {expanded === p.recId && (
                <div style={{ borderBottom: "1px solid var(--border-rail)" }}>
                  <PositionDetail recId={p.recId} />
                </div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div style={{ padding: "38px 20px", textAlign: "center" }}>
          <div style={{ fontSize: 22, color: "#2a323d", marginBottom: 6 }}>—</div>
          <div style={{ fontSize: 12, color: "var(--text-6)", fontWeight: 600 }}>No open positions</div>
          <div className="mono" style={{ fontSize: 10.5, color: "var(--text-9)", marginTop: 4 }}>
            {feed.noPositionsReason}
          </div>
        </div>
      )}
    </div>
  );
}
