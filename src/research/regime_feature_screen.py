"""Mine 2-3 more REGIME FEATURES and validate them the GEX way.

For each candidate signal, test (cross-validated across SPY/QQQ/IWM) whether it
predicts next-day behaviour:
  - VOL magnitude:  corr(feature, |next-day return|) + top/bottom-quartile move ratio
  - DIRECTION:      corr(feature, signed next-day return)
A feature earns a place if the effect is consistent in sign across all three.

Features (from vrp_inputs.parquet — iv30/iv60/spot, 484 names, 2020-2026):
  term_slope = iv60 - iv30        (backwardation<0 = stress -> expect bigger moves)
  iv_mom_5d  = iv30 - iv30[-5]    (vol expanding -> clustering -> bigger moves)
  iv_level_z = iv30 z vs own 252d (high absolute vol -> bigger moves, trivial check)

  python src/research/regime_feature_screen.py
"""
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
INP = ROOT / "data" / "vrp_inputs.parquet"
TICKERS = ["SPY", "QQQ", "IWM"]


def features(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("date").copy()
    g["term_slope"] = g["iv60"] - g["iv30"]
    g["iv_mom_5d"] = g["iv30"] - g["iv30"].shift(5)
    g["iv_level_z"] = (g["iv30"] - g["iv30"].rolling(252).mean()) / g["iv30"].rolling(252).std()
    g["ret_fwd"] = np.log(g["spot"].shift(-1) / g["spot"])
    g["abs_fwd"] = g["ret_fwd"].abs()
    return g.dropna(subset=["abs_fwd"])


def test_feature(data: dict, feat: str):
    print(f"\n=== {feat} ===")
    rows = []
    for tk, g in data.items():
        d = g.dropna(subset=[feat])
        if len(d) < 200:
            continue
        cv = d[feat].corr(d["abs_fwd"])           # vol magnitude
        cd = d[feat].corr(d["ret_fwd"])           # direction
        q = d[feat].quantile([0.25, 0.75])
        hi = d[d[feat] >= q[0.75]]["abs_fwd"].mean()
        lo = d[d[feat] <= q[0.25]]["abs_fwd"].mean()
        print(f"  {tk}: corr(feat,|move|)={cv:+.3f}  ratio(hiQ/loQ |move|)={hi/lo:.2f}  "
              f"corr(feat,signed ret)={cd:+.3f}")
        rows.append((cv, cd, hi / lo))
    if rows:
        cv = np.mean([r[0] for r in rows]); cd = np.mean([r[1] for r in rows])
        consistent_vol = len({np.sign(r[0]) for r in rows}) == 1
        consistent_dir = len({np.sign(r[1]) for r in rows}) == 1
        print(f"  AVG corr(|move|) {cv:+.3f} (consistent sign: {consistent_vol}) | "
              f"avg corr(direction) {cd:+.3f} (consistent: {consistent_dir})")


def main():
    df = pd.read_parquet(INP)
    data = {tk: features(df[df.ticker == tk]) for tk in TICKERS}
    print(f"REGIME-FEATURE SCREEN — {TICKERS}, "
          f"{min(g.date.min() for g in data.values())}..{max(g.date.max() for g in data.values())}")
    for feat in ["term_slope", "iv_mom_5d", "iv_level_z"]:
        test_feature(data, feat)
    print("\nREAD: a feature with CONSISTENT sign across all 3 ETFs (esp. on |move|) "
          "is a real, wireable regime signal. Direction corr is usually weak (markets "
          "are ~efficient on direction) — these earn their keep as VOL-regime signals.")


if __name__ == "__main__":
    main()
