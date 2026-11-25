import { useEffect, useState } from "react";
import { useCockpit } from "../store";
import type { PositionDetailData } from "../feed/types";
import { SAMPLE_DETAIL } from "../feed/sampleDetail";

/** Per-position payoff / price / GEX panels (dataviz-spec SVG, no libs).
 * Labels wear text tokens; green/red are the app's semantic polarity pair
 * (CVD-validated on the panel surface); grids are recessive hairlines. */

const W = 300;
const H = 150;
const PAD = { l: 8, r: 8, t: 12, b: 20 };
const PW = W - PAD.l - PAD.r;
const PH = H - PAD.t - PAD.b;

const GRN = "var(--grn)";
const RED = "var(--red)";
const money = (n: number) =>
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;

function Panel({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <div
      style={{
        flex: "1 1 300px",
        minWidth: 280,
        border: "1px solid var(--border-inset)",
        borderRadius: 8,
        background: "var(--bg-inset)",
        padding: "10px 12px",
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.12em", color: "var(--text-4)" }}>
          {title}
        </span>
        {note && (
          <span className="mono" style={{ fontSize: 8.5, color: "var(--text-9)" }}>
            {note}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}

/* ── payoff at expiry ── */
function PayoffChart({ d }: { d: NonNullable<PositionDetailData["payoff"]> }) {
  const xs = d.points.map((p) => p.s);
  const lo = Math.min(...xs);
  const hi = Math.max(...xs);
  const yMax = Math.max(d.maxProfit, 0);
  const yMin = Math.min(d.maxLoss, 0);
  const X = (s: number) => PAD.l + ((s - lo) / (hi - lo)) * PW;
  const Y = (pnl: number) => PAD.t + ((yMax - pnl) / (yMax - yMin || 1)) * PH;
  const line = d.points.map((p, i) => `${i ? "L" : "M"}${X(p.s).toFixed(1)},${Y(p.pnl).toFixed(1)}`).join(" ");
  const zero = Y(0);
  const area = `${line} L${X(hi)},${zero} L${X(lo)},${zero} Z`;
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block", marginTop: 6 }}>
      <clipPath id="pf-up"><rect x={0} y={0} width={W} height={zero} /></clipPath>
      <clipPath id="pf-dn"><rect x={0} y={zero} width={W} height={H - zero} /></clipPath>
      <path d={area} fill="rgba(53,208,127,0.13)" clipPath="url(#pf-up)" />
      <path d={area} fill="rgba(255,90,95,0.13)" clipPath="url(#pf-dn)" />
      <line x1={PAD.l} x2={W - PAD.r} y1={zero} y2={zero} stroke="var(--hairline)" strokeWidth={1} />
      <path d={line} fill="none" stroke="var(--text-2)" strokeWidth={2} strokeLinejoin="round" />
      {d.breakevens.map((be) => (
        <g key={be}>
          <line x1={X(be)} x2={X(be)} y1={PAD.t} y2={H - PAD.b} stroke="var(--text-7)" strokeWidth={1} strokeDasharray="3 3" />
          <text x={X(be)} y={H - 7} textAnchor="middle" fontSize={8.5} fill="var(--text-6)" fontFamily="var(--font-mono)">
            BE {be}
          </text>
        </g>
      ))}
      {d.spot != null && (
        <g>
          <line x1={X(d.spot)} x2={X(d.spot)} y1={PAD.t} y2={H - PAD.b} stroke="var(--accent)" strokeWidth={1.5} />
          <text x={X(d.spot)} y={PAD.t - 2} textAnchor="middle" fontSize={8.5} fill="var(--accent)" fontFamily="var(--font-mono)">
            spot {d.spot}
          </text>
        </g>
      )}
      <text x={W - PAD.r} y={Y(d.maxProfit) + 9} textAnchor="end" fontSize={8.5} fill={GRN} fontFamily="var(--font-mono)">
        max {money(d.maxProfit)}
      </text>
      <text x={W - PAD.r} y={Y(d.maxLoss) - 3} textAnchor="end" fontSize={8.5} fill={RED} fontFamily="var(--font-mono)">
        max {money(d.maxLoss)}
      </text>
      {/* hover targets */}
      {d.points.map((p, i) => (
        <rect key={i} x={X(p.s) - 4} y={PAD.t} width={8} height={PH} fill="transparent">
          <title>{`${p.s.toFixed(2)} → ${money(p.pnl)} at expiry`}</title>
        </rect>
      ))}
    </svg>
  );
}

/* ── intraday price + VWAP ── */
function PriceChart({ d }: { d: NonNullable<PositionDetailData["price"]> }) {
  const vals = d.bars.flatMap((b) => [b.c, b.vwap]);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const X = (i: number) => PAD.l + (i / Math.max(1, d.bars.length - 1)) * PW;
  const Y = (v: number) => PAD.t + ((hi - v) / (hi - lo || 1)) * PH;
  const path = (get: (b: { c: number; vwap: number }) => number) =>
    d.bars.map((b, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(get(b)).toFixed(1)}`).join(" ");
  const entryIdx = d.entryT ? d.bars.findIndex((b) => b.t >= (d.entryT as string)) : -1;
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block", marginTop: 6 }}>
      {[0.25, 0.5, 0.75].map((f) => (
        <line key={f} x1={PAD.l} x2={W - PAD.r} y1={PAD.t + PH * f} y2={PAD.t + PH * f}
          stroke="var(--hairline)" strokeWidth={0.5} opacity={0.5} />
      ))}
      <path d={path((b) => b.vwap)} fill="none" stroke="var(--amb)" strokeWidth={2} strokeDasharray="4 3" />
      <path d={path((b) => b.c)} fill="none" stroke="var(--accent)" strokeWidth={2} />
      {entryIdx >= 0 && (
        <g>
          <line x1={X(entryIdx)} x2={X(entryIdx)} y1={PAD.t} y2={H - PAD.b} stroke="var(--text-7)" strokeWidth={1} strokeDasharray="2 3" />
          <text x={X(entryIdx)} y={H - 7} textAnchor="middle" fontSize={8.5} fill="var(--text-6)" fontFamily="var(--font-mono)">
            ▲ entry {d.entryT}
          </text>
        </g>
      )}
      {/* legend (2 series): colored marks carry identity, text wears tokens */}
      <g fontFamily="var(--font-mono)" fontSize={8.5}>
        <rect x={PAD.l} y={2} width={8} height={2.5} fill="var(--accent)" />
        <text x={PAD.l + 11} y={6} fill="var(--text-6)">price</text>
        <rect x={PAD.l + 42} y={2} width={8} height={2.5} fill="var(--amb)" />
        <text x={PAD.l + 53} y={6} fill="var(--text-6)">VWAP</text>
      </g>
      {d.bars.map((b, i) => (
        <rect key={i} x={X(i) - 5} y={PAD.t} width={10} height={PH} fill="transparent">
          <title>{`${b.t} · ${b.c.toFixed(2)} (VWAP ${b.vwap.toFixed(2)})`}</title>
        </rect>
      ))}
    </svg>
  );
}

/* ── per-strike GEX ── */
function GexChart({ d }: { d: NonNullable<PositionDetailData["gex"]> }) {
  const mags = d.strikes.map((s) => Math.abs(s.gex));
  const maxMag = Math.max(...mags, 1);
  const n = d.strikes.length;
  const slot = PW / n;
  const bw = Math.max(4, slot - 2); // 2px surface gap between bars
  const zero = PAD.t + PH / 2;
  const ks = d.strikes.map((s) => s.k);
  const X = (k: number) =>
    PAD.l + ((k - Math.min(...ks)) / (Math.max(...ks) - Math.min(...ks) || 1)) * (PW - slot) + slot / 2;
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block", marginTop: 6 }}>
      <line x1={PAD.l} x2={W - PAD.r} y1={zero} y2={zero} stroke="var(--hairline)" strokeWidth={1} />
      {d.strikes.map((s) => {
        const h = (Math.abs(s.gex) / maxMag) * (PH / 2 - 4);
        const up = s.gex >= 0;
        return (
          <g key={s.k}>
            <rect
              x={X(s.k) - bw / 2}
              y={up ? zero - h : zero}
              width={bw}
              height={Math.max(2, h)}
              rx={2}
              fill={up ? GRN : RED}
              opacity={0.85}
            >
              <title>{`${s.k}: ${s.gex >= 0 ? "+" : "−"}${Math.abs(s.gex).toLocaleString()} GEX`}</title>
            </rect>
            <text x={X(s.k)} y={H - 7} textAnchor="middle" fontSize={8} fill="var(--text-8)" fontFamily="var(--font-mono)">
              {s.k}
            </text>
          </g>
        );
      })}
      <line x1={X(d.spot)} x2={X(d.spot)} y1={PAD.t} y2={H - PAD.b} stroke="var(--accent)" strokeWidth={1.5} />
      <text x={X(d.spot)} y={PAD.t - 2} textAnchor="middle" fontSize={8.5} fill="var(--accent)" fontFamily="var(--font-mono)">
        spot
      </text>
      {d.zeroGamma != null && d.zeroGamma >= Math.min(...ks) && d.zeroGamma <= Math.max(...ks) && (
        <g>
          <line x1={X(d.zeroGamma)} x2={X(d.zeroGamma)} y1={PAD.t} y2={H - PAD.b}
            stroke="var(--amb)" strokeWidth={1} strokeDasharray="3 3" />
          <text x={X(d.zeroGamma)} y={PAD.t + 8} textAnchor="middle" fontSize={8.5} fill="var(--amb)" fontFamily="var(--font-mono)">
            0γ {d.zeroGamma}
          </text>
        </g>
      )}
    </svg>
  );
}

const Unavailable = ({ what }: { what: string }) => (
  <div className="mono" style={{ padding: "30px 0", textAlign: "center", fontSize: 10, color: "var(--text-9)" }}>
    {what} unavailable
  </div>
);

export function PositionDetail({ recId }: { recId: string }) {
  const { feed } = useCockpit();
  const [detail, setDetail] = useState<PositionDetailData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    if (feed.simulated) {
      setDetail(SAMPLE_DETAIL);
      return;
    }
    fetch(`/api/position/${encodeURIComponent(recId)}/detail`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => alive && (d.recId ? setDetail(d) : setFailed(true)))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [recId, feed.simulated]);

  if (failed) return <Unavailable what="position detail" />;
  if (!detail)
    return (
      <div className="mono" style={{ padding: "22px 0", textAlign: "center", fontSize: 10, color: "var(--text-9)" }}>
        loading detail…
      </div>
    );

  return (
    <div style={{ display: "flex", gap: 10, flexWrap: "wrap", padding: "10px 15px 14px", background: "rgba(0,0,0,0.15)" }}>
      <Panel
        title="PAYOFF AT EXPIRY"
        note={detail.payoff ? `debit ${detail.payoff.entryDebit} ×${detail.payoff.quantity}` : undefined}
      >
        {detail.payoff ? <PayoffChart d={detail.payoff} /> : <Unavailable what="payoff" />}
      </Panel>
      <Panel title="UNDERLYING · 5M" note={detail.price?.session}>
        {detail.price ? <PriceChart d={detail.price} /> : <Unavailable what="price data" />}
      </Panel>
      <Panel title="GEX BY STRIKE" note={detail.gex ? `${detail.gex.label} · net ${detail.gex.netGex.toLocaleString()}` : undefined}>
        {detail.gex ? <GexChart d={detail.gex} /> : <Unavailable what="chain data" />}
      </Panel>
    </div>
  );
}
