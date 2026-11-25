"""Structure-aware Variance Risk Premium screen.

Reads data/vrp_inputs.parquet (per ticker/day: spot, iv30, iv60, oi) and, for
every name in the applicable universe, measures the TWO edges that short-premium
structures actually monetize:

  LEVEL VRP   (iron condor / iron fly / strangle / straddle)
      iv30(t) - realized vol over (t, t+21 trading days].  Positive = options
      were priced richer than the move that followed -> sellable.
      Reported per regime (incl. the 2020 crash and 2022 bear acid tests),
      with hit-rate and a clean non-overlapping monthly Sharpe.

  TERM VRP    (calendar / diagonal)
      iv60(t) - iv30(t).  Persistent positive slope (contango) is what a short-
      front / long-back calendar leans on; backwardation is the danger state.

Plus IV-rank distribution (feeds the live iv_rank_over_40 gate) and a composite
sellability score that TAGS which structures each name suits. Honest by design:
the crash column shows where selling vol blows up.

  python src/research/vrp_screen.py
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
INP = ROOT / "data" / "vrp_inputs.parquet"
OUT = ROOT / "data" / "vrp_screen_results.csv"

TRADING_DAYS = 252
FWD = 21                     # ~30 calendar days, matches iv30 horizon
RANK_WIN = 252

# regime windows (acid tests called out explicitly)
def _regime(d: pd.Timestamp) -> str:
    if pd.Timestamp("2020-02-20") <= d <= pd.Timestamp("2020-04-30"): return "covid_crash"
    if d.year == 2021: return "calm_2021"
    if pd.Timestamp("2022-01-01") <= d <= pd.Timestamp("2022-10-15"): return "bear_2022"
    if 2023 <= d.year <= 2024: return "recovery_23_24"
    return "recent_25_26"


def _screen_one(g: pd.DataFrame) -> dict | None:
    g = g.sort_values("date").reset_index(drop=True)
    g["dt"] = pd.to_datetime(g["date"])
    if len(g) < 252:           # need ~1yr for IV-rank + stats
        return None
    logret = np.log(g["spot"] / g["spot"].shift(1))
    # forward realized vol over next FWD trading days (annualized), aligned to t
    fwd_rv = logret.shift(-1).rolling(FWD).std().shift(-(FWD - 1)) * np.sqrt(TRADING_DAYS)
    g["fwd_rv"] = fwd_rv
    g["vrp"] = g["iv30"] - g["fwd_rv"]          # LEVEL VRP
    g["term"] = g["iv60"] - g["iv30"]           # TERM VRP
    g["iv_rank"] = g["iv30"].rolling(RANK_WIN).apply(
        lambda w: (w.iloc[-1] > w).mean() * 100, raw=False)
    valid = g.dropna(subset=["vrp"])
    if len(valid) < 100:
        return None
    # clean non-overlapping monthly sample for Sharpe (daily windows overlap)
    monthly = valid.iloc[::FWD]["vrp"]
    sharpe = float(monthly.mean() / monthly.std() * np.sqrt(12)) if monthly.std() > 0 else 0.0
    reg = valid.assign(r=valid["dt"].map(_regime)).groupby("r")["vrp"].mean()
    res = {
        "ticker": g["ticker"].iloc[0], "days": len(g),
        "oi": int(g["oi"].median()),
        "vrp_mean": round(valid["vrp"].mean(), 4),
        "vrp_median": round(valid["vrp"].median(), 4),
        "vrp_hit": round((valid["vrp"] > 0).mean(), 3),
        "vrp_sharpe": round(sharpe, 2),
        "iv30_mean": round(valid["iv30"].mean(), 3),
        "iv_rank_mean": round(g["iv_rank"].mean(), 1),
        "pct_ivrank_gt40": round((g["iv_rank"] > 40).mean(), 3),
        "term_mean": round(valid["term"].mean(), 4),
        "pct_contango": round((valid["term"] > 0).mean(), 3),
    }
    for r in ["covid_crash", "calm_2021", "bear_2022", "recovery_23_24", "recent_25_26"]:
        res[f"vrp_{r}"] = round(float(reg.get(r, np.nan)), 4)
    return res


def _score_and_tag(df: pd.DataFrame) -> pd.DataFrame:
    # condor/fly/strangle: rich level VRP, high hit-rate, survives the bear
    df["condor_score"] = (
        df["vrp_mean"].clip(lower=0) * 100
        + (df["vrp_hit"] - 0.5) * 100
        + df["vrp_bear_2022"].clip(lower=-0.1) * 50
        + df["vrp_sharpe"] * 5)
    # calendar/diagonal: persistent contango + positive front VRP
    df["calendar_score"] = (
        df["term_mean"].clip(lower=0) * 200
        + (df["pct_contango"] - 0.5) * 100
        + df["vrp_mean"].clip(lower=0) * 50)

    def tags(r):
        t = []
        if r["vrp_mean"] > 0.02 and r["vrp_hit"] > 0.6 and r["vrp_bear_2022"] > -0.02:
            t.append("condor/fly/strangle")
        if r["term_mean"] > 0.005 and r["pct_contango"] > 0.6:
            t.append("calendar/diagonal")
        return ",".join(t) if t else "—"
    df["structures"] = df.apply(tags, axis=1)
    return df


def main():
    if not INP.exists():
        sys.exit(f"missing {INP} — run ingest_vrp_inputs.py first")
    data = pd.read_parquet(INP)
    print(f"loaded {len(data):,} rows, {data['ticker'].nunique()} symbols, "
          f"{data['date'].min()}..{data['date'].max()}\n")
    rows = [r for _, g in data.groupby("ticker") if (r := _screen_one(g))]
    res = _score_and_tag(pd.DataFrame(rows)).sort_values("condor_score", ascending=False)
    res.to_csv(OUT, index=False)

    n = len(res)
    print(f"=== VRP SCREEN: {n} names with sufficient history ===\n")
    print(f"Universe-wide LEVEL VRP (iv30 - fwd realized):")
    print(f"  mean across names: {res['vrp_mean'].mean():+.3f} vol pts | "
          f"median {res['vrp_median'].median():+.3f} | "
          f"names with +mean VRP: {(res['vrp_mean']>0).mean():.0%}")
    print(f"  mean hit-rate: {res['vrp_hit'].mean():.1%} of days IV>realized")
    print(f"\nACID TESTS (mean VRP in regime, across names):")
    for r in ["covid_crash", "bear_2022", "calm_2021", "recovery_23_24", "recent_25_26"]:
        col = f"vrp_{r}"; pos = (res[col] > 0).mean()
        print(f"  {r:16}: {res[col].mean():+.3f}  ({pos:.0%} of names positive)")
    print(f"\nTERM structure: mean slope {res['term_mean'].mean():+.4f} | "
          f"names in contango>60% of days: {(res['pct_contango']>0.6).mean():.0%}")

    tagged = res[res["structures"] != "—"]
    print(f"\nSELLABLE names (pass a structure tag): {len(tagged)} / {n}")
    cols = ["ticker", "vrp_mean", "vrp_hit", "vrp_sharpe", "vrp_bear_2022",
            "term_mean", "pct_contango", "iv_rank_mean", "structures"]
    print(f"\nTOP 20 for CONDOR/FLY/STRANGLE (level VRP):")
    print(res.head(20)[cols].to_string(index=False))
    print(f"\nTOP 15 for CALENDAR/DIAGONAL (term VRP):")
    print(res.sort_values("calendar_score", ascending=False).head(15)[cols].to_string(index=False))
    print(f"\nfull results -> {OUT}")


if __name__ == "__main__":
    main()
