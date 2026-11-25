import type { PositionDetailData } from "./types";

/** Simulated-mode sample detail (the 07-02 TLT drill trade: 88/86 put debit
 * spread ×10 @ 0.16). Payoff points are the exact piecewise-linear kinks. */
export const SAMPLE_DETAIL: PositionDetailData = {
  recId: "sample", ticker: "TLT", structure: "Put debit spread",
  generatedAt: "sample",
  payoff: {
    points: [
      { s: 79.2, pnl: 1840 }, { s: 86, pnl: 1840 },
      { s: 88, pnl: -160 }, { s: 96.8, pnl: -160 },
    ],
    breakevens: [87.84], maxProfit: 1840, maxLoss: -160,
    spot: 87.5, entryDebit: 0.16, quantity: 10,
  },
  price: {
    session: "sample session",
    bars: [
      { t: "06:30", c: 87.9, vwap: 87.9 }, { t: "07:00", c: 87.72, vwap: 87.82 },
      { t: "07:30", c: 87.61, vwap: 87.76 }, { t: "08:00", c: 87.48, vwap: 87.7 },
      { t: "08:30", c: 87.55, vwap: 87.66 }, { t: "09:00", c: 87.41, vwap: 87.62 },
      { t: "09:30", c: 87.3, vwap: 87.58 }, { t: "10:00", c: 87.38, vwap: 87.55 },
      { t: "10:30", c: 87.26, vwap: 87.52 }, { t: "11:00", c: 87.34, vwap: 87.5 },
      { t: "11:30", c: 87.45, vwap: 87.49 }, { t: "12:00", c: 87.52, vwap: 87.49 },
    ],
    entryT: "08:00",
  },
  gex: {
    expiration: "2026-07-11", spot: 87.5, zeroGamma: 88.5, netGex: -41_000_000,
    strikes: [
      { k: 84, gex: -9_800_000 }, { k: 85, gex: -14_200_000 },
      { k: 86, gex: -22_500_000 }, { k: 87, gex: -18_100_000 },
      { k: 88, gex: -6_400_000 }, { k: 89, gex: 8_900_000 },
      { k: 90, gex: 14_600_000 }, { k: 91, gex: 6_500_000 },
    ],
    label: "estimated (chain-derived, OI-weighted) · SAMPLE",
  },
};
