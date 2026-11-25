"""Full-archive mirror of the ORATS SMV-Strikes S3 bucket to local disk.

Preserves the ENTIRE raw archive (all ~5,000 symbols, every trading day 2007->present,
~188 GB / ~4,900 daily zips) under data/orats_raw/{year}/, so the data survives losing
S3 access. Raw, unfiltered, unprocessed — the faithful copy. Subset processing
(orats_ingest.py / ingest_vrp_inputs.py) can run against this local mirror later.

RESUMABLE + IDEMPOTENT: skips any file already present locally with a matching byte
size; re-run after an interruption to finish. Atomic writes (.tmp -> rename) so a
killed download never leaves a half file that looks complete.

  python src/data/orats_mirror.py                 # mirror everything (resume-safe)
  python src/data/orats_mirror.py --start-year 2007 --end-year 2019
  python src/data/orats_mirror.py --workers 16
"""
import argparse, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.research.ingest_vrp_inputs import _s3   # reuse the verified creds loader

DEST = ROOT / "data" / "orats_raw"
_SINGLE = TransferConfig(use_threads=False)       # one stream per file; our pool sets concurrency


def _list(s3, bucket, start_year, end_year):
    pag = s3.get_paginator("list_objects_v2")
    out = []
    for yr in range(start_year, end_year + 1):
        for page in pag.paginate(Bucket=bucket, Prefix=f"{yr}/"):
            for o in page.get("Contents", []):
                if o["Key"].endswith(".zip"):
                    out.append((o["Key"], o["Size"]))
    return out


def _fetch(s3, bucket, key, size):
    out = DEST / key                               # mirrors {year}/{file}.zip
    if out.exists() and out.stat().st_size == size:
        return ("skip", size)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".zip.tmp")
    for attempt in range(4):
        try:
            s3.download_file(bucket, key, str(tmp), Config=_SINGLE)
            if tmp.stat().st_size == size:
                tmp.rename(out)
                return ("done", size)
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    if tmp.exists():
        tmp.unlink()
    return ("fail", 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=2007)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    s3, bucket = _s3()
    DEST.mkdir(parents=True, exist_ok=True)
    objs = _list(s3, bucket, args.start_year, args.end_year)
    total_bytes = sum(s for _, s in objs)
    print(f"ORATS mirror -> {DEST}", flush=True)
    print(f"  {len(objs)} files, {total_bytes/1e9:.1f} GB, years {args.start_year}-{args.end_year}, "
          f"{args.workers} workers", flush=True)

    done = skipped = failed = 0
    got_bytes = 0
    fails = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_fetch, s3, bucket, k, sz): (k, sz) for k, sz in objs}
        for i, f in enumerate(as_completed(futs), 1):
            status, nbytes = f.result()
            got_bytes += nbytes
            if status == "done": done += 1
            elif status == "skip": skipped += 1
            else: failed += 1; fails.append(futs[f][0])
            if i % 50 == 0 or i == len(objs):
                el = time.time() - t0
                rate = got_bytes / el / 1e6 if el else 0
                print(f"  {i}/{len(objs)}  done={done} skip={skipped} fail={failed}  "
                      f"{got_bytes/1e9:.1f} GB  {rate:.0f} MB/s  {el/60:.1f} min", flush=True)

    print(f"\nDONE: {done} downloaded, {skipped} already present, {failed} failed, "
          f"{got_bytes/1e9:.1f} GB in {(time.time()-t0)/60:.1f} min -> {DEST}", flush=True)
    if fails:
        print(f"FAILED ({len(fails)}) — re-run to retry: {fails[:10]}{' ...' if len(fails)>10 else ''}", flush=True)


if __name__ == "__main__":
    main()
