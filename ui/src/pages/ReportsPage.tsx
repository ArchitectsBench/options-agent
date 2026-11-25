import { useCockpit } from "../store";
import { ActionButton } from "../components/ActionButton";
import type { ReportBlock, ReportSpan } from "../feed/types";

const SPAN_COLOR: Record<NonNullable<ReportSpan["color"]>, string> = {
  grn: "var(--grn)",
  amb: "var(--amb)",
  red: "var(--red)",
  bright: "var(--text-1)",
};

function Span({ s }: { s: ReportSpan }) {
  const style: React.CSSProperties = {
    ...(s.mono ? { fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums" } : {}),
    ...(s.color ? { color: SPAN_COLOR[s.color] } : {}),
  };
  if (s.bold) return <b style={style}>{s.text}</b>;
  if (s.mono || s.color) return <span style={style}>{s.text}</span>;
  return <>{s.text}</>;
}

const bodyText: React.CSSProperties = {
  color: "var(--text-3)",
  fontSize: 14,
  lineHeight: 1.7,
};

function Block({ b, last }: { b: ReportBlock; last: boolean }) {
  switch (b.kind) {
    case "callout": {
      const warn = b.tone === "warn";
      return (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            margin: "20px 0",
            padding: "13px 16px",
            borderRadius: 9,
            background: warn ? "rgba(240,178,58,0.07)" : "rgba(53,208,127,0.06)",
            border: `1px solid ${warn ? "rgba(240,178,58,0.3)" : "rgba(53,208,127,0.28)"}`,
          }}
        >
          <span
            className="mono"
            style={{
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.08em",
              color: warn ? "var(--amb)" : "var(--grn)",
            }}
          >
            {b.text}
          </span>
        </div>
      );
    }
    case "h2":
      return (
        <h2
          style={{
            fontSize: 15,
            fontWeight: 700,
            margin: "24px 0 10px",
            paddingBottom: 7,
            borderBottom: "1px solid var(--border-inset)",
          }}
        >
          {b.text}
        </h2>
      );
    case "list": {
      const Tag = b.ordered ? "ol" : "ul";
      return (
        <Tag style={{ ...bodyText, margin: last ? 0 : "0 0 20px", paddingLeft: 20 }}>
          {b.items.map((item, i) => (
            <li
              key={i}
              style={b.ordered && i < b.items.length - 1 ? { marginBottom: 8 } : undefined}
            >
              {item.map((s, j) => (
                <Span key={j} s={s} />
              ))}
            </li>
          ))}
        </Tag>
      );
    }
    case "para":
      return (
        <p style={{ ...bodyText, margin: last ? 0 : "0 0 14px" }}>
          {b.spans.map((s, j) => (
            <Span key={j} s={s} />
          ))}
        </p>
      );
  }
}

export function ReportsPage() {
  const { feed, report, setReport } = useCockpit();
  const active = feed.reports.find((r) => r.id === report) ?? feed.reports[0];

  return (
    <div style={{ flex: 1, display: "flex", gap: 14, padding: 14, alignItems: "flex-start" }}>
      {/* report list */}
      <div style={{ flex: "0 0 250px", width: 250, display: "flex", flexDirection: "column", gap: 9 }}>
        <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.14em", color: "var(--text-5)" }}>
          REPORTS
        </span>
        {feed.reports.map((r) => {
          const on = r.id === active.id;
          return (
            <ActionButton
              key={r.id}
              allowRapid onAct={() => setReport(r.id)}
              aria-pressed={on}
              aria-label={`Report: ${r.title}`}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: "12px 14px",
                borderRadius: 9,
                background: on ? "rgba(var(--accent-rgb),0.1)" : "var(--bg-panel)",
                border: `1px solid ${on ? "rgba(var(--accent-rgb),0.35)" : "var(--border-panel)"}`,
              }}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span
                  style={{
                    fontWeight: 600,
                    fontSize: 13,
                    color: on ? "var(--text-1)" : "var(--text-3)",
                  }}
                >
                  {r.title}
                </span>
                {r.actionCount > 0 && (
                  <span
                    className="mono"
                    style={{
                      fontSize: 9,
                      fontWeight: 700,
                      padding: "1px 6px",
                      borderRadius: 8,
                      background: "var(--amb)",
                      color: "#1a1206",
                    }}
                  >
                    {r.actionCount} ACTION
                  </span>
                )}
              </div>
              <div className="mono" style={{ fontSize: 10, color: "var(--text-7)", marginTop: 3 }}>
                {r.date}
              </div>
            </ActionButton>
          );
        })}
      </div>

      {/* report document */}
      <div
        style={{
          flex: 1,
          minWidth: 0,
          border: "1px solid var(--border-panel)",
          borderRadius: 10,
          background: "var(--bg-panel)",
          maxHeight: "calc(100vh - 120px)",
          overflowY: "auto",
        }}
      >
        <div style={{ padding: "26px 34px", maxWidth: 760 }}>
          <div className="mono" style={{ fontSize: 10, letterSpacing: "0.14em", color: "var(--text-9)" }}>
            {active.kicker}
          </div>
          <h1 style={{ margin: "8px 0 4px", fontSize: 24, fontWeight: 700, letterSpacing: "-0.01em" }}>
            {active.heading}
          </h1>
          <div style={{ fontSize: 13, color: "var(--text-5)" }}>{active.subtitle}</div>
          {active.blocks.map((b, i) => (
            <Block key={i} b={b} last={i === active.blocks.length - 1} />
          ))}
        </div>
      </div>
    </div>
  );
}
