"""Select the 'applicable' premium-selling universe from ORATS liquidity.

Premium selling is uninvestable on illiquid options (you can't get filled near
mid). So the universe = optionable underlyings with real near-ATM open interest
and tight markets. We scan a few recent ORATS daily files (the whole ~5,800
symbol market is in each), score every symbol on liquidity, and emit the set
that clears hard tradability floors. Recent dates -> reflects what we can trade
NOW (the live engine trades now), used as the screen's universe over 2020-2026.

Output: data/vrp_universe.csv  (ticker, oi, rel_spread, spot, dollar_vol)
"""
import io, os, sys, zipfile
from pathlib import Path
import boto3, pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

SCAN_DATES_PREFIX = ["2025/", "2024/"]   # pull the latest few files across these
N_FILES = 3
# hard tradability floors
MIN_SPOT = 5.0
MAX_REL_SPREAD = 0.15     # near-ATM call (ask-bid)/mid median
MIN_OI = 2000             # summed near-ATM call+put OI (14-60 DTE)


def _s3():
    return boto3.client("s3", aws_access_key_id=os.environ["ORATS_S3_KEY"],
                        aws_secret_access_key=os.environ["ORATS_S3_SECRET"],
                        region_name="us-east-1"), os.environ["ORATS_S3_BUCKET"]


def _score_file(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["stkPx", "strike", "yte"])
    df = df[df["stkPx"] > 0]
    dte = df["yte"] * 365
    near = df[(dte.between(14, 60)) & ((df["strike"] / df["stkPx"] - 1).abs() <= 0.10)].copy()
    near["mid"] = (near["cBidPx"] + near["cAskPx"]) / 2
    near["rel_spread"] = (near["cAskPx"] - near["cBidPx"]) / near["mid"].where(near["mid"] > 0)
    g = near.groupby("ticker")
    return pd.DataFrame({
        "oi": g.apply(lambda x: (x["cOi"].fillna(0) + x["pOi"].fillna(0)).sum()),
        "dollar_vol": g.apply(lambda x: ((x["cVolu"].fillna(0) + x["pVolu"].fillna(0)) * x["mid"].fillna(0) * 100).sum()),
        "rel_spread": g["rel_spread"].median(),
        "spot": g["stkPx"].median(),
    })


def main():
    s3, bucket = _s3()
    keys = []
    for pfx in SCAN_DATES_PREFIX:
        ks = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix=pfx)["Contents"]
              if o["Key"].endswith(".zip")]
        keys += ks[-2:]
    keys = sorted(set(keys))[-N_FILES:]
    print(f"scanning {len(keys)} files: {keys}")
    frames = []
    for k in keys:
        obj = s3.get_object(Bucket=bucket, Key=k)["Body"].read()
        zf = zipfile.ZipFile(io.BytesIO(obj))
        df = pd.read_csv(zf.open(zf.namelist()[0]))
        frames.append(_score_file(df))
        print(f"  {k}: {df['ticker'].nunique()} symbols")
    # average liquidity across scanned dates
    allf = pd.concat(frames).groupby(level=0).mean()
    sel = allf[(allf["spot"] >= MIN_SPOT) & (allf["rel_spread"] <= MAX_REL_SPREAD)
               & (allf["oi"] >= MIN_OI)].sort_values("oi", ascending=False)
    out = ROOT / "data" / "vrp_universe.csv"
    sel.round(4).to_csv(out)
    print(f"\nuniverse: {len(sel)} symbols (of {len(allf)} optionable) -> {out}")
    print(f"OI range: {sel['oi'].min():,.0f} - {sel['oi'].max():,.0f}")
    print(f"top 15: {list(sel.index[:15])}")
    print(f"median rel_spread: {sel['rel_spread'].median():.3f}")


if __name__ == "__main__":
    main()
