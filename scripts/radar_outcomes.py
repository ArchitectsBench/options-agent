"""Buy-zone radar OUTCOME WRITER (2026-09-13) — logging only.

Nightly (scripts/nightly_ops.sh step `radar_outcomes`, AFTER `buyzone_radar`):
for every state instance the radar opened (a `zone_entered` event, or a
`fired` event when no `zone_entered` exists), compute what the underlying did
afterwards and append one row per (state_instance_id, horizon) to
logs/alerts/outcomes.jsonl. The radar's state machine, zone rules, admission,
invalidation and notification rules are NOT touched or consulted here — this
is a forward-test ledger, read by scripts/radar_m5_query.py and the cockpit's
`outcomes recorded: N` stamp.

Every row carries the stamp
    "exploratory, close-to-close, no costs — NO EDGE CLAIM"
because that is exactly what it is.

EXACT DEFINITIONS
-----------------
sources     hot logs/alerts/events.jsonl + every logs/alerts/archive/*.jsonl.
            Rows are excluded (never counted) when they are the synthetic
            fixture — `schema != "wl_event_v1"`, ticker "OLD", or an
            instance id starting "OLD-x" — or carry no state_instance_id.
instance    all rows sharing a state_instance_id. `fired` /
            `invalidated_by_machine` are True when the instance had a
            `fired` / `invalidated` event in the log AS OF `computed_at` —
            a +3 row written three sessions after entry cannot know about a
            fire on session 6, and rows are never rewritten. They are a
            snapshot, not lifetime facts: readers that need lifetime fire /
            invalidation rates re-derive them from the event log
            (scripts/radar_m5_query.py does, via `instances(events)`).
read lock   the radar's rotate_events truncates and rewrites events.jsonl;
            a read landing inside that window could see an instance's later
            rows without its zone_entered row and anchor it — permanently —
            on `fired`. So the event files are read under a SHARED
            non-blocking flock on the radar's own run lock
            (data/watchlist_squeeze.lock), held only for the milliseconds of
            the read: a running radar (exclusive lock) makes this run skip
            with exit 0 (retried next run); the radar in turn skips only if
            its own attempt lands inside that read window.
anchor      the instance's earliest `zone_entered` row by (data_asof,
            detected_at); if none, its earliest `fired` row. Re-entries
            (re_entered=true) never move the anchor. Everything below is
            measured from the anchor's `data_asof`.
completed   `completed` / `terminal`: the instance has a terminal event —
            `invalidated` or `removed` (`terminal` = the earliest one). This
            is the pre-registered M5 population's "zone_entered -> terminal
            event" (docs/watchlist_known_asymmetries.md); the M5 query reads
            it from here, not from the outcome rows.
writer lock the ledger has its own EXCLUSIVE non-blocking flock
            (<outcomes>.lock beside it), held from before `existing_keys()`
            through the append: two overlapping writers (a manual run inside
            the nightly step) would otherwise both read the same key set and
            both append the same rows. The second writer skips with exit 0
            (`skipped_run: outcomes_lock_held`) and is retried next run.
direction   +1 LONG / -1 SHORT from the anchor's `zone_dir`, else `setup_dir`,
            else `fire_direction` (`direction_source` records which). An
            instance with none of the three gets no rows.
sessions    the sorted stems of data/research_orb/grouped/*.parquet (".empty"
            days are not sessions). `entry_session` = data_asof[:10].
            Forward session k = the k-th stem strictly after entry_session.
            Horizon N (3, 5, 10) is MATURED only when >= N forward sessions
            exist AND the ticker has a close at every one of them; an
            unmatured (instance, horizon) gets no row tonight and is retried
            on the next run.
entry_close the close of the instance's OWN timeframe bar at data_asof, read
            from the store the state machine read (the event rows do not
            carry a close):
              1d  grouped/<entry_session>.parquet close for the ticker
                  (entry_close_source "1d_store");
              1h  bars5m/<ticker>.parquet resampled to 1h exactly as
                  watch_buyzone.tf_ohlc does (left-labelled bins, so the
                  15:00 label is the 15:00-15:55 bin), truncated to labels
                  <= data_asof, last close (entry_close_source "1h_bars5m").
            `entry_session_close` (the daily close of entry_session) is
            recorded next to it so the 1h-vs-daily gap is visible.
ema21/atr14 recomputed from the lane's own bars ending at data_asof, with
            the state machine's construction (EMA21 = ewm span 21 adjust=False;
            ATR14 = ewm alpha 1/14 adjust=False of the true range, first bar's
            prev_close = its own close), over the last LOOKBACK bars (the
            adjust=False seed is weightless long before). The recorded
            `mean_level` and `dist_atr` are copied into the row beside the
            recomputed values (`ema21_abs_diff`, `dist_atr_recomputed`) so a
            reconstruction that drifts from what the radar saw is visible.
            Known drift (dry-run 2026-09-13 over all 79 evaluable real
            instances): 1h lane max relative EMA21 diff 1.6e-5; 1d lane
            exact except anchors dated 2026-07-28 / 2026-08-03, where the
            grouped files for 07-27, 07-30 and 07-31 were rewritten by a
            store backfill on 08-03 AFTER the radar had read them (max
            relative diff 0.74%, FTAI). The row keeps both numbers; the
            store as it stands today is the reference.
direction_conflict  True when the anchor's zone_dir and setup_dir are both
            set and differ (RDDT 2026-07-23: PULLBACK-SHORT inside a long
            zone). The frozen level follows `direction` (zone_dir first) —
            the state machine's own trailing level follows setup_dir, so
            `inval_dist_atr_recorded` and `_recomputed` differ by exactly
            2*stop_atr on such instances. Stated, not hidden.
frozen level  inval_level_frozen = ema21 - stop_atr*atr14 (LONG) or
            ema21 + stop_atr*atr14 (SHORT), stop_atr from
            config/squeeze_watchlist.yaml::invalidation.stop_atr (1.5),
            FROZEN at the anchor — NOT the trailing level the state machine
            re-derives every run. `breached_by_close` at horizon N is True
            when any forward DAILY close k in 1..N satisfies
            direction*(close_k - level) < 0; `breach_session_index` is the
            first such k. Closes only: no gap-through/open test, no intrabar
            touch. For the 1h lane the level is 1h-scale but the test uses
            daily closes (1h forward bars are not durable in the store) — an
            UNDER-count of intrabar breaches, stated here on purpose.
moves       signed_move_pct = direction*(close_N/entry_close - 1)*100
            signed_move_atr = direction*(close_N - entry_close)/atr14
            win = signed_move_pct > 0. `signed_move_atr` is in the LANE's ATR
            (1h ATR << 1d ATR); use `signed_move_pct` to pool lanes.
mae/mfe     over forward closes k=1..N, m_k = direction*(close_k -
            entry_close): mfe_atr = max(0, max_k m_k)/atr14,
            mae_atr = max(0, -min_k m_k)/atr14 (non-negative magnitudes);
            *_pct likewise against entry_close.
idempotent  a (state_instance_id, horizon) key already present in
            outcomes.jsonl is skipped; nothing is ever rewritten. A row
            written under this definition persists — change the definition,
            bump SCHEMA, never edit rows in place.

Run: .venv/bin/python scripts/radar_outcomes.py [--events-hot F] [--archive-dir D]
     [--outcomes F] [--grouped D] [--bars5m D] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
EVENTS_HOT = ROOT / "logs" / "alerts" / "events.jsonl"
ARCHIVE_DIR = ROOT / "logs" / "alerts" / "archive"
OUTCOMES = ROOT / "logs" / "alerts" / "outcomes.jsonl"
GROUPED = ROOT / "data" / "research_orb" / "grouped"
BARS5M = ROOT / "data" / "research_orb" / "bars5m"
CFG_PATH = ROOT / "config" / "squeeze_watchlist.yaml"
# The radar's run lock (watch_buyzone.LOCK) — shared on purpose, see "read lock".
LOCK = ROOT / "data" / "watchlist_squeeze.lock"

SCHEMA = "wl_outcome_v1"
STAMP = "exploratory, close-to-close, no costs — NO EDGE CLAIM"
HORIZONS = (3, 5, 10)
LOOKBACK = 300            # bars of lane history before data_asof for EMA21/ATR14
MIN_BARS = 55             # the state machine's own minimum (eval_tf / tf_ohlc)
FIXTURE_TICKERS = {"OLD"}
FIXTURE_ID_PREFIX = "OLD-x"
ANCHOR_TRANSITIONS = ("zone_entered", "fired")
TERMINAL_TRANSITIONS = ("invalidated", "removed")


# ── config ──────────────────────────────────────────────────────────────────

def stop_atr(cfg_path: Path | None = None) -> float:
    """invalidation.stop_atr from config/squeeze_watchlist.yaml (1.5); read
    lazily so importing this module has no config or env side effects."""
    p = Path(cfg_path) if cfg_path is not None else CFG_PATH
    try:
        import yaml
        return float(yaml.safe_load(p.read_text())["invalidation"]["stop_atr"])
    except Exception:
        return 1.5


# ── read lock ───────────────────────────────────────────────────────────────

def acquire_read_lock(lock_path: Path | None = None):
    """SHARED non-blocking flock on the radar's run lock: returns the open
    handle, or None when a radar run holds it exclusively."""
    import fcntl
    p = Path(lock_path) if lock_path is not None else LOCK
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = p.open("a")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def release_read_lock(fh) -> None:
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def writer_lock_path(outcomes: Path) -> Path:
    """The ledger-specific writer lock, <outcomes>.lock beside the ledger —
    derived at call time so a redirected ledger (tests, --outcomes) carries
    its lock with it and production's lives under logs/alerts/."""
    outcomes = Path(outcomes)
    return outcomes.with_name(outcomes.name + ".lock")


def acquire_writer_lock(outcomes: Path):
    """EXCLUSIVE non-blocking flock on the ledger's own lock (see "writer
    lock"): returns the open handle, or None when another writer holds it."""
    import fcntl
    p = writer_lock_path(outcomes)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = p.open("a")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


release_writer_lock = release_read_lock


# ── events → instances ──────────────────────────────────────────────────────

def is_fixture(row: dict) -> bool:
    """The synthetic fixture (OLD / OLD-x, no schema stamp) is never counted."""
    if row.get("schema") != "wl_event_v1":
        return True
    if str(row.get("ticker", "")) in FIXTURE_TICKERS:
        return True
    return str(row.get("state_instance_id", "")).startswith(FIXTURE_ID_PREFIX)


def load_events(events_hot: Path | None = None, archive_dir: Path | None = None
                ) -> tuple[list[dict], dict]:
    """All wl_event_v1 rows from archive + hot, fixture rows excluded.
    Returns (rows, counts) where counts reports what was excluded."""
    hot = Path(events_hot) if events_hot is not None else EVENTS_HOT
    adir = Path(archive_dir) if archive_dir is not None else ARCHIVE_DIR
    files = sorted(adir.glob("*.jsonl")) if adir.exists() else []
    if hot.exists():
        files.append(hot)
    rows, counts = [], {"files": [str(f) for f in files], "read": 0,
                        "fixture_excluded": 0, "no_instance_id": 0, "bad_json": 0}
    for f in files:
        for ln in f.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
                if not isinstance(r, dict):
                    raise ValueError("non-dict jsonl line")
            except (json.JSONDecodeError, ValueError):
                counts["bad_json"] += 1
                continue
            counts["read"] += 1
            if is_fixture(r):
                counts["fixture_excluded"] += 1
                continue
            if not r.get("state_instance_id"):
                counts["no_instance_id"] += 1
                continue
            rows.append(r)
    return rows, counts


def _sort_key(r: dict) -> tuple:
    return (str(r.get("data_asof", "")), str(r.get("detected_at", "")))


def instances(events: list[dict]) -> dict[str, dict]:
    """state_instance_id -> {anchor, fired, invalidated_by_machine, ...}.
    Instances with neither a zone_entered nor a fired event get no entry."""
    by_id: dict[str, list[dict]] = {}
    for r in events:
        by_id.setdefault(r["state_instance_id"], []).append(r)
    out = {}
    for iid, rows in by_id.items():
        anchor = None
        for tr in ANCHOR_TRANSITIONS:
            cands = sorted((r for r in rows if r.get("transition") == tr), key=_sort_key)
            if cands:
                anchor = cands[0]
                break
        if anchor is None:
            continue
        transitions = {r.get("transition") for r in rows}
        ordered = sorted(rows, key=_sort_key)
        fire_dir = next((r.get("fire_direction") for r in ordered
                         if r.get("transition") == "fired" and r.get("fire_direction")), None)
        terminal = next((r.get("transition") for r in ordered
                         if r.get("transition") in TERMINAL_TRANSITIONS), None)
        direction, source = None, None
        for key in ("zone_dir", "setup_dir"):
            if anchor.get(key) in ("long", "short"):
                direction, source = anchor[key], key
                break
        if direction is None and fire_dir in ("long", "short"):
            direction, source = fire_dir, "fire_direction"
        out[iid] = {"state_instance_id": iid, "anchor": anchor,
                    "ticker": anchor.get("ticker"), "timeframe": anchor.get("timeframe"),
                    "entry_asof": str(anchor.get("data_asof")),
                    "entry_session": str(anchor.get("data_asof"))[:10],
                    "direction": direction, "direction_source": source,
                    "fired": "fired" in transitions,
                    "invalidated_by_machine": "invalidated" in transitions,
                    "completed": terminal is not None, "terminal": terminal,
                    "fire_direction": fire_dir}
    return out


# ── store readers ───────────────────────────────────────────────────────────

def session_dates(grouped: Path | None = None) -> list[str]:
    g = Path(grouped) if grouped is not None else GROUPED
    return sorted(f.stem for f in g.glob("*.parquet"))


def load_daily(grouped: Path, tickers: set[str], dates: list[str]) -> dict[str, pd.DataFrame]:
    """ticker -> DataFrame(date, open, high, low, close) sorted by date, for
    the given session dates only (bounded read)."""
    frames = []
    cols = ["ticker", "open", "high", "low", "close"]
    for d in dates:
        f = Path(grouped) / f"{d}.parquet"
        if not f.exists():
            continue
        try:
            df = pd.read_parquet(f, columns=cols, filters=[("ticker", "in", sorted(tickers))])
        except Exception:
            df = pd.read_parquet(f, columns=cols)
            df = df[df["ticker"].isin(tickers)]
        if len(df):
            df = df.assign(date=d)
            frames.append(df)
    if not frames:
        return {}
    all_ = pd.concat(frames, ignore_index=True).sort_values(["ticker", "date"])
    return {t: g.reset_index(drop=True) for t, g in all_.groupby("ticker")}


def hourly_bars(bars5m: Path, ticker: str) -> pd.DataFrame | None:
    """1h OHLC from the 5m store, the same construction as
    watch_buyzone.tf_ohlc (resample first, drop empty bins, then reindex the
    open/high/low onto the close's bins). Index = left-labelled bin start."""
    f = Path(bars5m) / f"{ticker}.parquet"
    if not f.exists():
        return None
    b0 = pd.read_parquet(f)
    if "date" not in b0.columns or len(b0) == 0:
        return None
    b0 = b0.assign(dt=pd.to_datetime(b0["date"].astype(str) + " " + b0["hm"].astype(str)))
    b = b0.set_index("dt").sort_index()
    cc = b["c"].resample("1h").last().dropna()
    if len(cc) == 0:
        return None
    idx = cc.index
    return pd.DataFrame({
        "o": b["o"].resample("1h").first().reindex(idx).to_numpy(float),
        "h": b["h"].resample("1h").max().reindex(idx).to_numpy(float),
        "l": b["l"].resample("1h").min().reindex(idx).to_numpy(float),
        "c": cc.to_numpy(float)}, index=idx)


# ── math ────────────────────────────────────────────────────────────────────

def ema_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> tuple[float, float]:
    """(EMA21, ATR14) at the last bar — the state machine's construction."""
    s = pd.Series(c)
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = float(pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    ema21 = float(s.ewm(span=21, adjust=False).mean().iloc[-1])
    return ema21, atr


def entry_context(inst: dict, daily: dict[str, pd.DataFrame], bars5m: Path,
                  stop: float) -> tuple[dict | None, str | None]:
    """Lane-own entry close + frozen EMA21/ATR14/level at the anchor.
    Returns (ctx, None) or (None, skip_reason)."""
    t, tf, asof = inst["ticker"], inst["timeframe"], inst["entry_asof"]
    sess = inst["entry_session"]
    g = daily.get(t)
    sess_close = None
    if g is not None:
        row = g[g["date"] == sess]
        if len(row):
            sess_close = float(row["close"].iloc[-1])
    if tf == "1d":
        if g is None or sess_close is None:
            return None, "1d_no_store_row_at_asof"
        w = g[g["date"] <= sess].tail(LOOKBACK)
        if len(w) < MIN_BARS:
            return None, "1d_history_short"
        h, l, c = (w[k].to_numpy(float) for k in ("high", "low", "close"))
        entry_close, source = float(c[-1]), "1d_store"
    elif tf == "1h":
        hb = hourly_bars(bars5m, t)
        if hb is None:
            return None, "1h_no_bars5m"
        try:
            ts = pd.Timestamp(asof)
        except Exception:
            return None, "1h_bad_asof"
        w = hb[hb.index <= ts].tail(LOOKBACK)
        if len(w) < MIN_BARS:
            return None, "1h_history_short"
        if str(w.index[-1]) != str(ts):
            return None, "1h_no_bar_at_asof"
        h, l, c = (w[k].to_numpy(float) for k in ("h", "l", "c"))
        entry_close, source = float(c[-1]), "1h_bars5m"
    else:
        return None, f"unsupported_timeframe:{tf}"
    ema21, atr = ema_atr(h, l, c)
    if not (atr > 0) or not (entry_close > 0):
        return None, "degenerate_atr_or_price"
    d = 1 if inst["direction"] == "long" else -1
    level = ema21 - d * stop * atr
    a = inst["anchor"]
    rec_mean = a.get("mean_level")
    return {"entry_close": entry_close, "entry_close_source": source,
            "entry_session_close": sess_close,
            "ema21_entry": round(ema21, 4), "atr14_entry": round(atr, 4),
            "stop_atr": stop, "inval_level_frozen": round(level, 4),
            "mean_level_recorded": rec_mean,
            "ema21_abs_diff": (round(abs(ema21 - float(rec_mean)), 4)
                               if isinstance(rec_mean, (int, float)) else None),
            "dist_atr_recorded": a.get("dist_atr"),
            "dist_atr_recomputed": round(abs(entry_close - ema21) / atr, 2),
            "inval_dist_atr_recorded": a.get("invalidation_level_atr"),
            "inval_dist_atr_recomputed": round((entry_close - level) / atr, 2)}, None


def outcome_row(inst: dict, ctx: dict, horizon: int, fwd: list[tuple[str, float]],
                computed_at: str) -> dict:
    """One wl_outcome_v1 row. `fwd` = [(session, close)] for k=1..horizon."""
    d = 1 if inst["direction"] == "long" else -1
    entry, atr, level = ctx["entry_close"], ctx["atr14_entry"], ctx["inval_level_frozen"]
    closes = np.array([c for _, c in fwd], dtype=float)
    m = d * (closes - entry)
    breach_idx = next((k + 1 for k, c in enumerate(closes) if d * (c - level) < 0), None)
    close_h = float(closes[-1])
    a = inst["anchor"]
    return {
        "schema": SCHEMA, "stamp": STAMP, "manual_decision_support": True,
        "computed_at": computed_at,
        "state_instance_id": inst["state_instance_id"], "ticker": inst["ticker"],
        "timeframe": inst["timeframe"], "direction": inst["direction"],
        "direction_source": inst["direction_source"],
        "zone_dir": a.get("zone_dir"), "setup_dir": a.get("setup_dir"),
        "direction_conflict": bool(a.get("zone_dir") and a.get("setup_dir")
                                   and a.get("zone_dir") != a.get("setup_dir")),
        "anchor_transition": a.get("transition"), "anchor_state": a.get("state"),
        "anchor_detected_at": a.get("detected_at"),
        "entry_asof": inst["entry_asof"], "entry_session": inst["entry_session"],
        **{k: ctx[k] for k in ("entry_close", "entry_close_source", "entry_session_close",
                               "ema21_entry", "atr14_entry", "stop_atr", "inval_level_frozen",
                               "mean_level_recorded", "ema21_abs_diff", "dist_atr_recorded",
                               "dist_atr_recomputed", "inval_dist_atr_recorded",
                               "inval_dist_atr_recomputed")},
        "horizon": int(horizon), "horizon_session": fwd[-1][0],
        "close_at_horizon": close_h,
        "signed_move_pct": round(d * (close_h / entry - 1) * 100, 4),
        "signed_move_atr": round(d * (close_h - entry) / atr, 4),
        "win": bool(d * (close_h - entry) > 0),
        "breached_by_close": breach_idx is not None,
        "breach_session_index": breach_idx,
        "breach_session_date": fwd[breach_idx - 1][0] if breach_idx else None,
        "mae_atr": round(max(0.0, -float(m.min())) / atr, 4),
        "mfe_atr": round(max(0.0, float(m.max())) / atr, 4),
        "mae_pct": round(max(0.0, -float(m.min())) / entry * 100, 4),
        "mfe_pct": round(max(0.0, float(m.max())) / entry * 100, 4),
        "fired": inst["fired"], "invalidated_by_machine": inst["invalidated_by_machine"],
        "fire_direction": inst["fire_direction"],
        "bias_d": a.get("bias_d"), "bias_w": a.get("bias_w"), "bias_m": a.get("bias_m"),
        "counter_trend": a.get("counter_trend"), "mtf": a.get("mtf"),
        "re_entered": a.get("re_entered"), "rvol_at_add": a.get("rvol_at_add"),
        "e_status": a.get("e_status"),
    }


# ── ledger ──────────────────────────────────────────────────────────────────

def existing_keys(outcomes: Path) -> set[tuple[str, int]]:
    keys = set()
    if not outcomes.exists():
        return keys
    for ln in outcomes.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
            keys.add((r["state_instance_id"], int(r["horizon"])))
        except Exception:
            continue
    return keys


def write_outcomes(events_hot: Path | None = None, archive_dir: Path | None = None,
                   outcomes: Path | None = None, grouped: Path | None = None,
                   bars5m: Path | None = None, cfg_path: Path | None = None,
                   horizons: tuple[int, ...] = HORIZONS, dry_run: bool = False,
                   now: datetime | None = None, lock_path: Path | None = None) -> dict:
    """Append every matured, not-yet-written (instance, horizon) row. Returns
    a summary dict (also printed by main). Never rewrites an existing row.
    Skips the whole run (summary["skipped_run"]) when a radar run holds the
    run lock ("radar_lock_held") or another writer holds the ledger's own
    lock ("outcomes_lock_held") — nothing is read or written then."""
    out_path = Path(outcomes) if outcomes is not None else OUTCOMES
    computed_at = (now or datetime.now()).isoformat(timespec="seconds")

    def skipped(why):
        return {"schema": "wl_outcomes_run_v1", "computed_at": computed_at, "stamp": STAMP,
                "skipped_run": why, "written": 0, "outcomes_path": str(out_path)}

    wfh = acquire_writer_lock(out_path)
    if wfh is None:
        return skipped("outcomes_lock_held")
    try:
        return _write_outcomes_locked(events_hot, archive_dir, out_path, grouped, bars5m,
                                      cfg_path, horizons, dry_run, computed_at, lock_path,
                                      skipped)
    finally:
        release_writer_lock(wfh)


def _write_outcomes_locked(events_hot, archive_dir, out_path: Path, grouped, bars5m, cfg_path,
                           horizons, dry_run, computed_at: str, lock_path, skipped) -> dict:
    """The body of write_outcomes, run under the ledger's writer lock."""
    grouped = Path(grouped) if grouped is not None else GROUPED
    bars5m = Path(bars5m) if bars5m is not None else BARS5M
    stop = stop_atr(cfg_path)
    fh = acquire_read_lock(lock_path)
    if fh is None:
        return skipped("radar_lock_held")
    try:
        events, counts = load_events(events_hot, archive_dir)
    finally:
        release_read_lock(fh)
    insts = instances(events)
    dates = session_dates(grouped)
    have = existing_keys(out_path)
    summary = {"schema": "wl_outcomes_run_v1", "computed_at": computed_at, "stamp": STAMP,
               "events": counts, "instances": len(insts), "already_written": len(have),
               "written": 0, "skipped": {}, "no_direction": 0, "unmatured": 0,
               "pending_instances": 0, "outcomes_path": str(out_path), "dry_run": dry_run}

    def skip(reason):
        summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1

    # which instances still owe a row that could be matured tonight?
    pending, start_i, end_i = [], None, None
    for inst in insts.values():
        if inst["direction"] is None:
            summary["no_direction"] += 1
            continue
        owed = [h for h in horizons if (inst["state_instance_id"], h) not in have]
        if not owed:
            continue
        n_fwd = sum(1 for s in dates if s > inst["entry_session"])
        if n_fwd < min(owed):
            summary["unmatured"] += 1
            continue
        inst["owed"] = owed
        pending.append(inst)
        i = np.searchsorted(dates, inst["entry_session"], side="right")   # first fwd index
        lo, hi = max(0, i - LOOKBACK - 1), min(len(dates), i + max(horizons))
        start_i = lo if start_i is None else min(start_i, lo)
        end_i = hi if end_i is None else max(end_i, hi)
    summary["pending_instances"] = len(pending)
    if not pending:
        return summary
    daily = load_daily(grouped, {p["ticker"] for p in pending}, dates[start_i:end_i])
    new_rows = []
    for inst in pending:
        ctx, reason = entry_context(inst, daily, bars5m, stop)
        if ctx is None:
            skip(reason)
            continue
        g = daily.get(inst["ticker"])
        fwd_sessions = [s for s in dates if s > inst["entry_session"]]
        closes = dict(zip(g["date"], g["close"])) if g is not None else {}
        for h in inst["owed"]:
            if len(fwd_sessions) < h:
                summary["unmatured"] += 1
                continue
            path = [(s, closes.get(s)) for s in fwd_sessions[:h]]
            if any(c is None or not np.isfinite(c) for _, c in path):
                skip("missing_forward_bar")
                continue
            new_rows.append(outcome_row(inst, ctx, h, [(s, float(c)) for s, c in path], computed_at))
    summary["written"] = len(new_rows)
    if new_rows and not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a") as f:
            for r in new_rows:
                f.write(json.dumps(r) + "\n")
    summary["rows"] = new_rows if dry_run else None
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--events-hot", type=Path, default=None)
    ap.add_argument("--archive-dir", type=Path, default=None)
    ap.add_argument("--outcomes", type=Path, default=None)
    ap.add_argument("--grouped", type=Path, default=None)
    ap.add_argument("--bars5m", type=Path, default=None)
    ap.add_argument("--cfg", type=Path, default=None)
    ap.add_argument("--lock", type=Path, default=None, help="radar run lock to share")
    ap.add_argument("--dry-run", action="store_true", help="compute, print, write nothing")
    a = ap.parse_args(argv)
    s = write_outcomes(a.events_hot, a.archive_dir, a.outcomes, a.grouped, a.bars5m,
                       a.cfg, dry_run=a.dry_run, lock_path=a.lock)
    if s.get("skipped_run"):
        why = {"radar_lock_held": "radar run in progress (run lock held)",
               "outcomes_lock_held": "another outcome writer holds the ledger lock"
               }.get(s["skipped_run"], s["skipped_run"])
        print(f"[radar-outcomes] {s['computed_at']} {why} — "
              f"skipped, nothing read or written; retried next run | {STAMP}")
        return 0
    rows = s.pop("rows", None)
    print(f"[radar-outcomes] {s['computed_at']} instances={s['instances']} "
          f"pending={s['pending_instances']} written={s['written']} "
          f"already={s['already_written']} unmatured={s['unmatured']} "
          f"no_direction={s['no_direction']} skipped={s['skipped']} "
          f"fixture_excluded={s['events']['fixture_excluded']} | {STAMP}")
    if a.dry_run and rows:
        for r in rows:
            print(json.dumps(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
