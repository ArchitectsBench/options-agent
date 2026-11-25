import { useCockpit } from "../store";
import type { LogTag } from "../feed/types";

const TAG_STYLE: Record<LogTag, { bg: string; color: string; accent: string }> = {
  cycle: { bg: "#161d26", color: "var(--text-5)", accent: "#2a323d" },
  fill: { bg: "rgba(var(--accent-rgb),0.15)", color: "var(--accent)", accent: "var(--accent)" },
  exit: { bg: "rgba(53,208,127,0.15)", color: "var(--grn)", accent: "var(--grn)" },
  safety: { bg: "rgba(240,178,58,0.15)", color: "var(--amb)", accent: "var(--amb)" },
  alarm: { bg: "rgba(255,90,95,0.15)", color: "var(--red)", accent: "var(--red)" },
  feed: { bg: "rgba(255,138,106,0.14)", color: "var(--dir)", accent: "var(--dir)" },
  // SCALP-v2 flow events (entry_ioc_unfilled, price-floor blocks) — the design
  // working, not incidents, so muted rather than amber.
  v2: { bg: "rgba(130,150,190,0.14)", color: "var(--text-5)", accent: "#3b4a63" },
};

// A kind the UI does not know yet must never blank the whole panel: on
// 2026-09-14 the first "v2" event (added server-side 09-12) did exactly that.
export function tagStyle(kind: string) {
  return TAG_STYLE[kind as LogTag] ?? TAG_STYLE.cycle;
}

const META_COLOR = { p: "var(--grn)", n: "var(--red)", mut: "var(--text-9)" } as const;

export function ActivityLog() {
  const { feed } = useCockpit();
  return (
    <div
      style={{
        flex: "0 0 340px",
        width: 340,
        position: "sticky",
        top: 14,
        alignSelf: "flex-start",
        border: "1px solid var(--border-panel)",
        borderRadius: 10,
        background: "var(--bg-panel)",
        display: "flex",
        flexDirection: "column",
        maxHeight: "calc(100vh - 28px)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "11px 14px",
          borderBottom: "1px solid var(--border-rail)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--grn)" }} />
          <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.12em", color: "var(--text-2)" }}>
            ACTIVITY LOG
          </span>
        </div>
        <span className="mono" style={{ fontSize: 9.5, color: "var(--text-9)" }}>
          {feed.dataAge}
        </span>
      </div>
      <div style={{ overflowY: "auto", padding: "6px 0" }}>
        {feed.logs.map((l, i) => {
          const t = tagStyle(l.kind);
          return (
            <div
              key={`${l.time}-${i}`}
              style={{ display: "flex", gap: 10, padding: "8px 14px", borderBottom: "1px solid var(--border-rail)" }}
            >
              <span style={{ width: 3, flex: "0 0 3px", borderRadius: 2, background: t.accent }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                  <span className="mono" style={{ fontSize: 9.5, color: "var(--text-9)" }}>
                    {l.time}
                  </span>
                  <span
                    className="mono"
                    style={{
                      fontSize: 8.5,
                      fontWeight: 700,
                      letterSpacing: "0.06em",
                      padding: "1px 5px",
                      borderRadius: 3,
                      background: t.bg,
                      color: t.color,
                    }}
                  >
                    {l.tag}
                  </span>
                </div>
                <div style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 3, lineHeight: 1.45 }}>{l.text}</div>
                {l.meta && (
                  <div
                    className="mono"
                    style={{ fontSize: 9.5, color: META_COLOR[l.metaKind ?? "mut"], marginTop: 2 }}
                  >
                    {l.meta}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
