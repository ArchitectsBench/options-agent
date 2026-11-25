"""Squeeze-watchlist buy-zone tracker — MANUAL DECISION SUPPORT.

Nightly: (1) add today's RVOL-surge x multi-TF-squeeze names (rvol >= 3,
>= 2 timeframes in squeeze) to a rolling watchlist (kept 10 sessions);
(2) for every watched name, check the BUY ZONE: close within 1 ATR(14) of
EMA21 (pulled back to its mean) AND above the 50-day SMA (not broken);
(3) push a phone notification (ntfy) when a name ENTERS the buy zone.

State: data/watchlist_squeeze.json. Stamp: decision support only — no
systematic-edge claim; the human decides.
"""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from src.engine.notify import send  # noqa: E402
import importlib

import yaml

scr = importlib.import_module("rvol_squeeze_screener")
STATE = ROOT / "data" / "watchlist_squeeze.json"
CFG = yaml.safe_load((ROOT / "config" / "squeeze_watchlist.yaml").read_text())
KEEP_SESSIONS = CFG["aging"]["keep_sessions"]
STOP_ATR = CFG["invalidation"]["stop_atr"]


def _av_key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("ALPHAVANTAGE_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def earnings_session(ticker: str):
    """Earnings date + BMO/AMC session. PRIMARY = AlphaVantage
    EARNINGS_CALENDAR (date only, no session), FALLBACK = yfinance (date +
    best-effort session from the timestamp hour). Returns (date_str, session,
    source). session in {'BMO','AMC','unknown'}; date-only -> 'unknown',
    never guessed. (additions 1/2026-07-23.)"""
    key = _av_key()
    if key:                              # PRIMARY: AlphaVantage
        try:
            import urllib.request, csv, io
            url = ("https://www.alphavantage.co/query?function=EARNINGS_CALENDAR"
                   f"&symbol={ticker}&horizon=3month&apikey={key}")
            txt = urllib.request.urlopen(url, timeout=20).read().decode()
            rows = list(csv.DictReader(io.StringIO(txt)))
            if rows and rows[0].get("reportDate"):
                return rows[0]["reportDate"], "unknown", "alphavantage"
        except Exception:
            pass
    try:                                 # FALLBACK: yfinance
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        dts = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dts:
            return "", "unknown", "none"
        ed = dts[0]
        ds = str(ed)[:10]
        hr = getattr(ed, "hour", None)
        if hr is None or (getattr(ed, "hour", 0) == 0 and getattr(ed, "minute", 0) == 0):
            return ds, "unknown", "yfinance"
        return ds, ("BMO" if hr < 12 else "AMC"), "yfinance"
    except Exception:
        return "", "unknown", "none"


EVENTS = ROOT / "logs" / "alerts" / "events.jsonl"
ALERT_STATE = ROOT / "data" / "watchlist_alert_state.json"
BARS5M = ROOT / "data" / "research_orb" / "bars5m"
# One run-marker row per radar run (2026-09-13) — the '[buyzone-alerts]'
# one-liner used to exist only on stdout, which the hourly sweep discarded.
SUMMARY = ROOT / "logs" / "alerts" / "summary.jsonl"
# Single-instance run lock (2026-09-13). The radar now has TWO launchd runners
# — the 18:40 nightly chain step and the hourly travel_catchup sweep, whose
# phase drifts on every sleep/wake — that can start seconds apart. Two
# concurrent radars would both see the same state transition (duplicate
# wl_event_v1 rows and duplicate ZONE ENTRY pushes) and race rotate_events'
# read-modify-write of events.jsonl. flock is per open-file-description, so a
# second run in ANY process gets BlockingIOError and skips (exit 0 — not a
# failure, the other run lands the summary row); the lock is released when
# the handle closes: normal exit, exception or crash. Module constant so
# tests redirect it to tmp_path.
LOCK = ROOT / "data" / "watchlist_squeeze.lock"
# Push ledger (2026-09-13): one wl_push_v1 row per phone push, so a push is
# a durable record (title, body, tickers, instance ids, delivered) and not
# just a line in ntfy's history. Additive: written by _send_logged, which is
# a pass-through to the notify layer — it never changes whether or when a
# push happens. Module constant so tests redirect it to tmp_path.
PUSHES = ROOT / "logs" / "alerts" / "pushes.jsonl"


def _send_logged(title: str, body: str, items: list[dict] | None = None, **kw) -> dict:
    """Pass-through to the notify layer's send function that ALSO appends one
    wl_push_v1 row to PUSHES. `items` = [{ticker, state, state_instance_id}]
    for the names in this push. `delivered` is True only when an ntfy topic is
    configured AND the notify layer logged no ntfy-channel failure during the
    call (it swallows failures into logger 'trader' warnings — that is the
    only signal it exposes); `ntfy_configured` and `error` are recorded
    separately so a silent no-topic run is distinguishable from a network
    failure. The ledger append is best-effort: a ledger failure is printed,
    never raised, and never suppresses the push (the push happens first)."""
    import io
    import logging
    import os
    lg = logging.getLogger("trader")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.WARNING)
    prev_level = lg.level
    if lg.getEffectiveLevel() > logging.WARNING:
        lg.setLevel(logging.WARNING)
    lg.addHandler(handler)
    try:
        send(title, body, **kw)
    finally:
        lg.removeHandler(handler)
        lg.setLevel(prev_level)
    ntfy_err = next((ln for ln in buf.getvalue().splitlines() if "ntfy" in ln), None)
    ntfy_configured = bool(os.environ.get("NTFY_TOPIC"))
    items = list(items or [])
    row = {"schema": "wl_push_v1", "ts": _now_iso(), "title": title, "body": body,
           "tickers": [i.get("ticker") for i in items],
           "state_instance_ids": [i.get("state_instance_id") for i in items],
           "items": items,
           "delivered": bool(ntfy_configured and ntfy_err is None),
           "ntfy_configured": ntfy_configured, "error": ntfy_err,
           "manual_decision_support": True}
    try:
        PUSHES.parent.mkdir(parents=True, exist_ok=True)
        with PUSHES.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:
        print(f"[buyzone] ⚠ push row not persisted to {PUSHES} ({exc})")
    return row


def acquire_run_lock():
    """Non-blocking: returns the open handle (keep it alive for the whole run)
    or None when another radar run holds the lock."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = LOCK.open("a")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def release_run_lock(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def refresh_watchlist_bars(names, day):
    """Item 2a: nightly refresh of 5m bars for CURRENT watchlist names so 1h
    states + rvol_current compute from fresh data. Skips names already fresh
    to 'day'. Merges into the bars5m store (dedup by date+hm)."""
    if not CFG.get("bars_refresh", {}).get("enabled"):
        return 0
    from datetime import date as _date, timedelta as _td
    try:
        from src.data.polygon_data import get_client
        client = get_client()
    except Exception:
        return 0
    lookback = CFG["bars_refresh"]["lookback_days"]
    start = (_date.fromisoformat(day) - _td(days=lookback)).isoformat()
    refreshed = 0
    for t in names:
        f = BARS5M / f"{t}.parquet"
        if CFG["bars_refresh"].get("skip_if_fresh") and f.exists():
            try:
                last = pd.read_parquet(f, columns=["date"])["date"].max()
                if str(last) >= day:
                    continue
            except Exception:
                pass
        url = (f"https://api.polygon.io/v2/aggs/ticker/{t}/range/5/minute/"
               f"{start}/{day}?adjusted=true&sort=asc&limit=50000")
        out = client._get(url) or {}
        rws = out.get("results") or []
        if not rws:
            continue
        nb = pd.DataFrame(rws)
        ts = pd.to_datetime(nb["t"], unit="ms", utc=True).dt.tz_convert("US/Eastern")
        nb = nb.assign(date=ts.dt.strftime("%Y-%m-%d"), hm=ts.dt.strftime("%H:%M"))
        nb = nb.rename(columns={"o": "o", "h": "h", "l": "l", "c": "c", "v": "v"})
        nb = nb[(nb["hm"] >= "09:30") & (nb["hm"] < "16:00")][["date", "hm", "o", "h", "l", "c", "v"]]
        if f.exists():
            old = pd.read_parquet(f)
            merged = pd.concat([old, nb]).drop_duplicates(["date", "hm"], keep="last")
        else:
            merged = nb
        merged.sort_values(["date", "hm"]).to_parquet(f)
        refreshed += 1
    return refreshed


def opening_rvol(ticker, day):
    """rvol_current: today's 09:30 5m bar volume / mean(prior-14 09:30 bars),
    time-of-day-matched (item 2a). None if store lacks today or <14 priors."""
    f = BARS5M / f"{ticker}.parquet"
    if not f.exists():
        return None
    try:
        b = pd.read_parquet(f, columns=["date", "hm", "v"])
        b930 = b[b["hm"] == "09:30"].set_index("date")["v"]
        if day not in b930.index:
            return None
        prior = b930[b930.index < day].tail(14)
        if len(prior) < 14 or prior.mean() <= 0:
            return None
        return round(float(b930.loc[day] / prior.mean()), 2)
    except Exception:
        return None


def session_lag(data_asof, day, session_dates):
    """Count trading sessions between data_asof (date part) and day."""
    d0 = str(data_asof)[:10]
    after = [s for s in session_dates if d0 < s <= day]
    return len(after)


def eval_tf(o, h, l, c):
    """Phase A1: one timeframe's OWN squeeze state from its OHLC arrays.
    Returns None if too few bars. Same construction as the daily machine,
    now applied per timeframe. `o` (open) enables gap-through invalidation."""
    n = len(c)
    if n < 55:
        return None
    s = pd.Series(c)
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = float(pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    ema8 = s.ewm(span=8, adjust=False).mean().iloc[-1]
    ema13 = s.ewm(span=13, adjust=False).mean().iloc[-1]
    ema21 = s.ewm(span=21, adjust=False).mean().iloc[-1]
    sma50 = s.rolling(50).mean().iloc[-1]
    sma20s = s.rolling(20).mean(); sd20s = s.rolling(20).std()
    bbup = (sma20s + 2 * sd20s).to_numpy()
    atr20 = pd.Series(tr).ewm(span=20, adjust=False).mean().to_numpy()
    on = (bbup - (sma20s.to_numpy() + 1.5 * atr20)) <= 0
    run = 0; fire_idx = None
    for i in range(len(on)):
        if on[i]:
            run += 1
        else:
            if run >= 6:
                fire_idx = i
            run = 0
    hh20 = pd.Series(h).rolling(20).max(); ll20 = pd.Series(l).rolling(20).min()
    ema20s = s.ewm(span=20, adjust=False).mean()
    dev = s - ((hh20 + ll20) / 2 + ema20s) / 2
    mom_sign = "long" if dev.iloc[-1] > 0 else "short"
    bars_since_fire = (len(on) - 1 - fire_idx) if fire_idx is not None else None
    dist_atr = abs(c[-1] - ema21) / atr if atr > 0 else 99
    dist_e13 = abs(c[-1] - ema13) / atr if atr > 0 else 99
    bias = ("bull" if ema8 > ema13 > ema21 and c[-1] > sma50
            else "bear" if ema8 < ema13 < ema21 and c[-1] < sma50 else "mixed")
    zone = None
    if dist_atr <= 1.0:
        if c[-1] > sma50 and bias != "bear":
            zone = "long"
        elif c[-1] < sma50 and bias == "bear":
            zone = "short"
    fire_dir = mom_sign if bars_since_fire is not None else None
    if bars_since_fire is not None and bars_since_fire <= 1:
        st = f"FIRED-{fire_dir.upper()}"
    elif bars_since_fire is not None and bars_since_fire <= 6:
        st = f"PULLBACK-{fire_dir.upper()}" if dist_e13 <= 1.0 else f"EXTENDED-{fire_dir.upper()}"
    elif zone and on[-1]:
        st = f"COILED-{zone.upper()}"
    elif zone:
        st = f"{zone.upper()}-ZONE"
    else:
        st = "WATCH"
    st_dir = "long" if "LONG" in st else "short" if "SHORT" in st else None
    setup_dir = st_dir or zone
    post_fire = st.startswith(("FIRED", "PULLBACK", "EXTENDED"))
    invalidated, reason, inval_level = False, None, None
    # breach = CLOSE beyond level OR GAP-through (open beyond level) — not a
    # mere touch (item 1, 2026-07-23: a gap that jumps the level counts).
    if setup_dir == "long":
        inval_level = ema21 - STOP_ATR * atr
        if c[-1] < inval_level or o[-1] < inval_level:
            invalidated, reason = True, "stop_structure_breach"
        elif post_fire and dev.iloc[-1] < 0:
            invalidated, reason = True, "momentum_flip"
    elif setup_dir == "short":
        inval_level = ema21 + STOP_ATR * atr
        if c[-1] > inval_level or o[-1] > inval_level:
            invalidated, reason = True, "stop_structure_breach"
        elif post_fire and dev.iloc[-1] > 0:
            invalidated, reason = True, "momentum_flip"
    if post_fire and (bars_since_fire is not None and bars_since_fire <= 6) and on[-1]:
        invalidated, reason = True, "squeeze_reforming"
    return {"squeeze_on": bool(on[-1]), "mom_sign": mom_sign, "bias": bias,
            "zone": zone, "state": st, "setup_dir": setup_dir,
            "dist_atr": round(float(dist_atr), 2),
            "mean_level": round(float(ema21), 4),
            "zone_bounds_atr": [-1.0, 1.0],
            "invalidation_level_atr": (round((c[-1] - inval_level) / atr, 2)
                                       if inval_level is not None and atr > 0 else None),
            "invalidated": invalidated, "invalidation_reason": reason,
            "fired_dir": fire_dir if post_fire else None, "close": round(float(c[-1]), 4)}


def tf_ohlc(ticker: str, tf: str, daily_g: pd.DataFrame):
    """OHLC (h,l,c) + data_asof for a timeframe. 1d = daily store; 1h =
    resampled from bars5m if present; 1m = only where 1-min data exists
    (we store 5-min, so 1m is skipped -> None)."""
    if tf == "1d":
        if daily_g is None or len(daily_g) < 55:
            return None
        return (daily_g["open"].to_numpy(float), daily_g["high"].to_numpy(float),
                daily_g["low"].to_numpy(float), daily_g["close"].to_numpy(float),
                str(daily_g["date"].iloc[-1]))
    if tf == "1h":
        f = ROOT / "data" / "research_orb" / "bars5m" / f"{ticker}.parquet"
        if not f.exists() or "date" not in (b0 := pd.read_parquet(f)).columns:
            return None
        b0["dt"] = pd.to_datetime(b0["date"] + " " + b0["hm"])
        b = b0.set_index("dt")
        oo = b["o"].resample("1h").first().dropna()
        hh = b["h"].resample("1h").max().dropna()
        ll = b["l"].resample("1h").min().dropna()
        cc = b["c"].resample("1h").last().dropna()
        idx = cc.index
        if len(cc) < 55:
            return None
        return (oo.reindex(idx).to_numpy(float), hh.reindex(idx).to_numpy(float),
                ll.reindex(idx).to_numpy(float), cc.to_numpy(float), str(idx[-1]))
    return None  # 1m: no 1-minute data in store (we store 5-min bars)


def _now_iso():
    from datetime import datetime as _dt
    return _dt.now().isoformat(timespec="seconds")


def rotate_events(day):
    """Phase A3: roll events older than events_keep_days out of the hot
    events.jsonl into the append-only monthly archive. Archive is never
    trimmed (permanent record); events.jsonl is the rolling window."""
    if not EVENTS.exists():
        return 0
    from datetime import date as _date, timedelta as _td
    keep = CFG["retention"]["events_keep_days"]
    cutoff = (_date.fromisoformat(day) - _td(days=keep)).isoformat()
    lines = EVENTS.read_text().splitlines()
    hot, cold = [], {}
    for ln in lines:
        if not ln.strip():
            continue
        try:
            d = json.loads(ln).get("detected_at", "")[:10]
        except json.JSONDecodeError:
            continue
        if d and d < cutoff:
            cold.setdefault(d[:7], []).append(ln)   # by YYYY-MM
        else:
            hot.append(ln)
    if not cold:
        return 0
    adir = ROOT / CFG["retention"]["archive_dir"]
    adir.mkdir(parents=True, exist_ok=True)
    for ym, rows in cold.items():
        with (adir / f"events-{ym}.jsonl").open("a") as f:
            f.write("\n".join(rows) + "\n")
    EVENTS.write_text("\n".join(hot) + ("\n" if hot else ""))
    return sum(len(v) for v in cold.values())


def nightly_summary(day):
    """Phase A3 one-liner: today's events by type, instances opened/closed,
    stale-timeframe warnings — folded into the job output."""
    if not EVENTS.exists():
        return "no events log"
    from collections import Counter
    by_type, opened, closed, stale_tfs = Counter(), 0, 0, set()
    for ln in EVENTS.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("detected_at", "")[:10] != day:
            continue
        by_type[e["transition"]] += 1
        if e["transition"] == "zone_entered" and not e.get("re_entered"):
            opened += 1
        if e["transition"] in ("invalidated", "removed"):
            closed += 1
        if e.get("stale_data"):
            stale_tfs.add(f"{e['ticker']}:{e['timeframe']}")
    types = " ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
    warn = f" ⚠stale[{','.join(sorted(stale_tfs))}]" if stale_tfs else ""
    return f"events: {types or 'none'} | instances +{opened}/-{closed}{warn}"


def persist_summary(day, summary: str, watching: int, entered: int,
                    archived: int = 0) -> dict:
    """Ops visibility (2026-09-13): append the '[buyzone-alerts]' one-liner as
    ONE JSON row per radar run to logs/alerts/summary.jsonl. Additive only —
    stdout, the state machine, zone rules, notifications and the event schema
    are untouched. `date` is the RUN's local calendar date (the run marker
    config/collection_expectations.yaml::buyzone_radar_daily asserts);
    `data_day` is the grouped-daily date the radar evaluated (a stale shadow
    scan is rvol_shadow_daily's alarm, not this one's). Never raises: a
    convenience file may not fail the radar."""
    from datetime import date as _date
    row = {"schema": "wl_summary_v1", "date": _date.today().isoformat(),
           "ts": _now_iso(), "data_day": day, "summary": summary,
           "watching": int(watching), "entered": int(entered),
           "archived": int(archived), "manual_decision_support": True}
    try:
        SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        with SUMMARY.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:
        print(f"[buyzone] ⚠ summary row not persisted to {SUMMARY} ({exc})")
    return row


def emit(ev: dict):
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a") as f:
        f.write(json.dumps(ev) + "\n")


def _new_instance_id(ticker, tf, day):
    import uuid
    return f"{ticker}-{tf}-{day}-{uuid.uuid4().hex[:6]}"


def process_ticker_events(t, e, daily_g, alert_state, day, common, session_dates):
    """Phase A1 per-timeframe state machines + transition events. Emits
    immutable events on TRANSITIONS only; dedup by (instance, transition);
    cooldown re-opens the same instance on fast re-entry. Returns the derived
    tfs_squeezed count."""
    ast = alert_state.setdefault(t, {})
    # PASS 1: evaluate every available timeframe, derive tfs_squeezed first.
    tf_states = {}
    tf_asof = {}
    for tf in CFG["timeframes"]["enabled"]:
        ohlc = tf_ohlc(t, tf, daily_g)
        if ohlc is None:
            continue                        # absent-data TF: skip, never fake
        o, h, l, c, data_asof = ohlc
        cur = eval_tf(o, h, l, c)
        if cur is not None:
            tf_states[tf] = cur
            tf_asof[tf] = data_asof
    tfs_sq = sum(1 for v in tf_states.values() if v["squeeze_on"])
    # PASS 2: transitions + events, with tfs_squeezed known.
    for tf, cur in tf_states.items():
        data_asof = tf_asof[tf]
        prior = ast.get(tf, {})
        inst = prior.get("instance_id")
        emitted = set(prior.get("emitted", []))
        active = prior.get("active", False)
        active_now = cur["state"] != "WATCH" and not cur["invalidated"]
        in_zone_now = cur["state"] != "WATCH"

        def event(transition, extra=None):
            ev = {"schema": "wl_event_v1", "ticker": t, "timeframe": tf,
                  "transition": transition, "state_instance_id": inst,
                  "state": cur["state"], "dist_atr": cur["dist_atr"],
                  "tfs_squeezed": tfs_sq,
                  "rvol_current": common["rvol_current"],
                  "rvol_at_add": common["rvol_at_add"],
                  "bias_d": common["bias_d"], "bias_w": common["bias_w"],
                  "bias_m": common["bias_m"], "mtf": common["mtf"],
                  "zone_dir": cur["zone"], "setup_dir": cur["setup_dir"],
                  "counter_trend": (cur["setup_dir"] is not None
                                    and common["bias_d"] not in
                                    (("bull" if cur["setup_dir"] == "long" else "bear"), "mixed")),
                  "e_status": common["e_status"],
                  "earnings_session": common["earnings_session"],
                  "mean_level": cur["mean_level"],
                  "zone_bounds_atr": cur["zone_bounds_atr"],
                  "invalidation_level_atr": cur["invalidation_level_atr"],
                  "invalidation_reason": cur["invalidation_reason"],
                  "data_asof": data_asof, "detected_at": _now_iso(),
                  "stale_data": (session_lag(data_asof, day, session_dates)
                                 > CFG["staleness"]["max_lag_sessions"].get(tf, 99)),
                  "data_lag_sessions": session_lag(data_asof, day, session_dates),
                  "manual_decision_support": True}
            if extra:
                ev.update(extra)
            emit(ev)

        # zone_entered (WATCH -> zone). Cooldown: re-entry within N sessions of
        # invalidation re-opens the SAME instance with re_entered=true.
        if in_zone_now and not active and "zone_entered" not in emitted:
            inv_day = prior.get("invalidated_day")
            recent = False
            if inv_day:
                try:
                    from datetime import date as _d
                    recent = (_d.fromisoformat(day) - _d.fromisoformat(inv_day)).days \
                        <= CFG["cooldown"]["reentry_bars"]
                except Exception:
                    recent = False
            if recent and inst:
                emitted.discard("invalidated")
                event("zone_entered", {"re_entered": True})
            else:
                inst = _new_instance_id(t, tf, day)
                emitted = set()
                event("zone_entered", {"re_entered": False})
            emitted.add("zone_entered")
            active = True
        # fired
        if cur["state"].startswith("FIRED") and inst and "fired" not in emitted:
            event("fired", {"fire_direction": cur["fired_dir"]})
            emitted.add("fired")
        # invalidated
        if cur["invalidated"] and inst and "invalidated" not in emitted:
            event("invalidated")
            emitted.add("invalidated")
            active = False
            ast.setdefault(tf, {})["invalidated_day"] = day
        ast[tf] = {**ast.get(tf, {}), "instance_id": inst, "prev_state": cur["state"],
                   "active": active, "emitted": sorted(emitted),
                   "invalidated_day": (day if cur["invalidated"]
                                       else ast.get(tf, {}).get("invalidated_day"))}
    return tfs_sq, tf_states


def main() -> None:
    lock = acquire_run_lock()
    if lock is None:
        print("[buyzone] another radar run in progress — skipped")
        return          # exit 0: no STEP-FAIL page, no sweep echo; nothing was read or written
    try:
        _run()
    finally:
        release_run_lock(lock)


def _run() -> None:
    day = sorted(scr.GROUPED.glob("*.parquet"))[-1].stem
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    # 1) refresh watchlist from today's screen
    shadow_f = ROOT / "logs" / "observation" / f"rvol_shadow-{day}.json"
    if shadow_f.exists():
        shadow = json.loads(shadow_f.read_text())
        picks = [x for x in shadow["top20"] if x["rvol"] >= 3.0]
        tickers = {x["ticker"] for x in picks}
        hist = scr.load_history(tickers)
        for x in picks:
            g = hist[hist["ticker"] == x["ticker"]]
            if len(g) < 60:
                continue
            n_on = 0
            for tf, k in scr.TFS.items():
                h, l, c = scr.resample(g, k, weekly=(tf == "W"))
                on, _ = scr.squeeze_state(h, l, c)
                n_on += on
            if n_on >= 2:
                e = state.setdefault(x["ticker"], {"added": day, "in_zone": False})
                e.update({"rvol_at_add": x["rvol"], "tfs": n_on, "last_seen": day})
    # alert-instance state + today's time-of-day-matched RVOL map
    alert_state = json.loads(ALERT_STATE.read_text()) if ALERT_STATE.exists() else {}
    rvol_today = {}
    if shadow_f.exists():
        sh = json.loads(shadow_f.read_text())
        rvol_today = {x["ticker"]: x["rvol"] for x in sh.get("top20", [])}
        rvol_today.update(sh.get("watchlist_rvols", {}))
    # expire old -> emit 'removed' events (aging rule) before dropping
    dates = sorted({f.stem for f in scr.GROUPED.glob("*.parquet")})
    keep_after = dates[-KEEP_SESSIONS] if len(dates) >= KEEP_SESSIONS else dates[0]
    for t, e in list(state.items()):
        if e["added"] < keep_after:
            for tf, a in (alert_state.get(t) or {}).items():
                if a.get("instance_id") and a.get("active"):
                    emit({"schema": "wl_event_v1", "ticker": t, "timeframe": tf,
                          "transition": "removed", "state_instance_id": a["instance_id"],
                          "reason": f"aged off after {KEEP_SESSIONS} sessions",
                          "data_asof": day, "detected_at": _now_iso(),
                          "manual_decision_support": True})
            alert_state.pop(t, None)
    state = {t: e for t, e in state.items() if e["added"] >= keep_after}
    if not state:
        STATE.write_text(json.dumps(state, indent=1))
        print("[buyzone] watchlist empty")
        persist_summary(day, "watchlist empty", 0, 0)   # run marker still lands
        return
    session_dates = dates
    # item 2a: refresh 5m bars for current watchlist names (fresh 1h + rvol)
    n_ref = refresh_watchlist_bars(list(state), day)
    # item 3: keep the degraded earnings source VISIBLE until AV key is set
    if not _av_key():
        print("[buyzone] ⚠ ALPHAVANTAGE_API_KEY absent — earnings on yfinance "
              "fallback (dates less reliable for obscure names)")
    print(f"[buyzone] refreshed 5m bars for {n_ref} watchlist names")
    # 2) buy-zone check on all watched names
    hist = scr.load_history(set(state), n_files=1300)  # full store: monthly EMAs need years
    entered, status = [], []
    entered_meta = []          # parallel to `entered`: {ticker, state, state_instance_id} per push line
    for t, e in state.items():
        g = hist[hist["ticker"] == t]
        if len(g) < 55:
            continue
        c = g["close"].to_numpy(float)
        h, l = g["high"].to_numpy(float), g["low"].to_numpy(float)
        prev_c = np.roll(c, 1); prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        s = pd.Series(c)
        ema8 = s.ewm(span=8, adjust=False).mean().iloc[-1]
        ema13 = s.ewm(span=13, adjust=False).mean().iloc[-1]
        ema21 = s.ewm(span=21, adjust=False).mean().iloc[-1]
        sma50 = s.rolling(50).mean().iloc[-1]
        # daily squeeze fire detection (mid tier, run >= 6) + momentum sign
        sq_on_hist = []
        sma20s = s.rolling(20).mean(); sd20s = s.rolling(20).std()
        bbup = (sma20s + 2 * sd20s).to_numpy()
        atr20 = pd.Series(tr).ewm(span=20, adjust=False).mean().to_numpy()
        dmid = bbup - (sma20s.to_numpy() + 1.5 * atr20)
        on = dmid <= 0
        run = 0; fire_idx = None
        for i in range(len(on)):
            if on[i]:
                run += 1
            else:
                if run >= 6:
                    fire_idx = i
                run = 0
        hh20 = pd.Series(h).rolling(20).max(); ll20 = pd.Series(l).rolling(20).min()
        ema20s = s.ewm(span=20, adjust=False).mean()
        dev = s - ((hh20 + ll20) / 2 + ema20s) / 2
        # M1 FIX (2026-07-23): fire direction is the SIGN of the momentum
        # histogram, NOT its slope. (Was mom_up = dev[-1] > dev[-2] — wrong;
        # a rising-but-negative histogram was mislabeled "long".)
        mom_sign = "long" if dev.iloc[-1] > 0 else "short"
        mom_rising = bool(dev.iloc[-1] > dev.iloc[-2])   # slope kept for info
        bars_since_fire = (len(on) - 1 - fire_idx) if fire_idx is not None else None
        bias = ("bull" if ema8 > ema13 > ema21 and c[-1] > sma50
                else "bear" if ema8 < ema13 < ema21 and c[-1] < sma50
                else "mixed")
        dist_atr = abs(c[-1] - ema21) / atr if atr > 0 else 99
        # long zone: pulled back to mean in an uptrend; short zone: rallied
        # to mean in a downtrend (RVOL is direction-agnostic — user obs
        # 2026-07-22, RDDT)
        zone = None
        if dist_atr <= 1.0:
            if c[-1] > sma50 and bias != "bear":
                zone = "long"
            elif c[-1] < sma50 and bias == "bear":
                zone = "short"
        # state machine: COILED -> FIRED (enter w/ confirmation, <=2 bars)
        # -> EXTENDED (chasing, hands off) -> PULLBACK (second entry window)
        fire_dir = mom_sign if bars_since_fire is not None else None
        dist_e13 = abs(c[-1] - ema13) / atr if atr > 0 else 99
        if bars_since_fire is not None and bars_since_fire <= 1:
            st_state = f"FIRED-{fire_dir.upper()}"
        elif bars_since_fire is not None and bars_since_fire <= 6:
            st_state = (f"PULLBACK-{fire_dir.upper()}" if dist_e13 <= 1.0
                        else f"EXTENDED-{fire_dir.upper()}")
        elif zone and on[-1]:
            st_state = f"COILED-{zone.upper()}"
        elif zone:
            st_state = f"{zone.upper()}-ZONE"
        else:
            st_state = "WATCH"
        # weekly bias for MTF alignment (tide vs wave)
        wdf = pd.DataFrame({"c": c}, index=pd.to_datetime(g["date"]))
        wk = wdf.resample("W-FRI").last().dropna()["c"]
        if len(wk) >= 21:
            w8 = wk.ewm(span=8, adjust=False).mean().iloc[-1]
            w13 = wk.ewm(span=13, adjust=False).mean().iloc[-1]
            w21 = wk.ewm(span=21, adjust=False).mean().iloc[-1]
            wk_bias = ("bull" if w8 > w13 > w21
                       else "bear" if w8 < w13 < w21 else "mixed")
        else:
            wk_bias = "mixed"
        e["wk_bias"] = wk_bias
        mo = wdf.resample("ME").last().dropna()["c"]
        if len(mo) >= 24:
            # structural lens (user calibration 2026-07-23, TEL case):
            # "monthly trend" = price vs rising/falling 20-month EMA, not
            # fast monthly EMAs (those read corrections as bear)
            m20 = mo.ewm(span=20, adjust=False).mean()
            rising = m20.iloc[-1] > m20.iloc[-2]
            mo_bias = ("bull" if c[-1] > m20.iloc[-1] and rising else
                       "bear" if c[-1] < m20.iloc[-1] and not rising
                       else "mixed")
        else:
            mo_bias = "mixed"
        e["mo_bias"] = mo_bias
        st_dir = ("long" if "LONG" in st_state else
                  "short" if "SHORT" in st_state else None)
        if st_dir:
            want = "bull" if st_dir == "long" else "bear"
            score = sum(1 for b in (wk_bias, mo_bias) if b == want)
            against = sum(1 for b in (wk_bias, mo_bias)
                          if b not in (want, "mixed"))
            e["mtf"] = (f"FULL-ALIGN" if score == 2 else
                        f"COUNTER-M" if mo_bias not in (want, "mixed") else
                        f"PARTIAL" if score == 1 else "—")
        else:
            e["mtf"] = "—"
        # M2 FIX: surface (never hide) a fire/zone direction contradiction.
        e["mom_sign"] = mom_sign
        e["mom_rising"] = mom_rising
        e["direction_conflict"] = bool(st_dir and zone and st_dir != zone)
        # M4 FIX: invalidation is now defined + computed (was undefined).
        # momentum_flip scoped to post-fire states; stop-structure + squeeze-
        # reforming apply to live setups. Level = EMA21 -/+ stop_atr*ATR.
        setup_dir = st_dir or zone
        invalidated, inval_reason, inval_level = False, None, None
        post_fire = st_state.startswith(("FIRED", "PULLBACK", "EXTENDED"))
        o_arr = g["open"].to_numpy(float)   # gap-through check (item 1)
        if setup_dir == "long":
            inval_level = ema21 - STOP_ATR * atr
            if c[-1] < inval_level or o_arr[-1] < inval_level:
                invalidated, inval_reason = True, "stop_structure_breach"
            elif post_fire and dev.iloc[-1] < 0:
                invalidated, inval_reason = True, "momentum_flip"
        elif setup_dir == "short":
            inval_level = ema21 + STOP_ATR * atr
            if c[-1] > inval_level or o_arr[-1] > inval_level:
                invalidated, inval_reason = True, "stop_structure_breach"
            elif post_fire and dev.iloc[-1] > 0:
                invalidated, inval_reason = True, "momentum_flip"
        if post_fire and (bars_since_fire is not None and bars_since_fire <= 6) and on[-1]:
            invalidated, inval_reason = True, "squeeze_reforming"
        e["invalidated"] = invalidated
        e["invalidation_reason"] = inval_reason
        e["invalidation_level_atr"] = (round((c[-1] - inval_level) / atr, 2)
                                       if inval_level is not None and atr > 0 else None)
        # earnings proximity + session (M3 FIX + addition 1: AV primary)
        if e.get("earnings") is None or e.get("earnings_session") is None:
            ed, sess, esrc = earnings_session(t)
            e["earnings"], e["earnings_session"], e["earnings_source"] = ed, sess, esrc
        ed, sess = e.get("earnings", ""), e.get("earnings_session", "unknown")
        ev_days = None
        if ed:
            try:
                from datetime import date as _date
                ev_days = (_date.fromisoformat(ed) - _date.fromisoformat(day)).days
            except Exception:
                pass
        e["days_to_earnings"] = ev_days
        # E-status per config. E-0d + unknown -> AMC-assumed (never suppress).
        e_status = None
        if ev_days is not None:
            if ev_days in CFG["earnings"]["warn_days"]:
                e_status = f"E-{ev_days}d"
            elif ev_days == 0:
                e_status = ("E-0d-AMC" if sess == "AMC"
                            else "E-passed-BMO" if sess == "BMO"
                            else "E-0d-AMC-assumed(session:unknown)")
            elif ev_days < 0:
                e_status = "E-passed"
        e["e_status"] = e_status
        was = e.get("state")
        e["zone"] = zone
        e["in_zone"] = zone is not None
        e["bias"] = bias
        e["dist_atr"] = round(float(dist_atr), 2)
        e["state"] = st_state

        # ── Phase A1 per-timeframe state machines + event emission ──
        rvol_cur = opening_rvol(t, day) or rvol_today.get(t)
        e["rvol_current"] = rvol_cur
        common = {"rvol_current": rvol_cur, "rvol_at_add": e.get("rvol_at_add"),
                  "bias_d": bias, "bias_w": wk_bias, "bias_m": mo_bias, "mtf": e["mtf"],
                  "e_status": e_status, "earnings_session": sess}
        tfs_sq, _tf_states = process_ticker_events(t, e, g, alert_state, day, common, session_dates)
        e["tfs_screen"] = e.get("tfs")          # old 2d/3d/4d/W add-screen count
        e["tfs"] = tfs_sq                        # A1: derived over enabled TFs
        e["tfs_by_state"] = {tf: v["squeeze_on"] for tf, v in _tf_states.items()}
        etrans = None
        if e_status in (f"E-{CFG['earnings']['warn_days'][0]}d",
                        f"E-{CFG['earnings']['warn_days'][1]}d",
                        "E-0d-AMC", "E-0d-AMC-assumed(session:unknown)"):
            etrans = "earnings_window"
        elif e_status in ("E-passed-BMO", "E-passed"):
            etrans = "earnings_passed"
        if etrans and e.get("_last_e_emitted") != f"{day}:{e_status}":
            inst_d = (alert_state.get(t, {}).get("1d", {}) or {}).get("instance_id")
            emit({"schema": "wl_event_v1", "ticker": t, "timeframe": "ticker",
                  "transition": etrans, "state_instance_id": inst_d,
                  "e_status": e_status, "earnings_session": sess,
                  "earnings_source": e.get("earnings_source"),
                  "days_to_earnings": ev_days, "data_asof": day,
                  "detected_at": _now_iso(), "manual_decision_support": True})
            e["_last_e_emitted"] = f"{day}:{e_status}"

        ev_tag = f" ⚠{e_status}" if e_status else ""
        cf_tag = " ⚑DIR-CONFLICT" if e["direction_conflict"] else ""
        iv_tag = f" ✖{inval_reason}" if invalidated else ""
        status.append(f"{t}: {st_state}{ev_tag}{cf_tag}{iv_tag} bias={bias}/{wk_bias}/{mo_bias} "
                      f"({dist_atr:.1f} ATR, {e['tfs']} TFs, mom={mom_sign})")
        if st_state != was and (st_state.startswith("FIRED")
                                or st_state.startswith("PULLBACK")
                                or st_state.startswith("COILED")):
            entered.append(f"{t} {st_state}{ev_tag} bias={bias} @ {c[-1]:.2f} "
                           f"({dist_atr:.1f} ATR, {e['tfs']} TFs, "
                           f"rvol {e['rvol_at_add']}x on add)")
            # push ledger metadata (2026-09-13): the push is driven by the
            # daily composite state, so the 1d lane's instance id is the one
            # this line belongs to (None if the lane opened no instance).
            entered_meta.append({"ticker": t, "state": st_state,
                                 "state_instance_id": ((alert_state.get(t) or {}).get("1d")
                                                       or {}).get("instance_id")})
    STATE.write_text(json.dumps(state, indent=1))
    ALERT_STATE.write_text(json.dumps(alert_state, indent=1))
    rolled = rotate_events(day)                      # A3: retention/archival
    summary = nightly_summary(day)
    print(f"[buyzone] {day}: watching {len(state)}; entered zone: {len(entered)}")
    print(f"[buyzone-alerts] {summary}"
          + (f" | archived {rolled} old events" if rolled else ""))
    persist_summary(day, summary, len(state), len(entered), rolled)
    for s in status:
        print("  " + s)
    # 3) notify only on new entries
    if entered:
        _send_logged("Squeeze watchlist: ZONE ENTRY",
                     "UNVALIDATED - decision support only\n" + "\n".join(entered),
                     items=entered_meta, priority="high", tags="eyes")


if __name__ == "__main__":
    main()
