"""CONTANGO RE-TEST on SPX 2007-2026 — the PRE-COMMITTED follow-up to study B5.

Ref: logs/research/contango_pro_20260705.md (the SPY/QQQ/IWM proxy run — FAIL at
all three fills) pre-committed exactly ONE follow-up: the SAME single
configuration on SPX 2007-2026 once the extraction landed. This is that run.
No iteration whatever the result; if it fails, the vol-regime arc is closed
permanently on 20 years of evidence.

Configuration: IDENTICAL to src/research/contango_pro.py (constants imported
from it, read-only — zero changes):

  Underlying  : SPX (weeklys folded under the SPX ticker in the store)
  Signal      : enter only when iv60 > iv30 (contango). Built from the SPX
                surface itself: per-session ATM IV per expiration (strike
                nearest spot, mean of positive call/put IV — the same ATM
                convention as src/research/ingest_vrp_inputs._term_iv),
                interpolated to 30d/60d linearly in TOTAL VARIANCE between
                the bracketing expirations (nearest expiry when the target
                is not bracketed, matching the _term_iv nearest precedent).
                Missing signal -> no trade (fail closed).
  Structure   : iron condor 45 DTE, 0.16-delta shorts, wings at 5% of spot
  Management  : exit at 50% max profit OR 21 DTE; NO stop; one position at a
                time (sequential, non-overlapping)
  Fills       : (a) mid, (b) mid+/-25% of half-spread [PRIMARY], (c) bid/ask
                $0.65/contract/leg commissions in all
  Window      : entries 2007-01-01 .. 2026-06-30
  Gate        : REVISED Stage 6 via src.research.monte_carlo.acceptance_gate,
                evaluated at (b) as primary.

SPX is a genuine section 1256 contract (cash-settled index option) — the
60/40 tax line here needs no proxy caveat.

  nice .venv/bin/python -m src.research.contango_spx
"""
from __future__ import annotations

import math
from dataclasses import asdict
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.backtest import metrics
from src.backtest.chain import ChainStore, _COLS, _parse_dates
from src.backtest.lifecycle import run_trade
from src.backtest.structures import iron_condor
# the EXACT pre-registered configuration + gate machinery, imported read-only
from src.research.contango_pro import (BLEND_1256, DTE, MIN_DTE, PROFIT_TARGET,
                                       SCEN_LABEL, SCENARIOS, SHORT_DELTA,
                                       ST_RATE, STOP_MULT, WING_PCT, gate_eval,
                                       per_year)

ROOT = Path(__file__).resolve().parents[2]
IV_FULL = ROOT / "data" / "iv_history_full"
OUT_CSV = ROOT / "logs" / "research" / "contango_spx_20260705.csv"

TICKER = "SPX"
ENTRY_START = pd.Timestamp("2007-01-01")
ENTRY_END = pd.Timestamp("2026-06-30")

# signal construction (surface -> iv30/iv60)
SIG_MIN_DTE = 7          # ignore expiry-week junk when building the term structure
SIG_MAX_DTE = 200        # far enough to bracket 60d even in monthly-only years
SIG_NEAR_BAND = 90       # nearest-expiry fallback only if within this of target
DTE_LOAD_MAX = 210       # store rows beyond this are irrelevant to signal + trading


def _load_spx() -> pd.DataFrame:
    """Concatenate the per-year SPX parquets with chain.py's exact parsing,
    trimmed to DTE <= DTE_LOAD_MAX (memory guard; a 45-DTE condor and a 60d
    signal never look further out)."""
    paths = sorted(IV_FULL.glob(f"{TICKER}_*.parquet"))
    if not paths:
        raise FileNotFoundError(f"no per-strike store for {TICKER} in {IV_FULL}")
    parts = []
    for p in paths:
        df = pd.read_parquet(p, columns=_COLS)
        df["trade_date"] = _parse_dates(df["trade_date"])
        df["expiration"] = _parse_dates(df["expiration"])
        df["dte"] = (df["expiration"] - df["trade_date"]).dt.days
        parts.append(df[(df["dte"] >= 0) & (df["dte"] <= DTE_LOAD_MAX)])
        print(f"  loaded {p.name}: {len(parts[-1]):,} rows (dte<={DTE_LOAD_MAX})", flush=True)
    df = pd.concat(parts, ignore_index=True).sort_values(
        ["trade_date", "expiration", "strike"])
    return df


class SPXStore(ChainStore):
    """ChainStore over a pre-parsed frame (adapts chain.py without touching it)."""

    def __init__(self, df: pd.DataFrame):          # noqa: bypass parent __init__
        self.ticker = TICKER
        self._df = df
        self._by_date = {d: g for d, g in df.groupby("trade_date")}
        self.sessions = sorted(self._by_date)


def _interp_iv(term: list[tuple[int, float]], target: int) -> float | None:
    """ATM IV at `target` DTE: linear in total variance between the bracketing
    expirations; nearest expiry (within SIG_NEAR_BAND) when not bracketed."""
    lo = [(d, v) for d, v in term if d <= target]
    hi = [(d, v) for d, v in term if d >= target]
    if lo and hi:
        d0, v0 = lo[-1]
        d1, v1 = hi[0]
        if d0 == d1:
            return v0
        w = (target - d0) / (d1 - d0)
        tot = (1 - w) * v0 * v0 * d0 + w * v1 * v1 * d1   # total variance interp
        return math.sqrt(tot / target)
    near = min(term, key=lambda t: abs(t[0] - target))
    return near[1] if abs(near[0] - target) <= SIG_NEAR_BAND else None


def contango_signal(df: pd.DataFrame) -> dict[str, float]:
    """{date_str: iv60 - iv30} from the SPX surface. Entry iff term > 0."""
    g = df[(df["dte"] >= SIG_MIN_DTE) & (df["dte"] <= SIG_MAX_DTE)][
        ["trade_date", "expiration", "dte", "strike", "spot", "call_iv", "put_iv"]].copy()
    g["dist"] = (g["strike"] - g["spot"]).abs()
    atm = g.loc[g.groupby(["trade_date", "expiration"])["dist"].idxmin()].copy()
    civ = atm["call_iv"].where(atm["call_iv"] > 0)
    piv = atm["put_iv"].where(atm["put_iv"] > 0)
    atm["atm_iv"] = pd.concat([civ, piv], axis=1).mean(axis=1)   # mean of positive IVs
    atm = atm.dropna(subset=["atm_iv"])
    out = {}
    for d, rows in atm.groupby("trade_date"):
        term = sorted(zip(rows["dte"].astype(int), rows["atm_iv"].astype(float)))
        iv30 = _interp_iv(term, 30)
        iv60 = _interp_iv(term, 60)
        if iv30 is not None and iv60 is not None:
            out[d.strftime("%Y-%m-%d")] = iv60 - iv30
    return out


def run_scenario(store: SPXStore, scenario: str, term: dict[str, float]) -> pd.DataFrame:
    """Sequential non-overlapping contango-gated condors — identical loop to
    contango_pro.run_scenario, single underlying."""
    fn = partial(iron_condor, dte=DTE, short_delta=SHORT_DELTA, wing_pct=WING_PCT)
    rows = []
    sessions = [d for d in store.sessions if ENTRY_START <= d <= ENTRY_END]
    i = 0
    while i < len(sessions):
        d = sessions[i]
        t = term.get(d.strftime("%Y-%m-%d"))
        if t is None or t <= 0:                     # signal: contango only
            i += 1
            continue
        snap = store.snapshot(d)
        s = fn(snap)
        if s is None or not s.tradable:
            i += 1
            continue
        tr = run_trade(store, s, scenario=scenario, frac=0.25,
                       profit_target=PROFIT_TARGET, stop_mult=STOP_MULT,
                       min_dte=MIN_DTE, regime_ok=None)   # static mgmt, no gate exit
        if tr is None:
            i += 1
            continue
        rows.append(asdict(tr))
        while i < len(sessions) and sessions[i] <= tr.exit_date:
            i += 1
    df = pd.DataFrame(rows)
    if not df.empty:
        df["entry_date"] = pd.to_datetime(df["entry_date"])
        df["exit_date"] = pd.to_datetime(df["exit_date"])
        df["r"] = df["pnl"] / df["capital"]
        df = df.sort_values("entry_date").reset_index(drop=True)
    return df


# ── 2007-2026 era map (vrp_screen._regime only covers 2020+) ──────────────
def _era(d: pd.Timestamp) -> str:
    y = d.year
    if y <= 2009: return "gfc_07_09"
    if y <= 2011: return "euro_10_11"
    if y <= 2014: return "qe_12_14"
    if y <= 2016: return "churn_15_16"
    if y <= 2017: return "melt_2017"
    if y == 2018: return "volmageddon_2018"
    if y == 2019: return "easy_2019"
    if y == 2020: return "covid_2020"
    if y == 2021: return "calm_2021"
    if y == 2022: return "bear_2022"
    if y <= 2024: return "recovery_23_24"
    return "recent_25_26"


_ERA_ORDER = ["gfc_07_09", "euro_10_11", "qe_12_14", "churn_15_16", "melt_2017",
              "volmageddon_2018", "easy_2019", "covid_2020", "calm_2021",
              "bear_2022", "recovery_23_24", "recent_25_26"]


def per_era(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["era"] = d["entry_date"].map(_era)
    g = d.groupby("era")
    out = pd.DataFrame({"trades": g.size(),
                        "win_rate": g["r"].apply(lambda x: (x > 0).mean()),
                        "avg_r": g["r"].mean(), "total_$": g["pnl"].sum()})
    return out.reindex([e for e in _ERA_ORDER if e in out.index]).round(4)


def margin_and_tax(df: pd.DataFrame) -> dict:
    """Same portfolio-margin approximation as contango_pro.margin_and_tax
    (margin ~= max loss of the defined-risk structure — APPROXIMATE: ignores
    PM house minimums and cross-position offsets), over THIS study's window.
    SPX options are genuine section 1256 contracts — the 60/40 line applies
    for real, no proxy caveat."""
    dollar_days = float((df["capital"] * df["days_held"].clip(lower=1)).sum())
    avg_margin_deployed = dollar_days / max((ENTRY_END - ENTRY_START).days, 1)
    years = (ENTRY_END - ENTRY_START).days / 365.25
    total = float(df["pnl"].sum())
    rom_ann = total / years / avg_margin_deployed if avg_margin_deployed > 0 else float("nan")
    after_st = total * (1 - ST_RATE) if total > 0 else total
    after_1256 = total * (1 - BLEND_1256) if total > 0 else total
    return {"avg_margin_deployed": avg_margin_deployed, "years": years,
            "total_pretax": total, "rom_annualized": rom_ann,
            "after_tax_st": after_st, "after_tax_1256": after_1256,
            "tax_pickup": after_1256 - after_st}


def contango_open_fractions(term: dict[str, float]) -> pd.DataFrame:
    counts: dict[str, list[int]] = {}
    for ds, t in term.items():
        if ds < "2007-01-01" or ds > "2026-06-30":
            continue
        y = ds[:4]
        counts.setdefault(y, [0, 0])
        counts[y][1] += 1
        if t > 0:
            counts[y][0] += 1
    return pd.DataFrame({"sessions": {y: n for y, (o, n) in counts.items()},
                         "contango_frac": {y: round(o / n, 3) for y, (o, n) in counts.items()}
                         }).sort_index()


def block(df: pd.DataFrame, sc: str) -> None:
    m = metrics(df)
    print(f"\n{'-'*72}\nFILL SCENARIO {SCEN_LABEL[sc]}\n{'-'*72}")
    print(f"  trades {m['n']}  win {m['win_rate']:.1%}  avg_r {m['avg_r']:+.4f}  "
          f"total ${m['total_$']:+,.0f}  ({m['trades_per_year']:.1f}/yr)")
    print(f"  Sharpe {m['sharpe']:.2f}  OOS {m['oos_sharpe']:.2f}  "
          f"maxDD {m['maxdd']:.1%}  MC5 {m['mc5_sharpe']:.2f}")
    print(f"  avg days held {df['days_held'].mean():.1f}  "
          f"avg credit {df['credit'].mean():.2f}  avg capital ${df['capital'].mean():,.0f}")
    print(f"  exit reasons: {df['exit_reason'].value_counts().to_dict()}")
    print(f"\n  PER-YEAR ({sc}):")
    print("  " + per_year(df).to_string().replace("\n", "\n  "))
    mt = margin_and_tax(df)
    print(f"\n  PM-approx margin (~= max loss, APPROXIMATE): avg deployed "
          f"${mt['avg_margin_deployed']:,.0f} over {mt['years']:.1f}y  "
          f"-> annualized return-on-margin {mt['rom_annualized']:+.1%}")
    print(f"  Sec-1256 after-tax (SPX is GENUINELY 1256; ST {ST_RATE:.0%} vs 60/40 "
          f"blend {BLEND_1256:.1%}): pre-tax ${mt['total_pretax']:+,.0f} -> "
          f"ST ${mt['after_tax_st']:+,.0f} | 1256 ${mt['after_tax_1256']:+,.0f}  "
          f"(pickup ${mt['tax_pickup']:+,.0f})")
    passed, checks = gate_eval(df, f"contango_spx_{sc}")
    print(f"\n  REVISED STAGE-6 (acceptance_gate): {'PASS' if passed else 'FAIL'}")
    for name, ok, actual in checks:
        print(f"    [{'OK' if ok else ' X'}] {name:38} actual {actual}")


def main():
    print("=" * 72)
    print("CONTANGO RE-TEST — SPX 2007-2026 (pre-committed follow-up to B5)  2026-07-05")
    print("SAME single configuration as contango_pro (constants imported):")
    print(f"iron condor {DTE}DTE {SHORT_DELTA:.2f}d shorts, wings {WING_PCT:.0%} of spot;"
          f" exit {PROFIT_TARGET:.0%} profit OR {MIN_DTE} DTE; NO stop; 1 position at a time")
    print(f"entries {ENTRY_START.date()} .. {ENTRY_END.date()};"
          " signal: iv60 > iv30 (contango), built from the SPX surface")
    print("=" * 72)

    print("\nloading SPX store...", flush=True)
    df = _load_spx()
    store = SPXStore(df)
    print(f"SPX store: {len(store.sessions)} sessions "
          f"{store.sessions[0].date()}..{store.sessions[-1].date()}, {len(df):,} rows")

    term = contango_signal(df)
    print(f"\nsignal coverage: {len(term)} sessions with iv30+iv60")
    print("CONTANGO-OPEN fraction of sessions per year (signal availability):")
    print(contango_open_fractions(term).to_string())

    dfs = {}
    for sc in SCENARIOS:
        tdf = run_scenario(store, sc, term)
        dfs[sc] = tdf
        if tdf.empty:
            print(f"\n[{sc}] NO TRADES")
            continue
        block(tdf, sc)

    b = dfs.get("midminus")
    if b is not None and not b.empty:
        print(f"\n{'='*72}\nPER-ERA P&L at PRIMARY scenario (b) — GFC/2011/2018/2020/2022 all in sample:")
        print(per_era(b).to_string())
        b.to_csv(OUT_CSV, index=False)
        print(f"\ntrade list (b) -> {OUT_CSV.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
