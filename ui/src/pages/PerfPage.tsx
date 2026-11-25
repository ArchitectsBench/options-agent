import { useCockpit } from "../store";
import { ActionButton } from "../components/ActionButton";
import type { ExitTone, RoundTrip } from "../feed/types";

const cur = (n: number) =>
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
const pnlColor = (n: number) => (n > 0 ? "var(--grn)" : n < 0 ? "var(--red)" : "var(--text-4)");

const TONE: Record<ExitTone, { color: string; bg: string }> = {
  g: { color: "var(--grn)", bg: "rgba(53,208,127,0.1)" },
  r: { color: "var(--red)", bg: "rgba(255,90,95,0.1)" },
  a: { color: "var(--amb)", bg: "rgba(240,178,58,0.12)" },
  mut: { color: "var(--text-5)", bg: "rgba(130,130,130,0.1)" },
};
// Never index TONE with a bare key: an unknown tone (e.g. a new backend value)
// must degrade to muted, not throw and unmount the whole app.
const toneOf = (t: string): { color: string; bg: string } =>
  (TONE as Record<string, { color: string; bg: string }>)[t] ?? TONE.mut;

const panel: React.CSSProperties = {
  border: "1px solid var(--border-panel)",
  borderRadius: 10,
  background: "var(--bg-panel)",
};
const sectionTitle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.14em",
  color: "var(--text-2)",
};
const groupTitle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.14em",
  color: "var(--text-5)",
};

/** ROUND-TRIP DETAIL grid — handoff-specified column widths */
const GRID = "96px 60px 74px 128px 96px 84px 60px 74px 56px";

function TradeRow({ tr }: { tr: RoundTrip }) {
  const tone = toneOf(tr.exitTone);
  return (
    <div
      className="mono"
      style={{
        display: "grid",
        gridTemplateColumns: GRID,
        gap: 10,
        padding: "9px 15px",
        borderBottom: "1px solid var(--border-rail)",
        alignItems: "center",
        fontSize: 11,
        background: tr.test ? "rgba(240,178,58,0.04)" : "transparent",
      }}
    >
      <div style={{ color: "var(--text-5)", fontSize: 10 }}>{tr.time}</div>
      <div style={{ fontWeight: 700 }}>
        {tr.ticker}
        {tr.test && <span style={{ fontSize: 8, color: "var(--amb)" }}> TEST</span>}
      </div>
      <div style={{ color: "var(--text-4)", fontSize: 10 }}>{tr.sleeve}</div>
      <div style={{ color: "var(--text-3)", fontSize: 10 }}>
        <span style={{ color: tr.dir === "LONG" ? "var(--grn)" : "var(--dir)" }}>{tr.dir}</span>{" "}
        {tr.struct}
      </div>
      <div style={{ color: "var(--text-3)" }}>{tr.px}</div>
      <div>
        <span
          style={{
            fontSize: 9.5,
            fontWeight: 600,
            padding: "2px 6px",
            borderRadius: 4,
            background: tone.bg,
            color: tone.color,
          }}
        >
          {tr.exit}
        </span>
      </div>
      <div style={{ textAlign: "right", color: "var(--text-4)" }}>{tr.slip}</div>
      <div style={{ textAlign: "right", fontWeight: 700, color: pnlColor(tr.pnlN) }}>{tr.pnl}</div>
      <div style={{ textAlign: "right", color: "var(--text-5)" }}>{tr.giveBack}</div>
    </div>
  );
}

export function PerfPage() {
  const { feed, blended, setBlended } = useCockpit();
  const perf = feed.perf;

  const series = perf.daily.map((s) => (blended ? s.blended : s.funnel));
  const maxMag = Math.max(...series.map((v) => Math.abs(v)), 1);
  const cum = series.reduce((a, b) => a + b, 0);
  const tset = blended ? perf.truth?.blended : perf.truth?.funnel;
  const exMax = Math.max(...perf.exitReasons.map((e) => e.count), 1);
  const slipMax = Math.max(...perf.slippage.buckets.map((b) => b.n), 1);
  const trades = blended ? [...perf.trades, ...perf.testTrades] : perf.trades;

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 14, padding: 14 }}>
      {/* header + view toggle */}
      <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 18, fontWeight: 700, letterSpacing: "-0.01em" }}>
            Performance &amp; Exits
          </h1>
          <div className="mono" style={{ fontSize: 10.5, marginTop: 4, display: "flex", flexWrap: "wrap", gap: "2px 10px", alignItems: "baseline" }}>
            <span style={{
              fontSize: 8.5, fontWeight: 700, letterSpacing: "0.08em", padding: "2px 7px",
              borderRadius: 4, background: "rgba(var(--accent-rgb),0.14)", color: "var(--accent)",
            }}>
              MARKET-TRUTH (NBBO MID) BASIS
            </span>
            {tset ? (
              <>
                <span style={{ color: "var(--text-6)" }}>
                  realized <b style={{ color: pnlColor(tset.realized) }}>{cur(tset.realized)}</b>
                </span>
                <span style={{ color: "var(--text-9)" }}>
                  · {tset.scored} scored{tset.pending ? `, ${tset.pending} pending` : ""} · {perf.daily.length} sessions
                </span>
              </>
            ) : (
              <span style={{ color: "var(--text-9)" }}>{perf.daily.length} sessions · coverage unavailable</span>
            )}
          </div>
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
                padding: "6px 12px",
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
                padding: "6px 12px",
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
            padding: "7px 12px",
            borderRadius: 6,
            background: "#231803",
            border: "1px solid #4a3a10",
          }}
        >
          <span style={{ fontSize: 11 }}>⚠</span>
          <span className="mono" style={{ fontSize: 11, color: "#e0c98a" }}>
            BLENDED VIEW — funnel + SCALP-candidate streams merged for a whole-book view.
            Score each stream only against its own gate.
          </span>
        </div>
      )}

      {/* daily P&L chart */}
      <div style={{ ...panel, padding: "14px 16px" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 14,
          }}
        >
          <span style={sectionTitle}>DAILY REALIZED P&L</span>
          <span className="mono" style={{ fontSize: 11, color: "var(--text-6)" }}>
            cumulative <b style={{ color: pnlColor(cum) }}>{cur(cum)}</b>
          </span>
        </div>
        <div style={{ position: "relative", display: "flex", alignItems: "stretch", gap: 4, height: 150 }}>
          <div
            style={{
              position: "absolute",
              left: 0,
              right: 0,
              top: "50%",
              height: 1,
              background: "var(--border-tile)",
            }}
          />
          {perf.daily.map((s, i) => {
            const v = series[i];
            return (
              <div key={s.day} title={`${s.day} · ${cur(v)}`} style={{ position: "relative", flex: 1, height: 150 }}>
                <div
                  style={{
                    position: "absolute",
                    left: 2,
                    right: 2,
                    top: v >= 0 ? "auto" : "50%",
                    bottom: v >= 0 ? "50%" : "auto",
                    height: Math.max(2, Math.round((Math.abs(v) / maxMag) * 74)),
                    background: v > 0 ? "var(--grn)" : v < 0 ? "var(--red)" : "var(--hairline)",
                    borderRadius: 2,
                  }}
                />
              </div>
            );
          })}
        </div>
        <div style={{ display: "flex", gap: 4, marginTop: 6 }}>
          {perf.daily.map((s) => (
            <div
              key={s.day}
              className="mono"
              style={{ flex: 1, textAlign: "center", fontSize: 7.5, color: "var(--text-9)" }}
            >
              {s.day}
            </div>
          ))}
        </div>
      </div>

      {/* sleeves + exit reasons */}
      <div style={{ display: "flex", gap: 14, alignItems: "stretch" }}>
        <div style={{ flex: 1.1, display: "flex", flexDirection: "column", gap: 9 }}>
          <span style={groupTitle}>BY SLEEVE</span>
          {perf.sleeves.map((s) => (
            <div
              key={s.name}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 14,
                padding: "12px 15px",
                borderRadius: 9,
                background: s.accent ? "rgba(var(--accent-rgb),0.06)" : "var(--bg-panel)",
                border: `1px solid ${s.accent ? "rgba(var(--accent-rgb),0.3)" : "var(--border-panel)"}`,
              }}
            >
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 13, fontWeight: 600 }}>{s.name}</span>
                  <span
                    className="mono"
                    style={{
                      fontSize: 8.5,
                      fontWeight: 700,
                      letterSpacing: "0.06em",
                      color: s.accent ? "var(--accent)" : "var(--text-9)",
                    }}
                  >
                    {s.tag}
                  </span>
                </div>
                <div className="mono" style={{ fontSize: 10, color: "var(--text-7)", marginTop: 2 }}>
                  {s.note}
                </div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div className="mono" style={{ fontSize: 9, color: "var(--text-9)" }}>
                  WIN
                </div>
                <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>
                  {s.win}{" "}
                  <span style={{ color: "var(--text-9)", fontSize: 10 }}>n={s.trades}</span>
                </div>
              </div>
              <div style={{ textAlign: "right", minWidth: 70 }}>
                <div className="mono" style={{ fontSize: 9, color: "var(--text-9)" }}>
                  P&L
                </div>
                <div
                  className="mono"
                  style={{ fontSize: 16, fontWeight: 700, color: pnlColor(s.pnl) }}
                >
                  {cur(s.pnl)}
                </div>
              </div>
            </div>
          ))}
        </div>

        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 9 }}>
          <span style={groupTitle}>BY EXIT REASON</span>
          <div
            style={{
              ...panel,
              flex: 1,
              borderRadius: 9,
              padding: "12px 15px",
              display: "flex",
              flexDirection: "column",
              gap: 11,
              justifyContent: "center",
            }}
          >
            {perf.exitReasons.map((e) => (
              <div key={e.label}>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: 4,
                  }}
                >
                  <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
                    {e.label}{" "}
                    <span className="mono" style={{ fontSize: 10, color: "var(--text-9)" }}>
                      ×{e.count}
                    </span>
                  </span>
                  <span
                    className="mono"
                    style={{ fontSize: 12, fontWeight: 600, color: pnlColor(e.pnl) }}
                  >
                    {cur(e.pnl)}
                  </span>
                </div>
                <div style={{ height: 6, borderRadius: 3, background: "var(--bg-tile)", overflow: "hidden" }}>
                  <div
                    style={{
                      height: "100%",
                      width: `${Math.round((e.count / exMax) * 100)}%`,
                      background: toneOf(e.tone).color,
                      borderRadius: 3,
                      opacity: 0.75,
                    }}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* exit quality + slippage */}
      <div style={{ display: "flex", gap: 14, alignItems: "stretch" }}>
        <div
          style={{
            flex: 1,
            border: "1px solid #2c2410",
            borderRadius: 9,
            background: "#12100a",
            padding: "14px 16px",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 12 }}>
            <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.14em", color: "#e0b256" }}>
              EXIT QUALITY · GIVE-BACK vs PEAK
            </span>
            <span
              className="mono"
              style={{
                fontSize: 8.5,
                fontWeight: 700,
                letterSpacing: "0.06em",
                padding: "2px 7px",
                borderRadius: 4,
                background: "rgba(240,178,58,0.14)",
                color: "var(--amb)",
              }}
            >
              LIVE WATCH ITEM
            </span>
          </div>
          <div style={{ display: "flex", gap: 22, alignItems: "flex-end" }}>
            <div>
              <div className="mono" style={{ fontSize: 32, fontWeight: 700, color: "var(--amb)", lineHeight: 1 }}>
                {perf.giveBack.avg}
              </div>
              <div className="mono" style={{ fontSize: 10, color: "#8a7a4a", marginTop: 4 }}>
                avg give-back from peak
              </div>
            </div>
            <div style={{ borderLeft: "1px solid #2c2410", paddingLeft: 22 }}>
              <div className="mono" style={{ fontSize: 22, fontWeight: 700, color: "var(--text-1)", lineHeight: 1 }}>
                {perf.giveBack.worst}
              </div>
              <div className="mono" style={{ fontSize: 10, color: "#8a7a4a", marginTop: 4 }}>
                worst single give-back
              </div>
            </div>
          </div>
          <div style={{ fontSize: 11, color: "#c9b78a", marginTop: 12, lineHeight: 1.5 }}>
            {perf.giveBack.note}
          </div>
        </div>

        <div style={{ ...panel, flex: 1, borderRadius: 9, padding: "14px 16px" }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 12,
            }}
          >
            <span style={sectionTitle}>EXECUTION DIVERGENCE · BOOKED vs TRUTH MID</span>
            <span className="mono" style={{ fontSize: 10, color: "var(--text-6)" }}>
              median {perf.slippage.median} · avg {perf.slippage.avg}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 8, height: 64 }}>
            {perf.slippage.buckets.map((sb) => (
              <div
                key={sb.bucket}
                style={{
                  flex: 1,
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "flex-end",
                  gap: 4,
                  height: 64,
                }}
              >
                <span className="mono" style={{ fontSize: 9, color: "var(--text-7)" }}>
                  {sb.n}
                </span>
                <div
                  style={{
                    width: "100%",
                    height: Math.round((sb.n / slipMax) * 54),
                    borderRadius: 2,
                    background: "var(--accent)",
                    opacity: 0.85,
                  }}
                />
              </div>
            ))}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 5 }}>
            {perf.slippage.buckets.map((sb) => (
              <div
                key={sb.bucket}
                className="mono"
                style={{ flex: 1, textAlign: "center", fontSize: 8, color: "var(--text-9)" }}
              >
                {sb.bucket}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* round-trip detail table */}
      <div style={{ ...panel, overflow: "hidden" }}>
        <div style={{ padding: "11px 15px", borderBottom: "1px solid var(--border-rail)" }}>
          <span style={sectionTitle}>ROUND-TRIP DETAIL</span>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: GRID,
            gap: 10,
            padding: "7px 15px",
            borderBottom: "1px solid var(--border-rail)",
            fontSize: 9,
            letterSpacing: "0.06em",
            color: "var(--text-9)",
            fontWeight: 600,
          }}
        >
          <div>TIME</div>
          <div>TICKER</div>
          <div>SLEEVE</div>
          <div>STRUCTURE</div>
          <div>ENTRY→EXIT</div>
          <div>EXIT</div>
          <div style={{ textAlign: "right" }}>DIV</div>
          <div style={{ textAlign: "right" }}>P&L</div>
          <div style={{ textAlign: "right" }}>G-BACK</div>
        </div>
        {trades.map((tr) => (
          <TradeRow key={tr.time + tr.ticker} tr={tr} />
        ))}
      </div>
    </div>
  );
}
