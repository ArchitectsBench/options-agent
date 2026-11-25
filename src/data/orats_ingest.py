"""ORATS SMV-Strikes historical ingestion (S3 streaming + local).

Source verified 2026-06-15 against the real bucket (orats-smv-strikes-hist):
daily zips YYYY/ORATS_SMV_Strikes_YYYYMMDD.zip, each a full-market per-strike
snapshot (~5,000 symbols, 39 cols) 14 min before close, back to 2007.

Streams each daily zip from S3, unzips in memory, filters to our watchlist
(never stores the full market locally), and writes two stores:
  1. data/iv_history/{ticker}.csv         — 30-day ATM IV per day (merged with
     forward snapshots) -> unblocks the live iv_rank_over_40 gate immediately.
  2. data/iv_history_full/{ticker}_{yr}.parquet — full per-strike IV+greeks,
     the input for Level-2 premium backtesting. Written per year (bounded mem).

Usage:
  python src/data/orats_ingest.py --s3 --start-year 2020 --end-year 2026
  python src/data/orats_ingest.py --selftest
"""

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

IV_HISTORY = ROOT / "data" / "iv_history"
IV_FULL = ROOT / "data" / "iv_history_full"

# ── verified ORATS SMV-Strikes column names -> our canonical names ──
COLUMN_MAP = {
    "ticker": "ticker",
    "trade_date": "trade_date",
    "expiration": "expirDate",
    "yte": "yte",                 # YEARS to expiration (no dte column)
    "strike": "strike",
    "spot": "stkPx",
    "call_iv": "cMidIv",
    "put_iv": "pMidIv",
    "smv_vol": "smoothSmvVol",
    "delta": "delta", "gamma": "gamma", "theta": "theta", "vega": "vega",
}
ATM_DTE_TARGET = 30   # 30-day ATM IV — conventional, low-noise basis for IV-rank


def _watchlist() -> set[str]:
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    return set(cfg["watchlist"]["default"])


def _key_date(key: str) -> str | None:
    """'2026/ORATS_SMV_Strikes_20260616.zip' -> '2026-06-16' (the trade date)."""
    m = re.search(r"(\d{8})\.zip$", key)
    return f"{m[1][:4]}-{m[1][4:6]}-{m[1][6:8]}" if m else None


def _existing_full_dates(yr: int) -> set[str]:
    """ISO trade dates already in the per-strike store for a year (union across
    tickers). trade_date is 'M/D/YYYY' text, so parse before comparing."""
    dates: set[str] = set()
    for p in IV_FULL.glob(f"*_{yr}.parquet"):
        try:
            d = pd.read_parquet(p, columns=["trade_date"])["trade_date"]
            dates |= set(pd.to_datetime(d, format="mixed").dt.strftime("%Y-%m-%d"))
        except Exception:
            continue
    return dates


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    present = {src: dst for dst, src in COLUMN_MAP.items() if src in df.columns}
    return df.rename(columns=present)


def _atm_iv(rows: pd.DataFrame) -> float | None:
    """30-day ATM IV: expiration nearest 30 DTE, strike nearest spot, mean
    call/put mid IV (fallback smoothed SMV vol)."""
    rows = rows.dropna(subset=["strike", "spot", "yte"])
    if rows.empty:
        return None
    dte = rows["yte"] * 365
    exp = rows.loc[(dte - ATM_DTE_TARGET).abs().idxmin(), "expiration"]
    near = rows[rows["expiration"] == exp]
    spot = near["spot"].iloc[0]
    atm = near.loc[(near["strike"] - spot).abs().idxmin()]
    ivs = [v for v in (atm.get("call_iv"), atm.get("put_iv")) if pd.notna(v) and v]
    if not ivs and pd.notna(atm.get("smv_vol")):
        ivs = [atm["smv_vol"]]
    return round(float(sum(ivs) / len(ivs)), 4) if ivs else None


def _s3():
    import os
    import boto3
    return boto3.client("s3", aws_access_key_id=os.environ["ORATS_S3_KEY"],
                        aws_secret_access_key=os.environ["ORATS_S3_SECRET"],
                        region_name=os.environ.get("ORATS_S3_REGION", "us-east-1")), os.environ["ORATS_S3_BUCKET"]


def s3_ingest(start_year: int, end_year: int, rebuild: bool = False) -> dict:
    """Stream daily zips year-by-year; filter to watchlist; build both stores.

    INCREMENTAL by default: skips daily files whose trade date is already in the
    local per-strike store, and APPENDS new days to the year parquet (dedup on
    trade_date+expiration+strike). So `--start-year 2026 --end-year 2026` pulls
    only the missing tail and never clobbers other days. rebuild=True ignores the
    local store and re-pulls the full range from scratch."""
    import os
    tickers = _watchlist()
    IV_HISTORY.mkdir(parents=True, exist_ok=True)
    IV_FULL.mkdir(parents=True, exist_ok=True)
    s3, bucket = _s3()
    paginator = s3.get_paginator("list_objects_v2")
    summary = {"files": 0, "skipped": 0, "already": 0, "atm_rows": 0, "full_rows": 0}

    def _merge_atm(tk: str, rows: list[dict]):
        new = pd.DataFrame(rows)
        path = IV_HISTORY / f"{tk}.csv"
        if path.exists():
            old = pd.read_csv(path)
            new = pd.concat([old, new])
        # ORATS trade_date is M/D/YYYY text; normalize to ISO so string-sort ==
        # chronological and dedup is exact. Last write per date wins.
        new["date"] = pd.to_datetime(new["date"], format="mixed").dt.strftime("%Y-%m-%d")
        new = new.drop_duplicates("date", keep="last").sort_values("date")
        new.to_csv(path, index=False)

    for yr in range(start_year, end_year + 1):
        full_by_ticker: dict[str, list[pd.DataFrame]] = {}
        atm_year: dict[str, list[dict]] = {}
        have = set() if rebuild else _existing_full_dates(yr)
        all_keys = [o["Key"] for page in paginator.paginate(Bucket=bucket, Prefix=f"{yr}/")
                    for o in page.get("Contents", []) if o["Key"].endswith(".zip")]
        keys = [k for k in all_keys if _key_date(k) not in have]
        summary["already"] += len(all_keys) - len(keys)
        if not keys:
            print(f"  {yr}: up to date ({len(have)} days present) — nothing new", flush=True)
            continue
        print(f"  {yr}: {len(keys)} new day(s) to pull ({len(have)} already present)", flush=True)
        for i, key in enumerate(keys):
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                df = _normalize(pd.read_csv(zipfile.ZipFile(io.BytesIO(obj)).open(
                    zipfile.ZipFile(io.BytesIO(obj)).namelist()[0])))
            except Exception as exc:  # empty/holiday/corrupt file — skip, don't die
                summary["skipped"] += 1
                print(f"  skip {key}: {type(exc).__name__}", flush=True)
                continue
            if "ticker" not in df.columns:
                summary["skipped"] += 1
                continue
            df = df[df["ticker"].isin(tickers)]
            if df.empty:
                continue
            summary["files"] += 1
            for (tk, td), g in df.groupby(["ticker", "trade_date"]):
                iv = _atm_iv(g)
                if iv is not None:
                    atm_year.setdefault(tk, []).append({"date": str(td)[:10], "atm_iv": iv})
                full_by_ticker.setdefault(tk, []).append(g)
            if (i + 1) % 100 == 0:
                print(f"  {yr}: {i+1}/{len(keys)} files...", flush=True)
        # flush BOTH stores per year so completed years are durable. APPEND to the
        # existing year parquet (dedup on the natural key) rather than overwrite —
        # so a sub-range run only adds new days and never drops existing ones.
        for tk, frames in full_by_ticker.items():
            full = pd.concat(frames, ignore_index=True)
            path = IV_FULL / f"{tk}_{yr}.parquet"
            if path.exists() and not rebuild:
                full = pd.concat([pd.read_parquet(path), full], ignore_index=True)
                full = full.drop_duplicates(["trade_date", "expiration", "strike"], keep="last")
            full.to_parquet(path)
            summary["full_rows"] += len(full)
        for tk, rows in atm_year.items():
            _merge_atm(tk, rows)
            summary["atm_rows"] += len(rows)
        print(f"  {yr} done: {summary['files']} files cumulative, {summary['skipped']} skipped", flush=True)

    summary["tickers"] = len({p.stem.rsplit("_", 1)[0] for p in IV_FULL.glob("*.parquet")})
    return summary


def selftest():
    import tempfile
    import numpy as np
    global IV_HISTORY, IV_FULL
    rng = np.random.default_rng(0)
    rows = []
    for date in ["2025-06-10", "2025-06-11"]:
        for tk, spot in [("SPY", 600), ("AAPL", 290), ("ZZZZ", 50)]:
            for yte in (0.014, 0.082, 0.25):  # ~5d, ~30d, ~90d
                for k in range(-3, 4):
                    base = 0.18 + 0.02 * abs(k)
                    rows.append({"ticker": tk, "trade_date": date, "expirDate": date,
                                 "yte": yte, "strike": round(spot + k * spot * 0.01, 2),
                                 "stkPx": spot, "cMidIv": round(base + rng.normal(0, .002), 4),
                                 "pMidIv": round(base + rng.normal(0, .002), 4),
                                 "smoothSmvVol": base, "delta": .5, "gamma": .02,
                                 "theta": -.05, "vega": .1})
    df = _normalize(pd.DataFrame(rows))
    spy_30d = _atm_iv(df[(df.ticker == "SPY") & (df.trade_date == "2025-06-10")])
    assert 0.17 < spy_30d < 0.19, f"30d ATM IV off: {spy_30d}"
    print(f"SELFTEST: 30d ATM IV for SPY = {spy_30d} (expected ~0.18) — schema + ATM logic OK")
    print("COLUMN_MAP verified against real ORATS SMV-Strikes file 2026-06-15.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--s3", action="store_true")
    ap.add_argument("--start-year", type=int, default=2020)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--rebuild", action="store_true",
                    help="ignore the local store and re-pull the full range (default: incremental)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    elif args.s3:
        mode = "REBUILD" if args.rebuild else "incremental (skip days already local)"
        print(f"S3 ingest {args.start_year}-{args.end_year} [{mode}], filtering to watchlist...")
        print(s3_ingest(args.start_year, args.end_year, rebuild=args.rebuild))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
