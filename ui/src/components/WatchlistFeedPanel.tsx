import { useEffect, useState } from "react";
import { ActionButton } from "./ActionButton";

// Squeeze Watchlist Alert Feed (Phase A2, 2026-07-23). PURE READER of
// logs/alerts/events.jsonl via /api/watchlist-events. No alert state lives
// here. Events describe STATE CHANGES — never "buy"/"sell". Stale-tagged
// events are demoted (grey + badge + sorted below), never hidden.

type Ev = {
  ticker: string; timeframe: string; transition: string;
  state_instance_id: string | null; state?: string; dist_atr?: number;
  severity: "high" | "med" | "low"; stale_data?: boolean;
  data_lag_sessions?: number; counter_trend?: boolean; mtf?: string;
  bias_d?: string; bias_w?: string; bias_m?: string;
  rvol_current?: number | null; rvol_at_add?: number;
  e_status?: string | null; earnings_session?: string;
  invalidation_reason?: string | null; fire_direction?: string;
  data_asof?: string; detected_at?: string; re_entered?: boolean;
};
type Feed = { events: Ev[]; threads: Record<string, Ev[]>;
  stale_count: number; severity_counts: Record<string, number>; };

const CADENCE = { LIVE: null, HOURLY: "1h", DAILY: "1d" } as const;
type Cadence = keyof typeof CADENCE;

const sevColor = (s: string) =>
  s === "high" ? "var(--neg, #d9534f)" : s === "med" ? "var(--warn, #d9a03f)" : "var(--text-9)";
const th: React.CSSProperties = { textAlign: "left", fontSize: 10, color: "var(--text-9)", fontWeight: 500, padding: "2px 8px 2px 0" };
const td: React.CSSProperties = { fontSize: 11, padding: "3px 8px 3px 0", verticalAlign: "top" };
const DEFAULT_ROWS = 25;

function gap(asof?: string, det?: string) {
  if (!asof || !det) return "";
  const a = new Date(asof.replace(" ", "T")).getTime();
  const d = new Date(det).getTime();
  if (isNaN(a) || isNaN(d)) return "";
  const h = Math.round((d - a) / 3.6e6);
  return h > 0 ? `+${h}h` : "";
}

export function WatchlistFeedPanel() {
  const [feed, setFeed] = useState<Feed | null>(null);
  const [cadence, setCadence] = useState<Cadence>("LIVE");
  const [showStale, setShowStale] = useState(true);
  const [threadMode, setThreadMode] = useState(false);
  // The endpoint returns the whole rolling window (155 events / 130 KB on
  // 2026-09-13). Rendering all of it is what made this section unreadable;
  // the tail is one click away.
  const [showAll, setShowAll] = useState(false);
  useEffect(() => {
    let live = true;
    const load = () => fetch("/api/watchlist-events").then(r => r.json())
      .then(d => { if (live) setFeed(d); }).catch(() => {});
    load();
    const id = setInterval(load, 60_000);
    return () => { live = false; clearInterval(id); };
  }, []);
  if (!feed || feed.events.length === 0) return null;

  const tf = CADENCE[cadence];
  let evs = feed.events.filter(e =>
    (tf === null || e.timeframe === tf || e.timeframe === "ticker") &&
    (showStale || !e.stale_data));
  // stale sorts below fresh, then by recency (already recency-sorted)
  evs = [...evs].sort((a, b) => Number(!!a.stale_data) - Number(!!b.stale_data));

  const Row = (e: Ev, i: number) => (
    <tr key={i} style={{ opacity: e.stale_data ? 0.5 : 1 }}>
      <td style={{ ...td, color: sevColor(e.severity), fontWeight: 600 }} className="mono">{e.ticker}</td>
      <td style={td} className="mono">{e.timeframe}</td>
      <td style={{ ...td, color: sevColor(e.severity), fontWeight: 600 }}>
        {e.transition}{e.fire_direction ? `·${e.fire_direction}` : ""}
        {e.re_entered ? " ↻" : ""}
        {e.invalidation_reason ? ` (${e.invalidation_reason})` : ""}
      </td>
      <td style={td}>
        {e.counter_trend ? <span style={{ color: "var(--warn,#d9a03f)" }}>⚑CT </span> : null}
        {e.mtf && e.mtf !== "—" ? <span className="mono" style={{ fontSize: 10 }}>{e.mtf} </span> : null}
        {e.e_status ? <span className="mono" style={{ fontSize: 10, color: "var(--warn,#d9a03f)" }}>⚠{e.e_status} </span> : null}
        {e.stale_data ? <span className="mono" style={{ fontSize: 9, background: "var(--bg-inset)", padding: "0 4px", borderRadius: 3 }}>STALE {e.data_lag_sessions}s</span> : null}
      </td>
      <td style={td} className="mono">{e.dist_atr ?? "—"}</td>
      <td style={td} className="mono">
        {e.rvol_current ?? "—"}/{e.rvol_at_add ?? "—"}
      </td>
      <td style={td} className="mono" title={`data ${e.data_asof} · detected ${e.detected_at}`}>
        {(e.data_asof ?? "").slice(0, 10)} {gap(e.data_asof, e.detected_at)}
      </td>
    </tr>
  );

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
        <h2 style={{ fontSize: 13, fontWeight: 600, margin: 0 }}>Squeeze Alert Feed</h2>
        <span className="mono" style={{ fontSize: 10, color: "var(--warn, #d9a03f)" }}>
          MANUAL DECISION SUPPORT — NOT SIGNALS
        </span>
        <span className="control-region" style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          {(Object.keys(CADENCE) as Cadence[]).map(c => (
            <ActionButton key={c} allowRapid onAct={() => setCadence(c)} aria-pressed={cadence === c}
              style={{ fontSize: 10, padding: "1px 7px", borderRadius: 5,
                border: "1px solid var(--border-inset)",
                background: cadence === c ? "var(--accent, #3d6)" : "transparent",
                color: cadence === c ? "#111" : "var(--text-9)" }}>{c}</ActionButton>
          ))}
          <ActionButton allowRapid onAct={() => setShowStale(s => !s)} aria-pressed={showStale}
            style={{ fontSize: 10, padding: "1px 7px", borderRadius: 5,
              border: "1px solid var(--border-inset)", color: "var(--text-9)",
              background: showStale ? "transparent" : "var(--bg-inset)" }}>
            {showStale ? "stale: shown" : "stale: hidden"}
          </ActionButton>
          <ActionButton allowRapid onAct={() => setShowAll(s => !s)} aria-pressed={showAll}
            style={{ fontSize: 10, padding: "1px 7px", borderRadius: 5,
              border: "1px solid var(--border-inset)", color: "var(--text-9)",
              background: showAll ? "var(--bg-inset)" : "transparent" }}>
            {showAll ? `all ${evs.length}` : `latest ${Math.min(DEFAULT_ROWS, evs.length)}`}
          </ActionButton>
          <ActionButton allowRapid onAct={() => setThreadMode(t => !t)} aria-pressed={threadMode}
            style={{ fontSize: 10, padding: "1px 7px", borderRadius: 5,
              border: "1px solid var(--border-inset)", color: "var(--text-9)",
              background: threadMode ? "var(--bg-inset)" : "transparent" }}>
            {threadMode ? "threads" : "flat"}
          </ActionButton>
        </span>
      </div>
      <div style={{ display: "flex", gap: 10, fontSize: 10, color: "var(--text-9)" }}>
        <span style={{ color: sevColor("high") }}>● {feed.severity_counts.high} high</span>
        <span style={{ color: sevColor("med") }}>● {feed.severity_counts.med} med</span>
        <span>● {feed.severity_counts.low} low</span>
        <span style={{ marginLeft: "auto" }}>{feed.stale_count} stale</span>
      </div>
      <table style={{ borderCollapse: "collapse" }}>
        <thead><tr>
          <th style={th}>ticker</th><th style={th}>tf</th><th style={th}>transition</th>
          <th style={th}>context</th><th style={th}>dist</th>
          <th style={th}>rvol now/add</th><th style={th}>data_asof</th>
        </tr></thead>
        <tbody>
          {!threadMode && (showAll ? evs : evs.slice(0, DEFAULT_ROWS)).map(Row)}
          {!threadMode && !showAll && evs.length > DEFAULT_ROWS && (
            <tr><td colSpan={7} style={{ ...td, fontSize: 10, color: "var(--text-9)" }} className="mono">
              {evs.length - DEFAULT_ROWS} older event{evs.length - DEFAULT_ROWS === 1 ? "" : "s"} hidden
            </td></tr>
          )}
          {threadMode && Object.entries(feed.threads)
            .filter(([, es]) => es.some(e => tf === null || e.timeframe === tf || e.timeframe === "ticker"))
            .sort((a, b) => (b[1].at(-1)?.detected_at ?? "").localeCompare(a[1].at(-1)?.detected_at ?? ""))
            .slice(0, 40)
            .map(([iid, es]) => (
              <>
                <tr key={iid}><td colSpan={7} style={{ ...td, fontSize: 9, color: "var(--text-9)", paddingTop: 8 }} className="mono">▸ {iid}</td></tr>
                {es.map(Row)}
              </>
            ))}
        </tbody>
      </table>
    </section>
  );
}
