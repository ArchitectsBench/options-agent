/** The cockpit's data contract — the seam where the real agent feed
 * (FastAPI REST + WebSocket, not yet built) will plug in. Everything the
 * UI renders comes through this shape; src/feed/mock.ts provides the
 * static prototype data until the API exists. */

export type AgentState = "running" | "orders-blocked" | "heartbeat" | "dead";
export type Mode = "observation" | "live";
export type ChipState = "pass" | "fail" | "wait";

export interface Regime {
  bias: string;
  trend: string;
  vix: string;
  gex: string; // dealer-gamma sign: POS | NEG | — (CONTEXT only, never scored)
  gexNet?: number | null; // net GEX in raw $ (chain-estimated)
  gexRegime?: string | null; // amplifying | suppressive | neutral
  gexFlip?: number | null; // zero-gamma flip level
  gexSpot?: number | null;
  gexEstimated?: boolean;
}

export interface ClockRing {
  done: number;
  goal: number;
  title: string;
  sub: string;
}

export interface ClockChip {
  label: string;
  value: string;
  n: string;
  state: ChipState;
}

export interface ForwardClock {
  rings: ClockRing[]; // [qualifying trades, trading-day span]
  adverse: { met: boolean; label: string };
  chips: ClockChip[]; // 5 pass/fail criteria
  gate: { label: string; icon: string; eta: string };
}

export interface PnlSet {
  realized: number;
  roundTrips: number;
  wins: number;
  losses: number;
  /** per-view open-position stats (2026-07-14: FUNNEL ONLY excludes test lane) */
  openPositions?: number;
  unrealized?: number;
  /** L21 market-truth realized (what the gates count) + coverage */
  realizedBooked?: number;
  marketTruth?: number | null;
  executionCost?: number | null;
  trueRealized?: number;
  trueScored?: number;
  truePending?: number;
}

export interface TodayStats {
  sessionDate: string;
  /** funnel = Claude-validated stream (default view); blended = funnel +
   * SCALP-candidate stream merged (whole-book view; per-stream gates stay separate) */
  funnel: PnlSet;
  blended: PnlSet;
  unrealized: number;
  markAge: string;
  breakerLimit: number; // e.g. -1500
  breakerTripped: boolean;
}

export type WatchdogState = "stream" | "idle" | "degraded";
export type ExitKey = "V" | "S" | "T" | "F";

export interface Position {
  /** "funnel" | "test" — which evidence stream owns this position */
  sleeve?: string;
  /** rec_id — keys the /api/position/{recId}/detail fetch */
  recId: string;
  ticker: string;
  structure: string;
  dir: "LONG" | "SHORT";
  qty: number;
  mae: string;
  mfe: string;
  /** current-mark marker position on the MFE/MAE bar, 0-100 */
  markerL: number;
  entry: string;
  mark: string;
  unreal: string;
  unrealN: number;
  maxLoss: string;
  age: string;
  exits: { k: ExitKey; armed: boolean; title: string }[];
  watchdog: WatchdogState;
}

export type RecStatus = "pending" | "approved" | "vetoed" | "skipped";

export interface Recommendation {
  id: string;
  ticker: string;
  dir: "LONG" | "SHORT";
  structure: string;
  legs: string; // preformatted mono block, \n-separated
  math: { k: string; v: string; kind: "neutral" | "loss" | "profit" }[] | null;
  status: RecStatus;
  statusLabel: string;
  verdict: string;
  verdictIcon: string;
  verdictKind: "approve" | "veto" | "skip";
  reasoning: string;
  /** pre-decided at feed time (e.g. filled) */
  decidedLabel?: string;
  decidedKind?: "confirmed" | "declined";
}

export type LogTag = "cycle" | "fill" | "exit" | "safety" | "alarm" | "feed" | "v2";

export interface LogEntry {
  time: string;
  tag: string;
  kind: LogTag;
  text: string;
  meta?: string;
  metaKind?: "p" | "n" | "mut";
}

/* ---------- Page 2 — Performance & Exits ---------- */

/** semantic tone for exit chips / reason bars: green / red / amber */
export type ExitTone = "g" | "r" | "a" | "mut";

export interface PnlSession {
  day: string; // x-axis label, e.g. "Jun12"
  funnel: number;
  blended: number;
}

export interface Sleeve {
  name: string;
  tag: string; // "FUNNEL" on the accent card, "—" otherwise
  accent: boolean;
  note: string;
  win: string; // e.g. "60%"
  trades: number;
  pnl: number;
}

export interface ExitReasonStat {
  label: string;
  count: number;
  pnl: number;
  tone: ExitTone;
}

export interface SlipBucket {
  bucket: string; // e.g. "−0.02"
  n: number;
}

export interface RoundTrip {
  time: string;
  ticker: string;
  sleeve: string;
  dir: "LONG" | "SHORT";
  struct: string;
  px: string; // "entry→exit" preformatted
  exit: string;
  exitTone: ExitTone;
  pnl: string;
  pnlN: number;
  slip: string;
  giveBack: string;
  /** SCALP-candidate stream row — only shown in the blended view */
  test?: boolean;
}

export interface TruthCoverage {
  realized: number;
  scored: number;
  pending: number;
}

export interface PerfStats {
  daily: PnlSession[]; // 16 sessions
  /** full-period market-truth (NBBO mid) realized + scored/pending coverage */
  truth?: { funnel: TruthCoverage; blended: TruthCoverage };
  sleeves: Sleeve[];
  exitReasons: ExitReasonStat[];
  giveBack: { avg: string; worst: string; note: string };
  slippage: { buckets: SlipBucket[]; median: string; avg: string };
  /** funnel round trips (always shown) */
  trades: RoundTrip[];
  /** execution-test rows appended in blended view */
  testTrades: RoundTrip[];
}

/* ---------- Page 3 — System Health ---------- */

export type FeedStatus = "live" | "standby" | "degraded" | "down";

export interface DataFeedRow {
  name: string;
  role: string;
  status: FeedStatus;
  latency: string;
  last: string;
}

export type EarningsState = "block" | "watch" | "clear" | "etf";

export interface EarningsRow {
  ticker: string;
  date: string;
  state: EarningsState;
}

export type JobStatus = "ok" | "warn" | "fail";

export interface JobRow {
  name: string;
  detail: string;
  last: string;
  status: JobStatus;
}

export type WatchdogHealth = "stream" | "degraded" | "offline";

export interface HealthStats {
  errors24h: number;
  warnings24h: number;
  feeds: DataFeedRow[];
  host: { sleepPrevention: string; uptime: string; cpu: string; mem: string };
  watchdog: WatchdogHealth;
  earnings: EarningsRow[];
  jobs: JobRow[];
}

/* ---------- Page 4 — Reports Reader ---------- */

/** inline run inside report text; color "bright" = primary text emphasis */
export interface ReportSpan {
  text: string;
  bold?: boolean;
  mono?: boolean;
  color?: "grn" | "amb" | "red" | "bright";
}

export type ReportBlock =
  | { kind: "callout"; tone: "warn" | "ok"; text: string }
  | { kind: "h2"; text: string }
  | { kind: "list"; ordered?: boolean; items: ReportSpan[][] }
  | { kind: "para"; spans: ReportSpan[] };

export interface Report {
  id: string;
  title: string;
  date: string;
  /** open ACTION items; drives the list badge + nav rail badge sum */
  actionCount: number;
  kicker: string; // e.g. "WEEKLY CHECK-IN · GENERATED Jun 29 2026 06:00 PT"
  heading: string;
  subtitle: string;
  blocks: ReportBlock[];
}

/* ---------- per-position detail (payoff / price / GEX panels) ---------- */

export interface PositionDetailData {
  recId: string;
  ticker: string;
  structure: string;
  generatedAt: string;
  payoff: {
    points: { s: number; pnl: number }[];
    breakevens: number[];
    maxProfit: number;
    maxLoss: number;
    spot: number | null;
    entryDebit: number;
    quantity: number;
  } | null;
  price: {
    session: string;
    bars: { t: string; c: number; vwap: number }[];
    entryT: string | null;
  } | null;
  gex: {
    expiration: string;
    spot: number;
    zeroGamma: number | null;
    netGex: number;
    strikes: { k: number; gex: number }[];
    label: string;
  } | null;
}

export interface CockpitFeed {
  /** true while the UI renders static mock data instead of the live agent
   * feed — drives an unmissable SIMULATED banner. The real API sets false. */
  simulated: boolean;
  /** authoritative kill-switch state from the agent (real feed only) */
  killEngaged?: boolean;
  agent: AgentState;
  mode: Mode;
  marketOpen: boolean;
  /** seconds until next cycle at feed time; UI ticks it locally */
  cycleSeconds: number;
  regime: Regime;
  claude: { used: number; max: number };
  forwardClock: ForwardClock;
  today: TodayStats;
  positions: Position[];
  /** shown in the positions empty state */
  noPositionsReason: string;
  cycleId: string;
  recs: Recommendation[];
  noRecsReason: string;
  /** seconds left on the pending confirmation at feed time */
  confirmSeconds: number;
  logs: LogEntry[];
  dataAge: string;
  perf: PerfStats;
  health: HealthStats;
  reports: Report[];
}
