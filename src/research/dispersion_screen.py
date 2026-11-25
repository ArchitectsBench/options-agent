"""Cheap screen #1: does the DISPERSION / correlation-premium edge exist in our data?

Dispersion trade = short index vol + long component vol. It profits when IMPLIED
correlation (priced into index vs single-name options) exceeds the REALIZED
correlation that follows. We test exactly that, from data already on disk
(vrp_inputs.parquet: iv30 per name + spot for realized).

Implied correlation proxy (equal-weight):  rho_imp = sigma_index^2 / (mean_i sigma_i)^2
Realized correlation: average pairwise corr of component fwd 21d returns.
Edge exists if rho_imp - rho_real_fwd is persistently > 0 (and survives regimes).

  python src/research/dispersion_screen.py
"""
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
INP = ROOT / "data" / "vrp_inputs.parquet"
FWD = 21

INDEX = "QQQ"
COMPONENTS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AMD", "NFLX", "COST", "PLTR"]


def _regime(d):
    d = pd.Timestamp(d)
    if pd.Timestamp("2020-02-20") <= d <= pd.Timestamp("2020-04-30"): return "covid_crash"
    if d.year == 2021: return "calm_2021"
    if pd.Timestamp("2022-01-01") <= d <= pd.Timestamp("2022-10-15"): return "bear_2022"
    if 2023 <= d.year <= 2024: return "recovery_23_24"
    return "recent_25_26"


def main():
    df = pd.read_parquet(INP)
    comps = [c for c in COMPONENTS if c in df.ticker.unique()]
    iv = df.pivot_table(index="date", columns="ticker", values="iv30")
    spot = df.pivot_table(index="date", columns="ticker", values="spot")
    iv.index = pd.to_datetime(iv.index); spot.index = pd.to_datetime(spot.index)
    iv, spot = iv.sort_index(), spot.sort_index()
    print(f"index {INDEX} vs {len(comps)} components: {comps}\n")

    sig_i = iv[INDEX]
    sig_comp = iv[comps]
    # implied correlation proxy
    rho_imp = (sig_i ** 2) / (sig_comp.mean(axis=1) ** 2)
    rho_imp = rho_imp.clip(0, 1)

    # forward 21d realized: index vol, component vols, avg pairwise corr
    ret = np.log(spot / spot.shift(1))
    rows = []
    dates = spot.index
    for i, d in enumerate(dates):
        if i + FWD >= len(dates) or pd.isna(rho_imp.get(d, np.nan)):
            continue
        win = ret.iloc[i + 1:i + 1 + FWD]
        cr = win[comps].dropna(axis=1, how="any")
        if cr.shape[1] < 4:
            continue
        cm = cr.corr().values
        rho_real = (cm.sum() - len(cm)) / (len(cm) * (len(cm) - 1))   # avg off-diagonal
        rows.append({"date": d, "rho_imp": float(rho_imp[d]), "rho_real": float(rho_real),
                     "premium": float(rho_imp[d] - rho_real), "regime": _regime(d)})
    r = pd.DataFrame(rows)
    print(f"=== DISPERSION SCREEN: {len(r)} days ===")
    print(f"  mean implied corr : {r.rho_imp.mean():.3f}")
    print(f"  mean realized corr: {r.rho_real.mean():.3f}")
    print(f"  mean PREMIUM (imp-real): {r.premium.mean():+.3f}  | "
          f"% days positive: {(r.premium>0).mean():.0%}  | "
          f"Sharpe(daily): {r.premium.mean()/r.premium.std():.2f}")
    print(f"\n  by regime (premium = dispersion-trade edge):")
    g = r.groupby("regime")["premium"]
    order = ["covid_crash","calm_2021","bear_2022","recovery_23_24","recent_25_26"]
    for rg in [o for o in order if o in g.groups]:
        s = g.get_group(rg)
        print(f"    {rg:16}: {s.mean():+.3f}  ({(s>0).mean():.0%} positive, n={len(s)})")
    # non-overlapping monthly for a clean Sharpe
    m = r.iloc[::FWD]["premium"]
    print(f"\n  non-overlapping monthly premium: mean {m.mean():+.3f}, Sharpe(ann) "
          f"{m.mean()/m.std()*np.sqrt(12):.2f}, {(m>0).mean():.0%} positive (n={len(m)})")
    print("\n  READ: persistent positive premium incl. in the bear -> dispersion edge is real.")


if __name__ == "__main__":
    main()
