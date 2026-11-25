import { useEffect, useState } from "react";

// Discovery sleeve (2026-07-18): the ONE live evidence stream. UNVALIDATED —
// PAPER ONLY. Self-polls /api/sleeves (60s). LDTE-PREM was retired 2026-07-18
// (holdout FAIL before its window opened) and is deliberately not shown.
//
// 2026-09-13: the panel reports what the frozen gate is judged on — the
// GATE-PRIMARY 09:45 executable lanes (amendment A1) after ledger hygiene —
// first; the idealized 09:35 lanes second, measurement-only. Within the
// gate-primary box the GATE-BINDING cost tier (r_tiers[stress_2x], A1 item 3)
// leads; the nightly's raw `r` (net of base slippage, GROSS of commission — a
// continuity metric, not a cost tier) is shown below it, labeled as such.
// Every derived flag is interim, NOT a gate judgment: the ≥/< bar badges use
// ONE neutral color on purpose — no pass/fail green/red on interim numbers.

type Lane = { n: number; pnl: number; mean_R: number | null; sd: number | null;
  t: number | null; skips?: number };
type Tier = { selection_n: number; selection_mean_R: number | null;
  selection_t: number | null; control_mean_R: number | null; gap: number | null };
type Month = { month: string; n: number; mean_R: number | null } | null;
type Binding = Tier & { tier: string; worst_month: Month };
type NightlyBC = { exec_selection_trades_cum?: number; gate_valid_sessions?: number;
  trades_per_session_run_rate?: number; projected_sessions_to_400?: number;
  hard_min?: number } | null;
type GatePrimary = {
  label: string; exec_sessions: number;
  selection: Lane; control: Lane; gap: number | null; raw_r_basis: string;
  tiers: Record<string, Tier>; gate_binding_tier: string; binding: Binding;
  worst_month: Month; worst_month_stress_2x?: Month;
  months: Record<string, { n: number; mean_R: number | null }>;
  bars: { t_min_deflated: number; gap_min: number; worst_month_mean_R_min: number;
    min_sim_sessions: number; hard_min_exec_selection_trades: number };
  progress: { sessions_valid: number; sessions_goal: number; exec_sessions: number;
    exec_trades: number; exec_trades_goal: number; run_rate: number | null;
    run_rate_basis: string; projected_sessions_to_400: number | null;
    projected_date_400: string | null; nightly_binding_constraint: NightlyBC };
  flags: Record<string, boolean | null | string>;
};
type Idealized = {
  label: string; status: string; sessions: number;
  selection: { n: number; pnl: number; mean_R: number | null; t: number | null };
  control: { n: number; pnl: number; mean_R: number | null };
  gap: number | null;
};
type Hygiene = {
  rows_total: number; rows_excluded_pre_window: number; excluded_dates: string[];
  duplicate_dates: string[]; missing_weekdays: string[]; sessions_valid: number;
  exec_sessions: number; window_start: string; last_ledger_date: string | null;
  criteria_loaded: boolean; criteria_amendment_blocks?: string[];
};
type Sleeves = {
  rvolrank?: {
    name: string; stamp: string; stamp_interim: string;
    gate_primary?: GatePrimary; idealized: Idealized; hygiene: Hygiene;
    recent_days: { date: string; lane: string; sel_n: number | null; sel_pnl: number | null;
      sel_mean_R: number | null; ctl_n: number | null; ctl_pnl: number | null;
      ctl_mean_R: number | null }[];
  };
};

const box: React.CSSProperties = {
  padding: "12px 14px", borderRadius: 10, background: "var(--bg-inset)",
  border: "1px solid var(--border-inset)", flex: 1, minWidth: 300,
};
const th: React.CSSProperties = {
  textAlign: "left", fontSize: 10, color: "var(--text-9)", fontWeight: 500,
  padding: "2px 8px 2px 0",
};
const td: React.CSSProperties = { fontSize: 11, padding: "2px 8px 2px 0" };
const label: React.CSSProperties = { fontSize: 10, color: "var(--text-9)", marginBottom: 4 };
const grid: React.CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
  gap: "4px 12px", marginBottom: 8,
};
// One neutral color for BOTH badge states: the text says ≥/< bar, the color
// must not say pass/fail (interim numbers at 192/400 trades, 37/60 sessions).
const BADGE = "var(--warn, #d9a03f)";

function money(n: number | null | undefined) {
  if (n === null || n === undefined) return "—";
  return (n < 0 ? "-$" : "+$") + Math.abs(n).toFixed(0);
}
function r4(n: number | null | undefined) {
  return n === null || n === undefined ? "—" : n.toFixed(3);
}
function r2(n: number | null | undefined) {
  return n === null || n === undefined ? "—" : n.toFixed(2);
}
function vsBar(flag: boolean | null | string | undefined) {
  if (flag === true) return { text: "≥ bar", color: BADGE };
  if (flag === false) return { text: "< bar", color: BADGE };
  return { text: "n/a", color: "var(--text-9)" };
}
function monthText(m: Month) {
  return m ? `${m.month} ${r4(m.mean_R)} (${m.n}t)` : "—";
}

function Bar({ value, goal, text }: { value: number; goal: number; text: string }) {
  const pct = goal > 0 ? Math.min(100, (100 * value) / goal) : 0;
  return (
    <div className="mono" style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 10 }}>
      <span style={{ width: 168, color: "var(--text-9)" }}>{text}</span>
      <div style={{ flex: 1, height: 6, background: "var(--border-inset)", borderRadius: 3,
        overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: "var(--accent, #5b9cff)" }} />
      </div>
      <span style={{ width: 64, textAlign: "right" }}>{value}/{goal}</span>
    </div>
  );
}

function Stat({ k, v, flag }: { k: string; v: string; flag?: boolean | null | string }) {
  const f = flag === undefined ? null : vsBar(flag);
  return (
    <div className="mono" style={{ fontSize: 11 }}>
      <span style={{ color: "var(--text-9)" }}>{k} </span>
      <b>{v}</b>
      {f && <span style={{ color: f.color, marginLeft: 4 }}>{f.text}</span>}
    </div>
  );
}

export function SleevesPanel() {
  const [s, setS] = useState<Sleeves | null>(null);
  useEffect(() => {
    let live = true;
    const load = () =>
      fetch("/api/sleeves").then(r => r.json()).then(d => { if (live) setS(d); })
        .catch(() => {});
    load();
    const id = setInterval(load, 60_000);
    return () => { live = false; clearInterval(id); };
  }, []);
  const rv = s?.rvolrank;
  const gp = rv?.gate_primary;
  if (s === null) return null;                       // still loading / API down
  if (!rv || !gp) {
    // A running API process that predates the 2026-09-13 panel serves the old
    // payload shape. Say so rather than render stale or wrong numbers.
    return (
      <section className="mono" style={{ fontSize: 10, color: "var(--warn, #d9a03f)" }}>
        Discovery sleeve · RVOL-RANK: server payload predates the 2026-09-13 panel
        (no gate_primary) — API restart needed.
      </section>
    );
  }
  const pr = gp.progress;
  const bind = gp.gate_binding_tier;
  const b = gp.binding ?? { tier: bind, ...gp.tiers[bind],
    worst_month: gp.worst_month_stress_2x ?? null };
  const bc = pr.nightly_binding_constraint;
  const hy = rv.hygiene;
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
        <h2 style={{ fontSize: 13, fontWeight: 600, margin: 0 }}>
          Discovery sleeve · RVOL-RANK
        </h2>
        <span className="mono" style={{ fontSize: 10, color: "var(--warn, #d9a03f)" }}>
          {rv.stamp || "UNVALIDATED — PAPER ONLY"}
        </span>
        <span className="mono" style={{ fontSize: 10, color: "var(--text-9)", marginLeft: "auto" }}>
          {pr.sessions_valid}/{pr.sessions_goal} sessions · {pr.exec_trades}/{pr.exec_trades_goal} exec trades
        </span>
      </div>

      {/* ---- GATE-PRIMARY: 09:45 executable lanes ---- */}
      <div style={box}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6 }}>
          <span className="mono" style={{ fontSize: 11, fontWeight: 600 }}>{gp.label}</span>
          <span className="mono" style={{ fontSize: 10, color: "var(--warn, #d9a03f)" }}>
            {rv.stamp_interim}
          </span>
          <span className="mono" style={{ fontSize: 10, color: "var(--text-9)", marginLeft: "auto" }}>
            {gp.exec_sessions} exec sessions
          </span>
        </div>

        {/* gate-binding tier FIRST: this is what the frozen gate is judged on */}
        <div style={label}>gate-binding ({b.tier}) — the cost tier the frozen gate is judged on</div>
        <div style={grid}>
          <Stat k="sel R̄" v={`${r4(b.selection_mean_R)} (${b.selection_n}t)`} />
          <Stat k="ctl R̄" v={r4(b.control_mean_R)} />
          <Stat k={`t vs ${gp.bars.t_min_deflated}`} v={r2(b.selection_t)}
            flag={gp.flags[`${b.tier}_t_vs_bar`]} />
          <Stat k={`gap vs ${gp.bars.gap_min}`} v={r4(b.gap)}
            flag={gp.flags[`${b.tier}_gap_vs_bar`]} />
          <Stat k={`worst month vs ${gp.bars.worst_month_mean_R_min}`} v={monthText(b.worst_month)}
            flag={gp.flags[`${b.tier}_worst_month_vs_bar`]} />
        </div>

        {/* raw r SECOND: continuity metric, not a cost tier */}
        <div style={label}>r (continuity, not a tier) — {gp.raw_r_basis}</div>
        <div style={grid}>
          <Stat k="sel R̄" v={`${r4(gp.selection.mean_R)} (${money(gp.selection.pnl)}, ${gp.selection.n}t)`} />
          <Stat k="ctl R̄" v={`${r4(gp.control.mean_R)} (${money(gp.control.pnl)}, ${gp.control.n}t)`} />
          <Stat k={`t vs ${gp.bars.t_min_deflated}`} v={r2(gp.selection.t)} flag={gp.flags.raw_r_t_vs_bar} />
          <Stat k={`gap vs ${gp.bars.gap_min}`} v={r4(gp.gap)} flag={gp.flags.raw_r_gap_vs_bar} />
          <Stat k={`worst month vs ${gp.bars.worst_month_mean_R_min}`} v={monthText(gp.worst_month)}
            flag={gp.flags.raw_r_worst_month_vs_bar} />
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 3, marginBottom: 6 }}>
          <Bar text={`sessions / min (${gp.exec_sessions} exec)`} value={pr.sessions_valid} goal={pr.sessions_goal} />
          <Bar text="exec sel trades / hard min" value={pr.exec_trades} goal={pr.exec_trades_goal} />
        </div>
        <div className="mono" style={{ fontSize: 10, color: "var(--text-9)" }}>
          run-rate {r2(pr.run_rate)} trades/session ({pr.run_rate_basis}) ·{" "}
          {pr.projected_sessions_to_400 === null ? "projection n/a"
            : pr.projected_sessions_to_400 === 0 ? "hard min met"
            : `~${pr.projected_sessions_to_400} sessions to ${pr.exec_trades_goal} → ${pr.projected_date_400 ?? "—"}`}
          {bc && (
            <>
              {" "}· nightly: {r2(bc.trades_per_session_run_rate)}/sess over{" "}
              {bc.gate_valid_sessions ?? "—"} gate-valid → ~{bc.projected_sessions_to_400 ?? "—"}
            </>
          )}
        </div>
        <div className="mono" style={{ fontSize: 10, color: "var(--text-9)" }}>
          tiers sel R̄ base {r4(gp.tiers.base?.selection_mean_R)} / 2x {r4(gp.tiers.stress_2x?.selection_mean_R)}
          {" "}/ 3x {r4(gp.tiers.stress_3x?.selection_mean_R)} · skips sel {gp.selection.skips ?? 0} ctl {gp.control.skips ?? 0}
        </div>
      </div>

      {/* ---- IDEALIZED 09:35 lanes: measurement-only ---- */}
      <div style={{ ...box, opacity: 0.85 }}>
        <div style={label}>{rv.idealized.label}</div>
        <div className="mono" style={{ fontSize: 11 }}>
          sel {r4(rv.idealized.selection.mean_R)}R ({money(rv.idealized.selection.pnl)},{" "}
          {rv.idealized.selection.n}t, t {r2(rv.idealized.selection.t)}) · ctl{" "}
          {r4(rv.idealized.control.mean_R)}R ({money(rv.idealized.control.pnl)},{" "}
          {rv.idealized.control.n}t) · gap <b>{r4(rv.idealized.gap)}</b> · {rv.idealized.sessions} sessions
          {" "}· not gate-eligible
        </div>
      </div>

      {/* ---- recent days (exec lanes; idealized fallback flagged) ---- */}
      <div style={box}>
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead><tr>
            <th style={th}>date</th><th style={th}>lane</th><th style={th}>sel n</th>
            <th style={th}>sel P&L</th><th style={th}>sel R̄</th>
            <th style={th}>ctl P&L</th><th style={th}>ctl R̄</th>
          </tr></thead>
          <tbody>
            {rv.recent_days.map(d => (
              <tr key={d.date} className="mono">
                <td style={td}>{d.date}</td>
                <td style={{ ...td, color: d.lane === "exec" ? undefined : "var(--warn, #d9a03f)" }}>
                  {d.lane === "exec" ? "exec" : "ideal*"}
                </td>
                <td style={td}>{d.sel_n ?? "—"}</td>
                <td style={td}>{money(d.sel_pnl)}</td><td style={td}>{r4(d.sel_mean_R)}</td>
                <td style={td}>{money(d.ctl_pnl)}</td><td style={td}>{r4(d.ctl_mean_R)}</td>
              </tr>
            ))}
            {rv.recent_days.length === 0 && (
              <tr><td style={td} colSpan={7}>
                no sessions yet — first evidence night Mon 21:30 PT
              </td></tr>
            )}
          </tbody>
        </table>
        <div className="mono" style={{ fontSize: 10, color: "var(--text-9)", marginTop: 6 }}>
          hygiene: {hy.rows_total} rows · {hy.rows_excluded_pre_window} excluded (pre-window/amendments)
          {hy.excluded_dates.length > 0 && ` [${hy.excluded_dates.join(", ")}]`}
          {" "}· {hy.duplicate_dates.length} duplicate date{hy.duplicate_dates.length === 1 ? "" : "s"}
          {hy.duplicate_dates.length > 0 && ` [${hy.duplicate_dates.join(", ")}]`}
          {" "}· {hy.missing_weekdays.length} missing weekday{hy.missing_weekdays.length === 1 ? "" : "s"}
          {hy.missing_weekdays.length > 0 && ` [${hy.missing_weekdays.join(", ")}]`}
          {" "}· {hy.sessions_valid} valid sessions ({hy.exec_sessions} exec) from {hy.window_start} to {hy.last_ledger_date ?? "—"}
          {hy.criteria_loaded
            ? ` · criteria ${(hy.criteria_amendment_blocks ?? []).length} amendment block${(hy.criteria_amendment_blocks ?? []).length === 1 ? "" : "s"}`
            : " · criteria file NOT loaded (frozen defaults)"}
        </div>
      </div>
    </section>
  );
}
