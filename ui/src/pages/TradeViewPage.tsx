import { useEffect, useRef, useState } from "react";
import { useCockpit } from "../store";

/** TRADE page (2026-09-14) — one option trade at a time, open or closed:
 * the engine's own 5-minute bars with VWAP and the entry/exit, the VWAP
 * distance both the entry guard and the exit rule read, the option's value
 * against its target and stop, and the engine's indicators each cycle, beside
 * a rules-based checklist, timeline and verdict (no model writes them).
 * Data: GET /api/tradeview?date= and /api/tradeview/{rec_id}; an open trade
 * refreshes every 30 s. UNVALIDATED — PAPER ONLY. */

type Cycle = {
  t: string; cycle: number; last: number; atr: number | null; vd: number | null; vwap: number | null;
  ema5?: string; ema15?: string; sq5?: string; sq15?: string; macd?: string; macdDir?: string;
  vol?: number | null; rs?: number | null; bias?: string; gex?: string;
};
type Bar = { t: string; o: number; h: number; l: number; c: number; v: number };
type Check = { label: string; value: string; verdict: "for" | "against" | "neutral" | "info" };
type TimelineEvent = { t: string; kind: string; text: string };
export type TradeView = {
  recId: string; date: string; state: "open" | "closed"; ticker: string; lane: string; structure: string;
  direction: string; qty: number; contract: string; decided: string | null; entry: number; fillT: string;
  fill: { midAtFill?: number | null; spreadAtFill?: number | null; fillMode?: string | null };
  levels: { target: number; stop: number }; marks: [string, number][]; cycles: Cycle[];
  bars: Bar[]; barsSource: string;
  exit?: number; exitT?: string; pnl?: number; exitReason?: string;
  lastValue?: number | null; unrealized?: number | null;
  checklist: Check[]; timeline: TimelineEvent[]; verdict: { headline: string; lines: string[] };
};
type Row = {
  recId: string; ticker: string; lane: string; structure: string; qty: number; entry: number;
  entryT: string; state: "open" | "closed"; exitT?: string; pnl?: number; exitReason?: string;
  lastValue?: number; unrealized?: number;
};

export const toMin = (t: string) => {
  const [h, m] = t.split(":").map(Number);
  return h * 60 + m;
};
const hm = (m: number) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const floor5 = (m: number) => Math.floor(m / 5) * 5;

export function niceTicks(lo: number, hi: number, n: number): number[] {
  const steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 50];
  const step = steps.find((s) => (hi - lo) / s <= n) ?? 100;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(4));
  return out;
}

/** Chart window: 30 min before the decision to 40 min after the exit (or the
 * latest cycle while open), clipped to the cycles that exist, on 5-min edges. */
export function windowOf(v: Pick<TradeView, "cycles" | "decided" | "fillT" | "exitT">): [number, number] {
  const ts = v.cycles.map((c) => toMin(c.t));
  const first = ts.length ? Math.min(...ts) : toMin(v.fillT) - 30;
  const last = ts.length ? Math.max(...ts) : toMin(v.exitT ?? v.fillT) + 30;
  const start = Math.max(first, toMin(v.decided ?? v.fillT) - 30);
  const end = Math.min(last, Math.max(v.exitT ? toMin(v.exitT) + 40 : last, start + 60));
  return [floor5(start), Math.ceil(end / 5) * 5];
}

const money = (n: number) =>
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
const f2 = (n: number | null | undefined, d = 2) => (n == null ? "—" : n.toFixed(d));
const trim3 = (n: number) => String(+n.toFixed(3));
const ptToday = () => new Date().toLocaleDateString("en-CA", { timeZone: "America/Los_Angeles" });
const pnlColor = (n: number) => (n > 0 ? "var(--grn)" : n < 0 ? "var(--red)" : "var(--text-4)");

const panel: React.CSSProperties = {
  border: "1px solid var(--border-panel)", borderRadius: 10, background: "var(--bg-panel)",
};
const capLabel: React.CSSProperties = {
  fontSize: 9, letterSpacing: "0.1em", fontWeight: 700, color: "var(--text-7)", textTransform: "uppercase",
};
const axisText: React.CSSProperties = { fontFamily: "var(--font-mono)", fontSize: 10.5, fill: "var(--text-7)" };
const lbl = (color = "var(--text-3)", weight = 600): React.CSSProperties => ({
  fontFamily: "var(--font-mono)", fontSize: 10.5, fill: color, fontWeight: weight,
  paintOrder: "stroke", stroke: "var(--bg-panel)", strokeWidth: 4, strokeLinejoin: "round",
});
const svgStyle: React.CSSProperties = { display: "block", width: "100%", overflow: "visible", touchAction: "none" };

// ── chart geometry ───────────────────────────────────────────────────────

const M = { l: 58, r: 104 };
type Ctx = {
  width: number; x: (m: number) => number; W0: number; W1: number;
  fillM: number; endM: number; decM: number | null; hover: number | null; outcome: string;
  onMove: (e: React.PointerEvent<SVGSVGElement>) => void; onLeave: () => void;
};
const scaleY = (h: number, lo: number, hi: number) => (v: number) => h - 20 - ((v - lo) / (hi - lo)) * (h - 28);
const pathOf = (pts: [number, number][], x: (m: number) => number, y: (v: number) => number) =>
  pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");

function Axes({ c, h, y, ticks, fmt }: {
  c: Ctx; h: number; y: (v: number) => number; ticks: number[]; fmt: (v: number) => string;
}) {
  const top = 8, bot = h - 20;
  const xt: number[] = [];
  for (let m = Math.ceil(c.W0 / 15) * 15; m <= c.W1; m += 15) xt.push(m);
  return (
    <g>
      <rect x={c.x(c.fillM)} y={top} width={Math.max(0, c.x(c.endM) - c.x(c.fillM))} height={bot - top}
        style={{ fill: "rgba(var(--accent-rgb), 0.08)" }} />
      {c.decM != null && (
        <line x1={c.x(c.decM)} x2={c.x(c.decM)} y1={top} y2={bot} strokeDasharray="2 3" style={{ stroke: "var(--text-7)" }} />
      )}
      {ticks.map((v) => (
        <g key={v}>
          <line x1={M.l} x2={c.width - M.r} y1={y(v)} y2={y(v)} style={{ stroke: "var(--border-rail)" }} />
          <text x={M.l - 8} y={y(v) + 3.5} textAnchor="end" style={axisText}>{fmt(v)}</text>
        </g>
      ))}
      {xt.map((m) => (
        <text key={m} x={c.x(m)} y={h - 5} textAnchor="middle" style={axisText}>{hm(m)}</text>
      ))}
      {c.hover != null && (
        <line x1={c.x(c.hover)} x2={c.x(c.hover)} y1={top} y2={bot} opacity={0.6} style={{ stroke: "var(--text-5)" }} />
      )}
    </g>
  );
}

function Frame({ c, h, children }: { c: Ctx; h: number; children: React.ReactNode }) {
  return (
    <svg viewBox={`0 0 ${c.width} ${h}`} height={h} style={svgStyle} onPointerMove={c.onMove} onPointerLeave={c.onLeave}>
      {children}
    </svg>
  );
}

function Empty({ h, text }: { h: number; text: string }) {
  return (
    <div className="mono" style={{ height: h, display: "grid", placeItems: "center", fontSize: 10.5, color: "var(--text-8)" }}>
      {text}
    </div>
  );
}

function PriceChart({ c, v, cyc }: { c: Ctx; v: TradeView; cyc: Cycle[] }) {
  const h = 230;
  const bars = v.bars.filter((b) => toMin(b.t) >= c.W0 - 5 && toMin(b.t) <= c.W1);
  const vw = cyc.filter((k) => k.vwap != null);
  const vals = [...bars.flatMap((b) => [b.h, b.l]), ...vw.map((k) => k.vwap as number), ...cyc.map((k) => k.last)];
  if (!vals.length) return <Empty h={h} text="No price data for this session" />;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.08 || 0.5;
  lo -= pad; hi += pad;
  const y = scaleY(h, lo, hi);
  const bw = Math.max(3, (c.x(c.W0 + 5) - c.x(c.W0)) * 0.56);
  const lastAt = (m: number) => [...cyc].reverse().find((k) => toMin(k.t) <= m)?.last;
  const long = v.direction === "long";
  const pe = lastAt(c.fillM);
  const px = v.exitT ? lastAt(toMin(v.exitT)) : undefined;
  const live = v.state === "open" ? cyc[cyc.length - 1] : undefined;
  return (
    <Frame c={c} h={h}>
      <Axes c={c} h={h} y={y} ticks={niceTicks(lo, hi, 6)} fmt={(n) => n.toFixed(hi >= 1000 ? 0 : 1)} />
      {bars.map((b) => {
        const cx = c.x(toMin(b.t) + 2.5);
        if (cx < M.l || cx > c.width - M.r) return null;
        const up = b.c >= b.o;
        return (
          <g key={b.t}>
            <line x1={cx} x2={cx} y1={y(b.h)} y2={y(b.l)} style={{ stroke: "var(--text-5)" }} />
            <rect x={cx - bw / 2} y={y(Math.max(b.o, b.c))} width={bw} height={Math.max(1, Math.abs(y(b.o) - y(b.c)))} rx={1}
              style={{ fill: up ? "var(--bg-panel)" : "var(--text-5)", stroke: "var(--text-5)" }} />
          </g>
        );
      })}
      {vw.length > 1 && (
        <path d={pathOf(vw.map((k) => [toMin(k.t), k.vwap as number]), c.x, y)}
          style={{ fill: "none", stroke: "var(--accent)", strokeWidth: 2, strokeLinejoin: "round" }} />
      )}
      {vw.length > 0 && (
        <text x={c.width - M.r + 8} y={y(vw[vw.length - 1].vwap as number) + 3.5} style={lbl("var(--accent)")}>VWAP</text>
      )}
      {pe != null && (
        <g>
          <path d={long ? `M${c.x(c.fillM)},${y(pe) - 1} l-7,12 l14,0 z` : `M${c.x(c.fillM)},${y(pe) + 1} l-7,-12 l14,0 z`}
            style={{ fill: "var(--accent)", stroke: "var(--bg-panel)", strokeWidth: 2 }} />
          <text x={c.x(c.fillM) - 10} y={y(pe) + (long ? 26 : 16)} textAnchor="end" style={lbl()}>
            IN {v.fillT} · {pe.toFixed(2)}
          </text>
        </g>
      )}
      {px != null && v.exitT && (
        <g>
          <circle cx={c.x(toMin(v.exitT))} cy={y(px)} r={6} style={{ fill: c.outcome, stroke: "var(--bg-panel)", strokeWidth: 2 }} />
          <text x={c.x(toMin(v.exitT)) + 10} y={y(px) + (long ? -10 : 4)} style={lbl(c.outcome)}>
            OUT {v.exitT} · {px.toFixed(2)}
          </text>
        </g>
      )}
      {live && (
        <g>
          <circle cx={c.x(toMin(live.t))} cy={y(live.last)} r={5} style={{ fill: "var(--accent)", stroke: "var(--bg-panel)", strokeWidth: 2 }} />
          <text x={c.x(toMin(live.t)) + 9} y={y(live.last) - 8} style={lbl("var(--accent)")}>NOW {live.t} · {live.last.toFixed(2)}</text>
        </g>
      )}
      {c.decM != null && <text x={c.x(c.decM) + 4} y={19} style={lbl("var(--text-5)", 400)}>decided {v.decided}</text>}
    </Frame>
  );
}

function VwapDistanceChart({ c, v, cyc }: { c: Ctx; v: TradeView; cyc: Cycle[] }) {
  const h = 130;
  const pts = cyc.filter((k) => k.vd != null).map((k) => [toMin(k.t), k.vd as number] as [number, number]);
  if (!pts.length) return <Empty h={h} text="No VWAP distance logged" />;
  const lo = Math.min(-0.6, ...pts.map((p) => p[1])) - 0.2;
  const hi = Math.max(0.6, ...pts.map((p) => p[1])) + 0.2;
  const y = scaleY(h, lo, hi);
  const key = new Set([c.decM, floor5(c.fillM), v.exitT ? floor5(toMin(v.exitT)) : null]);
  return (
    <Frame c={c} h={h}>
      <Axes c={c} h={h} y={y} ticks={niceTicks(lo, hi, 5)} fmt={(n) => (n > 0 ? "+" : "") + (n % 1 ? n.toFixed(1) : n.toFixed(0))} />
      <rect x={M.l} width={c.width - M.l - M.r} y={y(0.25)} height={y(-0.25) - y(0.25)} style={{ fill: "var(--amb)", opacity: 0.12 }} />
      <line x1={M.l} x2={c.width - M.r} y1={y(0)} y2={y(0)} style={{ stroke: "var(--text-4)", strokeWidth: 1.5 }} />
      <text x={c.width - M.r + 8} y={y(0) + 3.5} style={lbl("var(--text-4)")}>exit line</text>
      <text x={c.width - M.r + 8} y={y(0.25) - 4} style={lbl("var(--amb)", 400)}>±0.25 no-entry</text>
      <path d={pathOf(pts, c.x, y)} style={{ fill: "none", stroke: "var(--accent)", strokeWidth: 2, strokeLinejoin: "round" }} />
      {pts.map(([m, val]) => (
        <circle key={m} cx={c.x(m)} cy={y(val)} r={key.has(m) ? 4.5 : 2.4}
          style={{ fill: key.has(m) ? "var(--accent)" : "var(--bg-panel)", stroke: "var(--accent)", strokeWidth: key.has(m) ? 2 : 1.5 }} />
      ))}
    </Frame>
  );
}

function OptionChart({ c, v }: { c: Ctx; v: TradeView }) {
  const h = 150;
  const pts: [number, number][] = v.marks.map(([t, val]) => [toMin(t), val]);
  if (v.state === "closed" && v.exitT && v.exit != null) pts.push([toMin(v.exitT), v.exit]);
  if (!pts.length) return <Empty h={h} text="No option marks yet" />;
  const vals = [...pts.map((p) => p[1]), v.levels.target, v.levels.stop, v.entry];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.12 || 0.2;
  lo -= pad; hi += pad;
  const y = scaleY(h, lo, hi);
  const level = (val: number, color: string, text: string, dash: string) => (
    <g key={text}>
      <line x1={M.l} x2={c.width - M.r} y1={y(val)} y2={y(val)} strokeDasharray={dash} style={{ stroke: color, strokeWidth: 1.25 }} />
      <text x={c.width - M.r + 8} y={y(val) + 3.5} style={lbl(color, 500)}>{text} {trim3(val)}</text>
    </g>
  );
  const end = pts[pts.length - 1];
  return (
    <Frame c={c} h={h}>
      <Axes c={c} h={h} y={y} ticks={niceTicks(lo, hi, 5)} fmt={(n) => n.toFixed(2)} />
      {level(v.levels.target, "var(--grn)", "target", "5 4")}
      {level(v.entry, "var(--text-5)", "paid", "2 3")}
      {level(v.levels.stop, "var(--red)", "stop", "5 4")}
      <circle cx={c.x(c.fillM)} cy={y(v.entry)} r={4.5} style={{ fill: "var(--accent)", stroke: "var(--bg-panel)", strokeWidth: 2 }} />
      <path d={pathOf(pts, c.x, y)} style={{ fill: "none", stroke: "var(--text-2)", strokeWidth: 2, strokeLinejoin: "round" }} />
      {pts.slice(0, -1).map(([m, val]) => (
        <circle key={m} cx={c.x(m)} cy={y(val)} r={3} style={{ fill: "var(--bg-panel)", stroke: "var(--text-2)", strokeWidth: 1.5 }} />
      ))}
      <circle cx={c.x(end[0])} cy={y(end[1])} r={6} style={{ fill: c.outcome, stroke: "var(--bg-panel)", strokeWidth: 2 }} />
      <text x={c.x(end[0]) + 10} y={y(end[1]) + 4} style={lbl(c.outcome)}>
        {end[1].toFixed(2)} · {v.state === "closed" ? money(v.pnl ?? 0) : `${money(v.unrealized ?? 0)} open`}
      </text>
    </Frame>
  );
}

function IndicatorStrip({ c, cyc }: { c: Ctx; cyc: Cycle[] }) {
  const h = 78;
  const rows: [string, (k: Cycle) => [string, string]][] = [
    ["EMA 5m", (k) => (k.ema5 === "bullish" ? ["▲", "var(--grn)"] : k.ema5 === "bearish" ? ["▼", "var(--red)"] : ["·", "var(--text-8)"])],
    ["MACD 5m", (k) => (k.macdDir
      ? [`${k.macd === "positive" ? "+" : "−"}${k.macdDir === "rising" ? "▲" : "▼"}`, k.macdDir === "rising" ? "var(--grn)" : "var(--red)"]
      : ["·", "var(--text-8)"])],
    ["Volume", (k) => [k.vol == null ? "·" : k.vol.toFixed(1), (k.vol ?? 0) >= 1 ? "var(--text-2)" : "var(--text-7)"]],
    ["SQZ 15m", (k) => (k.sq15 === "fired_short" ? ["F↓", "var(--red)"] : k.sq15 === "fired_long" ? ["F↑", "var(--grn)"]
      : k.sq15 === "on" ? ["on", "var(--amb)"] : ["·", "var(--text-8)"])],
  ];
  return (
    <Frame c={c} h={h}>
      <rect x={c.x(c.fillM)} y={0} width={Math.max(0, c.x(c.endM) - c.x(c.fillM))} height={h} style={{ fill: "rgba(var(--accent-rgb), 0.08)" }} />
      {c.hover != null && <line x1={c.x(c.hover)} x2={c.x(c.hover)} y1={0} y2={h} opacity={0.6} style={{ stroke: "var(--text-5)" }} />}
      {rows.map(([name, fn], i) => (
        <g key={name}>
          <text x={M.l - 8} y={15.5 + i * 18} textAnchor="end" style={axisText}>{name}</text>
          {cyc.map((k) => {
            const [txt, col] = fn(k);
            return (
              <text key={k.t} x={c.x(toMin(k.t))} y={15.5 + i * 18} textAnchor="middle"
                style={{ ...axisText, fill: col, fontWeight: col.includes("text-8") ? 400 : 600 }}>{txt}</text>
            );
          })}
        </g>
      ))}
    </Frame>
  );
}

function ChartLabel({ left, right }: { left: string; right: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "10px 4px 3px" }}>
      <span style={capLabel}>{left}</span>
      <span className="mono" style={{ fontSize: 10.5, color: "var(--text-7)" }}>{right}</span>
    </div>
  );
}

function TradeCharts({ v }: { v: TradeView }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver((es) => setWidth(Math.max(520, Math.floor(es[0].contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const [W0, W1] = windowOf(v);
  const X0 = W0 - 4, X1 = W1 + 3;
  const x = (m: number) => M.l + ((m - X0) / (X1 - X0)) * (width - M.l - M.r);
  const cyc = v.cycles.filter((k) => toMin(k.t) >= W0 && toMin(k.t) <= W1);
  const fillM = toMin(v.fillT);
  const endM = v.exitT ? toMin(v.exitT) : cyc.length ? toMin(cyc[cyc.length - 1].t) : fillM;
  const outcome = v.state === "open" ? "var(--accent)" : (v.pnl ?? 0) >= 0 ? "var(--grn)" : "var(--red)";
  const c: Ctx = {
    width, x, W0, W1, fillM, endM, decM: v.decided ? toMin(v.decided) : null, hover, outcome,
    onMove: (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      const m = X0 + ((((e.clientX - r.left) * width) / r.width - M.l) / (width - M.l - M.r)) * (X1 - X0);
      const s = Math.round(m / 5) * 5;
      setHover(s >= W0 && s <= W1 ? s : null);
    },
    onLeave: () => setHover(null),
  };
  const hc = hover != null ? cyc.find((k) => toMin(k.t) === hover) : undefined;
  const hmk = hover != null ? v.marks.find(([t]) => floor5(toMin(t)) === hover) : undefined;
  const long = v.direction === "long";
  return (
    <div ref={box} style={{ minWidth: 0 }}>
      <div className="mono" style={{ display: "flex", flexWrap: "wrap", gap: "2px 14px", minHeight: 20, fontSize: 11, color: "var(--text-5)", padding: "0 4px" }}>
        {hc ? (
          <>
            <span><b style={{ color: "var(--text-2)" }}>{hc.t}</b> cycle {hc.cycle}</span>
            <span>price <b style={{ color: "var(--text-2)" }}>{f2(hc.last)}</b></span>
            <span>VWAP <b style={{ color: "var(--text-2)" }}>{f2(hc.vwap)}</b></span>
            <span>dist <b style={{ color: "var(--text-2)" }}>{hc.vd != null && hc.vd > 0 ? "+" : ""}{f2(hc.vd)} ATR</b></span>
            <span>MACD {hc.macd}/{hc.macdDir}</span>
            <span>vol {f2(hc.vol)}×</span>
            {hmk && <span>option <b style={{ color: "var(--text-2)" }}>{f2(hmk[1])}</b></span>}
          </>
        ) : (
          <span>Hover the charts to read any cycle.</span>
        )}
      </div>
      <ChartLabel left={`${v.ticker} · 5-min bars · VWAP`} right={long ? "long: wants price above VWAP" : "short: wants price below VWAP"} />
      <PriceChart c={c} v={v} cyc={cyc} />
      <ChartLabel left="Distance from VWAP, in ATR" right="entry blocked inside ±0.25 · exit at 0" />
      <VwapDistanceChart c={c} v={v} cyc={cyc} />
      <ChartLabel left={`Option value · ${v.contract.split(" ·")[0] || v.structure}`} right="target · paid · stop" />
      <OptionChart c={c} v={v} />
      <ChartLabel left="Engine indicators each cycle" right="▲ bullish/rising · ▼ bearish/falling · volume × 20-bar avg" />
      <IndicatorStrip c={c} cyc={cyc} />
      {v.barsSource !== "engine" && (
        <div className="mono" style={{ fontSize: 10.5, color: "var(--amb)", padding: "8px 4px 0" }}>
          Candles: {v.barsSource}. VWAP and indicators are still the engine's own.
        </div>
      )}
    </div>
  );
}

// ── rail ─────────────────────────────────────────────────────────────────

const GRADE: Record<Check["verdict"], [string, string]> = {
  for: ["FOR", "var(--grn)"], against: ["AGAINST", "var(--red)"], neutral: ["—", "var(--text-7)"], info: ["", "var(--text-7)"],
};
const KIND: Record<string, string> = {
  decision: "var(--accent)", fill: "var(--accent)", risk: "var(--amb)", for: "var(--grn)",
  against: "var(--red)", exit: "var(--text-2)", after: "var(--text-5)", live: "var(--accent)",
};

function Rail({ v }: { v: TradeView }) {
  return (
    <aside style={{ flex: "0 1 340px", minWidth: 280, display: "flex", flexDirection: "column", gap: 16,
      padding: "14px 16px", background: "var(--bg-inset)", borderLeft: "1px solid var(--border-rail)" }}>
      <div>
        <div style={{ ...capLabel, marginBottom: 6 }}>At the {v.decided ?? "entry"} decision</div>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11.5 }}>
          <tbody>
            {v.checklist.map((k) => (
              <tr key={k.label} style={{ borderBottom: "1px solid var(--border-rail)" }}>
                <td style={{ padding: "5px 8px 5px 0", color: "var(--text-5)" }}>{k.label}</td>
                <td className="mono" style={{ padding: "5px 0", textAlign: "right", color: "var(--text-2)", whiteSpace: "nowrap" }}>{k.value}</td>
                <td className="mono" style={{ width: 62, textAlign: "right", fontSize: 9.5, fontWeight: 700, letterSpacing: "0.05em", color: GRADE[k.verdict][1] }}>
                  {GRADE[k.verdict][0]}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        <div style={{ ...capLabel, marginBottom: 6 }}>What happened</div>
        <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 7 }}>
          {v.timeline.map((e, i) => (
            <li key={i} style={{ display: "grid", gridTemplateColumns: "44px 1fr", gap: 8, fontSize: 12, lineHeight: 1.45 }}>
              <time className="mono" style={{ color: KIND[e.kind] ?? "var(--text-5)", fontWeight: 600 }}>{e.t}</time>
              <span style={{ color: "var(--text-3)" }}>{e.text}</span>
            </li>
          ))}
        </ol>
      </div>
      <div>
        <div style={{ ...capLabel, marginBottom: 6 }}>Verdict</div>
        <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-1)", marginBottom: 6, textWrap: "balance" }}>{v.verdict.headline}</div>
        {v.verdict.lines.map((l, i) => (
          <p key={i} style={{ margin: "0 0 6px", fontSize: 12, lineHeight: 1.5, color: i === v.verdict.lines.length - 1 ? "var(--text-7)" : "var(--text-4)" }}>{l}</p>
        ))}
      </div>
      <div className="mono" style={{ fontSize: 9.5, letterSpacing: "0.12em", color: "var(--amb)" }}>UNVALIDATED — PAPER ONLY</div>
    </aside>
  );
}

function TradeDetail({ v }: { v: TradeView }) {
  const closed = v.state === "closed";
  const win = (v.pnl ?? 0) >= 0;
  const chip = closed
    ? [`${(v.exitReason ?? "exit").replace(/_/g, " ")} · ${money(v.pnl ?? 0)}`, win ? "var(--grn)" : "var(--red)"]
    : [`live · ${money(v.unrealized ?? 0)} open`, "var(--accent)"];
  return (
    <section style={{ ...panel, overflow: "hidden" }}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px 18px", padding: "12px 16px", borderBottom: "1px solid var(--border-rail)" }}>
        <span style={{ fontSize: 18, fontWeight: 700, color: "var(--text-1)" }}>{v.ticker}</span>
        <span className="mono" style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", padding: "5px 8px", borderRadius: 4,
          color: chip[1], border: `1px solid ${chip[1]}` }}>{chip[0]}</span>
        <span className="mono" style={{ fontSize: 11.5, color: "var(--text-5)", display: "flex", flexWrap: "wrap", gap: "2px 16px" }}>
          <span><b style={{ color: "var(--text-2)" }}>{v.structure}</b> {v.contract}</span>
          <span>{v.lane}</span>
          <span>qty <b style={{ color: "var(--text-2)" }}>{v.qty}</b></span>
          <span>in <b style={{ color: "var(--text-2)" }}>{v.entry}</b> @ {v.fillT}</span>
          {closed
            ? <span>out <b style={{ color: "var(--text-2)" }}>{v.exit}</b> @ {v.exitT}</span>
            : <span>now <b style={{ color: "var(--text-2)" }}>{f2(v.lastValue)}</b></span>}
        </span>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 640px", minWidth: 0, padding: "10px 12px 14px" }}>
          <TradeCharts v={v} />
        </div>
        <Rail v={v} />
      </div>
    </section>
  );
}

// ── page ─────────────────────────────────────────────────────────────────

export function TradeViewPage() {
  const { feed } = useCockpit();
  const [date, setDate] = useState(ptToday);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [view, setView] = useState<TradeView | null>(null);
  const isToday = date === ptToday();

  useEffect(() => {
    if (feed.simulated) return;
    let alive = true;
    const load = () =>
      fetch(`/api/tradeview?date=${date}`)
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((d) => { if (alive) { setRows(d.trades ?? []); setFailed(false); } })
        .catch(() => alive && setFailed(true));
    load();
    const t = isToday ? setInterval(load, 30000) : undefined;
    return () => { alive = false; if (t) clearInterval(t); };
  }, [date, isToday, feed.simulated]);

  const selected = rows?.find((r) => r.recId === sel) ?? rows?.[0];
  const selId = selected?.recId ?? null;
  const live = selected?.state === "open";

  useEffect(() => {
    if (!selId || feed.simulated) return;
    let alive = true;
    const load = () =>
      fetch(`/api/tradeview/${encodeURIComponent(selId)}`)
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((d) => alive && setView(d.recId ? d : null))
        .catch(() => alive && setView(null));
    load();
    const t = live ? setInterval(load, 30000) : undefined;
    return () => { alive = false; if (t) clearInterval(t); };
  }, [selId, live, feed.simulated]);

  const shown = view && view.recId === selId ? view : null;

  return (
    <div style={{ padding: 14, display: "flex", gap: 14, alignItems: "flex-start", flexWrap: "wrap" }}>
      <aside style={{ ...panel, flex: "0 0 250px", overflow: "hidden" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 12px", borderBottom: "1px solid var(--border-rail)" }}>
          <span style={capLabel}>Trades</span>
          <input type="date" value={date} max={ptToday()} onChange={(e) => e.target.value && setDate(e.target.value)}
            className="mono" aria-label="Session date"
            style={{ fontSize: 11, background: "var(--bg-inset)", color: "var(--text-3)", border: "1px solid var(--border-panel)", borderRadius: 5, padding: "3px 6px" }} />
        </div>
        {feed.simulated ? (
          <div className="mono" style={{ padding: 14, fontSize: 11, color: "var(--text-7)" }}>Needs the live cockpit API.</div>
        ) : failed ? (
          <div className="mono" style={{ padding: 14, fontSize: 11, color: "var(--red)" }}>Could not load trades.</div>
        ) : rows && rows.length === 0 ? (
          <div className="mono" style={{ padding: 14, fontSize: 11, color: "var(--text-7)" }}>No option trades filled on {date}.</div>
        ) : (
          (rows ?? []).map((r) => {
            const active = r.recId === selId;
            const val = r.state === "open" ? r.unrealized : r.pnl;
            return (
              <button key={r.recId} onClick={() => setSel(r.recId)} aria-pressed={active}
                style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: "2px 8px", width: "100%", textAlign: "left", cursor: "pointer",
                  padding: "9px 12px", border: 0, borderBottom: "1px solid var(--border-rail)",
                  background: active ? "rgba(var(--accent-rgb), 0.12)" : "transparent", color: "var(--text-3)",
                  boxShadow: active ? "inset 3px 0 0 var(--accent)" : "none" }}>
                <span style={{ fontWeight: 700, color: "var(--text-1)", fontSize: 13 }}>
                  {r.ticker} <span className="mono" style={{ fontSize: 9, color: r.lane === "SCALP" ? "var(--amb)" : "var(--accent)", fontWeight: 700 }}>{r.lane}</span>
                </span>
                <span className="mono" style={{ fontSize: 12, fontWeight: 700, textAlign: "right", color: val == null ? "var(--text-7)" : pnlColor(val) }}>
                  {val == null ? "—" : money(val)}
                </span>
                <span className="mono" style={{ fontSize: 10, color: "var(--text-6)" }}>
                  {r.entryT}{r.exitT ? `–${r.exitT}` : ""} · {r.structure.replace(/_/g, " ")}
                </span>
                <span className="mono" style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: "0.06em", textAlign: "right",
                  color: r.state === "open" ? "var(--accent)" : "var(--text-7)" }}>
                  {r.state === "open" ? "● LIVE" : (r.exitReason ?? "closed").replace(/_/g, " ")}
                </span>
              </button>
            );
          })
        )}
      </aside>
      <main style={{ flex: "1 1 700px", minWidth: 0 }}>
        {shown ? (
          <TradeDetail v={shown} />
        ) : (
          <div className="mono" style={{ ...panel, padding: 30, textAlign: "center", fontSize: 11, color: "var(--text-7)" }}>
            {feed.simulated ? "Trade view needs the live cockpit API." : selId ? "Loading trade…" : "Select a trade."}
          </div>
        )}
      </main>
    </div>
  );
}
