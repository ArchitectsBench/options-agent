import { useEffect, useState } from "react";
import { useCockpit } from "../store";

// Dashboard summary of the two RESEARCH streams (2026-09-13). The full
// evidence lives on the RSCH page; the dashboard gets ONE line each.
//
// Why: the dashboard stacked the whole RVOL-RANK evidence report (two stat
// grids, two progress bars, a 10-row table, a hygiene paragraph), the radar's
// 10-column table and a 155-event alert feed above the things a session
// actually needs (positions, recs, today). Operator verdict: "too much data
// and I couldn't even read it."
//
// A line here answers one question only — is anything different today? — and
// clicks through to the depth. Nothing is summarised that the page does not
// show in full, and no verdict is computed here: every judgement word comes
// from the API.

type Sleeves = {
  rvolrank?: {
    stamp_interim?: string;
    gate_primary?: {
      gate_binding_tier: string;
      binding: { selection_mean_R: number | null; selection_t: number | null };
      bars: { t_min_deflated: number };
      progress: {
        sessions_valid: number; sessions_goal: number;
        exec_trades: number; exec_trades_goal: number;
        projected_date_400: string | null;
      };
    };
  };
};
type Radar = {
  rows?: { in_zone?: boolean }[];
  data_asof?: string | null;
  stale?: boolean;
  outcomes_recorded?: number;
  backtest?: { verdict: string; bar_t?: number | null; gate_t?: number | null } | null;
};

const row: React.CSSProperties = {
  display: "flex", alignItems: "baseline", gap: 10, width: "100%",
  padding: "7px 10px", borderRadius: 8, border: "1px solid var(--border-inset)",
  background: "var(--bg-inset)", textAlign: "left", cursor: "pointer",
  font: "inherit", color: "inherit",
};
const name: React.CSSProperties = { fontSize: 11, fontWeight: 600, minWidth: 132 };
const facts: React.CSSProperties = { fontSize: 11, color: "var(--text-9)", flex: 1, minWidth: 0 };
const WARN = "var(--warn, #d9a03f)";

function n3(v: number | null | undefined) {
  return v === null || v === undefined ? "—" : v.toFixed(3);
}
function n2(v: number | null | undefined) {
  return v === null || v === undefined ? "—" : v.toFixed(2);
}

export function ResearchStrip() {
  const { setPage } = useCockpit();
  const [sl, setSl] = useState<Sleeves | null>(null);
  const [rd, setRd] = useState<Radar | null>(null);

  useEffect(() => {
    let live = true;
    const load = () => {
      fetch("/api/sleeves").then(r => r.json())
        .then(d => { if (live) setSl(d); }).catch(() => {});
      fetch("/api/watchlist-squeeze").then(r => r.json())
        .then(d => { if (live) setRd(d); }).catch(() => {});
    };
    load();
    const id = setInterval(load, 120_000);
    return () => { live = false; clearInterval(id); };
  }, []);

  const gp = sl?.rvolrank?.gate_primary;
  const pr = gp?.progress;
  const inZone = (rd?.rows ?? []).filter(r => r.in_zone).length;
  const bt = rd?.backtest;

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
        <h2 style={{ fontSize: 13, fontWeight: 600, margin: 0 }}>Research</h2>
        <span className="mono" style={{ fontSize: 10, color: WARN }}>
          UNVALIDATED — PAPER ONLY
        </span>
        <button
          onClick={() => setPage("research")}
          className="control-region"
          style={{ marginLeft: "auto", fontSize: 10, padding: "1px 8px", borderRadius: 5,
            border: "1px solid var(--border-inset)", background: "transparent",
            color: "var(--text-9)", cursor: "pointer" }}
        >
          open RSCH ▸
        </button>
      </div>

      <button style={row} onClick={() => setPage("research")}
        title="RVOL-RANK nightly simulation — full evidence on the Research page">
        <span style={name}>RVOL-RANK</span>
        <span className="mono" style={facts}>
          {pr
            ? `${pr.sessions_valid}/${pr.sessions_goal} sessions · ${pr.exec_trades}/${pr.exec_trades_goal} trades · ` +
              `${gp!.gate_binding_tier} R̄ ${n3(gp!.binding?.selection_mean_R)} ` +
              `(t ${n2(gp!.binding?.selection_t)} vs ${gp!.bars.t_min_deflated}) · ` +
              `gate ~${pr.projected_date_400 ?? "—"}`
            : "no payload — API restart needed"}
        </span>
        {pr && (
          <span className="mono" style={{ fontSize: 10, color: WARN }}>
            {sl?.rvolrank?.stamp_interim ?? "interim, NOT a gate judgment"}
          </span>
        )}
      </button>

      <button style={row} onClick={() => setPage("research")}
        title="Squeeze buy-zone radar — full watchlist and alert feed on the Research page">
        <span style={name}>Squeeze radar</span>
        <span className="mono" style={facts}>
          {rd
            ? `${(rd.rows ?? []).length} names · ${inZone} in zone · eval ${rd.data_asof ?? "—"} · ` +
              `${rd.outcomes_recorded ?? 0} outcomes recorded`
            : "no payload"}
        </span>
        {rd?.stale && (
          <span className="mono" style={{ fontSize: 10, color: WARN }}>STALE</span>
        )}
        {bt && (
          <span className="mono" style={{ fontSize: 10, color: WARN }}>
            backtest {bt.verdict} — decision support only
          </span>
        )}
      </button>
    </section>
  );
}
