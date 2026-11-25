import { SleevesPanel } from "../components/SleevesPanel";
import { WatchlistPanel } from "../components/WatchlistPanel";
import { WatchlistFeedPanel } from "../components/WatchlistFeedPanel";

// RSCH page (2026-09-13): the home of the two discovery/research streams at
// full depth — RVOL-RANK's nightly simulation evidence and the squeeze
// buy-zone radar with its alert feed. The dashboard keeps a two-line summary
// (ResearchStrip) and links here.
//
// Nothing on this page is actionable today: RVOL-RANK is an after-the-fact
// nightly simulation whose frozen gate is evaluated once, and the radar is
// manual decision support whose pre-registered backtest FAILED (the verdict
// line comes from the API, never from this file). The header says so once so
// the panels below do not each have to.

const note: React.CSSProperties = {
  fontSize: 11, color: "var(--text-9)", lineHeight: 1.5, maxWidth: 900,
};

export function ResearchPage() {
  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 18, padding: 14,
      minWidth: 0 }}>
      <header style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <h1 style={{ fontSize: 15, fontWeight: 600, margin: 0 }}>Research</h1>
          <span className="mono" style={{ fontSize: 10, color: "var(--warn, #d9a03f)" }}>
            UNVALIDATED — PAPER ONLY
          </span>
        </div>
        <p style={{ ...note, margin: 0 }}>
          Evidence streams, not trading surfaces. RVOL-RANK is an after-the-fact nightly
          simulation under frozen criteria: its numbers are interim until the pre-registered
          evaluation point and no reading of them is a gate judgment. The squeeze radar is
          manual decision support with no systematic-edge claim.
        </p>
      </header>

      <SleevesPanel />
      <WatchlistPanel />
      <WatchlistFeedPanel />
    </div>
  );
}
