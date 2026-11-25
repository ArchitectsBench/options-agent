"""EXIT-DESIGN GRID on the SCALP attempt-path cache (2026-09-24).

PRE-DECLARED before the run (operator asked for it after an outside review of
the TSLA 09-16 trade, which was +17% at 08:55 and exited -25% at 09:25):

  ENTRY held fixed at the live design: marketable limit at the natural
  (mid + 0.5*spread), filled at the decision minute; $1 floor, per-ticker
  dedup, max_open 7, 10 fills / 30 attempts per day — identical to
  scripts/analysis/v2_counterfactual.py so the baseline can be calibrated.

  BASELINE = today's exits, checked every 5 minutes: flat 15:45 ET; VWAP
  invalidation; stop at -50% of entry; target at +50% of max profit (spreads)
  or +50% of entry (single legs).

  HYPOTHESIS (stated up front): premium protection checked every MINUTE beats
  the 5-minute check on 0DTE, and a peak-trail cuts the give-back pattern.

  CELLS (each changes ONE thing vs baseline, except the declared combo):
    M1          stop/target checked every minute (VWAP still every 5 min)
    S35, S25    stop at -35% / -25%
    T15, T30    time stop: exit if not above entry 15 / 30 min after entry
    T30_10      time stop: exit if not +10% within 30 min
    TR15_BE     once +15%, exit at breakeven
    TR20_HALF   once +20%, exit if half the peak gain is given back
    G35, G25    nearer target: 35% / 25%
    NOVWAP      no VWAP invalidation (stop/target/time only)
    M1_TR15_BE  the hypothesis combo: minute checks + breakeven trail

  SCORING: truth = exit value - entry; realistic = truth - exit half-spread
  (0.039) - commissions (0.65/contract/leg, both ways). By structure and by
  split half. In-sample (same data as every earlier replay): the best cell is
  a CANDIDATE for a v3 pre-registration, never a verdict.

Run: .venv/bin/python scripts/analysis/exit_design_grid.py data/cache/attempt_paths_polygon.pkl [out.json]
UNVALIDATED — PAPER ONLY. NOT gate evidence.
"""
import collections
import json
import pickle
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "scripts" / "analysis"))
from enrich_trades_thetadata import pt_to_et_minute  # noqa: E402
import v2_counterfactual as cf  # noqa: E402

EXIT_HALF, COMM = cf.EXIT_HALF, cf.COMM
d = pickle.load(open(sys.argv[1], "rb"))
A, P, V = d["attempts"], d["paths"], d["vwap"]
cf.A, cf.P, cf.V = A, P, V

BASE = dict(minute=False, stop=0.50, tgt=0.50, time_stop=None, trail=None, vwap=True)
CELLS = {
    "BASELINE": {},
    "M1": dict(minute=True),
    "S35": dict(stop=0.35), "S25": dict(stop=0.25),
    "T15": dict(time_stop=(15, 0.0)), "T30": dict(time_stop=(30, 0.0)), "T30_10": dict(time_stop=(30, 0.10)),
    "TR15_BE": dict(trail=(0.15, "be")), "TR20_HALF": dict(trail=(0.20, "half")),
    "G35": dict(tgt=0.35), "G25": dict(tgt=0.25),
    "NOVWAP": dict(vwap=False),
    "M1_TR15_BE": dict(minute=True, trail=(0.15, "be")),
}


def mins(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def replay(a, entry, R):
    t = a["trade"]
    key = (tuple(x["symbol"] for x in t["legs"]), tuple(x["action"] for x in t["legs"]), a["day"])
    p = P.get(key)
    if not p:
        return None
    e0 = pt_to_et_minute(a["ts"]); e0m = mins(e0)
    seg = [(k, v) for k, v in p if k >= e0]
    if not seg:
        return None
    single = len(t["legs"]) == 1
    maxp = t.get("max_profit")
    tgt_gain = (R["tgt"] * entry) if (single or not maxp) else (R["tgt"] * float(maxp))
    stop_loss = R["stop"] * entry
    direction = t.get("direction")
    peak = entry
    for k, v in seg:
        km = mins(k)
        five = (km % 5 == 0)
        if not five and not R["minute"]:
            continue
        if km >= 15 * 60 + 45:
            return ("time_flat", v, k)
        peak = max(peak, v)
        pnl = v - entry
        # premium rules — every minute under M1, else on the 5-minute grid
        if pnl <= -stop_loss:
            return ("stop", v, k)
        if pnl >= tgt_gain:
            return ("target", v, k)
        if R["trail"]:
            arm, kind = R["trail"]
            if peak >= entry * (1 + arm):
                floor = entry if kind == "be" else entry + 0.5 * (peak - entry)
                if v <= floor:
                    return ("trail", v, k)
        if R["time_stop"] and km - e0m >= R["time_stop"][0] and v <= entry * (1 + R["time_stop"][1]):
            return ("time_stop", v, k)
        # VWAP invalidation lives on the 5-minute signal grid regardless
        if R["vwap"] and five and direction in ("long", "short"):
            vd = cf.vw_at(a["day"], a["ticker"], k)
            if vd is not None and ((direction == "long" and vd < 0) or (direction == "short" and vd > 0)):
                return ("vwap", v, k)
    return ("eod", seg[-1][1], seg[-1][0])


def run(R, days, floor=1.0, max_fill=10, max_att=30, max_open=7):
    rows = []; byday = collections.defaultdict(list)
    for a in A:
        byday[a["day"]].append(a)
    for day in sorted(byday):
        if day not in days:
            continue
        fills = att = 0; busy = []
        for a in sorted(byday[day], key=lambda x: x["ts"]):
            t = a["trade"]
            if float(t.get("net_debit") or 0) < floor:
                continue
            et = pt_to_et_minute(a["ts"]); busy = [(tk, x) for tk, x in busy if x > et]
            if any(tk == a["ticker"] for tk, _ in busy):
                continue
            if len(busy) >= max_open or att >= max_att or fills >= max_fill:
                continue
            att += 1
            entry = cf.natural(t, 0.5); out = replay(a, entry, R)
            if out is None:
                continue
            fills += 1; q = int(a.get("qty") or 1); nl = len(t["legs"])
            truth = (out[1] - entry) * 100 * q
            real = truth - EXIT_HALF * 100 * q - COMM * q * nl * 2
            rows.append(dict(day=day, n=nl, truth=truth, real=real, reason=out[0]))
            busy.append((a["ticker"], out[2]))
    return rows


def summ(rows):
    if not rows:
        return None
    r = [x["real"] for x in rows]
    return dict(n=len(rows), truth=st.mean(x["truth"] for x in rows), real=st.mean(r),
                win=sum(x > 0 for x in r) / len(r))


live = {r["day"] for r in cf.v1_actual(None)} & {a["day"] for a in A}
ds = sorted(live); H1, H2 = set(ds[: len(ds) // 2]), set(ds[len(ds) // 2:])
print(f"ruler: {d.get('source','thetadata')} | live days {len(live)} (H1 {len(H1)}, H2 {len(H2)})")
cal = cf.run(days=live)
print(f"CALIBRATION — v2_counterfactual.run() on this cache: n={len(cal)} "
      f"realistic/trade {st.mean(r['real'] for r in cal):+.1f}  (ThetaData reference: -44.2, n=309)\n")
hdr = f"{'cell':12} {'struct':7} {'n':>4} {'truth/t':>8} {'real/t':>7} {'win':>4} | {'H1 real':>8} {'H2 real':>8}  exits"
print(hdr)
out = {}
for name, over in CELLS.items():
    R = {**BASE, **over}
    rows = run(R, live); r1 = run(R, H1); r2 = run(R, H2)
    for struct, pick in (("all", lambda x: True), ("0dte", lambda x: x["n"] == 1), ("spread", lambda x: x["n"] > 1)):
        s = summ([x for x in rows if pick(x)]); s1 = summ([x for x in r1 if pick(x)]); s2 = summ([x for x in r2 if pick(x)])
        if not s:
            continue
        mix = dict(collections.Counter(x["reason"] for x in rows if pick(x)).most_common(4)) if struct == "all" else ""
        print(f"{name if struct=='all' else '':12} {struct:7} {s['n']:>4} {s['truth']:>+8.1f} {s['real']:>+7.1f} {s['win']:>4.0%} | "
              f"{(s1 or {}).get('real', float('nan')):>+8.1f} {(s2 or {}).get('real', float('nan')):>+8.1f}  {mix}")
        out[f"{name}|{struct}"] = dict(all=s, h1=s1, h2=s2)
if len(sys.argv) > 2:
    json.dump(out, open(sys.argv[2], "w"), indent=1)
