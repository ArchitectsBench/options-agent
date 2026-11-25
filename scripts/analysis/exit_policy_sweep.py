"""EXIT-POLICY SWEEP. Same trades, different exit rules, real minute paths.
UNVALIDATED — PAPER ONLY. Per-trade; caps/breaker ignored. NOT GATE EVIDENCE.
Anything that looks good here is IN-SAMPLE and needs prospective testing."""
import pickle, sys, bisect
sys.path.insert(0,'/Users/you/projects/OptionsAgent'); sys.path.insert(0,'/Users/you/projects/OptionsAgent/scripts')
from enrich_trades_thetadata import pt_to_et_minute
d=pickle.load(open(sys.argv[1],'rb'))
trades,paths,vwap=d['trades'],d['paths'],d['vwap']

def vw_at(day,tk,et):
    s=vwap.get((day,tk))
    if not s: return None
    i=bisect.bisect_right([x[0] for x in s], et)-1
    return s[i][1] if i>=0 else None

def run(deadband=0.0, use_inval=True, stop=0.50, target=0.50, time_stop=None, trail=None):
    tot=0.0; n=0; reasons={}
    for t in trades:
        key=(tuple(l['symbol'] for l in t['spec']['trade']['legs']),
             tuple(l['action'] for l in t['spec']['trade']['legs']), t['day'])
        p=paths.get(key)
        if not p: continue
        tr=t['spec']['trade']; entry=t['entry']; q=t['qty']
        direction=tr.get('direction'); mp=tr.get('max_profit')
        ent_et=t['spec']['ts']
        e0=pt_to_et_minute(ent_et)
        seg=[(k,v) for k,v in p if k>=e0]
        if not seg: continue
        n+=1
        peak=entry; out=None
        for k,v in seg:
            hh,mm=int(k[:2]),int(k[3:5])
            if mm%5: continue
            if (hh,mm)>=(15,45): out=("time_flat",v); break
            mins=(hh*60+mm)-(int(e0[:2])*60+int(e0[3:5]))
            peak=max(peak,v)
            if use_inval:
                vd=vw_at(t['day'],t['tk'],k)
                if vd is not None and direction in ("long","short"):
                    if direction=="short" and vd> deadband: out=("invalidation",v); break
                    if direction=="long"  and vd< -deadband: out=("invalidation",v); break
            pnl=v-entry
            if pnl <= -stop*entry: out=("stop",v); break
            if mp and pnl >= target*float(mp): out=("target",v); break
            if not mp and pnl >= target*entry: out=("target",v); break
            if trail and peak>entry and v <= peak - trail*(peak-entry): out=("trail",v); break
            if time_stop and mins>=time_stop: out=("time_stop",v); break
        if out is None: out=("eod",seg[-1][1])
        tot += (out[1]-entry)*100*q
        reasons[out[0]]=reasons.get(out[0],0)+1
    return tot,n,reasons

base=sum(t['actual'] for t in trades if paths.get(
    (tuple(l['symbol'] for l in t['spec']['trade']['legs']),
     tuple(l['action'] for l in t['spec']['trade']['legs']), t['day'])))
print(f"ACTUAL (as traded, truth ruler): ${base:>+9,.0f}\n")
print(f"{'policy':46} {'truth P&L':>11} {'vs actual':>11}")
cur,n,r = run()
print(f"{'CURRENT RULES (replay check)':46} {cur:>+11,.0f} {cur-base:>+11,.0f}   n={n}")
print()
for db in (0.10,0.25,0.50,0.75,1.00,1.50):
    v,_,_=run(deadband=db)
    print(f"{'VWAP deadband '+str(db)+' ATR':46} {v:>+11,.0f} {v-cur:>+11,.0f}")
v,_,_=run(use_inval=False); print(f"{'NO invalidation at all':46} {v:>+11,.0f} {v-cur:>+11,.0f}")
print()
for ts in (15,30,60,120):
    v,_,_=run(time_stop=ts); print(f"{'time stop '+str(ts)+'m (invalidation ON)':46} {v:>+11,.0f} {v-cur:>+11,.0f}")
for ts in (30,60,120):
    v,_,_=run(use_inval=False,time_stop=ts); print(f"{'time stop '+str(ts)+'m, NO invalidation':46} {v:>+11,.0f} {v-cur:>+11,.0f}")
print()
for tr_ in (0.30,0.50):
    v,_,_=run(trail=tr_); print(f"{'trailing '+str(int(tr_*100))+'% giveback (inval ON)':46} {v:>+11,.0f} {v-cur:>+11,.0f}")
    v,_,_=run(use_inval=False,trail=tr_); print(f"{'trailing '+str(int(tr_*100))+'% giveback, NO inval':46} {v:>+11,.0f} {v-cur:>+11,.0f}")
print()
for tg in (0.25,0.35,0.75,1.00):
    v,_,_=run(target=tg); print(f"{'profit target '+str(int(tg*100))+'%':46} {v:>+11,.0f} {v-cur:>+11,.0f}")
for sl in (0.25,0.35,0.75):
    v,_,_=run(stop=sl); print(f"{'stop loss '+str(int(sl*100))+'%':46} {v:>+11,.0f} {v-cur:>+11,.0f}")
