"""Nightly mirror of Nasdaq Data Link tables for the watchlist universe.

OFFLINE / EOD ONLY. This runs from the nightly chain (scripts/nightly_ops.sh);
it must NEVER be called from the live 5-minute cycle — see the module docstring
of src/data/nasdaq_data_link.py. Read-only against NDL; the only writes are the
mirror files under data/nasdaq/ and the run receipt.

WHAT IS MIRRORED (dataset names verified live 2026-10-19/10-20 — do not "fix"
NDAQ/RTAT10 to SHARADAR/RTAT10, that 404s as a namespace error):

    SHARADAR/TICKERS   ticker metadata, whole-universe snapshot (no date column)
    SHARADAR/SEP       EOD prices, date-incremental
    SHARADAR/EVENTS    corporate events, date-incremental
    SHARADAR/SF2       insider transactions, date-incremental (filingdate)
    NDAQ/RTAT10        retail activity, date-incremental

DATE RANGE — the design call (autopilot evening 2026-11-18): the first run
mirrors MIRROR_START_DEFAULT (2 years, matching the Track A backtest window)
forward; every later run is incremental from the last date already on disk,
re-fetching a small REFETCH_OVERLAP_DAYS window so late-arriving rows (SF2
filings especially) are picked up rather than lost in a seam. Rows are keyed and
merged, so an overlap cannot duplicate.

PARTIAL PROGRESS IS DELIBERATE: each table is written as soon as it is fetched,
so a timeout or a killed run leaves the tables it finished intact and the next
run resumes from their own last date. One table's failure never aborts another.

CLI:
    .venv/bin/python scripts/nasdaq_mirror.py --dry-run     # plan only, no network
    .venv/bin/python scripts/nasdaq_mirror.py               # incremental mirror
    .venv/bin/python scripts/nasdaq_mirror.py --table SHARADAR/SEP
    .venv/bin/python scripts/nasdaq_mirror.py --full        # ignore on-disk dates
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.nasdaq_data_link import NasdaqDataLink  # noqa: E402

MIRROR_DIR = ROOT / "data" / "nasdaq"
CONFIG_PATH = ROOT / "config" / "config.yaml"

# 2 years back, matching the Track A backtest window in CLAUDE.md.
MIRROR_START_DEFAULT = 730
# Re-fetch this much of the tail every run. SF2 filings and EVENTS can arrive
# days after their nominal date, so a strict "> last_date" seam would drop them
# permanently; merging on a key makes the overlap free.
REFETCH_OVERLAP_DAYS = 7

# Per table: the date column to filter/merge on (None = whole-universe snapshot
# with no date dimension), whether it is fetched per-ticker, and the key columns
# that make a row unique.
TABLES = {
    "SHARADAR/TICKERS": {"date_col": None, "per_ticker": False,
                         "key": ("table", "ticker")},
    "SHARADAR/SEP": {"date_col": "date", "per_ticker": True,
                     "key": ("ticker", "date")},
    "SHARADAR/EVENTS": {"date_col": "date", "per_ticker": True,
                        "key": ("ticker", "date", "eventcodes")},
    "SHARADAR/SF2": {"date_col": "filingdate", "per_ticker": True,
                     "key": ("ticker", "filingdate", "ownername",
                             "transactiondate", "securityadcode")},
    "NDAQ/RTAT10": {"date_col": "date", "per_ticker": True,
                    "key": ("ticker", "date")},
}


def _slug(datatable: str) -> str:
    return datatable.replace("/", "_").lower()


def mirror_path(datatable: str) -> Path:
    return MIRROR_DIR / f"{_slug(datatable)}.jsonl"


def _rel(path: Path) -> str:
    """Repo-relative when it is inside the repo, absolute otherwise. A test
    repoints MIRROR_DIR into tmp, where relative_to(ROOT) would raise."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def watchlist(config_path: Path | None = None) -> list[str]:
    """The watchlist universe from config.yaml, deduped, order preserved.

    Parsed with a tiny reader rather than yaml.safe_load so this script has no
    import-time dependency beyond the client: the default watchlist is a single
    inline flow sequence.
    """
    path = config_path or CONFIG_PATH
    try:
        text = path.read_text()
    except OSError:
        return []
    out: list[str] = []
    in_watchlist = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("watchlist:"):
            in_watchlist = True
            continue
        if in_watchlist:
            # leaving the block: a new top-level key
            if line and not line[0].isspace() and stripped.endswith(":"):
                break
            if stripped.startswith("default:"):
                body = stripped.split(":", 1)[1].strip().strip("[]")
                for part in body.split(","):
                    name = part.strip().strip("'\"")
                    if name and name not in out:
                        out.append(name)
                break
    return out


def _rows_to_dicts(result: dict) -> list[dict]:
    cols = result.get("columns") or []
    return [dict(zip(cols, row)) for row in (result.get("rows") or [])]


def read_mirror(datatable: str) -> list[dict]:
    path = mirror_path(datatable)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn final line from a killed run is skipped, not fatal
    return out


def last_date(rows: list[dict], date_col: str | None) -> str | None:
    if not date_col:
        return None
    dates = [str(r.get(date_col)) for r in rows if r.get(date_col)]
    return max(dates) if dates else None


def _row_key(row: dict, key_cols: tuple) -> tuple:
    return tuple(str(row.get(c, "")) for c in key_cols)


def merge_rows(existing: list[dict], fresh: list[dict], key_cols: tuple) -> list[dict]:
    """Merge fresh rows over existing ones on the key. Fresh wins (a restated
    row replaces its earlier version); order is existing-then-new so the file
    stays append-ish and diffs stay readable."""
    index = {_row_key(r, key_cols): i for i, r in enumerate(existing)}
    merged = list(existing)
    for row in fresh:
        key = _row_key(row, key_cols)
        if key in index:
            merged[index[key]] = row
        else:
            index[key] = len(merged)
            merged.append(row)
    return merged


def write_mirror(datatable: str, rows: list[dict]) -> Path:
    """Write via a temp file + replace, so a kill mid-write cannot corrupt an
    existing mirror (the partial-progress guarantee is per table, not per row)."""
    path = mirror_path(datatable)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    tmp.replace(path)
    return path


def start_date_for(rows: list[dict], date_col: str | None, full: bool,
                   today: dt.date) -> str | None:
    if not date_col:
        return None
    if full or not rows:
        return str(today - dt.timedelta(days=MIRROR_START_DEFAULT))
    seen = last_date(rows, date_col)
    if not seen:
        return str(today - dt.timedelta(days=MIRROR_START_DEFAULT))
    try:
        anchor = dt.date.fromisoformat(seen[:10])
    except ValueError:
        return str(today - dt.timedelta(days=MIRROR_START_DEFAULT))
    return str(anchor - dt.timedelta(days=REFETCH_OVERLAP_DAYS))


def mirror_table(client, datatable: str, tickers: list[str], *, full: bool,
                 today: dt.date) -> dict:
    """Mirror one table. Returns a receipt dict; never raises for a fetch
    failure — the caller records it and moves to the next table."""
    spec = TABLES[datatable]
    date_col, key_cols = spec["date_col"], spec["key"]
    existing = read_mirror(datatable)
    since = start_date_for(existing, date_col, full, today)

    fresh: list[dict] = []
    errors: list[str] = []
    if spec["per_ticker"]:
        targets = tickers
    else:
        targets = [None]
    for ticker in targets:
        filters: dict = {}
        if ticker:
            filters["ticker"] = ticker
        if date_col and since:
            filters[date_col] = {"gte": since}
        result = client.fetch(datatable, **filters)
        if "error" in result:
            errors.append(f"{ticker or 'ALL'}: {result['error']}"[:160])
            continue
        fresh.extend(_rows_to_dicts(result))

    merged = merge_rows(existing, fresh, key_cols)
    added = len(merged) - len(existing)
    path = write_mirror(datatable, merged)
    # A table that errors is a fault; a table that mirrors NOTHING is not
    # necessarily one, but it must never report "ok" — that read cost three
    # nights of a green nightly step standing for an empty SEP/SF2 mirror
    # (2026-11-19/20/23). Probed 2026-11-23: the subscription serves only a
    # 2018 sample window for those two tables (SEP 2018-09-04..2018-12-31),
    # so a 2-year `since` legitimately matches zero rows. Reporting-only:
    # `empty` does NOT fail the step (see main()), by design.
    if errors:
        status = "errors"
    elif not merged:
        status = "empty"
    else:
        status = "ok"
    return {
        "table": datatable, "since": since, "rows_fetched": len(fresh),
        "rows_added": added, "rows_total": len(merged),
        "errors": errors, "file": _rel(path),
        "status": status,
        "ok": status == "ok",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nightly NDL mirror (read-only fetch)")
    ap.add_argument("--table", action="append", choices=sorted(TABLES),
                    help="limit to this table (repeatable); default all")
    ap.add_argument("--full", action="store_true",
                    help="ignore on-disk dates and refetch the full window")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan; makes no network call and writes nothing")
    args = ap.parse_args(argv)

    tables = args.table or list(TABLES)
    tickers = watchlist()
    today = dt.date.today()

    if args.dry_run:
        for name in tables:
            rows = read_mirror(name)
            since = start_date_for(rows, TABLES[name]["date_col"], args.full, today)
            print(f"{name}: on_disk={len(rows)} since={since} "
                  f"tickers={len(tickers) if TABLES[name]['per_ticker'] else 'ALL'}")
        return 0

    client = NasdaqDataLink()
    if not client.available():
        print("SKIP: NASDAQ_DATA_LINK_KEY not set — mirror not attempted")
        return 1
    if not tickers:
        print("SKIP: empty watchlist — refusing to mirror an empty universe")
        return 1

    receipts = [mirror_table(client, name, tickers, full=args.full, today=today)
                for name in tables]
    MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    (MIRROR_DIR / "mirror_receipt.json").write_text(json.dumps(
        {"at": dt.datetime.now().isoformat(timespec="seconds"),
         "tickers": len(tickers), "tables": receipts}, indent=1) + "\n")

    for r in receipts:
        if r["status"] == "errors":
            label = f"ERRORS({len(r['errors'])})"
        elif r["status"] == "empty":
            label = "EMPTY (subscription returns no rows in window)"
        else:
            label = "ok"
        print(f"{r['table']}: +{r['rows_added']} added, {r['rows_total']} total, "
              f"since={r['since']} {label}")
        for err in r["errors"][:3]:
            print(f"    {err}")
    # Exit code keys off ERRORS ONLY. An `empty` table is reported, not failed:
    # making it red would turn the nightly step permanently rc=1 while the
    # subscription stays sample-tier. DO NOT "fix" this to `all(r["ok"])`.
    return 0 if all(not r["errors"] for r in receipts) else 1


if __name__ == "__main__":
    sys.exit(main())
