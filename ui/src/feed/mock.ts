import type { CockpitFeed } from "./types";

/** Static mock matching the design prototype's happy-path view-model
 * (OptionsAgent Cockpit.dc.html renderVals). Replaced by the live feed
 * when the API layer ships. */
export const mockFeed: CockpitFeed = {
  simulated: true,
  killEngaged: false,
  agent: "running",
  mode: "observation",
  marketOpen: true,
  cycleSeconds: 167,
  regime: { bias: "bearish", trend: "down", vix: "18.4", gex: "negative" },
  claude: { used: 17, max: 30 },
  forwardClock: {
    rings: [
      { done: 7, goal: 100, title: "Qualifying trades", sub: "funnel-validated round trips" },
      { done: 4, goal: 60, title: "Trading-day span", sub: "distinct sessions elapsed" },
    ],
    adverse: { met: false, label: "NOT YET" },
    chips: [
      { label: "Sharpe", value: "—", n: "n=7 · need 20", state: "wait" },
      { label: "Profit factor", value: "1.84", n: "n=7", state: "pass" },
      { label: "Max drawdown", value: "−4.1%", n: "ok", state: "pass" },
      { label: "Per-sleeve edge", value: "2/3", n: "1 short", state: "fail" },
      { label: "Concentration", value: "12%", n: "ok", state: "pass" },
    ],
    gate: { label: "LOCKED", icon: "🔒", eta: "Q4 2026" },
  },
  today: {
    sessionDate: "2026-07-04",
    funnel: { realized: 248, roundTrips: 6, wins: 4, losses: 2 },
    blended: { realized: 612, roundTrips: 14, wins: 10, losses: 4 },
    unrealized: 40,
    markAge: "2s",
    breakerLimit: -1500,
    breakerTripped: false,
  },
  positions: [
    {
      recId: "sample-tlt", ticker: "TLT", structure: "Put debit spread", dir: "SHORT", qty: 10,
      mae: "−$40", mfe: "+$120", markerL: 34, entry: "0.16", mark: "0.17",
      unreal: "+$10", unrealN: 10, maxLoss: "$160", age: "1h04",
      exits: [
        { k: "V", armed: true, title: "VWAP invalidation armed" },
        { k: "S", armed: true, title: "Stop armed" },
        { k: "T", armed: true, title: "Target armed" },
        { k: "F", armed: true, title: "15:45 flatten" },
      ],
      watchdog: "stream",
    },
    {
      recId: "sample-nvda", ticker: "NVDA", structure: "Call debit spread", dir: "LONG", qty: 3,
      mae: "−$70", mfe: "+$210", markerL: 62, entry: "2.10", mark: "2.42",
      unreal: "+$96", unrealN: 96, maxLoss: "$630", age: "42m",
      exits: [
        { k: "V", armed: true, title: "VWAP invalidation armed" },
        { k: "S", armed: true, title: "Stop armed" },
        { k: "T", armed: true, title: "Target armed" },
        { k: "F", armed: true, title: "15:45 flatten" },
      ],
      watchdog: "stream",
    },
    {
      recId: "sample-spy", ticker: "SPY", structure: "Put debit spread", dir: "SHORT", qty: 5,
      mae: "−$150", mfe: "+$60", markerL: 22, entry: "0.90", mark: "0.72",
      unreal: "−$90", unrealN: -90, maxLoss: "$450", age: "1h51",
      exits: [
        { k: "V", armed: true, title: "VWAP invalidation armed" },
        { k: "S", armed: true, title: "Stop armed" },
        { k: "T", armed: false, title: "Target not armed" },
        { k: "F", armed: true, title: "15:45 flatten" },
      ],
      watchdog: "stream",
    },
    {
      recId: "sample-qqq", ticker: "QQQ", structure: "0DTE call spread", dir: "LONG", qty: 4,
      mae: "−$88", mfe: "+$96", markerL: 56, entry: "0.55", mark: "0.61",
      unreal: "+$24", unrealN: 24, maxLoss: "$220", age: "18m",
      exits: [
        { k: "V", armed: true, title: "VWAP invalidation armed" },
        { k: "S", armed: true, title: "Stop armed" },
        { k: "T", armed: true, title: "Target armed" },
        { k: "F", armed: true, title: "15:45 flatten" },
      ],
      watchdog: "stream",
    },
  ],
  noPositionsReason: "Scanner active · awaiting a qualifying setup.",
  cycleId: "#148",
  confirmSeconds: 92,
  recs: [
    {
      id: "aapl", ticker: "AAPL", dir: "LONG",
      structure: "Call debit spread · exp 2026-07-11",
      legs: "BUY  +1  AAPL  07/11  212.5 C\nSELL −1  AAPL  07/11  217.5 C\nsized ×4  ·  net debit 1.85",
      math: [
        { k: "NET DEBIT", v: "$740", kind: "neutral" },
        { k: "MAX LOSS", v: "$740", kind: "loss" },
        { k: "MAX PROFIT", v: "$1,260", kind: "profit" },
      ],
      status: "pending", statusLabel: "● CONFIRM NEEDED",
      verdict: "APPROVED", verdictIcon: "✓", verdictKind: "approve",
      reasoning:
        "Momentum aligns with regime; IV rank 38%, chain liquid (bid/ask ≤0.04). R:R 1.7, within sleeve limits.",
    },
    {
      id: "tlt", ticker: "TLT", dir: "SHORT",
      structure: "Put debit spread · exp 2026-07-11",
      legs: "BUY  +1  TLT  07/11  88 P\nSELL −1  TLT  07/11  86 P\nsized ×10  ·  net debit 0.16",
      math: [
        { k: "NET DEBIT", v: "$1,600", kind: "neutral" },
        { k: "MAX LOSS", v: "$160", kind: "loss" },
        { k: "MAX PROFIT", v: "$1,840", kind: "profit" },
      ],
      status: "approved", statusLabel: "APPROVED · FILLED",
      verdict: "APPROVED", verdictIcon: "✓", verdictKind: "approve",
      reasoning:
        "Bearish rate bias confirmed by trend + negative GEX. Executed — now tracked in open positions.",
      decidedLabel: "✓ FILLED — see open positions (×10 @ 0.16)",
      decidedKind: "confirmed",
    },
    {
      id: "smci", ticker: "SMCI", dir: "LONG",
      structure: "Call debit spread · exp 2026-07-11",
      legs: "BUY  +1  SMCI 07/11  46 C\nSELL −1  SMCI 07/11  50 C\nnet debit 1.95",
      math: null,
      status: "vetoed", statusLabel: "VETOED",
      verdict: "VETOED", verdictIcon: "⛔", verdictKind: "veto",
      reasoning:
        "Chasing an extended move into a mean-reversion regime; poor R:R. Reviewer blocked entry.",
    },
    {
      id: "jpm", ticker: "JPM", dir: "SHORT",
      structure: "Put debit spread",
      legs: "setup skipped before sizing",
      math: null,
      status: "skipped", statusLabel: "SKIPPED · EARNINGS",
      verdict: "EARNINGS VETO", verdictIcon: "⤼", verdictKind: "skip",
      reasoning:
        "JPM earnings 2026-07-14 falls inside the holding window. Hard earnings-veto rule triggered.",
    },
  ],
  noRecsReason: "Scanner running · no setups cleared the gate this cycle.",
  logs: [
    { time: "13:04:58", tag: "CYCLE", kind: "cycle", text: "Cycle #148 complete · 30 tickers scanned · 1 recommendation surfaced", meta: "budget 17/30 AI calls", metaKind: "mut" },
    { time: "13:03:11", tag: "FILL", kind: "fill", text: "NVDA call debit spread ×3 filled @ 2.10", meta: "slippage +0.02 ($6)", metaKind: "n" },
    { time: "12:41:07", tag: "EXIT", kind: "exit", text: "MARA call spread ×6 closed · target hit", meta: "+$132 realized", metaKind: "p" },
    { time: "12:38:52", tag: "SAFETY", kind: "safety", text: "Earnings veto · JPM report 2026-07-14 inside window" },
    { time: "12:30:00", tag: "CYCLE", kind: "cycle", text: "Cycle #142 heartbeat · regime unchanged (bearish/down)" },
    { time: "12:19:44", tag: "EXIT", kind: "exit", text: "SPY put spread ×4 · VWAP invalidation exit", meta: "−$58 realized", metaKind: "n" },
    { time: "11:58:03", tag: "FEED", kind: "feed", text: "yfinance fallback engaged for TICK proxy (Tradier lag 4s)" },
    { time: "11:30:00", tag: "CYCLE", kind: "cycle", text: "Cycle #124 complete · no qualifying setups" },
  ],
  dataAge: "updated 2s ago",

  /* ---------- Page 2 — Performance & Exits ---------- */
  perf: {
    daily: [
      { day: "Jun12", funnel: -58, blended: -58 },
      { day: "Jun13", funnel: 120, blended: 120 },
      { day: "Jun16", funnel: 0, blended: 90 },
      { day: "Jun17", funnel: 210, blended: 210 },
      { day: "Jun18", funnel: -40, blended: -40 },
      { day: "Jun19", funnel: 96, blended: 180 },
      { day: "Jun20", funnel: 24, blended: 140 },
      { day: "Jun23", funnel: 180, blended: 180 },
      { day: "Jun24", funnel: -110, blended: -40 },
      { day: "Jun25", funnel: 132, blended: 132 },
      { day: "Jun26", funnel: 60, blended: 220 },
      { day: "Jun27", funnel: -32, blended: 60 },
      { day: "Jun30", funnel: 88, blended: 150 },
      { day: "Jul1", funnel: 40, blended: 120 },
      { day: "Jul2", funnel: 156, blended: 260 },
      { day: "Jul3", funnel: 248, blended: 612 },
    ],
    sleeves: [
      { name: "0DTE", tag: "—", accent: false, note: "same-day call/put spreads", win: "60%", trades: 5, pnl: 128 },
      { name: "Debit spread", tag: "—", accent: false, note: "1–5 DTE directional", win: "62%", trades: 8, pnl: 184 },
      { name: "Funnel · validated", tag: "FUNNEL", accent: true, note: "AI-reviewed recommendations only", win: "57%", trades: 7, pnl: 248 },
    ],
    exitReasons: [
      { label: "Target hit", count: 3, pnl: 312, tone: "g" },
      { label: "VWAP invalidation", count: 4, pnl: -146, tone: "r" },
      { label: "Stop", count: 2, pnl: -98, tone: "r" },
      { label: "Time-flatten 15:45", count: 3, pnl: 64, tone: "a" },
      { label: "Quote-outage flatten", count: 1, pnl: -22, tone: "a" },
    ],
    giveBack: {
      avg: "18%",
      worst: "$255",
      note: "Trades are surrendering ~1/5 of peak unrealized before exit fires. Tightening VWAP-invalidation latency is the current tuning priority.",
    },
    slippage: {
      buckets: [
        { bucket: "−0.02", n: 1 },
        { bucket: "−0.01", n: 2 },
        { bucket: "0.00", n: 4 },
        { bucket: "+0.01", n: 5 },
        { bucket: "+0.02", n: 3 },
        { bucket: "+0.03", n: 1 },
        { bucket: "+0.05", n: 1 },
      ],
      median: "+0.01",
      avg: "+$1.20/ct",
    },
    trades: [
      { time: "Jul3 12:41", ticker: "MARA", sleeve: "0DTE", dir: "LONG", struct: "Call spread ×6", px: "0.22→0.44", exit: "Target", exitTone: "g", pnl: "+$132", pnlN: 132, slip: "+0.01", giveBack: "6%" },
      { time: "Jul3 12:19", ticker: "SPY", sleeve: "Debit", dir: "SHORT", struct: "Put spread ×4", px: "0.90→0.76", exit: "VWAP inv.", exitTone: "r", pnl: "−$58", pnlN: -58, slip: "−0.01", giveBack: "—" },
      { time: "Jul2 13:44", ticker: "NVDA", sleeve: "Debit", dir: "LONG", struct: "Call spread ×3", px: "1.80→2.40", exit: "Target", exitTone: "g", pnl: "+$180", pnlN: 180, slip: "+0.02", giveBack: "12%" },
      { time: "Jul2 11:02", ticker: "QQQ", sleeve: "0DTE", dir: "LONG", struct: "Call spread ×5", px: "0.60→0.48", exit: "Stop", exitTone: "r", pnl: "−$60", pnlN: -60, slip: "0.00", giveBack: "—" },
      { time: "Jul1 13:45", ticker: "AAPL", sleeve: "Debit", dir: "LONG", struct: "Call spread ×4", px: "1.50→1.88", exit: "Time-flat", exitTone: "a", pnl: "+$152", pnlN: 152, slip: "+0.01", giveBack: "22%" },
      { time: "Jul1 10:31", ticker: "TLT", sleeve: "Funnel", dir: "SHORT", struct: "Put spread ×10", px: "0.14→0.20", exit: "Target", exitTone: "g", pnl: "+$60", pnlN: 60, slip: "+0.01", giveBack: "8%" },
      { time: "Jun30 12:12", ticker: "NFLX", sleeve: "Debit", dir: "LONG", struct: "Call spread ×2", px: "2.20→1.66", exit: "Stop", exitTone: "r", pnl: "−$108", pnlN: -108, slip: "−0.02", giveBack: "—" },
    ],
    testTrades: [
      { time: "Jul3 09:15", ticker: "SPY", sleeve: "exec-test", dir: "LONG", struct: "Call spread ×1", px: "0.50→0.51", exit: "Time-flat", exitTone: "a", pnl: "+$1", pnlN: 1, slip: "+0.00", giveBack: "—", test: true },
      { time: "Jul2 09:16", ticker: "QQQ", sleeve: "exec-test", dir: "LONG", struct: "Put spread ×1", px: "0.40→0.42", exit: "Target", exitTone: "g", pnl: "+$2", pnlN: 2, slip: "+0.00", giveBack: "—", test: true },
    ],
  },

  /* ---------- Page 3 — System Health ---------- */
  health: {
    errors24h: 0,
    warnings24h: 3,
    feeds: [
      { name: "Tradier", role: "primary quotes", status: "live", latency: "42 ms", last: "2s ago" },
      { name: "yfinance", role: "fallback source", status: "standby", latency: "—", last: "engaged 11:58" },
      { name: "Option chains", role: "Tradier", status: "live", latency: "310 ms", last: "8s ago" },
      { name: "Put/Call proxy", role: "derived", status: "live", latency: "—", last: "this cycle" },
      { name: "TICK proxy", role: "yfinance", status: "live", latency: "120 ms", last: "9s ago" },
    ],
    host: { sleepPrevention: "ACTIVE", uptime: "4d 06h", cpu: "6%", mem: "420 MB" },
    watchdog: "stream",
    earnings: [
      { ticker: "JPM", date: "2026-07-14", state: "block" },
      { ticker: "NFLX", date: "2026-07-17", state: "watch" },
      { ticker: "AAPL", date: "2026-07-31", state: "clear" },
      { ticker: "MARA", date: "2026-08-05", state: "clear" },
      { ticker: "NVDA", date: "2026-08-27", state: "clear" },
      { ticker: "SPY", date: "—", state: "etf" },
      { ticker: "QQQ", date: "—", state: "etf" },
      { ticker: "TLT", date: "—", state: "etf" },
    ],
    jobs: [
      { name: "Nightly ops", detail: "snapshots + broker reconcile", last: "Jul 3 · 02:10", status: "ok" },
      { name: "Weekly review", detail: "check-in report generated", last: "Jun 29 · 06:00", status: "ok" },
      { name: "EOD snapshots", detail: "16 sessions archived", last: "Jul 3 · 13:05", status: "ok" },
      { name: "Calendar refresh", detail: "2 tickers missing earnings date", last: "Jul 4 · 05:30", status: "warn" },
    ],
  },

  /* ---------- Page 4 — Reports Reader ---------- */
  reports: [
    {
      id: "weekly",
      title: "Weekly Check-in",
      date: "Jun 29 2026",
      actionCount: 2,
      kicker: "WEEKLY CHECK-IN · GENERATED Jun 29 2026 06:00 PT",
      heading: "Week of Jun 23–27, 2026",
      subtitle: "Observation mode · paper only · no capital at risk",
      blocks: [
        { kind: "callout", tone: "warn", text: "⚠ ACTION NEEDED · 2" },
        {
          kind: "list",
          ordered: true,
          items: [
            [
              { text: "Per-sleeve edge failing.", bold: true },
              { text: " 0DTE and debit-spread sleeves clear, but the third sleeve is negative on n=3. Do not count toward acceptance until it stabilizes." },
            ],
            [
              { text: "Exit give-back trending up.", bold: true },
              { text: " Avg give-back from peak rose to 18% (worst $255). Review VWAP-invalidation latency before next tuning cycle." },
            ],
          ],
        },
        { kind: "h2", text: "Forward-acceptance progress" },
        {
          kind: "list",
          items: [
            [
              { text: "Qualifying trades: " },
              { text: "7 / 100", bold: true, mono: true, color: "bright" },
              { text: " — on pace but early; small-sample caveats apply throughout." },
            ],
            [
              { text: "Trading-day span: " },
              { text: "4 / 60", bold: true, mono: true, color: "bright" },
              { text: " distinct sessions." },
            ],
            [
              { text: "Adverse-regime week: " },
              { text: "not yet survived.", bold: true, mono: true, color: "amb" },
            ],
            [
              { text: "Gate status: " },
              { text: "LOCKED", bold: true, mono: true, color: "amb" },
              { text: " — five pass/fail chips: 3 pass, 1 pending (Sharpe, n too small), 1 fail (per-sleeve)." },
            ],
          ],
        },
        { kind: "h2", text: "Week summary" },
        {
          kind: "para",
          spans: [
            { text: "Funnel realized " },
            { text: "+$248", bold: true, mono: true, color: "grn" },
            { text: " across 6 round trips (4W / 2L, " },
            { text: "n=6", mono: true },
            { text: "). Win rate and profit factor (1.84) are encouraging but not yet meaningful at this sample size. Blended results merge the SCALP-candidate stream — score each stream against its own gate." },
          ],
        },
        {
          kind: "para",
          spans: [
            { text: "No safety breakers tripped. One earnings veto (JPM, 07/14) correctly blocked an in-window entry. Feed health nominal aside from a brief yfinance fallback on the TICK proxy." },
          ],
        },
      ],
    },
    {
      id: "nightly",
      title: "Nightly Readiness",
      date: "Jul 4 2026",
      actionCount: 0,
      kicker: "NIGHTLY READINESS · GENERATED Jul 4 2026 02:10 PT",
      heading: "Readiness — Fri Jul 3 session",
      subtitle: "Observation mode · paper only",
      blocks: [
        { kind: "callout", tone: "ok", text: "✓ NO ACTION NEEDED — SYSTEM READY FOR NEXT SESSION" },
        { kind: "h2", text: "Session recap" },
        {
          kind: "list",
          items: [
            [
              { text: "Funnel realized " },
              { text: "+$248", bold: true, mono: true, color: "grn" },
              { text: ", 6 round trips, all positions flattened at 15:45 PT." },
            ],
            [{ text: "148 cycles completed · 17 / 30 AI-validation calls used · 1 recommendation surfaced & confirmed." }],
            [{ text: "Breaker headroom preserved — no realized-loss limit approach." }],
          ],
        },
        { kind: "h2", text: "Pre-open checklist" },
        {
          kind: "list",
          items: [
            [
              { text: "Feeds: Tradier live, chains live, watchdog streaming. " },
              { text: "OK", bold: true, color: "grn" },
            ],
            [
              { text: "Nightly ops + EOD snapshots ran clean; calendar refresh flagged " },
              { text: "2 missing earnings dates", bold: true, color: "amb" },
              { text: " (low priority)." },
            ],
            [{ text: "Earnings window: JPM (07/14) veto-blocked, NFLX (07/17) entering window soon." }],
            [{ text: "Host sleep-prevention active; next session opens 06:30 PT." }],
          ],
        },
      ],
    },
  ],
};
