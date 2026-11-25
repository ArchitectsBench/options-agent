"""FILL-DESIGN GRID on the pre-window attempt cache (SCALP, 2026-06-16..09-11).
PRE-DECLARED before the run (2026-09-16):
  cells = offset from decision mid {mid, mid+0.25w, natural=mid+0.5w}
        x timeout {0=IOC, 5, 10, 15, 30 min}  x liquidity gate {off, width/debit<=5%}
  v1 = (mid, 30, off).  v2 = (natural, 0, off).
  HYPOTHESIS (monotone, stated up front): a limit AT the decision mid with a SHORT
  timeout (5-10 min) beats both endpoints on realistic P&L per trade, while
  keeping a fill rate >= 40%.  Anything else the grid shows is a candidate to
  PRE-REGISTER on a fresh window, never a verdict: this is the same 36-day
  sample v2 was pre-registered on (in-sample; picking the max cell = mining).
Fill rule (conservative): a resting BUY fills at the first minute within the
timeout where net_mid <= limit - 0.5*width_at_decision (the ask must touch the
limit, not the mid). IOC (timeout 0) assumes the fill at the natural (upper
bound, as the original replay). Fill price = limit. Exits replay UNCHANGED rules
from the FILL minute. truth = exit_mid - limit; realistic = truth - exit
half-spread (0.039) - commissions (0.65/contract/leg, both ways).
UNVALIDATED - PAPER ONLY. NOT gate evidence."""
import pickle, sys, bisect, json, statistics as st, collections
sys.path.insert(0, '/Users/you/projects/OptionsAgent'); sys.path.insert(0, '/Users/you/projects/OptionsAgent/scripts')
sys.path.insert(0, '/Users/you/projects/OptionsAgent/scripts/analysis')
from enrich_trades_thetadata import pt_to_et_minute
from src.engine.exit_manager import evaluate_exit
from datetime import datetime, time
import v2_counterfactual as cf

CFG = cf.CFG; EXIT_HALF = cf.EXIT_HALF; COMM = cf.COMM
d = pickle.load(open(sys.argv[1], 'rb')); A, P, V = d['attempts'], d['paths'], d['vwap']
cf.A, cf.P, cf.V = A, P, V

def mins(k): return int(k[:2]) * 60 + int(k[3:5])
def width(t): return sum(float(l['ask']) - float(l['bid']) for l in t['legs'])
def mid0(t):
    return float(t['legs'][0]['mid']) if len(t['legs']) == 1 else float(t['net_debit'])

def exits_from(a, k_fill, entry):
    key = (tuple(x['symbol'] for x in a['trade']['legs']), tuple(x['action'] for x in a['trade']['legs']), a['day'])
    p = P.get(key)
    seg = [(k, v) for k, v in p if k > k_fill]
    t = a['trade']; pos = {"direction": t.get('direction'), "entry_price": entry, "max_profit": t.get('max_profit')}
    for k, v in seg:
        hh, mm = int(k[:2]), int(k[3:5])
        if mm % 5: continue
        if (hh, mm) >= (15, 45): return ("time_flat", v, k)
        vd = cf.vw_at(a['day'], a['ticker'], k)
        r = evaluate_exit(pos, v, {"vwap_dist_atr": vd},
                          datetime.combine(datetime.strptime(a['day'], "%Y-%m-%d").date(), time(hh, mm)), CFG)
        if r: return (r.split(' (')[0], v, k)
    return ("eod", seg[-1][1], seg[-1][0]) if seg else None

def run(offset, timeout, gate, days, floor=1.0, max_fill=10, max_att=30, max_open=7):
    rows = []; att_n = 0; byday = collections.defaultdict(list)
    for a in A: byday[a['day']].append(a)
    for day in sorted(byday):
        if day not in days: continue
        fills = att = 0; busy = []   # (ticker, until_minute)
        for a in sorted(byday[day], key=lambda x: x['ts']):
            t = a['trade']
            if float(t.get('net_debit') or 0) < floor: continue
            w = width(t); m0 = mid0(t)
            if gate and m0 > 0 and w / m0 > 0.05: continue
            key = (tuple(x['symbol'] for x in t['legs']), tuple(x['action'] for x in t['legs']), a['day'])
            if not P.get(key): continue
            e0 = pt_to_et_minute(a['ts']); e0m = mins(e0)
            busy = [(tk, u) for tk, u in busy if u > e0m]
            if any(tk == a['ticker'] for tk, _ in busy): continue
            if len(busy) >= max_open or att >= max_att or fills >= max_fill: continue
            att += 1; att_n += 1
            L = round(m0 + offset * w, 4)
            if timeout == 0:
                k_fill = e0; entry = round(m0 + 0.5 * w, 4)          # IOC at the natural (upper bound)
            else:
                k_fill = None
                for k, v in P[key]:
                    km = mins(k)
                    if km < e0m: continue
                    if km > e0m + timeout: break
                    if v <= L - 0.5 * w: k_fill = k; break
                entry = L
                if k_fill is None:
                    busy.append((a['ticker'], e0m + timeout)); continue
            out = exits_from(a, k_fill, entry)
            if out is None: continue
            fills += 1; q = int(a.get('qty') or 1); nlegs = len(t['legs'])
            truth = (out[1] - entry) * 100 * q; real = truth - EXIT_HALF * 100 * q - COMM * q * nlegs * 2
            rows.append(dict(day=day, tk=a['ticker'], n=nlegs, truth=truth, real=real, reason=out[0],
                             wait=mins(k_fill) - e0m))
            busy.append((a['ticker'], mins(out[2])))
    return rows, att_n

def summ(rows, att):
    if not rows: return dict(n=0)
    tr = [r['truth'] for r in rows]; re_ = [r['real'] for r in rows]
    return dict(n=len(rows), fill=len(rows) / att if att else 0, truth=sum(tr) / len(tr), real=sum(re_) / len(re_),
                win=sum(1 for x in re_ if x > 0) / len(rows), vwap=sum(1 for r in rows if r['reason'].startswith('vwap')) / len(rows),
                spreads=sum(1 for r in rows if r['n'] > 1))

live = {r['day'] for r in cf.v1_actual(None)}
ds = sorted(live); h = len(ds) // 2; H1, H2 = set(ds[:h]), set(ds[h:])
cells = []
for gate in (False, True):
    for off, offl in ((0.0, 'mid'), (0.25, 'mid+.25w'), (0.5, 'natural')):
        for to in ((0,) if off == 0.5 else (0, 5, 10, 15, 30)):
            rows, att = run(off, to, gate, live)
            r1, a1 = run(off, to, gate, H1); r2, a2 = run(off, to, gate, H2)
            s = summ(rows, att); s1 = summ(r1, a1); s2 = summ(r2, a2)
            cells.append(dict(gate=gate, offset=offl, timeout=to, att=att, **s,
                              h1=s1.get('real'), h2=s2.get('real'), n1=s1.get('n'), n2=s2.get('n')))
print(f"live days={len(live)}  (first half {len(H1)} d, second half {len(H2)} d)")
print(f"{'gate':4} {'offset':9} {'to':>3} {'att':>4} {'fills':>5} {'fill%':>5} {'truth/t':>8} {'real/t':>7} {'win':>4} {'vwapx':>5} {'spr':>4} | {'H1 real/t (n)':>16} {'H2 real/t (n)':>16}")
for c in cells:
    if c['n'] == 0: print(f"{'on' if c['gate'] else 'off':4} {c['offset']:9} {c['timeout']:>3} {c['att']:>4}  no fills"); continue
    print(f"{'on' if c['gate'] else 'off':4} {c['offset']:9} {c['timeout']:>3} {c['att']:>4} {c['n']:>5} {c['fill']:>5.0%} {c['truth']:>+8.1f} {c['real']:>+7.1f} {c['win']:>4.0%} {c['vwap']:>5.0%} {c['spreads']:>4} | "
          f"{(c['h1'] if c['h1'] is not None else float('nan')):>+9.1f} ({c['n1']:>3})  {(c['h2'] if c['h2'] is not None else float('nan')):>+9.1f} ({c['n2']:>3})")
json.dump(cells, open(sys.argv[2], 'w'), indent=1) if len(sys.argv) > 2 else None
