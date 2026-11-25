import { useEffect, useState } from "react";

// Squeeze watchlist (2026-07-22): RVOL-surge x multi-TF-squeeze names with
// buy-zone tracking (within 1 ATR of EMA21, above SMA50). MANUAL DECISION
// SUPPORT ONLY — no systematic-edge claim (intake #5 verdict). Ntfy pings
// on zone entry; this panel mirrors the same state file.
// 2026-09-13: the outcome ledger stamp (scripts/radar_outcomes.py) — how many
// instances have a fully matured (+10 session) close-to-close outcome row, and
// the pre-registered M5 query's per-side n (COMPLETED instances from the event
// log, the pre-registered population). The NO EDGE CLAIM line is rendered
// verbatim from the API's no_claim_stamp (NO_CLAIM_FALLBACK only until the
// first response) so the cockpit can never imply a reading.

type Row = {
  ticker: string; added: string; last_seen?: string; rvol_at_add?: number;
  tfs?: number; in_zone?: boolean; dist_atr?: number;
  zone?: "long" | "short" | null; bias?: string; state?: string; days_to_earnings?: number | null; mtf?: string; wk_bias?: string; mo_bias?: string; rvol_current?: number | null; e_status?: string | null; earnings_session?: string;
};

type Backtest = {
  prereg_id?: string; verdict: string; consequence?: string | null;
  gate_t?: number | null; bar_t?: number | null;
  n_treated?: number | null; sessions?: number | null; as_of?: string | null;
} | null;

type Meta = {
  outcomes_recorded: number; m5_n_long: number; m5_n_short: number; no_claim_stamp: string;
  data_asof: string | null; stale: boolean; last_run_at: string | null; backtest: Backtest;
};

// MTF grades the script actually emits (watch_buyzone.py: FULL-ALIGN /
// PARTIAL / COUNTER-M / —). The panel used to test for "ALIGNED"/"CONFLICT",
// which the radar has never emitted, so the column was permanently grey.
export function mtfColor(v: string | undefined) {
  if (v === "FULL-ALIGN") return "var(--pos, #4caf7d)";
  if (v === "COUNTER-M") return "var(--neg, #d9534f)";
  if (v === "PARTIAL") return "var(--warn, #d9a03f)";
  return "var(--text-9)";
}

// Must equal src/api/watchlist_squeeze.NO_CLAIM_STAMP (pinned by tests/test_radar_outcomes.py).
const NO_CLAIM_FALLBACK = "exploratory, close-to-close, no costs — NO EDGE CLAIM (M5 needs >= 30/side)";

const th: React.CSSProperties = {
  textAlign: "left", fontSize: 10, color: "var(--text-9)", fontWeight: 500,
  padding: "2px 8px 2px 0",
};
const td: React.CSSProperties = { fontSize: 11, padding: "2px 8px 2px 0" };

export function WatchlistPanel() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  useEffect(() => {
    let live = true;
    const load = () =>
      fetch("/api/watchlist-squeeze").then(r => r.json())
        .then(d => {
          if (!live) return;
          setRows(d.rows ?? []);
          setMeta({
            outcomes_recorded: d.outcomes_recorded ?? 0,
            m5_n_long: d.m5_n_long ?? 0,
            m5_n_short: d.m5_n_short ?? 0,
            no_claim_stamp: d.no_claim_stamp ?? NO_CLAIM_FALLBACK,
            data_asof: d.data_asof ?? null,
            stale: Boolean(d.stale),
            last_run_at: d.last_run_at ?? null,
            backtest: d.backtest ?? null,
          });
        })
        .catch(() => {});
    load();
    const id = setInterval(load, 120_000);
    return () => { live = false; clearInterval(id); };
  }, []);
  if (!rows || rows.length === 0) return null;
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
        <h2 style={{ fontSize: 13, fontWeight: 600, margin: 0 }}>
          Squeeze watchlist · buy-zone radar
        </h2>
        <span className="mono" style={{ fontSize: 10, color: "var(--warn, #d9a03f)" }}>
          MANUAL DECISION SUPPORT — NOT SIGNALS
        </span>
      </div>
      {/* The pre-registered offline backtest's verdict, rendered from the API
          (src/api/watchlist_squeeze.backtest_verdict) — never hardcoded here,
          so this line cannot outlive the result file it describes. */}
      {meta?.backtest && (
        <div className="mono" style={{ fontSize: 10, color: "var(--warn, #d9a03f)" }}>
          offline backtest {meta.backtest.verdict}
          {meta.backtest.prereg_id ? ` (${meta.backtest.prereg_id})` : ""}
          {typeof meta.backtest.gate_t === "number" && typeof meta.backtest.bar_t === "number"
            ? ` · gate t ${meta.backtest.gate_t.toFixed(2)} vs ${meta.backtest.bar_t}`
            : ""}
          {meta.backtest.n_treated ? ` · n=${meta.backtest.n_treated}` : ""}
          {meta.backtest.consequence ? ` — ${meta.backtest.consequence}` : ""}
        </div>
      )}
      <div className="mono" style={{ fontSize: 10, color: "var(--text-9)" }}>
        {`evaluated ${meta?.data_asof ?? "—"}`}
        {meta?.stale ? " · STALE (radar has not run since the last close)" : ""}
        {` · outcomes recorded: ${meta?.outcomes_recorded ?? 0} — ${meta?.no_claim_stamp ?? NO_CLAIM_FALLBACK}`}
        {` · M5 long n=${meta?.m5_n_long ?? 0} short n=${meta?.m5_n_short ?? 0}`}
      </div>
      <table style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th style={th}>ticker</th>
            <th style={th}>state</th>
            <th style={th}>bias (D/W)</th>
            <th style={th}>MTF</th>
            <th style={th}>dist (ATR)</th>
            <th style={th} title="1d/1h lane count — NOT the add-time multiday count">TFs (1d/1h)</th>
            <th style={th}>rvol now/add</th>
            <th style={th}>E-session</th>
            <th style={th} title="session the radar last evaluated this name on">evaluated</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.ticker}>
              <td style={{ ...td, fontWeight: 600 }} className="mono">{r.ticker}</td>
              <td style={{ ...td, color: (r.state ?? "").includes("LONG") || r.zone === "long" ? "var(--pos, #4caf7d)"
                  : (r.state ?? "").includes("SHORT") || r.zone === "short" ? "var(--neg, #d9534f)" : "var(--text-9)" }}>
                {r.state ?? (r.zone === "long" ? "LONG ZONE" : (r.state ?? "").includes("SHORT") || r.zone === "short" ? "SHORT ZONE" : "watching")}{typeof r.days_to_earnings === "number" && r.days_to_earnings >= 0 && r.days_to_earnings <= 7 ? ` \u26A0E-${r.days_to_earnings}d` : ""}
              </td>
              <td style={{ ...td, color: r.bias === "bull" ? "var(--pos, #4caf7d)"
                  : r.bias === "bear" ? "var(--neg, #d9534f)" : "var(--text-9)" }}>
                {r.bias ?? "—"}/{r.wk_bias ?? "—"}
              </td>
              <td style={{ ...td, fontWeight: 600, color: mtfColor(r.mtf) }}>
                {r.mtf ?? "—"}
              </td>
              <td style={td} className="mono">{r.dist_atr?.toFixed(2) ?? "—"}</td>
              <td style={td} className="mono">{r.tfs ?? "—"}</td>
              <td style={td} className="mono">{r.rvol_current ?? "—"}/{r.rvol_at_add ? r.rvol_at_add.toFixed(1) : "—"}</td>
              <td style={td} className="mono">{r.e_status ? `${r.e_status}·${r.earnings_session ?? "?"}` : "—"}</td>
              {/* A name the radar has not re-evaluated on the newest session it
                  DID evaluate is ageing out; it used to render as if current. */}
              <td style={{ ...td, color: meta?.data_asof && (r.last_seen ?? r.added) < meta.data_asof
                  ? "var(--warn, #d9a03f)" : undefined }} className="mono">
                {r.last_seen ?? r.added}
                {meta?.data_asof && (r.last_seen ?? r.added) < meta.data_asof ? " ·ageing" : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
