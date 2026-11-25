"""RVOL-RANK real-ledger FIDELITY regression (docs/rvol_squeeze_build/STATE.md
item 1c, binding before the Monday 2026-09-14 producers run).

WHY: scripts/rvolrank_sleeve.py and scripts/rvol_shadow_scan.py were hardened
on 2026-09-13 (429 backoff, coverage fields, pacing). The synthetic behaviour
lock (tests/test_rvol_producers_hardening.py) proves the K18 construction is
byte-stable on a fixture; THIS file proves it on the RECORDED EVIDENCE: every
exec-session trade in logs/observation/rvolrank_sleeve.jsonl is rebuilt from
the exact 5-minute bars the nightly pulled that night
(data/research_orb/bars5m_fresh/<date>/<TICKER>.parquet — pull_5m's own
output, RTH-filtered, with the "hm" column) through the CURRENT replay_k18 and
the CURRENT run_lane (rank-priority allocation, SLOTS cap, share cap), and must
match the ledger field-for-field. Any diff is the Monday-13:00 revert signal —
the producers are NOT to be changed to make this pass.

ORACLE WINDOW: sessions 2026-07-23 .. 2026-09-11 (34 sessions carrying
selection_exec; 07-31 has two rows — the LAST `generated` wins). Everything in
that window was produced by the PRE-hardening code, which is what makes it a
regression oracle; later rows come from the hardened code and are not
included, so the pinned counts (192 selection_exec / 205 control_exec) stay
stable as the ledger grows.

ATR SOURCE — stated per the task: the per-name ATR14 the nightly used is not
stored on any row except as stop_dist = STOP_ATR_FRAC * ATR14. The float is
recovered EXACTLY from stop_dist (nudged by ULPs until
STOP_ATR_FRAC * atr == stop_dist bit-for-bit, so the round(..., 4) on r and
r_tiers cannot flip at a boundary). No on-disk source reproduces atr14():
data/research_orb/grouped/ (the 14:10 PT grouped snapshot) reconstructs the
per-ticker daily ATR exactly for only 286 of 589 listed names with a complete
window (probed 2026-09-13), so it is NOT used. Names the ledger never traded
therefore have NO recoverable ATR; they are handled by the separate
unlisted-name check below with ATR-independent logic only.

NAME LISTS: the ledger's own trade + skip tickers, placed at their recorded
`rank` in a 20-slot list; unlisted slots are placeholders with no bars (they
skip as no_bars and are filtered from the skip comparison). This preserves the
recorded rank field and the rank-priority cap exactly.

READ-ONLY BY CONSTRUCTION: this suite reads logs/observation/ and
data/research_orb/ (the recorded evidence IS the oracle) and never writes them
— every production path constant of the loaded scripts is redirected under
tmp_path, pull_5m/get_client raise if reached (the cache is pre-filled, so no
parquet is ever written), and the O-20 seal in tests/conftest.py refuses any
production write as a second layer.

Marked `fidelity`; runs in the default suite (~1 min). Deselect with
`-m "not fidelity"`.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "logs" / "observation" / "rvolrank_sleeve.jsonl"
OBS = ROOT / "logs" / "observation"
FRESH = ROOT / "data" / "research_orb" / "bars5m_fresh"
GROUPED = ROOT / "data" / "research_orb" / "grouped"
STORE = ROOT / "data" / "research_orb" / "bars5m"

WINDOW_START, WINDOW_END = "2026-07-23", "2026-09-11"
EXPECTED_SESSIONS = 34
EXPECTED_EXEC = {"selection_exec": 192, "control_exec": 205}
TOL = 1e-6
SLOT_COUNT = 20                                   # top-20 per lane

# ledger lane key -> (run_lane lane name, entry_start window)
LANE_MAP = {
    "selection": ("selection", "09:35"),
    "selection_exec": ("selection", "09:45"),
    "control": ("control", "09:35"),
    "control_exec": ("control", "09:45"),
}
FIELDS_EXACT = ("ticker", "fill_t", "side", "reason", "rank", "lane", "entry_window")
FIELDS_FLOAT = ("shares", "r", "pnl_per_share", "stop_dist", "open_vol", "pnl",
                "spread_proxy")
PLACEHOLDER = "~"                                 # unlisted rank slot marker

pytestmark = pytest.mark.fidelity


# ═══════════════════════════════════════════════════════════════════════════
# Loading the producers (fresh module objects; production paths -> tmp)
# ═══════════════════════════════════════════════════════════════════════════
def _load(name: str, rel: str, tmp_path: Path, monkeypatch, path_attrs):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for attr in path_attrs:
        assert hasattr(m, attr), attr
        monkeypatch.setattr(m, attr, tmp_path / name / attr.lower())
        assert not str(getattr(m, attr)).startswith(str(ROOT)), attr
    monkeypatch.setattr(m, "REQ_SLEEP", 0.0)
    return m


@pytest.fixture
def sleeve(tmp_path, monkeypatch):
    m = _load("rvolrank_sleeve_fidelity", "scripts/rvolrank_sleeve.py", tmp_path,
              monkeypatch, ("FRESH", "OBS_DIR", "LEDGER", "ALERT_LOG"))
    monkeypatch.setattr(m, "OUTER_RETRY_SLEEP_S", 0.0)

    def _no_network(*a, **k):
        raise AssertionError("fidelity replay reached the network/pull path — "
                             "the bars cache must be pre-filled")
    monkeypatch.setattr(m, "get_client", _no_network)
    monkeypatch.setattr(m, "pull_5m", _no_network)
    m.PULL_ERRORS.clear()
    return m


@pytest.fixture
def scan(tmp_path, monkeypatch):
    return _load("rvol_shadow_scan_fidelity", "scripts/rvol_shadow_scan.py",
                 tmp_path, monkeypatch, ("OUT_DIR", "RAW"))


# ═══════════════════════════════════════════════════════════════════════════
# Ledger helpers
# ═══════════════════════════════════════════════════════════════════════════
def _sessions() -> dict[str, dict]:
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]
    byd: dict[str, dict] = {}
    for r in rows:
        d = r.get("date", "")
        if "selection_exec" not in r or not (WINDOW_START <= d <= WINDOW_END):
            continue
        if d not in byd or r["generated"] > byd[d]["generated"]:
            byd[d] = r                                   # last `generated` wins
    return dict(sorted(byd.items()))


def _exact_atr(stop_dist: float, frac: float) -> float:
    """The float a with frac * a == stop_dist bit-for-bit (a = stop_dist/frac
    can be one ULP off); falls back to the plain quotient if none within 16."""
    a = stop_dist / frac
    if frac * a == stop_dist:
        return a
    lo = hi = a
    for _ in range(16):
        lo, hi = math.nextafter(lo, -math.inf), math.nextafter(hi, math.inf)
        for c in (lo, hi):
            if frac * c == stop_dist:
                return c
    return a


def _atr_map(row: dict, frac: float) -> dict[str, float]:
    """One ATR per (date, ticker) from every lane/window that traded it —
    a name capped out of selection may carry its stop_dist in control."""
    atr: dict[str, float] = {}
    for key in LANE_MAP:
        for t in row[key]["trades"]:
            a = _exact_atr(t["stop_dist"], frac)
            prev = atr.setdefault(t["ticker"], a)
            assert abs(prev - a) <= 1e-9 * max(1.0, a), \
                (row["date"], t["ticker"], "stop_dist inconsistent across lanes")
    return atr


def _lane_inputs(row: dict, lane: str):
    """(names at their recorded ranks, ledger skips) for one lane."""
    ranked: dict[str, int] = {}
    for key in (lane, lane + "_exec"):
        for t in row[key]["trades"]:
            r = ranked.setdefault(t["ticker"], t["rank"])
            assert r == t["rank"], (row["date"], lane, t["ticker"], "rank differs across windows")
    by_rank = {r: t for t, r in ranked.items()}
    assert len(by_rank) == len(ranked), (row["date"], lane, "duplicate rank")
    n_slots = max(SLOT_COUNT, max(by_rank) if by_rank else 0)
    names = [by_rank.get(r, f"{PLACEHOLDER}{r}") for r in range(1, n_slots + 1)]
    skips = [(s[0], s[1]) for s in row[lane]["skips"]]
    return names, skips


def _fill_cache(day: str, names: list[str], skips: list[tuple[str, str]]):
    """Pre-fill run_lane's bars cache from bars5m_fresh; returns (cache, missing
    traded names). A no_bars skip is replayed as None (fidelity to the recorded
    pull); a no_atr skip loads its bars (they were stored before the ATR pull)."""
    cache: dict = {}
    missing: list[str] = []
    for t in names:
        if t.startswith(PLACEHOLDER):
            cache[t] = None
            continue
        f = FRESH / day / f"{t}.parquet"
        if f.exists():
            cache[t] = pd.read_parquet(f)
        else:
            missing.append(t)
    for t, reason in skips:
        f = FRESH / day / f"{t}.parquet"
        if reason.split(":")[0] == "no_bars" or not f.exists():
            cache[t] = None
        else:
            cache[t] = pd.read_parquet(f)
    return cache, missing


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= TOL


def _compare(day: str, key: str, ledger: list[dict], replay: list[dict], diffs: list) -> int:
    """Field-for-field comparison in ledger order; returns the number of ledger
    trades reproduced completely. Every difference lands in `diffs` as
    (date, lane_key, ticker, field, ledger_value, replay_value)."""
    if len(ledger) != len(replay):
        diffs.append((day, key, "*", "n_trades", len(ledger), len(replay)))
    n_ok = 0
    for i, lt in enumerate(ledger):
        if i >= len(replay):
            diffs.append((day, key, lt["ticker"], "missing_in_replay", lt["fill_t"], None))
            continue
        rt = replay[i]
        ok = True
        for f in FIELDS_EXACT:
            if lt.get(f) != rt.get(f):
                diffs.append((day, key, lt["ticker"], f, lt.get(f), rt.get(f)))
                ok = False
        for f in FIELDS_FLOAT:
            if not _close(lt.get(f), rt.get(f)):
                diffs.append((day, key, lt["ticker"], f, lt.get(f), rt.get(f)))
                ok = False
        lt_tiers, rt_tiers = lt.get("r_tiers") or {}, rt.get("r_tiers") or {}
        for tier in sorted(set(lt_tiers) | set(rt_tiers)):
            if not _close(lt_tiers.get(tier), rt_tiers.get(tier)):
                diffs.append((day, key, lt["ticker"], f"r_tiers.{tier}",
                              lt_tiers.get(tier), rt_tiers.get(tier)))
                ok = False
        n_ok += ok
    for rt in replay[len(ledger):]:
        diffs.append((day, key, rt["ticker"], "extra_in_replay", None, rt["fill_t"]))
    return n_ok


# ═══════════════════════════════════════════════════════════════════════════
# 1. Every recorded exec-session trade reproduces through the CURRENT code
# ═══════════════════════════════════════════════════════════════════════════
def test_ledger_trades_reproduce_from_fresh_bars(sleeve):
    sessions = _sessions()
    assert len(sessions) == EXPECTED_SESSIONS, sorted(sessions)
    total = {k: 0 for k in LANE_MAP}
    reproduced = {k: 0 for k in LANE_MAP}
    shortfall = {k: 0 for k in LANE_MAP}
    not_rebuildable: list[tuple[str, str, list[str]]] = []
    skip_diffs: list = []
    diffs: list = []

    for day, row in sessions.items():
        for key in LANE_MAP:
            assert row[key]["n"] == len(row[key]["trades"]), (day, key, "ledger n != trades")
            total[key] += row[key]["n"]
        atr = _atr_map(row, sleeve.STOP_ATR_FRAC)
        sleeve.atr14 = lambda client, t, d, _atr=atr: _atr.get(t)   # ledger-recovered ATR
        for lane in ("selection", "control"):
            names, skips = _lane_inputs(row, lane)
            cache, missing = _fill_cache(day, names, skips)
            keys = [k for k, (ln, _) in LANE_MAP.items() if ln == lane]
            if missing:
                not_rebuildable.append((day, lane, missing))
                for k in keys:
                    shortfall[k] += row[k]["n"]
                continue
            out, got_skips = sleeve.run_lane(None, day, lane, names + [s for s, _ in skips], cache)
            for k in keys:
                reproduced[k] += _compare(day, k, row[k]["trades"], out[LANE_MAP[k][1]], diffs)
            want = sorted((t, r.split(":")[0]) for t, r in skips)
            got = sorted((t, r.split(":")[0]) for t, r in got_skips if not t.startswith(PLACEHOLDER))
            if want != got:
                skip_diffs.append((day, lane, want, got))

    print("\nRVOL-RANK ledger fidelity — sessions", WINDOW_START, "..", WINDOW_END,
          f"({len(sessions)} sessions)")
    for k in LANE_MAP:
        print(f"  {k:15s} reproduced {reproduced[k]}/{total[k]}"
              f"  (not rebuildable: {shortfall[k]})")
    if not_rebuildable:
        print("  NOT REBUILDABLE (bars missing on disk):")
        for day, lane, missing in not_rebuildable:
            print(f"    {day} {lane}: {missing}")
    for d in diffs:
        print("  DIFF", d)
    for d in skip_diffs:
        print("  SKIP-DIFF", d)

    assert total["selection_exec"] == EXPECTED_EXEC["selection_exec"], total
    assert total["control_exec"] == EXPECTED_EXEC["control_exec"], total
    assert not diffs, "\n".join(
        f"{d[0]} {d[1]} {d[2]} {d[3]}: ledger={d[4]!r} replay={d[5]!r}" for d in diffs)
    assert not skip_diffs, skip_diffs
    for k in LANE_MAP:
        assert reproduced[k] == total[k] - shortfall[k], (k, reproduced[k], total[k], shortfall[k])
    # the headline claim of item 1c (holds while every session's bars are on disk)
    if not not_rebuildable:
        assert reproduced["selection_exec"] == 192 and reproduced["control_exec"] == 205


# ═══════════════════════════════════════════════════════════════════════════
# 2. Names on disk the ledger never traded must not enter a lane
# ═══════════════════════════════════════════════════════════════════════════
def test_unlisted_names_cannot_enter_a_lane(sleeve):
    """For every parquet in bars5m_fresh/<date>/ that no lane traded or
    skipped, the CURRENT replay must return None in a window, or the recorded
    lane must have been at the SLOTS cap with the name ranked past the 10th
    slot. The ATR of these names is not recoverable, so the check is
    ATR-independent: replay with an unbounded ATR (the cancel guardrail can
    never fire) and with a vanishing ATR (any gap fill cancels). A trade under
    the vanishing ATR is a trade under every ATR; a None under the unbounded
    ATR is a None under every ATR. Gap-fill names whose outcome depends on the
    unknown ATR are counted and printed, never asserted."""
    sessions = _sessions()
    defects: list = []
    n_independent_none = n_dependent = n_cap_ok = n_foreign = 0
    dependent: list = []
    for day, row in sessions.items():
        listed = {t["ticker"] for k in LANE_MAP for t in row[k]["trades"]}
        listed |= {s[0] for k in ("selection", "control") for s in row[k]["skips"]}
        shadow = json.loads((OBS / f"rvol_shadow-{day}.json").read_text())
        sel_rank = {x["ticker"]: i for i, x in enumerate(shadow["top20"], 1)}
        files = {f.stem: f for f in (FRESH / day).glob("*.parquet")}
        # The directory is SHARED with another producer: scripts/
        # bars5m_traded_refresh.py (nightly after the sleeve, or the catch-up
        # sweep) pulls the FUNNEL/SCALP lanes' traded names (BAC, BABA, SMCI,
        # XOM, ...) into the same date folder, so "on disk" is not "pulled by
        # the sleeve". The sleeve's own pulls are the shadow top-20 (exact
        # membership) plus every file written between its first selection pull
        # and the record's `generated` stamp (control names — run_lane writes
        # each parquet the moment it is pulled). `generated` is a naive LOCAL
        # (PT) stamp, hence datetime.fromisoformat, not pd.Timestamp (UTC).
        generated = datetime.fromisoformat(row["generated"]).timestamp()
        sel_mtimes = [files[t].stat().st_mtime for t in sel_rank if t in files
                      and files[t].stat().st_mtime <= generated + 60]
        assert sel_mtimes, (day, "no selection parquet inside the run window")
        run_start = min(sel_mtimes)
        for t in sorted(files):
            f = files[t]
            if t in listed:
                continue
            if t not in sel_rank and not (run_start <= f.stat().st_mtime <= generated + 60):
                n_foreign += 1                           # the radar's file, not a lane name
                continue
            bars = pd.read_parquet(f)
            lane = "selection" if t in sel_rank else "control"
            for es in ("09:35", "09:45"):
                key = lane if es == "09:35" else lane + "_exec"
                unbounded = sleeve.replay_k18(bars, 1e9, entry_start=es)
                if unbounded is None:
                    n_independent_none += 1
                    continue
                vanishing = sleeve.replay_k18(bars, 1e-12, entry_start=es)
                if vanishing is None:
                    n_dependent += 1                     # gap fill: cancel depends on ATR
                    dependent.append((day, key, t))
                    continue
                # trades under every ATR: only the rank-priority cap explains its absence
                ranks = [x["rank"] for x in row[key]["trades"]]
                capped = row[key]["n"] >= sleeve.SLOTS
                past_cap = (sel_rank[t] > max(ranks)) if lane == "selection" else True
                if capped and past_cap:
                    n_cap_ok += 1
                else:
                    defects.append((day, key, t, "trades regardless of ATR but lane not at cap"
                                    if not capped else f"rank {sel_rank.get(t)} inside the cap"))
    print(f"\nunlisted names (name-windows): ATR-independent None={n_independent_none}, "
          f"dropped by the SLOTS cap={n_cap_ok}, ATR-dependent (unverifiable)={n_dependent}; "
          f"foreign files ignored={n_foreign}")
    for d in dependent:
        print("  ATR-dependent:", d)
    for d in defects:
        print("  DEFECT", d)
    assert not defects, defects


# ═══════════════════════════════════════════════════════════════════════════
# 3. Rank fidelity: one recorded day's top-20 from the CURRENT rank_candidates
# ═══════════════════════════════════════════════════════════════════════════
RANK_DAY = "2026-07-15"
RANK_KNOWN_ABSENT = {"BTSG"}        # its universe-store backfill ends 2024-09-11


class _StoreClient:
    """Serves data/research_orb/bars5m/<T>.parquet (same endpoint, adjusted=true,
    RTH-filtered — scripts/bars5m_backfill.py) in Polygon's aggregate shape,
    restricted to [start, today] exactly as the live request would be."""

    def __init__(self, start: str, today: str):
        self.start, self.today, self.calls = start, today, 0

    def _get(self, url: str) -> dict:
        self.calls += 1
        t = url.split("/ticker/")[1].split("/")[0]
        f = STORE / f"{t}.parquet"
        if not f.exists():
            return {"results": []}
        df = pd.read_parquet(f, columns=["date", "hm", "o", "h", "l", "c", "v"],
                             filters=[("date", ">=", self.start), ("date", "<=", self.today)])
        if df.empty:
            return {"results": []}
        ts = pd.to_datetime(df["date"] + " " + df["hm"]).dt.tz_localize("US/Eastern")
        ms = ts.dt.as_unit("ms").astype("int64")
        return {"results": [{"t": int(m), "o": o, "h": h, "l": l, "c": c, "v": v}
                            for m, o, h, l, c, v in zip(ms, df["o"], df["h"], df["l"], df["c"], df["v"])]}


def test_rank_fidelity_reproduces_logged_top20(scan):
    """The universe store (data/research_orb/bars5m/) covers the candidate
    universe through 2026-07-17, so a recorded day inside the store's range is
    replayed in full: the candidate filter as the scan computes it from the
    grouped store, then the CURRENT rank_candidates over every candidate. On
    2026-07-15 the logged top-20 carries no post-scan volume revisions (07-16
    and 07-17 each differ by a few v930 units on 2-4 names); one name (BTSG)
    has no store coverage and is listed explicitly."""
    logged = json.loads((OBS / f"rvol_shadow-{RANK_DAY}.json").read_text())
    # ── candidate filter: copied from scripts/rvol_shadow_scan.py main() step 2
    # ("point-in-time filters from the trailing store") — deliberately NOT
    # factored out of the producer, which is under test as committed.
    files = [f for f in sorted(GROUPED.glob("2*.parquet")) if f.stem <= RANK_DAY][-16:]
    hist = []
    for f in files:
        d = pd.read_parquet(f)
        d["date"] = f.stem
        hist.append(d)
    h = pd.concat(hist, ignore_index=True).sort_values(["ticker", "date"])
    g = h.groupby("ticker", sort=False)
    prev_close = g["close"].shift(1)
    tr = pd.concat([h["high"] - h["low"], (h["high"] - prev_close).abs(),
                    (h["low"] - prev_close).abs()], axis=1).max(axis=1)
    h["atr14"] = tr.groupby(h["ticker"]).transform(lambda s: s.rolling(14).mean().shift(1))
    h["avgvol14"] = g["volume"].transform(lambda s: s.rolling(14).mean().shift(1))
    cand = h[(h["date"] == RANK_DAY) & (h["open"] >= 5.0)
             & (h["avgvol14"] >= 1_000_000) & (h["atr14"] > 0.50)]
    assert len(cand) == logged["candidates_scanned"] == 1875, len(cand)

    start = (date.fromisoformat(RANK_DAY) - timedelta(days=30)).isoformat()
    client = _StoreClient(start, RANK_DAY)
    results, stats = scan.rank_candidates(client, list(cand["ticker"]), RANK_DAY, start)
    assert client.calls == len(cand)

    by_t = {r["ticker"]: r for r in results}
    logged_top = [(r["ticker"], r["rvol"], r["v930"]) for r in logged["top20"]]
    absent = [t for t, _, _ in logged_top if t not in by_t]
    print(f"\nrank fidelity {RANK_DAY}: candidates {len(cand)}, ranked {len(results)}, "
          f"store misses {stats['scan_errors']}, logged top-20 absent from store: {absent}")
    assert set(absent) <= RANK_KNOWN_ABSENT, absent
    covered = [x for x in logged_top if x[0] not in absent]
    replay_top = [(r["ticker"], r["rvol"], r["v930"]) for r in results[:len(covered)]]
    for want, got in zip(covered, replay_top):
        print(f"  {want[0]:6s} logged rvol={want[1]} v930={want[2]} | replay {got}")
    assert replay_top == covered, (covered, replay_top)
    assert len(covered) == 20 - len(absent) >= 19
