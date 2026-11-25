/**
 * Research reorganization (2026-09-13). The dashboard used to stack the whole
 * RVOL-RANK evidence report, the radar's 10-column table and a 155-event alert
 * feed above positions/recs; the operator could not read it. The depth moved to
 * an RSCH page and the dashboard kept two summary lines.
 *
 * These tests pin the parts that silently rot: that the summary is a LINK (not
 * a dead-end), that no judgement word is hardcoded in the strip, and that the
 * radar's MTF grades are the ones the script actually emits.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { StrictMode } from "react";
import { ResearchStrip } from "../ResearchStrip";
import { mtfColor } from "../WatchlistPanel";
import { useCockpit } from "../../store";

const SLEEVES = {
  rvolrank: {
    stamp_interim: "interim, NOT a gate judgment",
    gate_primary: {
      gate_binding_tier: "stress_2x",
      binding: { selection_mean_R: -0.3403, selection_t: -1.9105 },
      bars: { t_min_deflated: 2.94 },
      progress: {
        sessions_valid: 37, sessions_goal: 60,
        exec_trades: 192, exec_trades_goal: 400,
        projected_date_400: "2026-11-03",
      },
    },
  },
};
const RADAR = {
  rows: [{ in_zone: true }, { in_zone: true }, { in_zone: false }],
  data_asof: "2026-09-11", stale: false, outcomes_recorded: 0,
  backtest: { verdict: "FAIL", bar_t: 3.37, gate_t: -0.47 },
};

function stubFetch(sleeves: unknown = SLEEVES, radar: unknown = RADAR) {
  vi.stubGlobal("fetch", vi.fn((url: string) =>
    Promise.resolve({
      json: () => Promise.resolve(String(url).includes("sleeves") ? sleeves : radar),
    } as Response)));
}

afterEach(() => {
  vi.unstubAllGlobals();
  useCockpit.setState({ page: "dash" });
});

async function renderStrip() {
  await act(async () => { render(<StrictMode><ResearchStrip /></StrictMode>); });
}

describe("ResearchStrip", () => {
  it("summarises both streams in one line each", async () => {
    stubFetch();
    await renderStrip();
    expect(screen.getByText(/37\/60 sessions/)).toBeTruthy();
    expect(screen.getByText(/192\/400 trades/)).toBeTruthy();
    expect(screen.getByText(/t -1\.91 vs 2\.94/)).toBeTruthy();   // bar from the API, not hardcoded
    expect(screen.getByText(/3 names · 2 in zone · eval 2026-09-11/)).toBeTruthy();
  });

  it("renders the interim stamp and the backtest verdict from the payload", async () => {
    stubFetch();
    await renderStrip();
    expect(screen.getByText("interim, NOT a gate judgment")).toBeTruthy();
    expect(screen.getByText(/backtest FAIL/)).toBeTruthy();
  });

  it("states no verdict of its own when the API carries none", async () => {
    stubFetch(SLEEVES, { ...RADAR, backtest: null });
    await renderStrip();
    expect(screen.queryByText(/backtest/)).toBeNull();
  });

  it("navigates to the research page when a row is clicked", async () => {
    stubFetch();
    await renderStrip();
    expect(useCockpit.getState().page).toBe("dash");
    fireEvent.click(screen.getByText(/37\/60 sessions/));
    expect(useCockpit.getState().page).toBe("research");
  });

  it("says the payload is missing rather than rendering blanks", async () => {
    stubFetch({}, { rows: [] });
    await renderStrip();
    expect(screen.getByText(/no payload/)).toBeTruthy();
  });
});

describe("radar MTF grades", () => {
  it("colors the grades watch_buyzone.py actually emits", () => {
    // The panel used to test for "ALIGNED"/"CONFLICT", which the radar has
    // never emitted, so the column was permanently grey. These four are the
    // full set (watch_buyzone.py :: mtf grading).
    const grey = mtfColor(undefined);
    expect(mtfColor("FULL-ALIGN")).not.toBe(grey);
    expect(mtfColor("PARTIAL")).not.toBe(grey);
    expect(mtfColor("COUNTER-M")).not.toBe(grey);
    expect(mtfColor("FULL-ALIGN")).not.toBe(mtfColor("COUNTER-M"));
    expect(mtfColor("—")).toBe(grey);
    expect(mtfColor("ALIGNED")).toBe(grey);            // never emitted
  });
});
