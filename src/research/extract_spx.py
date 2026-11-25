"""SPX index-options extraction from the raw ORATS SMV-Strikes archive.

Reads the local Google Drive cache of daily full-market zips
(~/.../ORATS_archive/{YYYY}/ORATS_SMV_Strikes_{YYYYMMDD}.zip, 2007-2026),
filters to index tickers {SPX, SPXW, XSP, VIX}, and writes per-ticker
per-year parquets to data/iv_history_full/{TICKER}_{YEAR}.parquet with the
EXACT schema of the existing store (see src/data/orats_ingest.py COLUMN_MAP
and e.g. SPY_2024.parquet), so src/backtest/chain.py reads them transparently.

Era note (verified against real 2007/2015/2020/2022/2026 files):
  - 2007..~2021 header: ticker,stkPx,expirDate,yte,strike,... (37 cols)
  - ~2022..2026 header: ticker,cOpra,pOpra,stkPx,... (39 cols; adds OPRA ids)
  All eras carry trade_date, spot_px, bid/ask, IVs, greeks. Extraction is BY
  HEADER NAME; columns absent in an era are emitted as null and logged once
  per header signature.
  - SPXW does not exist as an ORATS ticker in any era (weeklys are folded
    under SPX); it is scanned for anyway and recorded as zero.

macOS TCC quirk: directory listing on the Drive mount is blocked from the
shell, but open/read on exact known paths works — so we iterate every
candidate weekday and skip quietly on FileNotFoundError (holidays).

RESUMABLE/IDEMPOTENT: completed dates tracked in
logs/research/spx_extract_state.jsonl (appended only AFTER their rows are
durably flushed); parquet writes merge+dedup on
trade_date+expiration+strike+ticker and are atomic (tmp + os.replace).

Usage:
  python src/research/extract_spx.py --validate            # 3 era samples -> scratch dir
  python src/research/extract_spx.py --start 2007-01-01 --end 2026-06-30
  (resume = just re-run the same command; state file is honored)

STRICT OWNERSHIP: this module only CREATES SPX_/SPXW_/XSP_/VIX_ parquets and
writes under logs/research/. It never touches other files.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = (Path.home() / "Library/CloudStorage"
           / "GoogleDrive-you@example.com/My Drive/ORATS_archive")
IV_FULL = ROOT / "data" / "iv_history_full"
LOG_DIR = ROOT / "logs" / "research"
STATE = LOG_DIR / "spx_extract_state.jsonl"
LOG = LOG_DIR / "spx_extract.log"

TICKERS = ("SPX", "SPXW", "XSP", "VIX")
PREFIXES = tuple(f"{t},".encode() for t in TICKERS)

# raw ORATS name -> canonical store name (mirror of orats_ingest.COLUMN_MAP,
# inverted; all other raw columns keep their raw names, as the existing
# per-strike parquets do)
RENAME = {"stkPx": "spot", "expirDate": "expiration", "cMidIv": "call_iv",
          "pMidIv": "put_iv", "smoothSmvVol": "smv_vol"}

# canonical output column order — exactly the modern-era store schema
# (verified against data/iv_history_full/SPY_2024.parquet)
CANON = ["ticker", "cOpra", "pOpra", "spot", "expiration", "yte", "strike",
         "cVolu", "cOi", "pVolu", "pOi", "cBidPx", "cValue", "cAskPx",
         "pBidPx", "pValue", "pAskPx", "cBidIv", "call_iv", "cAskIv",
         "smv_vol", "pBidIv", "put_iv", "pAskIv", "iRate", "divRate",
         "residualRateData", "delta", "gamma", "theta", "vega", "rho", "phi",
         "driftlessTheta", "extVol", "extCTheo", "extPTheo", "spot_px",
         "trade_date"]
STR_COLS = {"ticker", "cOpra", "pOpra", "expiration", "trade_date"}
INT_COLS = {"cVolu", "cOi", "pVolu", "pOi"}
FLOAT_COLS = [c for c in CANON if c not in STR_COLS and c not in INT_COLS]
DEDUP_KEY = ["trade_date", "expiration", "strike", "ticker"]


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def zip_path(d: dt.date) -> Path:
    return ARCHIVE / f"{d.year}" / f"ORATS_SMV_Strikes_{d:%Y%m%d}.zip"


def weekdays(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def completed_dates() -> set[str]:
    done: set[str] = set()
    if STATE.exists():
        for line in STATE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["date"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def extract_day(d: dt.date, seen_headers: dict[str, list[str]]) -> pd.DataFrame | None:
    """Read one daily zip, return normalized rows for our tickers (or None if
    the file doesn't exist — holiday). Extraction is strictly by header name."""
    try:
        with zip_path(d).open("rb") as fh:
            raw_zip = fh.read()
    except FileNotFoundError:
        return None
    zf = zipfile.ZipFile(io.BytesIO(raw_zip))
    raw = zf.open(zf.namelist()[0]).read()
    nl = raw.find(b"\n")
    header = raw[:nl].rstrip(b"\r")
    matched = [ln for ln in raw[nl + 1:].split(b"\n") if ln.startswith(PREFIXES)]
    if not matched:
        return pd.DataFrame(columns=CANON)

    df = pd.read_csv(io.BytesIO(header + b"\n" + b"\n".join(matched)),
                     dtype=str, keep_default_na=False, na_values=[""])
    df = df.rename(columns=RENAME)

    sig = header.decode()
    if sig not in seen_headers:
        missing = [c for c in CANON if c not in df.columns]
        seen_headers[sig] = missing
        log(f"NEW HEADER ERA (first seen {d}): {len(df.columns)} cols; "
            f"unavailable canonical cols -> null: {missing or 'none'}")

    for c in CANON:
        if c not in df.columns:
            df[c] = np.nan
    df = df[CANON]
    # exact-ticker filter (prefix scan can't overmatch since prefixes include
    # the comma, but be explicit)
    df = df[df["ticker"].isin(TICKERS)]
    for c in FLOAT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    for c in INT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
    # trade_date must exist for the store's natural key; fall back to filename
    # date in ORATS M/D/YYYY style if the column is missing/blank
    fallback = f"{d.month}/{d.day}/{d.year}"
    td = df["trade_date"].astype("object")
    df["trade_date"] = td.where(td.notna() & (td != ""), fallback)
    for c in STR_COLS:
        df[c] = df[c].astype("object")
    return df


def flush(buffers: dict[tuple[str, int], list[pd.DataFrame]],
          pending: list[dict], counts: dict) -> None:
    """Durably write buffered rows (merge+dedup, atomic), then mark dates done."""
    for (tk, yr), frames in sorted(buffers.items()):
        new = pd.concat(frames, ignore_index=True)
        path = IV_FULL / f"{tk}_{yr}.parquet"
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
        new = new.drop_duplicates(DEDUP_KEY, keep="last")
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        new.to_parquet(tmp)
        os.replace(tmp, path)
        counts[f"{tk}_{yr}"] = len(new)
    buffers.clear()
    if pending:
        with STATE.open("a") as f:
            for rec in pending:
                f.write(json.dumps(rec) + "\n")
        pending.clear()


def run(start: dt.date, end: dt.date, out_dir: Path = IV_FULL,
        state_enabled: bool = True) -> dict:
    global IV_FULL
    IV_FULL = out_dir
    IV_FULL.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    done = completed_dates() if state_enabled else set()
    seen_headers: dict[str, list[str]] = {}
    buffers: dict[tuple[str, int], list[pd.DataFrame]] = {}
    pending: list[dict] = []
    counts: dict[str, int] = {}
    n_files = n_missing = n_skipped = 0
    cur_year = None
    t0 = time.time()
    log(f"START extract {start}..{end}  ({len(done)} dates already done)")

    for d in weekdays(start, end):
        iso = d.isoformat()
        if iso in done:
            continue
        if cur_year is not None and d.year != cur_year:
            flush(buffers, pending, counts)
            yr_counts = {k: v for k, v in counts.items() if k.endswith(str(cur_year))}
            log(f"YEAR {cur_year} complete: {yr_counts}")
        cur_year = d.year
        try:
            df = extract_day(d, seen_headers)
        except Exception as exc:  # corrupt zip etc. — log, keep going, do NOT mark done
            n_skipped += 1
            log(f"SKIP {iso}: {type(exc).__name__}: {exc}")
            continue
        if df is None:
            n_missing += 1
            continue
        n_files += 1
        day_counts = df["ticker"].value_counts().to_dict() if len(df) else {}
        for tk, g in df.groupby("ticker"):
            buffers.setdefault((tk, d.year), []).append(g)
        if state_enabled:
            pending.append({"date": iso, "rows": day_counts})
        if n_files % 100 == 0:
            rate = n_files / (time.time() - t0)
            log(f"progress: {n_files} files (@{iso}), {n_missing} missing/holiday, "
                f"{n_skipped} skipped, {rate:.1f} files/s")
            flush(buffers, pending, counts)

    flush(buffers, pending, counts)
    elapsed = time.time() - t0
    log(f"DONE: {n_files} files in {elapsed/60:.1f} min, {n_missing} missing "
        f"(weekends-adjacent holidays), {n_skipped} skipped/corrupt")
    log(f"per-year row counts: {json.dumps(counts, sort_keys=True)}")
    absent = [t for t in TICKERS if not any(k.startswith(t + "_") for k in counts)]
    if absent and n_files:
        log(f"NOTE: no rows found this run for tickers: {absent}")
    return {"files": n_files, "missing": n_missing, "skipped": n_skipped,
            "counts": counts, "headers": seen_headers}


def validate() -> None:
    """Run the three era samples into a scratch dir and diff schema vs the
    live store (SPY_2024.parquet). Writes nothing under data/."""
    scratch = LOG_DIR / "validate_out"
    samples = [dt.date(2007, 1, 3), dt.date(2015, 1, 5), dt.date(2026, 6, 15)]
    seen: dict[str, list[str]] = {}
    ref = pd.read_parquet(ROOT / "data" / "iv_history_full" / "SPY_2024.parquet")
    for d in samples:
        t0 = time.time()
        df = extract_day(d, seen)
        assert df is not None and len(df), f"{d}: no rows"
        assert list(df.columns) == list(ref.columns) == CANON, "column order mismatch"
        for c in FLOAT_COLS:
            assert df[c].dtype == "float64", f"{c} dtype {df[c].dtype}"
        # spot-check critical fields are populated
        crit = ["spot", "strike", "expiration", "trade_date", "cBidPx", "cAskPx",
                "pBidPx", "pAskPx", "call_iv", "put_iv", "delta", "gamma",
                "theta", "vega"]
        nulls = {c: int(df[c].isna().sum()) for c in crit if df[c].isna().any()}
        vc = df["ticker"].value_counts().to_dict()
        spx = df[df.ticker == "SPX"]
        log(f"VALIDATE {d}: {len(df)} rows {vc} in {time.time()-t0:.2f}s | "
            f"SPX spot={spx['spot'].iloc[0] if len(spx) else 'n/a'} | "
            f"critical-field nulls: {nulls or 'none'}")
        scratch.mkdir(parents=True, exist_ok=True)
        for tk, g in df.groupby("ticker"):
            g.to_parquet(scratch / f"{tk}_{d.year}_sample.parquet")
    log(f"VALIDATE OK — schema matches live store ({len(CANON)} cols); "
        f"header eras seen: {len(seen)}; samples in {scratch}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--start", default="2007-01-01")
    ap.add_argument("--end", default="2026-06-30")
    a = ap.parse_args()
    if a.validate:
        validate()
    else:
        run(dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end))


if __name__ == "__main__":
    main()
