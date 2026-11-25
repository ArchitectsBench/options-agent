/**
 * TRADE page helpers (2026-09-14). The chart window must start 30 min before
 * the decision and end 40 min after the exit (or at the latest cycle while the
 * trade is open), clipped to the cycles that exist; ticks must land on round
 * values inside the domain.
 */
import { describe, expect, it } from "vitest";
import { niceTicks, toMin, windowOf } from "../../pages/TradeViewPage";

const cycles = (from: string, to: string) => {
  const out = [];
  for (let m = toMin(from); m <= toMin(to); m += 5) {
    out.push({ t: `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`,
      cycle: m, last: 1, atr: 1, vd: 0, vwap: 1 });
  }
  return out;
};

describe("trade view window", () => {
  it("frames a closed trade from decision-30 to exit+40", () => {
    const w = windowOf({ cycles: cycles("06:30", "09:00"), decided: "07:30", fillT: "07:35", exitT: "08:05" });
    expect(w).toEqual([toMin("07:00"), toMin("08:45")]);
  });

  it("frames an open trade up to the latest cycle", () => {
    const w = windowOf({ cycles: cycles("06:30", "08:20"), decided: "08:10", fillT: "08:15" });
    expect(w).toEqual([toMin("07:40"), toMin("08:20")]);
  });

  it("clips to the session's first cycle", () => {
    const w = windowOf({ cycles: cycles("06:30", "08:00"), decided: "06:40", fillT: "06:45", exitT: "07:00" });
    expect(w[0]).toBe(toMin("06:30"));
  });
});

describe("niceTicks", () => {
  it("returns round ticks inside the domain", () => {
    expect(niceTicks(497.3, 506.2, 6)).toEqual([498, 500, 502, 504, 506]);
    expect(niceTicks(-0.8, 4.5, 5)).toEqual([0, 2, 4]);
  });
});
