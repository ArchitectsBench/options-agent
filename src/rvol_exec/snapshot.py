"""Polygon whole-market snapshot: T_ready detection (spec §5) and the
two-stage rank at T_ready (spec §6). UNVALIDATED — PAPER ONLY.

Units (F3, verified on the 2026-09-13 probe record): `updated` is ns epoch,
`min.t` is ms epoch. All comparisons are made in aware ET.

The poller is a STEP function (`TReadyPoller.poll()`), driven by the lane's
tick so the session simulator can advance a fake clock between polls;
`wait_t_ready()` is the spec's blocking form, a thin wrapper over it.

The Polygon client is injected and duck-typed:
    snapshot_all() -> {"tickers": [...]} | {"error": str, "retry_after": s|None} | None
    bars_5m(ticker, day) -> list[{"t": ms, "o", "h", "l", "c", "v"}] | None
    last_price(ticker) -> float | None
Production: `PolygonSnapshotClient` over `src.data.polygon_data.get_client()`.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import datetime, time, timezone
from pathlib import Path

from src.rvol_exec.constants import (STAGE2_RETRIES, STAGE2_RETRY_WAIT_S, ET, FILTER_ATR14_MIN, FILTER_AVGVOL14_MIN, FILTER_OPEN_MIN,
                                     PRERANK_K, PRERANK_K_CONTAMINATED, REALTIME_THRESHOLD_S,
                                     SNAPSHOT_MIN_CAND_COVERAGE, SNAPSHOT_MIN_TICKERS,
                                     SNAPSHOT_POLL_S, SNAPSHOT_REF, STAGE2_EXACT_BAR,
                                     T_READY_DEADLINE, T_SNAPSHOT, BASELINE_N)

logger = logging.getLogger("rvol_exec")

READY_MINUTE = time(9, 34, 0)          # the 09:34 minute closed -> the 09:30 5m bar is complete
CONTAMINATED_MINUTE = time(9, 35, 0)   # first ready view already past the bar -> contaminated
SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
STAGE2_TIMEOUT_S = 10
STAGE2_BUDGET_S = 20
STAGE2_WORKERS = 10


# ── unit helpers ─────────────────────────────────────────────────────────────
def ns_to_et(ns: int | float) -> datetime:
    return datetime.fromtimestamp(float(ns) / 1e9, tz=timezone.utc).astimezone(ET)


def ms_to_et(ms: int | float) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1e3, tz=timezone.utc).astimezone(ET)


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


# ── §5 parse one snapshot ────────────────────────────────────────────────────
def parse_snapshot(snap: dict, prep_symbols, now_utc: datetime) -> dict:
    """Facts about one snapshot body: newest `updated`, delay vs wall clock,
    realtime flag, REF `min.t` (prior-day stamps excluded), ready/contaminated,
    ticker count and prep coverage."""
    tk = snap.get("tickers") or []
    today_et = now_utc.astimezone(ET).date()
    newest_ns = 0
    ref_min_ms = None
    present = set()
    for t in tk:
        u = t.get("updated")
        try:
            newest_ns = max(newest_ns, int(u or 0))
        except (TypeError, ValueError):
            pass
        sym = t.get("ticker")
        d = t.get("day") or {}
        if sym and _f(d.get("o")) is not None:
            present.add(sym)
        mn = t.get("min") or {}
        if sym in SNAPSHOT_REF and mn and mn.get("t") is not None:
            try:
                ms = int(mn["t"])
            except (TypeError, ValueError):
                continue
            if ms_to_et(ms).date() != today_et:
                continue                              # a prior-day minute is not evidence
            ref_min_ms = ms if ref_min_ms is None else max(ref_min_ms, ms)
    delay_s = (now_utc.timestamp() - newest_ns / 1e9) if newest_ns else None
    realtime = delay_s is not None and delay_s <= REALTIME_THRESHOLD_S
    ref_min_t_et = ms_to_et(ref_min_ms) if ref_min_ms is not None else None
    ready = ref_min_t_et is not None and ref_min_t_et.timetz().replace(tzinfo=None) >= READY_MINUTE
    contaminated = ready and ref_min_t_et.timetz().replace(tzinfo=None) >= CONTAMINATED_MINUTE
    prep_symbols = list(prep_symbols or [])
    coverage = (sum(1 for s in prep_symbols if s in present) / len(prep_symbols)) if prep_symbols else 0.0
    ok = len(tk) >= SNAPSHOT_MIN_TICKERS and coverage >= SNAPSHOT_MIN_CAND_COVERAGE
    return {"newest_updated_ns": newest_ns,
            "newest_updated_et": ns_to_et(newest_ns).isoformat(timespec="seconds") if newest_ns else None,
            "delay_s": round(delay_s, 1) if delay_s is not None else None, "realtime": realtime,
            "ref_min_t_et": ref_min_t_et.isoformat(timespec="seconds") if ref_min_t_et else None,
            "ready": bool(ready), "contaminated": bool(contaminated), "n_tickers": len(tk),
            "coverage": round(coverage, 3), "ok": bool(ok)}


def _retry_after(err) -> float | None:
    if isinstance(err, dict) and err.get("retry_after") is not None:
        try:
            return float(err["retry_after"])
        except (TypeError, ValueError):
            return None
    m = re.search(r"retry[-_ ]after[:= ]+(\d+)", str(err), re.I)
    return float(m.group(1)) if m else None


class TReadyPoller:
    """One snapshot call per poll; `poll()` returns
    ("early"|"wait"|"ready"|"timeout", snap, meta)."""

    def __init__(self, clock, polygon, ledger, prep_symbols, session_dir: Path | None = None):
        self.clock = clock
        self.polygon = polygon
        self.ledger = ledger
        self.prep_symbols = list(prep_symbols or [])
        self.session_dir = Path(session_dir) if session_dir else None
        self.next_poll_at: datetime | None = None
        self.polls = 0
        self.errors = 0
        self.started_at: datetime | None = None
        self.result: tuple | None = None

    def poll(self) -> tuple[str, dict | None, dict | None]:
        if self.result is not None:
            return self.result
        now = self.clock.now_et()
        t = now.timetz().replace(tzinfo=None)
        if t < T_SNAPSHOT:
            return ("early", None, None)
        if t >= T_READY_DEADLINE:
            self.ledger.emit("t_ready_timeout", polls=self.polls, errors=self.errors)
            self.result = ("timeout", None, None)
            return self.result
        if self.next_poll_at is not None and now < self.next_poll_at:
            return ("wait", None, None)
        if self.started_at is None:
            self.started_at = now
        self.polls += 1
        wall_utc = self.clock.now_utc()
        try:
            snap = self.polygon.snapshot_all()
        except Exception as exc:  # noqa: BLE001
            snap = {"error": f"exception: {exc}"}
        if snap is None or not isinstance(snap, dict) or "tickers" not in snap:
            self.errors += 1
            err = (snap or {}).get("error") if isinstance(snap, dict) else "no_body"
            ra = _retry_after(snap) if isinstance(snap, dict) else None
            is_429 = "429" in str(err or "")
            wait = ra if (is_429 and ra) else (60.0 if is_429 else float(SNAPSHOT_POLL_S))
            self.ledger.emit("snapshot_error", error=str(err or "empty_body")[:200],
                             retry_in_s=wait, polls=self.polls)
            self.next_poll_at = now.__class__.fromtimestamp(now.timestamp() + wait, tz=ET)
            return ("wait", None, None)
        meta = parse_snapshot(snap, self.prep_symbols, wall_utc)
        elapsed = (now - self.started_at).total_seconds()
        self.ledger.emit("snapshot_polled", wall_et=now.isoformat(timespec="seconds"),
                         newest_updated_et=meta["newest_updated_et"], delay_s=meta["delay_s"],
                         realtime=meta["realtime"], ref_min_t_et=meta["ref_min_t_et"],
                         ready=meta["ready"], n_tickers=meta["n_tickers"],
                         coverage=meta["coverage"], ok=meta["ok"], elapsed_s=round(elapsed, 1))
        if meta["ready"] and meta["ok"]:
            t_snapshot_dt = now.replace(hour=T_SNAPSHOT.hour, minute=T_SNAPSHOT.minute,
                                        second=0, microsecond=0)
            contamination_s = max(0.0, (now - t_snapshot_dt).total_seconds()) if meta["contaminated"] else 0.0
            meta = dict(meta, mode="realtime" if meta["realtime"] else "delayed",
                        t_ready_et=now.isoformat(timespec="seconds"),
                        contamination_s=round(contamination_s, 1), polls=self.polls)
            self.ledger.emit("t_ready", mode=meta["mode"], t_ready_et=meta["t_ready_et"],
                             delay_s=meta["delay_s"], contaminated=meta["contaminated"],
                             contamination_s=meta["contamination_s"], polls=self.polls)
            if self.session_dir is not None:
                try:
                    self.session_dir.mkdir(parents=True, exist_ok=True)
                    with gzip.open(self.session_dir / "snapshot_ready.json.gz", "wt") as fh:
                        json.dump(snap, fh)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("snapshot persist failed: %s", exc)
            self.result = ("ready", snap, meta)
            return self.result
        self.next_poll_at = now.__class__.fromtimestamp(now.timestamp() + SNAPSHOT_POLL_S, tz=ET)
        return ("wait", None, None)


def wait_t_ready(clock, polygon, ledger, prep_symbols, session_dir=None):
    """Spec §5 blocking form: (snap, meta) or None at the deadline."""
    poller = TReadyPoller(clock, polygon, ledger, prep_symbols, session_dir)
    while True:
        status, snap, meta = poller.poll()
        if status == "ready":
            return snap, meta
        if status == "timeout":
            return None
        clock.sleep(1 if status == "early" else 1)


# ── §6 two-stage rank ────────────────────────────────────────────────────────
def bar_0930(rows, day: str) -> dict | None:
    """The 09:30 ET 5m bar among Polygon aggregate rows for `day`."""
    for r in rows or []:
        try:
            ts = ms_to_et(int(r["t"]))
        except (KeyError, TypeError, ValueError):
            continue
        if ts.strftime("%Y-%m-%d") == day and ts.strftime("%H:%M") == "09:30":
            o, h, l, c, v = (_f(r.get(k)) for k in ("o", "h", "l", "c", "v"))
            if None in (o, h, l, c, v):
                return None
            return {"o": o, "h": h, "l": l, "c": c, "v": v}
    return None


def rank_universe(snap: dict, prep_rows, polygon, day: str, contaminated: bool = False,
                  stage2: bool = STAGE2_EXACT_BAR, k: int | None = None,
                  min_cover_floor: bool = True, retry_sleep=None) -> dict:
    """Stage 1 on the snapshot (`min.av`, fallback `day.v`), stage 2 exact
    09:30 bars for the top-K, final rank on `v930_used / baseline14`.

    `prep_rows`: iterable of dicts with ticker, atr14, avgvol14, baseline14,
    n_baseline (a DataFrame's `to_dict('records')` works)."""
    by_sym = {t.get("ticker"): t for t in (snap.get("tickers") or []) if t.get("ticker")}
    k = k or (PRERANK_K_CONTAMINATED if contaminated else PRERANK_K)
    excluded: list[dict] = []
    cands = []
    for p in prep_rows:
        sym = p.get("ticker")
        t = by_sym.get(sym)
        if t is None:
            continue
        av14, atr, base, nb = (_f(p.get("avgvol14")), _f(p.get("atr14")),
                               _f(p.get("baseline14")), p.get("n_baseline"))
        try:
            nb = int(nb) if nb is not None else 0
        except (TypeError, ValueError):
            nb = 0
        if av14 is None or av14 < FILTER_AVGVOL14_MIN or atr is None or atr <= FILTER_ATR14_MIN:
            continue
        if base is None or base <= 0 or nb != BASELINE_N:
            continue
        d = t.get("day") or {}
        mn = t.get("min") or {}
        open_px = _f(d.get("o"))
        if open_px is None or open_px < FILTER_OPEN_MIN:
            continue
        v_snap = _f(mn.get("av")) if mn else None
        v_source = "min.av"
        if v_snap is None:
            v_snap = _f(d.get("v"))
            v_source = "day.v"
        if v_snap is None or v_snap <= 0:
            continue
        cands.append({"ticker": sym, "atr14": atr, "avgvol14": av14, "baseline14": base,
                      "n_baseline": nb, "open": open_px, "prev_close": _f(p.get("prev_close")),
                      "v_snap": v_snap, "v_snap_source": v_source,
                      "rvol_snap": v_snap / base, "day": d, "min": mn,
                      "updated": t.get("updated")})
    cands.sort(key=lambda r: (-r["rvol_snap"], r["ticker"]))
    stage1 = cands[:k]

    exact: dict[str, dict | None] = {}
    stage2_stats = {"enabled": bool(stage2), "requested": 0, "exact": 0, "fallback": 0,
                    "unavailable": 0}
    if stage2 and stage1:
        stage2_stats["requested"] = len(stage1)
        exact = _fetch_exact_bars(polygon, [r["ticker"] for r in stage1], day)
        # bounded retry of the misses (a transient per-ticker miss must not become
        # skipped{orb_unavailable} on a contaminated snapshot, nor an
        # orb_source=snapshot fallback row on a clean one); CONTAMINATED waits
        # STAGE2_RETRY_WAIT_S between attempts via the injected clock, clean
        # snapshots retry back-to-back so placement is not delayed
        retries = 0
        for _ in range(STAGE2_RETRIES):
            missing = [s for s in exact if exact.get(s) is None]
            if not missing:
                break
            if contaminated and retry_sleep is not None:
                retry_sleep(STAGE2_RETRY_WAIT_S)
            retries += 1
            exact.update(_fetch_exact_bars(polygon, missing, day))
        stage2_stats["retries"] = retries
        stage2_stats["missing_after_retry"] = sorted(s for s in exact if exact.get(s) is None)

    rows = []
    for r in stage1:
        sym = r["ticker"]
        d, mn = r["day"], r["min"]
        b = exact.get(sym) if stage2 else None
        if b is not None:
            src = "exact"
            stage2_stats["exact"] += 1
            o, h, l, c, v = b["o"], b["h"], b["l"], b["c"], b["v"]
        else:
            if contaminated:
                stage2_stats["unavailable"] += 1
                excluded.append({"ticker": sym, "reason": "orb_unavailable"})
                continue
            if stage2:
                stage2_stats["fallback"] += 1
            src = "snapshot"
            min_t_et = None
            try:
                min_t_et = ms_to_et(int(mn.get("t"))) if mn and mn.get("t") is not None else None
            except (TypeError, ValueError):
                min_t_et = None
            c_src = (_f(mn.get("c")) if (min_t_et is not None and min_t_et.strftime("%H:%M") == "09:34")
                     else _f(d.get("c")))
            o, h, l, c, v = _f(d.get("o")), _f(d.get("h")), _f(d.get("l")), c_src, r["v_snap"]
            if None in (o, h, l, c, v):
                excluded.append({"ticker": sym, "reason": "orb_unavailable"})
                continue
        rows.append({"ticker": sym, "atr14": r["atr14"], "avgvol14": r["avgvol14"],
                     "baseline14": r["baseline14"], "n_baseline": r["n_baseline"],
                     "open": r["open"], "prev_close": r["prev_close"],
                     "o": o, "h": h, "l": l, "c": c, "v930_used": v, "orb_source": src,
                     "rvol": v / r["baseline14"], "rvol_snap": r["rvol_snap"],
                     "v_snap": r["v_snap"], "v_snap_source": r["v_snap_source"],
                     "day": {kk: d.get(kk) for kk in ("o", "h", "l", "c", "v", "vw")},
                     "min_t": mn.get("t"), "min_av": mn.get("av"), "min_c": mn.get("c"),
                     "updated": r["updated"]})
    rows.sort(key=lambda x: (-x["rvol"], x["ticker"]))
    top = rows[:20]
    for i, x in enumerate(top, 1):
        x["rank"] = i
    return {"top20": top, "excluded": excluded, "n_candidates": len(cands),
            "stage1_k": k, "stage2": stage2_stats}


def _fetch_exact_bars(polygon, symbols: list[str], day: str) -> dict:
    out: dict[str, dict | None] = {}
    serial = bool(getattr(polygon, "serial", False))
    if serial:
        for s in symbols:
            try:
                out[s] = bar_0930(polygon.bars_5m(s, day), day)
            except Exception:  # noqa: BLE001
                out[s] = None
        return out
    with ThreadPoolExecutor(max_workers=min(STAGE2_WORKERS, max(1, len(symbols)))) as ex:
        futs = {s: ex.submit(polygon.bars_5m, s, day) for s in symbols}
        for s, f in futs.items():
            try:
                out[s] = bar_0930(f.result(timeout=STAGE2_TIMEOUT_S), day)
            except (FutTimeout, Exception):  # noqa: BLE001
                out[s] = None
    return out


# ── production client ────────────────────────────────────────────────────────
class PolygonSnapshotClient:
    """Thin wrapper over `src.data.polygon_data.get_client()._get` (the only
    Polygon surface the lane uses). Production only — never under pytest."""

    BASE = "https://api.polygon.io"
    serial = False

    def __init__(self, client=None):
        if client is None:
            from src.data.polygon_data import get_client   # lazy: allowed reuse (§0)
            client = get_client()
        self._c = client

    def snapshot_all(self):
        out = self._c._get(SNAPSHOT_URL)
        if out is None:
            return None
        if "error" in out:
            return {"error": str(out["error"]), "retry_after": _retry_after(out["error"])}
        return out

    def bars_5m(self, ticker: str, day: str):
        url = (f"{self.BASE}/v2/aggs/ticker/{ticker}/range/5/minute/{day}/{day}"
               f"?adjusted=true&sort=asc&limit=10")
        out = self._c._get(url) or {}
        return out.get("results") if "error" not in out else None

    def bars_5m_range(self, ticker: str, start: str, end: str):
        url = (f"{self.BASE}/v2/aggs/ticker/{ticker}/range/5/minute/{start}/{end}"
               f"?adjusted=true&sort=asc&limit=50000")
        out = self._c._get(url) or {}
        if "error" in out:
            return {"error": out["error"]}
        return out.get("results") or []

    def last_price(self, ticker: str):
        out = self._c._get(f"{SNAPSHOT_URL}/{ticker}") or {}
        t = out.get("ticker") or {}
        mn = t.get("min") or {}
        return _f(mn.get("c"))
