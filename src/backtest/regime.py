"""L2 backtester — regime gate (build phase 1e).

The VRP screen's verdict: defined-risk premium selling only works when you DON'T
sell into a vol expansion. This gate encodes that as the setup-validity test used
both to permit ENTRY and to force exit on setup invalidation (the lifecycle's
`regime_ok` hook). Built entirely from data/vrp_inputs.parquet (per-ticker
iv30/iv60/spot + VIX's spot = the VIX level).

Entry permitted only when ALL hold (params tunable per walk-forward fold):
  1. CONTANGO            iv60 - iv30 > contango_min   (term structure upward)
  2. PREMIUM PRESENT     iv_rank(iv30, 252d) > iv_rank_min
  3. VOL NOT SPIKING     VIX < vix_max  AND  VIX < vix_spike_ratio × VIX_10d_avg

Unknown (ticker, date) or <252d history -> NOT ok (can't confirm setup -> no trade).

  python src/backtest/regime.py --selftest
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
INP = ROOT / "data" / "vrp_inputs.parquet"


@dataclass
class GateParams:
    contango_min: float = 0.0
    iv_rank_min: float = 40.0
    vix_max: float = 30.0
    vix_spike_ratio: float = 1.20
    rank_win: int = 252
    # HOLD-exit thresholds — deliberately much looser than entry (asymmetric:
    # strict to enter, loose to stay) so daily gate flicker doesn't churn trades.
    vix_hard: float = 40.0          # absolute vol-shock -> bail
    vix_exit_ratio: float = 1.5     # VIX > 1.5× its 10d avg -> fast vol explosion


class RegimeGate:
    def __init__(self, params: GateParams | None = None, inputs_path: Path = INP):
        self.p = params or GateParams()
        df = pd.read_parquet(inputs_path)
        # global VIX level + 10d average
        vix = (df[df.ticker == "VIX"][["date", "spot"]]
               .rename(columns={"spot": "vix"}).sort_values("date"))
        vix["vix_ma"] = vix["vix"].rolling(10, min_periods=3).mean()
        self.vix = vix.set_index("date")
        # per-ticker iv_rank + term slope
        self.sig: dict[str, pd.DataFrame] = {}
        for tk, g in df.groupby("ticker"):
            g = g.sort_values("date").copy()
            g["iv_rank"] = g["iv30"].rolling(self.p.rank_win).apply(
                lambda w: (w.iloc[-1] > w).mean() * 100, raw=False)
            g["term"] = g["iv60"] - g["iv30"]
            self.sig[tk] = g.set_index("date")[["iv_rank", "term"]]

    def detail(self, ticker: str, date) -> dict | None:
        ds = pd.Timestamp(date).strftime("%Y-%m-%d")
        g = self.sig.get(ticker)
        if g is None or ds not in g.index or ds not in self.vix.index:
            return None
        row = g.loc[ds]
        v = self.vix.loc[ds]
        if pd.isna(row["iv_rank"]) or pd.isna(v["vix_ma"]):
            return None
        p = self.p
        checks = {
            "contango": bool(row["term"] > p.contango_min),
            "premium": bool(row["iv_rank"] > p.iv_rank_min),
            "vix_calm": bool(v["vix"] < p.vix_max),
            "no_spike": bool(v["vix"] < p.vix_spike_ratio * v["vix_ma"]),
        }
        return {"term": round(float(row["term"]), 4), "iv_rank": round(float(row["iv_rank"]), 1),
                "vix": round(float(v["vix"]), 1), "vix_ma": round(float(v["vix_ma"]), 1),
                "checks": checks, "ok": all(checks.values())}

    def ok(self, ticker: str, date) -> bool:
        d = self.detail(ticker, date)
        return bool(d and d["ok"])

    def hard_break(self, ticker: str, date) -> bool:
        """HOLD-exit trigger: only a genuine vol shock, NOT a daily gate flip.
        Missing VIX data -> False (let profit/stop/time manage the exit)."""
        ds = pd.Timestamp(date).strftime("%Y-%m-%d")
        if ds not in self.vix.index:
            return False
        v = self.vix.loc[ds]
        if pd.isna(v["vix_ma"]):
            return v["vix"] > self.p.vix_hard
        return bool(v["vix"] > self.p.vix_hard or v["vix"] > self.p.vix_exit_ratio * v["vix_ma"])


class TrendGate:
    """Bear-exclusion trend filter — the pre-committed FINAL premium-selling
    iteration (2026-07-01). Entry ok iff the underlying's PRIOR-day close is
    above its 200-day SMA of daily closes ending that PRIOR day (both series
    shifted one session -> only data available before the entry session is
    used; no lookahead). Spot series = ORATS spot from vrp_inputs.parquet
    (verified identical to the iv_history_full store spot; ISO-string dates,
    so string sort is correct). Unknown (ticker, date) or <200d history ->
    NOT ok. hard_break is always False: this is an ENTRY-only filter —
    management rules are unchanged, and a trend flip is not a vol shock."""

    def __init__(self, sma_win: int = 200, inputs_path: Path = INP):
        self.win = sma_win
        df = pd.read_parquet(inputs_path)
        self.sig: dict[str, pd.DataFrame] = {}
        for tk, g in df.groupby("ticker"):
            g = g.sort_values("date").copy()
            g["prev_close"] = g["spot"].shift(1)
            g["prev_sma"] = g["spot"].rolling(sma_win).mean().shift(1)
            self.sig[tk] = g.set_index("date")[["prev_close", "prev_sma"]]

    def detail(self, ticker: str, date) -> dict | None:
        ds = pd.Timestamp(date).strftime("%Y-%m-%d")
        g = self.sig.get(ticker)
        if g is None or ds not in g.index:
            return None
        row = g.loc[ds]
        if pd.isna(row["prev_close"]) or pd.isna(row["prev_sma"]):
            return None
        ok = bool(row["prev_close"] > row["prev_sma"])
        return {"prev_close": round(float(row["prev_close"]), 2),
                f"sma{self.win}": round(float(row["prev_sma"]), 2), "ok": ok}

    def ok(self, ticker: str, date) -> bool:
        d = self.detail(ticker, date)
        return bool(d and d["ok"])

    def hard_break(self, ticker: str, date) -> bool:
        return False


class AndGate:
    """Composite entry gate: ok iff EVERY sub-gate is ok (a missing signal in
    any sub-gate -> not ok, same fail-closed semantics as the parts).
    hard_break if ANY sub-gate hard-breaks — each part keeps its own
    asymmetric strict-entry / loose-hold behavior."""

    def __init__(self, *gates):
        self.gates = gates

    def detail(self, ticker: str, date) -> dict:
        ds = [g.detail(ticker, date) for g in self.gates]
        return {"gates": ds, "ok": all(bool(d and d["ok"]) for d in ds)}

    def ok(self, ticker: str, date) -> bool:
        return all(g.ok(ticker, date) for g in self.gates)

    def hard_break(self, ticker: str, date) -> bool:
        return any(g.hard_break(ticker, date) for g in self.gates)


class LongVolGate:
    """Entry gate for LONG-vol (buy premium): the inverse regime to short-vol. Opens on
    BACKWARDATION (iv60 - iv30 < backwardation_max), where realized vol tends to exceed
    implied and the term structure predicts ~2x bigger moves (premise test 2026-06-29).
    Same interface as RegimeGate so it drops into run_backtest. hard_break = False: let
    profit_target / time_stop manage long vol (no vol-shock bail — a shock HELPS long vol)."""

    def __init__(self, backwardation_max: float = 0.0, inputs_path: Path = INP):
        self.bmax = backwardation_max
        df = pd.read_parquet(inputs_path)
        self.sig: dict[str, pd.DataFrame] = {}
        for tk, g in df.groupby("ticker"):
            g = g.sort_values("date").copy()
            g["term"] = g["iv60"] - g["iv30"]
            self.sig[tk] = g.set_index("date")[["term"]]

    def detail(self, ticker: str, date) -> dict | None:
        ds = pd.Timestamp(date).strftime("%Y-%m-%d")
        g = self.sig.get(ticker)
        if g is None or ds not in g.index:
            return None
        t = g.loc[ds, "term"]
        if pd.isna(t):
            return None
        return {"term": round(float(t), 4), "backwardation": bool(t < self.bmax),
                "ok": bool(t < self.bmax)}

    def ok(self, ticker: str, date) -> bool:
        d = self.detail(ticker, date)
        return bool(d and d["ok"])

    def hard_break(self, ticker: str, date) -> bool:
        return False     # long vol: hold through shocks; manage via profit/time only


def selftest():
    gate = RegimeGate()
    # SAFETY-CRITICAL: the gate must block selling vol into stress.
    blocks = [
        ("SPY", "2022-10-12", "2022 bear — the losing-condor date (-42% in lifecycle)"),
        ("SPY", "2020-03-18", "COVID crash, VIX 79"),
        ("SPY", "2020-03-30", "COVID aftermath, backwardation"),
    ]
    for tk, d, note in blocks:
        det = gate.detail(tk, d)
        assert gate.ok(tk, d) is False, f"gate must BLOCK {tk} {d}: {det}"
        print(f"{tk} {d}  ok=False | {det['checks'] if det else 'no-signal'}  [BLOCK ✓ {note}]")

    # NON-VACUOUS: the gate must also OPEN on a meaningful share of sweet-spot days
    # (elevated-but-receding vol in contango), else it's uselessly strict.

    # coverage: how often is SPY 'sellable' per year (sanity, not a pass/fail)
    g = gate.sig["SPY"]
    yrs = {}
    for ds in g.index:
        det = gate.detail("SPY", ds)
        if det:
            yrs.setdefault(ds[:4], [0, 0]); yrs[ds[:4]][1] += 1
            if det["ok"]:
                yrs[ds[:4]][0] += 1
    print("\nSPY gate-open days per year (open/total):")
    total_open = 0
    for y in sorted(yrs):
        o, t = yrs[y]
        total_open += o
        print(f"  {y}: {o:3}/{t:3}  ({o/t:.0%})")
    assert total_open >= 30, f"gate too strict — only {total_open} open days ever"
    print(f"\nSELFTEST OK — gate blocks stress entries, opens on {total_open} sweet-spot days")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        ap.print_help()
