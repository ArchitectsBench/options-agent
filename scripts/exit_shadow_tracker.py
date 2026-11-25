"""Nightly exit-variant shadow tracker (config/exit_variant_shadow.yaml).

For every enriched closed trade not yet tracked: compute actual truth pnl,
tp30_flat, tp_cond_30_50 (regime bias at entry minute from the day's cycle
log) and append to logs/observation/exit_variants.jsonl. Idempotent per
rec_id. Never touches live exits — measurement only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import importlib

import yaml

from src.util.atomic import read_jsonl

m = importlib.import_module("enrich_trades_thetadata")
LEDGER = ROOT / "logs" / "observation" / "exit_variants.jsonl"
START = "20260722"  # prospective window start (retro week excluded)
PEAK_TRAIL = yaml.safe_load(
    (ROOT / "config" / "exit_variant_shadow.yaml").read_text()
)["variants"]["peak_trail"]


def bias_timeline(day: str) -> list[tuple[str, str]]:
    f = ROOT / "logs" / "observation" / f"{day}.jsonl"
    if not f.exists():
        return []
    tl = []
    for line in f.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = r.get("ts") or r.get("generated_at") or ""
        b = (r.get("regime") or {}).get("bias")
        if ts and b:
            tl.append((ts, b))
    return sorted(tl)


_ATR_CACHE: dict = {}


def _spy_atr14_prior(day: str) -> float | None:
    """ATR14 of SPY daily bars as of the session BEFORE `day` (prior close —
    entry-time-available by construction). Cached per day."""
    if day in _ATR_CACHE:
        return _ATR_CACHE[day]
    val = None
    try:
        import pandas as pd
        df = pd.read_parquet(ROOT / "data" / "historical" / "SPY.parquet")
        df = df[df.index < day] if df.index.dtype == object else df[df.index < pd.Timestamp(day)]
        if len(df) >= 15:
            h, low, cprev = df["High"], df["Low"], df["Close"].shift(1)
            tr = pd.concat([h - low, (h - cprev).abs(), (low - cprev).abs()], axis=1).max(axis=1)
            val = round(float(tr.rolling(14).mean().iloc[-1]), 4)
    except Exception:
        val = None
    _ATR_CACHE[day] = val
    return val


def regime_stamp(day: str, entry_ts: str, cycles: list[dict]) -> dict:
    """AMENDMENT B (2026-08-01): entry-time-only day-regime stamp per the
    frozen formula in config/exit_variant_shadow.yaml#regime_stamp_v1.
    Uses ONLY cycle rows with ts <= entry_ts (data available at entry).
    The adoption gate's regime slices read THIS field exclusively;
    hindsight day labels are barred from gate evaluation."""
    upto = [c for c in cycles if (c.get("ts") or "") and c["ts"] <= entry_ts]
    spots = [((c.get("regime") or {}).get("gex") or {}).get("spot") for c in upto]
    spots = [s for s in spots if isinstance(s, (int, float))]
    out = {"regime_stamp": "insufficient", "regime_er": None, "regime_rr": None,
           "regime_gex_sign": None, "regime_vix": None, "regime_slice": None}
    if upto:
        last = upto[-1].get("regime") or {}
        out["regime_gex_sign"] = (last.get("gex") or {}).get("sign")
        out["regime_vix"] = last.get("vix")
    if len(spots) < 4:
        return out
    rng = max(spots) - min(spots)
    atr = _spy_atr14_prior(day)
    if rng <= 0 or not atr:
        return out
    er = round(abs(spots[-1] - spots[0]) / rng, 3)
    rr = round(rng / atr, 3)
    label = "strong" if (er >= 0.60 and rr >= 0.50) else ("chop" if er < 0.40 else "mild")
    out.update({"regime_stamp": label, "regime_er": er, "regime_rr": rr,
                "regime_slice": "trend" if label in ("strong", "mild") else "chop"})
    return out


def cycle_rows(day: str) -> list[dict]:
    f = ROOT / "logs" / "observation" / f"{day}.jsonl"
    rows = []
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("ts"):
                    rows.append(r)
            except json.JSONDecodeError:
                continue
    return sorted(rows, key=lambda r: r["ts"])


def fill_ts_map(day: str) -> dict[str, str]:
    """rec_id -> paper_fill ts from fills.jsonl (fallback entry-ts source)."""
    out: dict[str, str] = {}
    f = ROOT / "logs" / "observation" / "fills.jsonl"
    if not f.exists():
        return out
    for line in f.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") == "paper_fill" and str(r.get("ts", "")).startswith(day):
            out.setdefault(r["rec_id"], r["ts"])
    return out


def entry_ts_map(day: str) -> dict[str, str]:
    f = ROOT / "logs" / "observation" / f"{day}.jsonl"
    out: dict[str, str] = {}
    if not f.exists():
        return out
    for line in f.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        for k in ("paper_fills", "fills", "paper_events", "trades"):
            for x in (r.get(k) or []):
                if x.get("event") == "paper_fill" and x.get("rec_id"):
                    out.setdefault(x["rec_id"], x.get("ts", ""))
    return out


def main() -> None:
    done = {r["rec_id"] for r in read_jsonl(LEDGER) if r.get("rec_id")}
    rows = list(read_jsonl(ROOT / "logs" / "observation" / "enriched_trades_v1.jsonl"))
    legsmap = m.fallback_legs_map()
    n_new = 0
    tl_cache: dict[str, list] = {}
    ets_cache: dict[str, dict] = {}
    cyc_cache: dict[str, list] = {}
    fillts_cache: dict[str, dict] = {}
    with LEDGER.open("a") as out:
        for r in rows:
            rec = str(r.get("rec_id", ""))
            day8 = rec[:8]
            if (rec in done or not day8.isdigit() or day8 < START
                    or r.get("status") != "ok"
                    or r.get("true_pnl_mid") is None):
                continue
            d = f"{day8[:4]}-{day8[4:6]}-{day8[6:]}"
            act = float(r["true_pnl_mid"])
            e = r.get("true_mid_entry")
            qty = int(r.get("quantity") or 1)
            legs = legsmap.get(rec)
            if d not in tl_cache:
                tl_cache[d] = bias_timeline(d)
                ets_cache[d] = entry_ts_map(d)
                cyc_cache[d] = cycle_rows(d)
                fillts_cache[d] = fill_ts_map(d)
            # entry ts: cycle-log map first, fills.jsonl paper_fill ts as the
            # more reliable fallback (the cycle map misses some rec_ids)
            ts = ets_cache[d].get(rec, "") or fillts_cache[d].get(rec, "")
            bias = "neutral"
            for t, b in tl_cache[d]:
                if t <= ts:
                    bias = b
                else:
                    break
            trending = bias in ("bullish", "bearish")
            row = {"rec_id": rec, "date": d, "bias_at_entry": bias,
                   "lane": "SCALP" if "-TEST-" in rec else "funnel",
                   "actual": round(act, 2)}
            # AMENDMENT B (2026-08-01): entry-time-only regime stamp — the
            # adoption gate's regime slices read these fields exclusively.
            row.update(regime_stamp(d, ts, cyc_cache[d]) if ts else
                       {"regime_stamp": "insufficient", "regime_er": None,
                        "regime_rr": None, "regime_gex_sign": None,
                        "regime_vix": None, "regime_slice": None})
            if legs and e and e > 0:
                path = m.structure_path(legs, [d])
                if path is not None:
                    p = path.reset_index() if hasattr(path, "reset_index") else path
                    mids = p["mid"] if "mid" in p else p.iloc[:, -1]
                    start = int((mids - e).abs().idxmin())
                    seg = mids.iloc[start:]
                    for name, mult_t in (("tp30_flat", 1.30),
                                         ("tp_cond_30_50",
                                          1.50 if trending else 1.30)):
                        lvl = e * mult_t
                        row[name] = round((lvl - e) * 100 * qty
                                          if (seg >= lvl).any() else act, 2)
                    # peak_trail (declared 2026-07-23, slot 1): arm at
                    # arm% of risk sustained MFE, exit on retrace% from peak
                    risk = e * 100 * qty  # debit paid ~ max risk for SCALP long
                    arm_lvl = PEAK_TRAIL["arm_mfe_pct_of_risk"] / 100 * risk
                    retr = PEAK_TRAIL["trail_retrace_pct_of_peak"] / 100
                    pnl_series = (seg - e) * 100 * qty
                    armed = False
                    peak = float("-inf")
                    pt_exit = None
                    for pnl in pnl_series:
                        peak = max(peak, float(pnl))
                        if not armed and peak >= arm_lvl:
                            armed = True
                        if armed and float(pnl) <= peak * (1 - retr):
                            pt_exit = float(pnl)
                            break
                    row["peak_trail"] = round(pt_exit if pt_exit is not None else act, 2)
            row.setdefault("tp30_flat", round(act, 2))
            row.setdefault("tp_cond_30_50", round(act, 2))
            row.setdefault("peak_trail", round(act, 2))
            out.write(json.dumps(row) + "\n")
            n_new += 1
    print(f"[exit-shadow] tracked {n_new} new trades -> {LEDGER.name}")


if __name__ == "__main__":
    main()
