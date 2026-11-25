import { useCockpit, type PageId, type ThemeId } from "../store";
import { ActionButton } from "./ActionButton";

const PAGES: { id: PageId; icon: string; label: string; title: string }[] = [
  { id: "dash", icon: "▣", label: "DASH", title: "Dashboard" },
  { id: "trades", icon: "◔", label: "TRDS", title: "Trades & clocks (5 evidence streams)" },
  { id: "tradeview", icon: "◧", label: "TRADE", title: "Trade view: entry, exit and indicators for each trade, live while open" },
  { id: "perf", icon: "◹", label: "PERF", title: "Performance & exits" },
  { id: "research", icon: "⌕", label: "RSCH", title: "Research: RVOL-RANK sleeve & squeeze radar" },
  { id: "health", icon: "❤", label: "SYS", title: "System health" },
  { id: "reports", icon: "▤", label: "RPTS", title: "Reports reader" },
];

const THEMES: { id: ThemeId; icon: string; label: string }[] = [
  { id: "night", icon: "☾", label: "NIGHT" },
  { id: "paper", icon: "¶", label: "PAPER" },
  { id: "amber", icon: "◉", label: "AMBER" },
  { id: "ice", icon: "❄", label: "ICE" },
  { id: "nebula", icon: "✦", label: "NEBULA" },
];

export function NavRail() {
  const { page, setPage, theme, setTheme, feed } = useCockpit();
  // handoff: the Reports nav badge count = sum of open ACTION items
  const actionTotal = feed.reports.reduce((sum, r) => sum + r.actionCount, 0);
  return (
    <div
      style={{
        width: 60,
        flex: "0 0 60px",
        background: "var(--bg-rail)",
        borderRight: "1px solid var(--border-rail)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        padding: "12px 0",
        gap: 4,
        position: "sticky",
        top: 0,
        height: "100vh",
      }}
    >
      <div
        className="mono"
        style={{
          width: 30,
          height: 30,
          borderRadius: 7,
          background: "linear-gradient(135deg, var(--accent), var(--accent-dark))",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontWeight: 700,
          fontSize: 15,
          color: "var(--bg-app)",
          marginBottom: 14,
        }}
      >
        ◆
      </div>
      {PAGES.map((p) => {
        const active = page === p.id;
        const badge = p.id === "reports" && actionTotal > 0 ? String(actionTotal) : undefined;
        return (
          <ActionButton
            key={p.id}
            title={p.title}
            allowRapid onAct={() => setPage(p.id)}
            aria-current={active ? "page" : undefined}
            style={{
              width: 44,
              height: 46,
              borderRadius: 8,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              gap: 3,
              position: "relative",
              background: active ? "rgba(var(--accent-rgb), 0.12)" : "transparent",
              border: `1px solid ${active ? "rgba(var(--accent-rgb), 0.35)" : "transparent"}`,
              color: active ? "var(--accent)" : "var(--text-7)",
            }}
          >
            <div style={{ fontSize: 16, lineHeight: 1 }}>{p.icon}</div>
            <div style={{ fontSize: 7.5, letterSpacing: "0.06em", fontWeight: 600 }}>{p.label}</div>
            {badge && (
              <div
                className="mono"
                style={{
                  position: "absolute",
                  top: 5,
                  right: 6,
                  minWidth: 14,
                  height: 14,
                  padding: "0 3px",
                  borderRadius: 7,
                  background: "var(--amb)",
                  color: "#1a1206",
                  fontSize: 9,
                  fontWeight: 700,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                {badge}
              </div>
            )}
          </ActionButton>
        );
      })}
      <div
        style={{
          marginTop: "auto",
          display: "flex",
          flexDirection: "column",
          gap: 3,
          alignItems: "center",
          paddingTop: 9,
          borderTop: "1px solid var(--border-rail)",
        }}
      >
        {THEMES.map((t) => {
          const active = theme === t.id;
          return (
            <ActionButton
              key={t.id}
              title={`Theme: ${t.label}`}
              allowRapid onAct={() => setTheme(t.id)}
              aria-pressed={active}
              aria-label={`Theme: ${t.label}`}
              style={{
                width: 44,
                height: 32,
                borderRadius: 7,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                gap: 1,
                background: active ? "rgba(var(--accent-rgb), 0.14)" : "transparent",
                border: `1px solid ${active ? "rgba(var(--accent-rgb), 0.32)" : "transparent"}`,
                color: active ? "var(--accent)" : "var(--text-7)",
              }}
            >
              <div style={{ fontSize: 13, lineHeight: 1 }}>{t.icon}</div>
              <div style={{ fontSize: 6.5, letterSpacing: "0.04em", fontWeight: 700 }}>{t.label}</div>
            </ActionButton>
          );
        })}
      </div>
    </div>
  );
}
