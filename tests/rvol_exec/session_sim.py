"""Session simulator for the RVOL-EXEC lane (spec §16.3): drives the REAL
`Lane` with the REAL `IbkrStockClient(ib=FakeIB)`, a `FakeClock`, and a
`FakePolygon` whose snapshot / per-ticker bars are synthesized from recorded
(or synthetic) 5m bars. Hermetic: no socket, no API, every path redirected
under tmp by the conftests. UNVALIDATED — PAPER ONLY.

Manual mode (read-only, not collected by pytest):
    python -m tests.rvol_exec.session_sim --bars data/research_orb/bars5m_fresh/2026-09-11 \
        --prep tests/rvol_exec/fixtures/2026-09-11/prep.parquet --out /tmp/rvx_sim
The day is the bars directory's name, or its parent's when the leaf is not
YYYY-MM-DD (the vendored fixture layout `fixtures/<day>/bars`); `--day` overrides.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import src.rvol_exec.paths as paths
from src.data.ibkr_stock_orders import IbkrStockClient
from src.rvol_exec.clock import FakeClock, at_et
from src.rvol_exec.constants import (ET, PARENT_TIF, STAGE2_EXACT_BAR, STAMP, T_ENTRY_END,
                                     T_NO_ENTRY_AFTER, TAG)
from src.rvol_exec.lane import Lane
from src.rvol_exec.ledger import Ledger
from tests.rvol_exec.fake_ib import FakeIB

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RTH_HM = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(0, 60, 5) if (h, m) >= (9, 30)]
PRINT_OFFSETS_S = (0, 15, 30, 45)


# ── bars helpers ─────────────────────────────────────────────────────────────
def load_bars_dir(d: Path, names=None) -> dict[str, pd.DataFrame]:
    out = {}
    for p in sorted(Path(d).glob("*.parquet")):
        if names is not None and p.stem not in names:
            continue
        df = pd.read_parquet(p)
        out[p.stem] = df[["hm", "o", "h", "l", "c", "v"]].reset_index(drop=True)
    return out


def make_bars(orb: tuple, path: dict | None = None, vol: float = 50_000.0) -> pd.DataFrame:
    """Synthetic 78-bar day. `orb` = (o, h, l, c, v) for 09:30; `path` maps
    hm -> (o, h, l, c) for later bars; unspecified bars are flat at the last
    close."""
    rows = []
    o, h, l, c, v = orb
    rows.append({"hm": "09:30", "o": o, "h": h, "l": l, "c": c, "v": v})
    last = c
    for hm in RTH_HM[1:]:
        if path and hm in path:
            bo, bh, bl, bc = path[hm]
        else:
            bo = bh = bl = bc = last
        rows.append({"hm": hm, "o": bo, "h": bh, "l": bl, "c": bc, "v": vol})
        last = bc
    return pd.DataFrame(rows)


def prints_for_bar(row) -> list[float]:
    o, h, l, c = float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"])
    return [o, l, h, c] if c >= o else [o, h, l, c]


# ── fake Polygon ─────────────────────────────────────────────────────────────
class FakePolygon:
    """Snapshot + per-ticker 5m bars synthesized from `bars` at the fake
    clock's time minus `delay_s` (the feed delay). Pads the snapshot with
    filler tickers so the >= 5,000 floor and the prep coverage floor hold."""

    serial = True

    def __init__(self, bars: dict[str, pd.DataFrame], day: str, clock, delay_s: float = 0.0,
                 prep_symbols=(), n_pad: int = 5200):
        self.bars = bars
        self.day = day
        self.clock = clock
        self.delay_s = float(delay_s)
        self.prep_symbols = [s for s in prep_symbols if s not in bars]
        self.n_pad = n_pad
        self.calls = {"snapshot": 0, "bars": 0, "last": 0}
        self.fail_queue: list = []
        self._starts = {s: [at_et(day, int(hm[:2]), int(hm[3:])) for hm in df["hm"]] for s, df in bars.items()}
        self.prev_day_min_t = at_et(day, 15, 59) - timedelta(days=1)

    def visible_until(self) -> datetime:
        return self.clock.now_et() - timedelta(seconds=self.delay_s)

    def _visible(self, sym: str) -> list[int]:
        vu = self.visible_until()
        return [i for i, st in enumerate(self._starts[sym]) if st + timedelta(minutes=5) <= vu]

    def snapshot_all(self):
        self.calls["snapshot"] += 1
        if self.fail_queue:
            return self.fail_queue.pop(0)
        vu = self.visible_until()
        updated_ns = int(vu.timestamp() * 1e9)
        tickers = []
        for sym, df in self.bars.items():
            idx = self._visible(sym)
            if not idx:
                tickers.append({"ticker": sym, "updated": updated_ns, "day": {},
                                "min": {"t": int(self.prev_day_min_t.timestamp() * 1000), "av": 0}})
                continue
            sub = df.iloc[idx]
            last = sub.iloc[-1]
            av = float(sub["v"].sum())
            min_t = self._starts[sym][idx[-1]] + timedelta(minutes=4)
            tickers.append({"ticker": sym, "updated": updated_ns,
                            "day": {"o": float(sub["o"].iloc[0]), "h": float(sub["h"].max()),
                                    "l": float(sub["l"].min()), "c": float(last["c"]), "v": av,
                                    "vw": float(last["c"])},
                            "min": {"t": int(min_t.timestamp() * 1000), "av": av, "o": float(last["o"]),
                                    "h": float(last["h"]), "l": float(last["l"]), "c": float(last["c"]),
                                    "v": float(last["v"])}})
        pad_min_t = int((vu - timedelta(minutes=1)).timestamp() * 1000)
        pads = list(self.prep_symbols) + [f"PAD{i:05d}" for i in range(max(0, self.n_pad - len(tickers) - len(self.prep_symbols)))]
        for s in pads:
            tickers.append({"ticker": s, "updated": updated_ns,
                            "day": {"o": 10.0, "h": 10.1, "l": 9.9, "c": 10.0, "v": 1000.0},
                            "min": {"t": pad_min_t, "av": 1000.0, "c": 10.0}})
        return {"status": "OK", "count": len(tickers), "tickers": tickers}

    def bars_5m(self, ticker: str, day: str):
        self.calls["bars"] += 1
        df = self.bars.get(ticker)
        if df is None or day != self.day:
            return None
        out = []
        for i in self._visible(ticker):
            r = df.iloc[i]
            out.append({"t": int(self._starts[ticker][i].timestamp() * 1000), "o": float(r["o"]),
                        "h": float(r["h"]), "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"])})
        return out

    def last_price(self, ticker: str):
        self.calls["last"] += 1
        df = self.bars.get(ticker)
        if df is None:
            return None
        idx = self._visible(ticker)
        return float(df.iloc[idx[-1]]["c"]) if idx else None


# ── the driver ───────────────────────────────────────────────────────────────
class InvariantViolation(AssertionError):
    pass


class SessionSim:
    def __init__(self, bars: dict[str, pd.DataFrame], prep: pd.DataFrame, day: str = "2026-09-11",
                 delay_s: float = 0.0, step_s: int = 15, fake_kw: dict | None = None,
                 lane_start=(9, 33, 0), tif: str = PARENT_TIF, stage2: bool = STAGE2_EXACT_BAR,
                 prep_meta: dict | None = None, kill_reader=None, foreign_symbols=(), dry_run=False):
        self.day = day
        self.bars = bars
        self.prep = prep
        self.step = timedelta(seconds=step_s)
        self.clock = FakeClock(at_et(day, 9, 30))
        self.fake = FakeIB(clock=self.clock, **(fake_kw or {}))
        self.polygon = FakePolygon(bars, day, self.clock, delay_s, prep_symbols=list(prep["ticker"]))
        self.lane_start = at_et(day, *lane_start)
        self.tif, self.stage2, self.dry_run = tif, stage2, dry_run
        self.kill_reader = kill_reader
        self.pages: list[tuple] = []
        self.foreign_symbols = set(foreign_symbols)
        self.lane: Lane | None = None
        self.client: IbkrStockClient | None = None
        self.exit_code: int | None = None
        self.ticks = 0
        self.seq_seen = 0
        self.max_pos_without_stop = 0
        self.heartbeats: list[dict] = []
        d = pd.Timestamp(day)
        self.prep_meta = prep_meta or {
            "prep_for": day, "built_from_through": _prev_trading_day(day), "generated_at": "x",
            "n_candidates": len(prep), "n_rankable": int(prep["baseline14"].notna().sum()),
            "coverage": {"candidates": len(prep), "with_baseline14": int(prep["baseline14"].notna().sum())}}
        self._write_prep()
        self._schedule = self._build_schedule()
        self._entry_placed_at: dict[int, datetime] = {}

    def _write_prep(self) -> None:
        paths.PREP_DIR.mkdir(parents=True, exist_ok=True)
        self.prep.to_parquet(paths.PREP_DIR / f"{self.day}.parquet")
        (paths.PREP_DIR / f"{self.day}.meta.json").write_text(json.dumps(self.prep_meta))

    def _build_schedule(self) -> dict[datetime, dict[str, list[float]]]:
        sched: dict[datetime, dict[str, list[float]]] = {}
        for sym, df in self.bars.items():
            for _, row in df.iterrows():
                start = at_et(self.day, int(row["hm"][:2]), int(row["hm"][3:]))
                for off, px in zip(PRINT_OFFSETS_S, prints_for_bar(row)):
                    sched.setdefault(start + timedelta(seconds=off), {}).setdefault(sym, []).append(px)
        return sched

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start_lane(self) -> Lane:
        self.client = IbkrStockClient(ib=self.fake, clock=self.clock, connect=False)
        ledger = Ledger(self.day, self.clock, sha="simtest", dry_run=self.dry_run)
        self.lane = Lane(self.day, self.clock, self.client, self.polygon, ledger=ledger,
                         notify=self._notify, kill_switch_reader=self.kill_reader, dry_run=self.dry_run,
                         parent_tif=self.tif, stage2=self.stage2,
                         prep_rows=self.prep.to_dict("records"), prep_meta=self.prep_meta, sha="simtest")
        return self.lane

    def kill_lane(self) -> None:
        """The process dies: the broker keeps working, the lane object is gone."""
        self.lane = None
        if self.client is not None:
            try:
                self.fake.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.client = None

    def _notify(self, title, body, priority="default"):
        self.pages.append((self.clock.now_et().isoformat(timespec="seconds"), title, body, priority))

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self, until=(16, 5, 0), stop_when=None, start_lane: bool = True) -> int | None:
        until_dt = at_et(self.day, *until) if isinstance(until, tuple) else until
        while self.clock.now_et() <= until_dt:
            t = self.clock.now_et()
            prints = self._schedule.get(t)
            self.fake.advance(t, prints)
            if self.lane is None and start_lane and t >= self.lane_start and self.exit_code is None:
                self.start_lane()
            if self.lane is not None and self.lane.s.exit_code is None:
                code = self.lane.tick()
                self.ticks += 1
                self._invariants()
                if code is not None:
                    self.exit_code = code
                    return code
            if stop_when is not None and stop_when(self):
                return None
            self.clock.set(t + self.step)
        return self.exit_code

    def run_until(self, hh, mm, ss=0, **kw):
        return self.run(until=(hh, mm, ss), **kw)

    # ── invariants after every tick (§16.3) ──────────────────────────────────
    def _invariants(self) -> None:
        lane = self.lane
        hb = lane.last_heartbeat
        self.heartbeats.append(hb)
        if lane.s.phase != "flatten":
            self.max_pos_without_stop = max(self.max_pos_without_stop, hb["positions_without_live_stop"])
        recs = lane.ledger.records
        for r in recs[self.seq_seen:]:
            if r.get("stamp") != STAMP:
                raise InvariantViolation(f"unstamped ledger line: {r}")
        seqs = [r["seq"] for r in recs]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            raise InvariantViolation("ledger seq not monotonic")
        self.seq_seen = len(recs)
        for tr in self.fake.placed:
            o = tr.order
            if (o.orderRef or "") != TAG:
                raise InvariantViolation(f"order without the lane tag: {o}")
            if tr.contract.symbol in self.foreign_symbols:
                raise InvariantViolation(f"order on a FOREIGN symbol {tr.contract.symbol}")
            if o.orderType == "STP LMT" and not o.parentId:
                placed_at = FakeIB._META[o.permId]["placed_at"].astimezone(ET)
                tt = placed_at.timetz().replace(tzinfo=None)
                if tt >= T_ENTRY_END or tt >= T_NO_ENTRY_AFTER:
                    raise InvariantViolation(f"entry placed at {placed_at} on {tr.contract.symbol}")

    # ── views ────────────────────────────────────────────────────────────────
    def events(self, name: str | None = None) -> list[dict]:
        recs = list(_read_ledger_records(self))
        return [r for r in recs if name is None or r["event"] == name]

    def names(self) -> dict:
        return self.lane.s.names if self.lane else {}


def _read_ledger_records(sim: SessionSim) -> list[dict]:
    from src.util.atomic import read_jsonl
    return [r for r in read_jsonl(paths.LEDGER) if r.get("date") == sim.day]


def _prev_trading_day(day: str) -> str:
    from datetime import date
    from src.rvol_exec.prep import previous_trading_day
    return previous_trading_day(date.fromisoformat(day)).isoformat()


# ── fixture loaders ──────────────────────────────────────────────────────────
def fixture_day(day: str = "2026-09-11", names=None) -> tuple[dict, pd.DataFrame, dict]:
    d = FIXTURES / day
    bars = load_bars_dir(d / "bars", names)
    prep = pd.read_parquet(d / "prep.parquet")
    meta = json.loads((d / "prep.meta.json").read_text())
    return bars, prep, meta


def synthetic_prep(bars: dict[str, pd.DataFrame], atr: dict[str, float], rvol: dict[str, float] | None = None,
                   n_fill: int = 600) -> pd.DataFrame:
    rows = []
    for s, df in bars.items():
        v930 = float(df[df["hm"] == "09:30"]["v"].iloc[0])
        r = (rvol or {}).get(s, 5.0)
        rows.append({"ticker": s, "atr14": float(atr[s]), "avgvol14": 2_000_000.0, "baseline14": v930 / r,
                     "n_baseline": 14, "prev_close": float(df["o"].iloc[0])})
    for i in range(n_fill):
        rows.append({"ticker": f"ZZ{i:04d}", "atr14": 1.0, "avgvol14": 2_000_000.0, "baseline14": 1e9,
                     "n_baseline": 14, "prev_close": 10.0})
    return pd.DataFrame(rows)


# ── manual mode ──────────────────────────────────────────────────────────────
def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="replay a recorded day through the lane (read-only inputs)")
    ap.add_argument("--bars", required=True, help="dir of <T>.parquet 5m bars (hm,o,h,l,c,v)")
    ap.add_argument("--prep", required=True, help="prep parquet for that day")
    ap.add_argument("--day", default=None)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--out", required=True, help="scratch root for the lane's books/ledger (never the repo)")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    if out == paths.ROOT or paths.ROOT in out.parents:
        print("refusing: --out must be outside the repo (read-only inputs, scratch outputs)")
        return 2
    for name in ("LANE_DIR", "SESSIONS_DIR", "PREP_DIR", "HEARTBEAT", "HOLD_FILE", "FLATTEN_NOW_FILE",
                 "UNFLATTENED_FILE", "PID_FILE", "LEDGER", "LOG_DIR", "KILL_SWITCH_FILE", "V930_CACHE"):
        rel = getattr(paths, name).relative_to(paths.ROOT)
        setattr(paths, name, out / rel)
    import re as _re
    leaf = Path(a.bars).resolve().name
    day = a.day or (leaf if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", leaf) else Path(a.bars).resolve().parent.name)
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        print(f"refusing: cannot infer the day from {a.bars!r}; pass --day YYYY-MM-DD")
        return 2
    bars = load_bars_dir(Path(a.bars))
    prep = pd.read_parquet(a.prep)
    sim = SessionSim(bars, prep, day=day, delay_s=a.delay)
    code = sim.run()
    closed = sim.events("session_closed")
    print(f"[{STAMP}] {day} exit={code} ticks={sim.ticks} session_closed={closed[-1] if closed else None}")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
