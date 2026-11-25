"""WIDE-UNIVERSE single-name extraction from the raw ORATS SMV-Strikes archive.

Adapted from src/research/extract_spx.py (which is NOT modified and has its own
state file). Reads the local Google Drive cache of daily full-market zips
(~/.../ORATS_archive/{YYYY}/ORATS_SMV_Strikes_{YYYYMMDD}.zip, 2007-2026),
filters to the 60-name single-name universe below (+2 historical-ticker
aliases), and writes per-ticker per-year parquets to
data/iv_history_full/{TICKER}_{YEAR}.parquet with the EXACT schema of the
existing store, so src/backtest/chain.py reads them transparently.

Purpose: give the confirmed event-vol edge (logs/research/eventvol_oos_20260705.md,
rule-A n=48 on 23 names / ~4.5 tradeable years) enough names and enough years
to reach Track A's >=100-trade gate.

UNIVERSE SELECTION (frozen 2026-07-05):
  Source: data/vrp_universe.csv (484 liquid-options names screened 2026-06;
  columns: ticker, oi, dollar_vol, rel_spread, spot). Rule: the 23 existing
  watchlist names (their 2007-2019 history is missing from the store and is
  force-included), UNION the top single names by option DOLLAR VOLUME after
  excluding (a) ETFs/indexes/funds — anything without earnings events,
  (b) the index complex already extracted today (SPX/XSP/VIX),
  (c) GOOG (duplicate Alphabet share class of GOOGL; scanned separately as an
  alias, see below), (d) BRK_B (raw-archive ticker-format ambiguity),
  until the union reaches 60 names. XOM is in the watchlist but absent from
  vrp_universe.csv; it is force-included anyway.

ALIASES also scanned (rows written under their own archive ticker, NOT
renamed — splicing across ticker changes is a research decision, not an
ingest decision):
  FB   -> META's history before the June-2022 ticker change
  GOOG -> Alphabet's single class pre-April-2014 + class C thereafter

*** SURVIVORSHIP CAVEAT (carry into every study using this data) ***
The universe is selected by TODAY'S (2026-06) option liquidity. Names that
died, were delisted, or were acquired during 2007-2026 are ABSENT from the
selection, so any strategy result on this universe is conditioned on
survival-to-2026 and is biased optimistic to that extent. Names that IPO'd
late simply have no early rows — that part is fine. The event-vol study must
state this caveat in its reports.

DO-NOT-TOUCH RULE: for the 23 names already in the store (2020+ coverage from
the earlier ORATS ingest), (ticker, year) parquets with year >= 2020 are NEVER
written — their rows are dropped at buffer time. This job only CREATES new
files; it never rewrites an existing one.

Era note (verified by extract_spx.py against real 2007/2015/2020/2022/2026
files): 2007..~2021 header has 37 cols; ~2022.. adds cOpra/pOpra (39 cols).
Extraction is BY HEADER NAME; columns absent in an era are emitted as null and
logged once per header signature.

macOS TCC quirk: directory listing on the Drive mount is blocked from the
shell, but open/read on exact known paths works — so we iterate every
candidate weekday and skip quietly on FileNotFoundError (holidays).

RESUMABLE/IDEMPOTENT: completed dates tracked in
logs/research/universe_extract_state.jsonl (appended only AFTER their rows are
durably flushed); parquet writes merge+dedup on
trade_date+expiration+strike+ticker and are atomic (tmp + os.replace).
Memory guard: with ~60 names the daily row volume is ~15x the SPX run, so
buffers are flushed whenever the buffered row count exceeds MAX_BUFFER_ROWS,
not just every 100 files / year boundary.

COORDINATION: --wait-pid <PID> polls kill(pid, 0) every 30s and starts only
after that process exits, so two extractors never hammer the archive zips
simultaneously (used to wait out the SPX extractor, pid 44109 on 2026-07-05).

Usage:
  python src/research/extract_universe.py --validate      # 3 era samples -> scratch dir
  python src/research/extract_universe.py --start 2007-01-01 --end 2026-06-30 [--wait-pid 44109]
  (resume = just re-run the same command; state file is honored)

STRICT OWNERSHIP: this module only CREATES new {TICKER}_{YEAR} parquets and
writes logs/research/universe_extract_state.jsonl / universe_extract.log.
It never modifies extract_spx.py, its state, or any existing file.
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
STATE = LOG_DIR / "universe_extract_state.jsonl"
LOG = LOG_DIR / "universe_extract.log"

# The 23 names already in the store with 2020+ coverage: extract 2007-2019
# only; never write their year>=2020 parquets (see DO-NOT-TOUCH RULE above).
STORE23 = ("AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "META", "AMD", "GOOGL",
           "NFLX", "JPM", "XOM", "COST", "CRM", "PYPL", "MARA", "SMCI",
           "PLTR", "BAC", "UBER", "WMT", "HOOD", "BABA", "INTC")
STORE_CUTOFF_YEAR = 2020  # first store-covered year for STORE23

# 37 new names: top-of-book by dollar_vol from vrp_universe.csv after
# exclusions (see docstring).
NEW37 = ("ADBE", "APP", "ARM", "ASML", "ASTS", "AVGO", "BA", "BIDU", "BKNG",
         "CAT", "COIN", "CRWD", "CRWV", "CVNA", "DELL", "DUOL", "FDX", "FICO",
         "FSLR", "GEV", "GS", "IBM", "LLY", "LRCX", "LULU", "MSTR", "MU",
         "NKE", "ORCL", "OXY", "PDD", "SOFI", "TSM", "UAL", "UNH", "V", "WFC")

ALIASES = ("FB", "GOOG")  # historical tickers, written under their own name

TICKERS = tuple(sorted(set(STORE23) | set(NEW37) | set(ALIASES)))  # 62 scan names
PREFIXES = tuple(f"{t},".encode() for t in TICKERS)

MAX_BUFFER_ROWS = 2_000_000  # flush threshold (~0.7 GB of buffered frames)

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
    buffered_rows = 0
    pending: list[dict] = []
    counts: dict[str, int] = {}
    n_files = n_missing = n_skipped = 0
    cur_year = None
    t0 = time.time()
    log(f"START universe extract {start}..{end}  ({len(TICKERS)} scan tickers; "
        f"{len(done)} dates already done)")

    for d in weekdays(start, end):
        iso = d.isoformat()
        if iso in done:
            continue
        if cur_year is not None and d.year != cur_year:
            flush(buffers, pending, counts)
            buffered_rows = 0
            yr_counts = {k: v for k, v in counts.items() if k.endswith(str(cur_year))}
            log(f"YEAR {cur_year} complete: {len(yr_counts)} ticker-files, "
                f"{sum(yr_counts.values())} rows")
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
        day_rows = 0
        for tk, g in df.groupby("ticker"):
            # DO-NOT-TOUCH RULE: store-covered (ticker, year>=2020) never written
            if tk in STORE23 and d.year >= STORE_CUTOFF_YEAR:
                continue
            buffers.setdefault((tk, d.year), []).append(g)
            day_rows += len(g)
        buffered_rows += day_rows
        if state_enabled:
            pending.append({"date": iso, "rows": day_rows,
                            "tickers": int(df["ticker"].nunique())})
        if buffered_rows >= MAX_BUFFER_ROWS or n_files % 100 == 0:
            rate = n_files / (time.time() - t0)
            log(f"progress: {n_files} files (@{iso}), {n_missing} missing/holiday, "
                f"{n_skipped} skipped, {rate:.1f} files/s, flushing {buffered_rows} rows")
            flush(buffers, pending, counts)
            buffered_rows = 0

    flush(buffers, pending, counts)
    elapsed = time.time() - t0
    log(f"DONE: {n_files} files in {elapsed/60:.1f} min, {n_missing} missing "
        f"(weekends-adjacent holidays), {n_skipped} skipped/corrupt")
    log(f"per-year row counts: {json.dumps(counts, sort_keys=True)}")
    absent = [t for t in TICKERS if not any(k.startswith(t + "_") for k in counts)]
    if absent and n_files:
        log(f"NOTE: no rows found this run for tickers: {absent} "
            f"(expected for late IPOs / store-covered years)")
    return {"files": n_files, "missing": n_missing, "skipped": n_skipped,
            "counts": counts, "headers": seen_headers}


def validate() -> None:
    """Run three era samples into a scratch dir and diff schema vs the live
    store (SPY_2024.parquet). Writes nothing under data/. Also reports RSS so
    the 15x-SPX row volume can be sanity-checked before the full run."""
    import resource
    scratch = LOG_DIR / "universe_validate_out"
    samples = [dt.date(2007, 1, 3), dt.date(2015, 1, 5), dt.date(2026, 6, 15)]
    spot_check = ["AAPL", "GS", "ORCL", "MU", "CAT"]
    seen: dict[str, list[str]] = {}
    ref = pd.read_parquet(ROOT / "data" / "iv_history_full" / "SPY_2024.parquet")
    for d in samples:
        t0 = time.time()
        df = extract_day(d, seen)
        assert df is not None and len(df), f"{d}: no rows"
        assert list(df.columns) == list(ref.columns) == CANON, "column order mismatch"
        for c in FLOAT_COLS:
            assert df[c].dtype == "float64", f"{c} dtype {df[c].dtype}"
        crit = ["spot", "strike", "expiration", "trade_date", "cBidPx", "cAskPx",
                "pBidPx", "pAskPx", "call_iv", "put_iv", "delta", "gamma",
                "theta", "vega"]
        nulls = {c: int(df[c].isna().sum()) for c in crit if df[c].isna().any()}
        vc = df["ticker"].value_counts().to_dict()
        checks = {t: vc.get(t, 0) for t in spot_check}
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9
        log(f"VALIDATE {d}: {len(df)} rows across {len(vc)}/{len(TICKERS)} tickers "
            f"in {time.time()-t0:.2f}s | spot-check {checks} | "
            f"critical-field nulls: {nulls or 'none'} | peak RSS {rss:.2f} GB")
        absent = sorted(set(TICKERS) - set(vc))
        log(f"VALIDATE {d}: tickers absent that day: {absent or 'none'}")
        scratch.mkdir(parents=True, exist_ok=True)
        for tk, g in df.groupby("ticker"):
            g.to_parquet(scratch / f"{tk}_{d.year}_sample.parquet")
    log(f"VALIDATE OK — schema matches live store ({len(CANON)} cols); "
        f"header eras seen: {len(seen)}; samples in {scratch}")


def wait_for_pid(pid: int) -> None:
    """Block until `pid` exits (poll every 30s) so two extractors never read
    the archive simultaneously."""
    waited = 0
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            if waited:
                log(f"pid {pid} exited after {waited}s wait — starting")
            else:
                log(f"pid {pid} not running — starting immediately")
            return
        except PermissionError:
            pass  # exists but not ours — keep waiting
        if waited == 0:
            log(f"waiting for pid {pid} to exit (poll 30s)...")
        time.sleep(30)
        waited += 30


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--start", default="2007-01-01")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--wait-pid", type=int, default=None,
                    help="wait for this PID to exit before starting")
    a = ap.parse_args()
    if a.validate:
        validate()
        return
    if a.wait_pid:
        wait_for_pid(a.wait_pid)
    run(dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end))


if __name__ == "__main__":
    main()
