import { create } from "zustand";
import { mockFeed } from "./feed/mock";
import type { CockpitFeed } from "./feed/types";

export type PageId = "dash" | "trades" | "tradeview" | "perf" | "research" | "health" | "reports";
export type ThemeId = "night" | "paper" | "amber" | "ice" | "nebula";

/** Staleness threshold (SEV1 R7): the live feed pushes every ~5s, so >12s with
 * no message ≈ 2 missed pushes — treat the on-screen numbers as frozen and raise
 * the DISCONNECTED banner. Laptop sleep often kills the socket without onclose
 * firing promptly, so we watchdog on message age rather than trusting onclose. */
export const STALE_AFTER_MS = 12_000;

interface CockpitStore {
  feed: CockpitFeed;
  page: PageId;
  theme: ThemeId;
  killEngaged: boolean;
  /** true when the last /api/kill POST failed/timed out: the command did NOT
   * land, killEngaged is unchanged (real last-known), and the UI must say so. */
  killError: boolean;
  /** true when no feed message has arrived for > STALE_AFTER_MS on a LIVE feed
   * — the numbers on screen are frozen; drives the hard DISCONNECTED banner.
   * Never set on a simulated feed (the SIMULATED banner already covers that). */
  stale: boolean;
  /** epoch ms of the last applied feed snapshot (WS message or /api/feed). */
  lastMessageAt: number;
  /** funnel-only (false, DEFAULT) vs blended view — merges BOTH evidence
   * streams (funnel + SCALP candidate); each stream keeps its own gate */
  blended: boolean;
  /** selected report id on the Reports Reader page */
  report: string;
  cycleSecs: number;
  confirmSecs: number;
  decisions: Record<string, "confirmed" | "declined">;
  decide: (id: string, d: "confirmed" | "declined") => Promise<void>;
  setPage: (p: PageId) => void;
  setTheme: (t: ThemeId) => void;
  toggleKill: () => Promise<void>;
  setBlended: (b: boolean) => void;
  setReport: (id: string) => void;
  tick: () => void;
  /** record that an authoritative feed snapshot just arrived: refresh the
   * watchdog clock and clear the stale flag. */
  noteMessage: () => void;
  /** watchdog evaluation: raise `stale` iff LIVE and last message older than
   * STALE_AFTER_MS; never stale on a simulated feed. Called on an interval
   * (see connectLive) and directly by tests. */
  checkStale: () => void;
  /** connect to the live API feed (WS with reconnect); mock stays as
   * fallback with the SIMULATED banner while unreachable */
  connectLive: () => void;
}

export const useCockpit = create<CockpitStore>((set, get) => ({
  feed: mockFeed,
  // deep link (2026-09-14): /#tradeview etc. opens that page directly
  page: (["dash", "trades", "tradeview", "perf", "research", "health", "reports"] as PageId[])
    .find((p) => typeof location !== "undefined" && location.hash === `#${p}`) ?? "dash",
  theme: (localStorage.getItem("cockpit-theme") as ThemeId) || "night",
  killEngaged: false,
  killError: false,
  stale: false,
  lastMessageAt: Date.now(),
  blended: false,
  report: "weekly",
  cycleSecs: mockFeed.cycleSeconds,
  confirmSecs: mockFeed.confirmSeconds,
  decisions: {},
  decide: (id, d) => {
    set((s) => ({ decisions: { ...s.decisions, [id]: d } }));
    if (get().feed.simulated) return Promise.resolve();
    // returns the POST promise so ActionButton can disable while in-flight;
    // the server transition is idempotent, so a lost race is a no-op anyway
    return fetch(`/api/${d === "confirmed" ? "confirm" : "decline"}/${encodeURIComponent(id)}`, {
      method: "POST",
    }).then(() => {}, () => {});
  },
  setPage: (page) => set({ page }),
  setTheme: (theme) => {
    localStorage.setItem("cockpit-theme", theme);
    document.documentElement.dataset.theme = theme;
    set({ theme });
  },
  toggleKill: async () => {
    const s = get();
    const next = !s.killEngaged;
    if (s.feed.simulated) {
      // Inert on the mock feed: there is no real engine, so a local flip is
      // harmless and the SIMULATED banner already tells the operator the book
      // is not real. Only the LIVE path can lie about the engine's state.
      set({ killEngaged: next, killError: false });
      return;
    }
    // SEV1 R7 fix: do NOT optimistically flip killEngaged. The button must
    // reflect REALITY — killEngaged changes ONLY from an accepted POST response
    // or the next authoritative WS/feed snapshot. If the POST fails or times
    // out the command never landed, so leave killEngaged at its real last-known
    // value and surface the failure (killError) instead of a false ENGAGED.
    set({ killError: false });
    try {
      const r = await fetch("/api/kill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on: next, reason: "cockpit UI" }),
      });
      if (!r.ok) throw new Error(`kill POST ${r.status}`);
      // Prefer the server's echoed truth; fall back to the accepted intent on a
      // 2xx with no body. The next WS snapshot remains authoritative either way.
      const body = await r.json().catch(() => null);
      const engaged =
        body && typeof body.killEngaged === "boolean" ? body.killEngaged : next;
      set({ killEngaged: engaged, killError: false });
    } catch {
      // Command did not land: killEngaged stays as-is (real last-known state).
      set({ killError: true });
    }
  },
  setBlended: (blended) => set({ blended }),
  setReport: (report) => set({ report }),
  tick: () =>
    set((s) => ({
      cycleSecs: s.cycleSecs <= 0 ? 300 : s.cycleSecs - 1,
      confirmSecs: Math.max(0, s.confirmSecs - 1),
    })),
  noteMessage: () => set({ lastMessageAt: Date.now(), stale: false }),
  checkStale: () => {
    const s = get();
    // A simulated feed never goes stale (SIMULATED banner owns that state);
    // clear any lingering flag if we somehow fell back to mock.
    const stale = !s.feed.simulated && Date.now() - s.lastMessageAt > STALE_AFTER_MS;
    if (stale !== s.stale) set({ stale });
  },
  connectLive: () => {
    if (location.search.includes("sim=1")) return; // ?sim=1: stay on mock samples (banner on)
    const apply = (f: CockpitFeed) =>
      set({
        feed: f,
        cycleSecs: f.cycleSeconds,
        confirmSecs: f.confirmSeconds,
        killEngaged: !!f.killEngaged, // authoritative snapshot corrects any drift
        killError: false,
        // watchdog: a fresh snapshot resets the staleness clock
        lastMessageAt: Date.now(),
        stale: false,
      });
    const open = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
      ws.onmessage = (ev) => apply(JSON.parse(ev.data) as CockpitFeed);
      ws.onclose = () => setTimeout(open, 5000); // reconnect; mock+banner meanwhile
      ws.onerror = () => ws.close();
    };
    fetch("/api/feed")
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((f: CockpitFeed) => apply(f))
      .catch(() => {}); // API down: stay on mock with SIMULATED banner
    open();
    // Staleness watchdog: onclose can lag (laptop sleep drops the socket
    // silently), so poll message age independently. Single timer only — the
    // connectLive effect double-fires under StrictMode.
    if (staleTimer === undefined) {
      staleTimer = window.setInterval(() => get().checkStale(), 3_000);
    }
  },
}));

/** module-scoped so StrictMode's double mount can't stack two watchdogs */
let staleTimer: number | undefined;

export const mmss = (t: number) =>
  `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
