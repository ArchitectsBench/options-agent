import { useEffect, useState } from "react";
import { ActionButton } from "../components/ActionButton";

/** Trades & Clocks — five tabs, one per evidence stream, NEVER blended (L12).
 * Data from GET /api/clocks (fault-isolated per section, 60s server cache). */

type TradeRow = {
  date: string; time: string; ticker: string; structure: string; dir: string;
  qty: number; pnl: number; exit: string; path: boolean;
};
type Clocks = {
  generated_at?: string;
  funnel?: any; scalp?: any; trend?: any; shadow?: any; l13?: any;
};

type TabId = "funnel" | "scalp" | "trend" | "shadow" | "l13";
const TABS: { id: TabId; label: string; color: string; sub: string }[] = [
  { id: "funnel", label: "FUNNEL", color: "var(--accent)", sub: "forward test · evidence" },
  { id: "scalp", label: "SCALP", color: "var(--amb)", sub: "frozen candidate 07-09" },
  { id: "trend", label: "TREND", color: "#4fc3f7", sub: "Track C shadow book" },
  { id: "shadow", label: "SHADOW", color: "#b39ddb", sub: "option-b gate · observation" },
  { id: "l13", label: "L13", color: "var(--grn)", sub: "give-back fix readiness" },
];

type Range = "today" | "week" | "all";

const cur = (n: number) =>
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
const pnlColor = (n: number) => (n > 0 ? "var(--grn)" : n < 0 ? "var(--red)" : "var(--text-4)");

const panel: React.CSSProperties = {
  border: "1px solid var(--border-panel)", borderRadius: 10, background: "var(--bg-panel)",
};

// Trade dates are session dates (America/Los_Angeles). toISOString() would give
// the UTC date, which rolls to "tomorrow" after ~5pm PT — that made TODAY go
// empty and WEEK slip a day every evening. Compute the window in PT instead.
const ptDate = (ms: number) =>
  new Date(ms).toLocaleDateString("en-CA", { timeZone: "America/Los_Angeles" }); // YYYY-MM-DD

function inRange(date: string, range: Range): boolean {
  if (range === "all" || !date) return true;
  if (range === "today") return date === ptDate(Date.now());
  const weekAgo = ptDate(Date.now() - 6 * 864e5); // rolling 7-day window incl. today
  return date >= weekAgo;
}

function RangeChips({ range, setRange }: { range: Range; setRange: (r: Range) => void }) {
  return (
    <div className="mono control-region" style={{ display: "flex", gap: 6, fontSize: 10, fontWeight: 600 }}>
      {(["today", "week", "all"] as Range[]).map((r) => (
        <ActionButton key={r} allowRapid onAct={() => setRange(r)} aria-pressed={range === r}
          style={{ padding: "4px 10px", borderRadius: 5, fontWeight: 600,
                   border: "1px solid var(--border-tile)",
                   background: range === r ? "rgba(var(--accent-rgb),0.16)" : "var(--bg-tile)",
                   color: range === r ? "var(--accent)" : "var(--text-7)" }}>
          {r.toUpperCase()}
        </ActionButton>
      ))}
    </div>
  );
}

const GRID = "52px 56px 1fr 64px 34px 130px 70px 34px";

function TradeTable({ trades, range }: { trades: TradeRow[]; range: Range }) {
  const rows = trades.filter((t) => inRange(t.date, range));
  const total = rows.reduce((a, t) => a + t.pnl, 0);
  const wins = rows.filter((t) => t.pnl > 0).length;
  return (
    <div style={{ ...panel, overflow: "hidden" }}>
      <div className="mono" style={{ display: "grid", gridTemplateColumns: GRID, gap: 8,
        padding: "7px 14px", borderBottom: "1px solid var(--border-rail)",
        fontSize: 9, letterSpacing: "0.06em", color: "var(--text-9)", fontWeight: 600 }}>
        <div>DATE</div><div>TIME</div><div>TICKER · STRUCTURE</div><div>DIR</div>
        <div>QTY</div><div>EXIT</div><div style={{ textAlign: "right" }}>P&L</div>
        <div style={{ textAlign: "right" }}>PATH</div>
      </div>
      {rows.length === 0 ? (
        <div className="mono" style={{ padding: "18px 14px", fontSize: 11, color: "var(--text-7)", textAlign: "center" }}>
          No trades in this range.
        </div>
      ) : rows.map((t, i) => (
        <div key={t.date + t.time + t.ticker + i} className="mono"
          style={{ display: "grid", gridTemplateColumns: GRID, gap: 8, padding: "8px 14px",
                   borderBottom: "1px solid var(--border-rail)", fontSize: 11, alignItems: "center" }}>
          <div style={{ color: "var(--text-6)", fontSize: 10 }}>{t.date.slice(5)}</div>
          <div style={{ color: "var(--text-6)", fontSize: 10 }}>{t.time}</div>
          <div><b>{t.ticker}</b> <span style={{ color: "var(--text-5)", fontSize: 10 }}>{t.structure}</span></div>
          <div style={{ color: t.dir === "LONG" ? "var(--grn)" : "var(--dir)", fontSize: 10, fontWeight: 700 }}>{t.dir}</div>
          <div style={{ color: "var(--text-5)" }}>{t.qty}</div>
          <div style={{ color: "var(--text-4)", fontSize: 10 }}>{t.exit}</div>
          <div style={{ textAlign: "right", fontWeight: 700, color: pnlColor(t.pnl) }}>{cur(t.pnl)}</div>
          <div style={{ textAlign: "right", color: t.path ? "var(--grn)" : "var(--text-9)", fontSize: 10 }}>
            {t.path ? "✓" : "·"}
          </div>
        </div>
      ))}
      <div className="mono" style={{ display: "flex", justifyContent: "flex-end", gap: 14,
        padding: "8px 14px", fontSize: 11, color: "var(--text-5)" }}>
        <span>{rows.length} trades · {wins}W {rows.length - wins}L</span>
        <b style={{ color: pnlColor(total) }}>{cur(total)}</b>
      </div>
    </div>
  );
}

function ClockHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="mono" style={{ ...panel, padding: "10px 14px", fontSize: 11,
      color: "var(--text-3)", display: "flex", flexWrap: "wrap", gap: "4px 18px", alignItems: "baseline" }}>
      {children}
    </div>
  );
}

const K = ({ k, v, c }: { k: string; v: React.ReactNode; c?: string }) => (
  <span style={{ color: "var(--text-7)", fontSize: 10 }}>
    {k} <b style={{ color: c ?? "var(--text-2)", fontSize: 11.5 }}>{v}</b>
  </span>
);

export function TradesPage() {
  const [data, setData] = useState<Clocks | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>("funnel");
  const [range, setRange] = useState<Range>("all");

  useEffect(() => {
    let live = true;
    const load = () =>
      fetch("/api/clocks").then((r) => r.json())
        .then((d) => live && (setData(d), setErr(null)))
        .catch((e) => live && setErr(String(e)));
    load();
    const t = setInterval(load, 60_000);
    return () => { live = false; clearInterval(t); };
  }, []);

  const active = TABS.find((t) => t.id === tab)!;
  const sect = data?.[tab] as any;

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 12, padding: 14 }}>
      <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>Trades &amp; Clocks</h1>
          <div className="mono" style={{ fontSize: 10.5, color: "var(--text-9)", marginTop: 3 }}>
            five evidence streams · never blended · all figures UNVALIDATED — PAPER ONLY
          </div>
        </div>
        <RangeChips range={range} setRange={setRange} />
      </div>

      {/* tab bar */}
      <div className="control-region" style={{ display: "flex", gap: 8 }}>
        {TABS.map((t) => {
          const on = t.id === tab;
          return (
            <ActionButton key={t.id} allowRapid onAct={() => setTab(t.id)} aria-pressed={on} aria-label={`${t.label} — ${t.sub}`}
              style={{ flex: 1, padding: "9px 12px", borderRadius: 8, textAlign: "left",
                       border: `1px solid ${on ? t.color : "var(--border-tile)"}`,
                       background: on ? "var(--bg-panel)" : "var(--bg-tile)" }}>
              <div className="mono" style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.1em",
                color: on ? t.color : "var(--text-6)" }}>{t.label}</div>
              <div className="mono" style={{ fontSize: 8.5, color: "var(--text-8)", marginTop: 2 }}>{t.sub}</div>
            </ActionButton>
          );
        })}
      </div>

      {err && <div className="mono" style={{ ...panel, padding: 12, fontSize: 11, color: "var(--red)" }}>
        clocks feed unreachable: {err}</div>}
      {!data && !err && <div className="mono" style={{ padding: 20, fontSize: 11, color: "var(--text-7)" }}>loading…</div>}
      {sect?.error && <div className="mono" style={{ ...panel, padding: 12, fontSize: 11, color: "var(--amb)" }}>
        section error: {sect.error}</div>}

      {tab === "funnel" && sect && !sect.error && (
        <>
          <ClockHeader>
            <K k="trades" v={`${sect.progress?.trades ?? "—"}/${sect.progress?.min_trades ?? 100}`} c={active.color} />
            <K k="days" v={`${sect.progress?.trading_days ?? "—"}/${sect.progress?.min_trading_days ?? 60}`} />
            <K k="adverse week" v={sect.progress?.adverse_regime?.met ? "MET" : "not yet"}
               c={sect.progress?.adverse_regime?.met ? "var(--grn)" : "var(--amb)"} />
            <K k="forward P&L" v={cur(sect.pnl_total ?? 0)} c={pnlColor(sect.pnl_total ?? 0)} />
            <span style={{ fontSize: 9.5, color: "var(--text-8)" }}>
              Claude-approved round trips only — the evidence set for live eligibility
            </span>
          </ClockHeader>
          <TradeTable trades={sect.trades ?? []} range={range} />
        </>
      )}

      {tab === "scalp" && sect && !sect.error && (
        <>
          <ClockHeader>
            <K k="status" v={sect.status} c={active.color} />
            <K k="trades" v={`${sect.checks?.trades?.value ?? 0}/100`} />
            <K k="days" v={`${sect.checks?.trading_days?.value ?? 0}/60`} />
            <K k="adverse week" v={sect.checks?.adverse_week?.pass ? "MET" : "not yet"}
               c={sect.checks?.adverse_week?.pass ? "var(--grn)" : "var(--amb)"} />
            <K k="window P&L" v={cur(sect.pnl_total ?? 0)} c={pnlColor(sect.pnl_total ?? 0)} />
            <span style={{ fontSize: 9.5, color: "var(--text-8)" }}>
              evidence from freeze {String(sect.window_start ?? "").slice(0, 10)} · gate-free by design · benign-tape prints do not extrapolate
            </span>
          </ClockHeader>
          <TradeTable trades={sect.trades ?? []} range={range} />
        </>
      )}

      {tab === "trend" && sect && !sect.error && (
        <>
          <ClockHeader>
            <K k="cycle" v={sect.cycle ?? "—"} c={active.color} />
            <K k="days" v={`${sect.days ?? 0}/~250`} />
            <K k="equity" v={`$${(sect.equity ?? 0).toLocaleString()}`}
               c={pnlColor((sect.equity ?? 50000) - 50000)} />
            <K k="since start" v={cur((sect.equity ?? 50000) - (sect.start_equity ?? 50000))}
               c={pnlColor((sect.equity ?? 50000) - (sect.start_equity ?? 50000))} />
            <span style={{ fontSize: 9.5, color: "var(--text-8)" }}>
              12-month shadow · ~70 signal flips/yr expected — most days trade nothing
            </span>
          </ClockHeader>
          <div style={{ ...panel, overflow: "hidden" }}>
            <div className="mono" style={{ display: "grid", gridTemplateColumns: "90px 60px 70px 1fr 90px", gap: 8,
              padding: "7px 14px", borderBottom: "1px solid var(--border-rail)",
              fontSize: 9, letterSpacing: "0.06em", color: "var(--text-9)", fontWeight: 600 }}>
              <div>DATE</div><div>CYCLE</div><div>TRADES</div><div>EQUITY</div>
              <div style={{ textAlign: "right" }}>DAY P&L</div>
            </div>
            {(sect.recent ?? []).filter((r: any) => inRange(r.date, range)).map((r: any) => (
              <div key={r.date} className="mono" style={{ display: "grid",
                gridTemplateColumns: "90px 60px 70px 1fr 90px", gap: 8, padding: "8px 14px",
                borderBottom: "1px solid var(--border-rail)", fontSize: 11 }}>
                <div style={{ color: "var(--text-5)" }}>{r.date}</div>
                <div style={{ color: "var(--text-6)" }}>#{r.cycle}</div>
                <div style={{ color: r.trades ? "var(--text-2)" : "var(--text-8)" }}>{r.trades || "—"}</div>
                <div>${(r.equity ?? 0).toLocaleString()}</div>
                <div style={{ textAlign: "right", fontWeight: 700, color: pnlColor(r.pnl ?? 0) }}>
                  {r.pnl != null ? cur(r.pnl) : "—"}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {tab === "shadow" && sect && !sect.error && (
        <>
          <ClockHeader>
            <K k="gate" v={`calibrated ≥ ${sect.threshold}`} c={active.color} />
            <K k="scoring from" v={sect.window_start} />
            <K k="pass cohort" v={sect.pass ? `n=${sect.pass.n} · hit ${(sect.pass.hit_rate * 100).toFixed(0)}%` : "no data yet"} />
            <K k="fail cohort" v={sect.fail ? `n=${sect.fail.n} · hit ${(sect.fail.hit_rate * 100).toFixed(0)}%` : "no data yet"} />
            <span style={{ fontSize: 9.5, color: "var(--text-8)" }}>
              OBSERVATION ONLY — counterfactual underlying moves, not real fills; informs the October decision only
            </span>
          </ClockHeader>
          <div style={{ ...panel, overflow: "hidden" }}>
            <div className="mono" style={{ display: "grid", gridTemplateColumns: "52px 56px 70px 1fr 64px 60px 80px", gap: 8,
              padding: "7px 14px", borderBottom: "1px solid var(--border-rail)",
              fontSize: 9, letterSpacing: "0.06em", color: "var(--text-9)", fontWeight: 600 }}>
              <div>DATE</div><div>TIME</div><div>TICKER</div><div>STRATEGY</div><div>DIR</div>
              <div>CONF</div><div style={{ textAlign: "right" }}>CF EOD</div>
            </div>
            {(sect.picks ?? []).filter((p: any) => inRange(p.date, range)).length === 0 ? (
              <div className="mono" style={{ padding: "18px 14px", fontSize: 11, color: "var(--text-7)", textAlign: "center" }}>
                No shadow picks yet — scoring starts {sect.window_start} (out-of-sample vs the frozen map).
              </div>
            ) : (sect.picks ?? []).filter((p: any) => inRange(p.date, range)).map((p: any, i: number) => (
              <div key={p.date + p.time + p.ticker + i} className="mono" style={{ display: "grid",
                gridTemplateColumns: "52px 56px 70px 1fr 64px 60px 80px", gap: 8, padding: "8px 14px",
                borderBottom: "1px solid var(--border-rail)", fontSize: 11 }}>
                <div style={{ color: "var(--text-6)", fontSize: 10 }}>{p.date.slice(5)}</div>
                <div style={{ color: "var(--text-6)", fontSize: 10 }}>{p.time}</div>
                <div><b>{p.ticker}</b></div>
                <div style={{ color: "var(--text-5)", fontSize: 10 }}>{p.structure}</div>
                <div style={{ color: p.dir === "LONG" ? "var(--grn)" : "var(--dir)", fontSize: 10, fontWeight: 700 }}>{p.dir}</div>
                <div style={{ color: "var(--text-5)" }}>{p.conf?.toFixed(2)}</div>
                <div style={{ textAlign: "right", fontWeight: 700,
                  color: pnlColor(p.cf_eod_pct ?? 0) }}>{p.cf_eod_pct != null ? `${p.cf_eod_pct > 0 ? "+" : ""}${p.cf_eod_pct}%` : "—"}</div>
              </div>
            ))}
          </div>
        </>
      )}

      {tab === "l13" && sect && !sect.error && (
        <>
          <ClockHeader>
            <K k="ordered paths" v={`${sect.readiness?.ordered_path_positions_pooled ?? 0}/${sect.readiness?.target_pooled ?? 25}`} c={active.color} />
            <K k="funnel paths" v={sect.readiness?.ordered_path_positions_funnel ?? 0} />
            <K k="ready to tune" v={sect.readiness?.ready_to_tune ? "YES" : "collecting"}
               c={sect.readiness?.ready_to_tune ? "var(--grn)" : "var(--amb)"} />
            <K k="funnel invalidation capture" v={sect.funnel_invalidation_capture != null
              ? `${(sect.funnel_invalidation_capture * 100).toFixed(0)}%` : "—"}
              c={pnlColor(sect.funnel_invalidation_capture ?? 0)} />
            <span style={{ fontSize: 9.5, color: "var(--text-8)" }}>
              trailing-stop fix STAGED — deploys only after out-of-sample tuning on these paths
            </span>
          </ClockHeader>
          <TradeTable trades={sect.trades_with_paths ?? []} range={range} />
        </>
      )}
    </div>
  );
}
