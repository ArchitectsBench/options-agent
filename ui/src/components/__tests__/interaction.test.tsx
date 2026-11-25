/**
 * Interaction regression suite (2026-07-31) — born from two real cockpit bugs
 * the user hit that wiring-level review missed:
 *
 *  1. RAPID-TOGGLE SWALLOW: ActionButton dropped any click with e.detail > 1
 *     (multi-click guard). Rapid FUNNEL/BLENDED alternation registers as
 *     double/triple-click in the browser -> clicks silently swallowed ->
 *     "button stops responding". Guard must apply ONLY to mutation buttons
 *     (allowRapid opt-out for view toggles).
 *
 *  2. TWO-STEP CONFIRM COOLDOWN RACE: the kill RELEASE arming click started a
 *     750ms cooldown that disabled the button during exactly the window the
 *     "CONFIRM?" label invites the second click -> confirm swallowed.
 *
 * These tests simulate REAL interaction sequences (rapid clicks with the
 * browser's detail counter, immediate confirm taps), not just handler wiring.
 * Add a scenario here whenever a control gains guard/cooldown/async behavior.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render as rtlRender, screen, fireEvent, act } from "@testing-library/react";
import { StrictMode, useState } from "react";
import { ActionButton } from "../ActionButton";
import { useArmed } from "../useArmed";
import { useCockpit, STALE_AFTER_MS } from "../../store";
import { SimulatedBanner } from "../Banners";

/**
 * ALL renders go through StrictMode (added 2026-07-31): the cockpit runs under
 * StrictMode in dev, and the worst button bug so far — every ActionButton
 * permanently disabling itself after ONE click — only manifested under
 * StrictMode's mount→unmount→remount (a cleanup-only mounted-ref effect left
 * the flag false). Tests that skip StrictMode certify the prod build while the
 * dev cockpit is broken. Never render bare here.
 */
const render = (ui: React.ReactElement) => rtlRender(<StrictMode>{ui}</StrictMode>);

/** Fire a click with a given browser multi-click count (e.detail). */
function clickWithDetail(el: HTMLElement, detail: number) {
  fireEvent.mouseDown(el, { detail });
  fireEvent.click(el, { detail });
}

// ── 1. rapid-toggle: the FUNNEL ONLY / BLENDED shape ────────────────────────
function TwoWayToggle() {
  const [blended, setBlended] = useState(false);
  return (
    <div>
      <ActionButton allowRapid onAct={() => setBlended(false)} aria-pressed={!blended}>
        FUNNEL ONLY
      </ActionButton>
      <ActionButton allowRapid onAct={() => setBlended(true)} aria-pressed={blended}>
        BLENDED
      </ActionButton>
      <output>{blended ? "blended" : "funnel"}</output>
    </div>
  );
}

describe("rapid view-toggle clicks (allowRapid)", () => {
  it("alternating fast clicks ALL take effect — none swallowed as multi-click", () => {
    render(<TwoWayToggle />);
    const funnel = screen.getByRole("button", { name: "FUNNEL ONLY" });
    const blend = screen.getByRole("button", { name: "BLENDED" });

    // Browser counts rapid successive clicks: detail 1, 2, 3...
    clickWithDetail(blend, 1);
    expect(screen.getByText("blended")).toBeTruthy();
    clickWithDetail(funnel, 2);        // rapid 2nd click -> detail=2 (the old bug dropped this)
    expect(screen.getByText("funnel")).toBeTruthy();
    clickWithDetail(blend, 3);         // rapid 3rd click -> detail=3
    expect(screen.getByText("blended")).toBeTruthy();
    clickWithDetail(funnel, 2);
    expect(screen.getByText("funnel")).toBeTruthy();
  });

  it("same-button rapid re-click still fires with allowRapid", () => {
    let count = 0;
    render(<ActionButton allowRapid onAct={() => { count += 1; }}>T</ActionButton>);
    const b = screen.getByRole("button");
    clickWithDetail(b, 1);
    clickWithDetail(b, 2);
    clickWithDetail(b, 3);
    expect(count).toBe(3);
  });

  it("REGRESSION (StrictMode stuck-busy): every click in a long unhurried sequence fires — no button ever wedges disabled", () => {
    // The 2026-07-31 cockpit bug: under StrictMode a cleanup-only mounted-ref
    // effect left the flag false, release() skipped setBusy(false), and each
    // button DISABLED ITSELF after its first click ("stops responding").
    // Unhurried clicks (detail=1) so no multi-click guard is involved: this
    // must pass for plain guarded buttons too, not just allowRapid ones.
    let count = 0;
    render(<ActionButton onAct={() => { count += 1; }}>SEQ</ActionButton>);
    const b = screen.getByRole("button") as HTMLButtonElement;
    for (let i = 1; i <= 5; i++) {
      clickWithDetail(b, 1);
      expect(b.disabled).toBe(false); // must re-enable after every sync action
    }
    expect(count).toBe(5);
  });
});

// ── 2. mutation buttons keep the multi-click guard ──────────────────────────
describe("mutation-button multi-click guard (default)", () => {
  it("double-click fires the action exactly once (reconcile/confirm class)", () => {
    let fires = 0;
    render(<ActionButton onAct={() => { fires += 1; }}>RECONCILE</ActionButton>);
    const b = screen.getByRole("button");
    clickWithDetail(b, 1);
    clickWithDetail(b, 2);             // the double-click's 2nd click must be dropped
    expect(fires).toBe(1);
  });

  it("async action disables the button while in flight", async () => {
    let resolve!: () => void;
    const p = new Promise<void>((r) => { resolve = r; });
    let fires = 0;
    render(<ActionButton onAct={() => { fires += 1; return p; }}>POST</ActionButton>);
    const b = screen.getByRole("button") as HTMLButtonElement;
    clickWithDetail(b, 1);
    expect(b.disabled).toBe(true);     // disabled while promise pending
    clickWithDetail(b, 1);             // click during flight must not fire
    expect(fires).toBe(1);
    await act(async () => { resolve(); await p; });
    expect(b.disabled).toBe(false);    // re-enabled after settle
  });
});

// ── 3. two-step confirm: arm then IMMEDIATE confirm must work ───────────────
function KillRelease({ onRelease }: { onRelease: () => void }) {
  const { armed, arm, disarm } = useArmed(3000);
  const onKill = () => {
    if (!armed) { arm(); return; }
    disarm();
    onRelease();
  };
  // No cooldownMs — the regression under test: a cooldown here swallows the
  // immediate confirm click (the exact click the CONFIRM? label invites).
  return <ActionButton onAct={onKill}>{armed ? "CONFIRM?" : "RELEASE"}</ActionButton>;
}

describe("two-step confirm (kill release / disengage class)", () => {
  it("arming click then IMMEDIATE confirm click releases — no cooldown swallow", () => {
    const released = vi.fn();
    render(<KillRelease onRelease={released} />);
    const b = screen.getByRole("button");
    clickWithDetail(b, 1);                       // arm
    expect(screen.getByText("CONFIRM?")).toBeTruthy();
    clickWithDetail(b, 1);                       // immediate confirm (0ms later)
    expect(released).toHaveBeenCalledTimes(1);   // the old 750ms cooldown dropped this
  });

  it("auto-disarms after the timeout — a stray arm can't linger", () => {
    vi.useFakeTimers();
    const released = vi.fn();
    render(<KillRelease onRelease={released} />);
    const b = screen.getByRole("button");
    clickWithDetail(b, 1);                       // arm
    act(() => { vi.advanceTimersByTime(3100); }); // past the 3s window
    clickWithDetail(b, 1);                       // this should ARM again, not release
    expect(released).not.toHaveBeenCalled();
    vi.useRealTimers();
  });
});

// ── 4. SEV1 R7: kill-switch truth (no optimistic ENGAGED) ───────────────────
// The cockpit must never show the kill switch ENGAGED while the engine keeps
// trading. Pre-fix, toggleKill optimistically set killEngaged BEFORE the POST
// and swallowed failures, so a dead POST left the UI falsely ENGAGED.
const liveFeed = () => ({ ...useCockpit.getState().feed, simulated: false });

describe("SEV1 R7 — kill-switch truth (toggleKill sources state from reality)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    useCockpit.setState({ killEngaged: false, killError: false, stale: false });
  });

  it("a REJECTED /api/kill POST does NOT leave killEngaged=true — it surfaces the failure", async () => {
    useCockpit.setState({ feed: liveFeed(), killEngaged: false, killError: false });
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("socket dead"))));
    await useCockpit.getState().toggleKill();
    expect(useCockpit.getState().killEngaged).toBe(false); // never optimistically engaged
    expect(useCockpit.getState().killError).toBe(true);    // failure is visible
  });

  it("a non-OK (500) /api/kill response also does NOT engage — killError is raised", async () => {
    useCockpit.setState({ feed: liveFeed(), killEngaged: false, killError: false });
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) })));
    await useCockpit.getState().toggleKill();
    expect(useCockpit.getState().killEngaged).toBe(false);
    expect(useCockpit.getState().killError).toBe(true);
  });

  it("killEngaged reflects the SETTLED POST result, not an optimistic in-flight guess", async () => {
    useCockpit.setState({ feed: liveFeed(), killEngaged: false, killError: false });
    let settle!: (r: unknown) => void;
    const pending = new Promise((res) => { settle = res; });
    vi.stubGlobal("fetch", vi.fn(() => pending));
    const done = useCockpit.getState().toggleKill();
    // in-flight: the OLD code flipped killEngaged here — the new code must not
    expect(useCockpit.getState().killEngaged).toBe(false);
    settle({ ok: true, json: () => Promise.resolve({ killEngaged: true }) });
    await done;
    expect(useCockpit.getState().killEngaged).toBe(true); // sourced from the response body
    expect(useCockpit.getState().killError).toBe(false);
  });

  it("the server's echoed truth WINS over the requested intent (POST accepted but engine says off)", async () => {
    useCockpit.setState({ feed: liveFeed(), killEngaged: false, killError: false });
    // operator asked to ENGAGE, but the server reports it stayed off
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ killEngaged: false }) })));
    await useCockpit.getState().toggleKill();
    expect(useCockpit.getState().killEngaged).toBe(false);
  });
});

// ── 5. SEV1 R7: staleness watchdog + DISCONNECTED banner ────────────────────
describe("SEV1 R7 — staleness watchdog (frozen numbers get a hard banner)", () => {
  afterEach(() => {
    useCockpit.setState({ stale: false, killEngaged: false, killError: false });
  });

  it("raises `stale` after the threshold with no messages and clears it on a new message", () => {
    useCockpit.setState({ feed: liveFeed(), stale: false, lastMessageAt: Date.now() });
    useCockpit.getState().checkStale();
    expect(useCockpit.getState().stale).toBe(false); // fresh feed is not stale

    // no messages for longer than the threshold (≈2 missed 5s pushes)
    useCockpit.setState({ lastMessageAt: Date.now() - (STALE_AFTER_MS + 2_000) });
    useCockpit.getState().checkStale();
    expect(useCockpit.getState().stale).toBe(true);

    // next feed message resets the watchdog
    useCockpit.getState().noteMessage();
    expect(useCockpit.getState().stale).toBe(false);
  });

  it("a simulated feed never goes stale (the SIMULATED banner already owns that)", () => {
    useCockpit.setState({ feed: { ...useCockpit.getState().feed, simulated: true }, stale: false, lastMessageAt: Date.now() - 10 * STALE_AFTER_MS });
    useCockpit.getState().checkStale();
    expect(useCockpit.getState().stale).toBe(false);
  });

  it("a stale ENGAGED is never shown alone — the DISCONNECTED banner is visible with it (StrictMode)", () => {
    useCockpit.setState({ feed: liveFeed(), stale: true, killEngaged: true });
    render(<SimulatedBanner />);
    const alerts = screen.getAllByRole("alert");
    expect(alerts.length).toBeGreaterThan(0);
    expect(screen.getByText(/DISCONNECTED/)).toBeTruthy();
  });
});
