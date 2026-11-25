"""Lightweight broad ORATS ingest for the VRP screen.

Streams the daily zips 2020-2026 and, for every symbol in the applicable
universe (data/vrp_universe.csv), keeps ONE row per (ticker, trade_date):
  spot, iv30/dte30, iv60/dte60 (term structure), near-ATM OI (tradability wt).
No per-strike greeks -> the whole 484-name, 6.5yr store is a few MB.

Everything the screen needs comes from here: spot series -> realized vol,
iv30 -> level VRP, iv60-iv30 -> term-structure VRP.

  python src/research/ingest_vrp_inputs.py --start-year 2020 --end-year 2026
"""
import argparse, io, os, re, sys, zipfile
from pathlib import Path
import boto3, pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
OUT = ROOT / "data" / "vrp_inputs.parquet"


def _s3():
    return boto3.client("s3", aws_access_key_id=os.environ["ORATS_S3_KEY"],
                        aws_secret_access_key=os.environ["ORATS_S3_SECRET"],
                        region_name="us-east-1"), os.environ["ORATS_S3_BUCKET"]


def _key_date(key: str) -> str | None:
    """'2026/ORATS_SMV_Strikes_20260616.zip' -> '2026-06-16'."""
    m = re.search(r"(\d{8})\.zip$", key)
    return f"{m[1][:4]}-{m[1][4:6]}-{m[1][6:8]}" if m else None


def _term_iv(rows: pd.DataFrame, target_dte: float):
    """ATM IV at target DTE: expiration nearest target, strike nearest spot,
    mean call/put mid IV. Returns (iv, actual_dte) or (None, None)."""
    dte = rows["yte"] * 365
    exp = rows.loc[(dte - target_dte).abs().idxmin(), "expirDate"]
    near = rows[rows["expirDate"] == exp]
    spot = near["stkPx"].iloc[0]
    atm = near.loc[(near["strike"] - spot).abs().idxmin()]
    ivs = [v for v in (atm["cMidIv"], atm["pMidIv"]) if pd.notna(v) and v > 0]
    if not ivs and pd.notna(atm["smoothSmvVol"]):
        ivs = [atm["smoothSmvVol"]]
    iv = round(float(sum(ivs) / len(ivs)), 4) if ivs else None
    return iv, round(float((atm["yte"] * 365)), 1)


def _day_rows(df: pd.DataFrame, universe: set) -> list[dict]:
    df = df[df["ticker"].isin(universe)].dropna(subset=["stkPx", "strike", "yte"])
    df = df[df["stkPx"] > 0]
    out = []
    for (tk, td), g in df.groupby(["ticker", "trade_date"]):
        iv30, dte30 = _term_iv(g, 30)
        iv60, dte60 = _term_iv(g, 60)
        if iv30 is None:
            continue
        atm_band = g[(g["strike"] / g["stkPx"] - 1).abs() <= 0.10]
        oi = float((atm_band["cOi"].fillna(0) + atm_band["pOi"].fillna(0)).sum())
        out.append({"ticker": tk, "date": pd.to_datetime(td, format="mixed").strftime("%Y-%m-%d"),
                    "spot": round(float(g["stkPx"].iloc[0]), 4),
                    "iv30": iv30, "dte30": dte30, "iv60": iv60, "dte60": dte60, "oi": oi})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=2020)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--rebuild", action="store_true",
                    help="ignore the existing file and re-pull the full range (default: incremental)")
    args = ap.parse_args()

    universe = set(pd.read_csv(ROOT / "data" / "vrp_universe.csv")["ticker"])
    # INCREMENTAL by default: keep existing rows, skip days already present, append
    # only new dates. (The old behaviour overwrote OUT with just the streamed years,
    # so running a sub-range nuked history — never do that again.) --rebuild starts fresh.
    existing = pd.read_parquet(OUT) if (OUT.exists() and not args.rebuild) else pd.DataFrame()
    have = set(existing["date"]) if not existing.empty else set()
    print(f"VRP inputs ingest {args.start_year}-{args.end_year} "
          f"[{'REBUILD' if args.rebuild else 'incremental'}], {len(universe)} symbols, "
          f"{len(existing)} existing rows ({len(have)} days present)")

    s3, bucket = _s3()
    paginator = s3.get_paginator("list_objects_v2")
    new_rows, skipped = [], 0

    def _flush():
        combined = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
        combined = combined.drop_duplicates(["ticker", "date"], keep="last").sort_values(["ticker", "date"])
        combined.to_parquet(OUT)
        return combined

    for yr in range(args.start_year, args.end_year + 1):
        all_keys = [o["Key"] for page in paginator.paginate(Bucket=bucket, Prefix=f"{yr}/")
                    for o in page.get("Contents", []) if o["Key"].endswith(".zip")]
        keys = [k for k in all_keys if _key_date(k) not in have]
        if not keys:
            print(f"  {yr}: up to date — nothing new", flush=True); continue
        print(f"  {yr}: {len(keys)} new day(s) ({len(all_keys) - len(keys)} already present)", flush=True)
        for i, key in enumerate(keys):
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                zf = zipfile.ZipFile(io.BytesIO(obj))
                df = pd.read_csv(zf.open(zf.namelist()[0]))
            except Exception as exc:
                skipped += 1; print(f"  skip {key}: {type(exc).__name__}", flush=True); continue
            new_rows.extend(_day_rows(df, universe))
            if (i + 1) % 100 == 0:
                print(f"  {yr}: {i+1}/{len(keys)} files, +{len(new_rows)} new rows", flush=True)
        print(f"  {yr} done ({skipped} skipped cumulative)", flush=True)
        _flush()  # durable per-year flush, preserving existing history

    if not new_rows:
        print("Already current — no new days pulled."); return
    out = _flush()
    print(f"\nDONE: +{len(new_rows)} new rows -> {len(out)} total, {out['ticker'].nunique()} symbols, "
          f"{out['date'].min()}..{out['date'].max()}, {skipped} skipped -> {OUT}")


if __name__ == "__main__":
    main()
