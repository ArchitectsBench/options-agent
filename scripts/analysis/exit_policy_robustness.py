import pickle, sys, bisect, statistics as st
sys.path.insert(0,'/Users/you/projects/OptionsAgent'); sys.path.insert(0,'/Users/you/projects/OptionsAgent/scripts')
from enrich_trades_thetadata import pt_to_et_minute
d=pickle.load(open(sys.argv[1],'rb')); trades,paths,vwap=d['trades'],d['paths'],d['vwap']
def vw_at(day,tk,et):
    s=vwap.get((day,tk))
    if not s: return None
    i=bisect.bisect_right([x[0] for x in s], et)-1
    return s[i][1] if i>=0 else None
def per_trade(deadband=0.0,use_inval=True,stop=0.50,target=0.50,time_stop=None,subset=None):
    out=[]
    for t in trades:
        if subset and t['day'] not in subset: continue
        key=(tuple(l['symbol'] for l in t['spec']['trade']['legs']),
             tuple(l['action'] for l in t['spec']['trade']['legs']), t['day'])
        p=paths.get(key)
        if not p: continue
        tr=t['spec']['trade']; entry=t['entry']; q=t['qty']; direction=tr.get('direction'); mp=tr.get('max_profit')
        e0=pt_to_et_minute(t['spec']['ts']); seg=[(k,v) for k,v in p if k>=e0]
        if not seg: continue
        res=None
        for k,v in seg:
            hh,mm=int(k[:2]),int(k[3:5])
            if mm%5: continue
            if (hh,mm)>=(15,45): res=v; break
            mins=(hh*60+mm)-(int(e0[:2])*60+int(e0[3:5]))
            if use_inval:
                vd=vw_at(t['day'],t['tk'],k)
                if vd is not None and direction in ("long","short"):
                    if direction=="short" and vd> deadband: res=v; break
                    if direction=="long"  and vd< -deadband: res=v; break
            pnl=v-entry
            if pnl<=-stop*entry: res=v; break
            if mp and pnl>=target*float(mp): res=v; break
            if not mp and pnl>=target*entry: res=v; break
            if time_stop and mins>=time_stop: res=v; break
        if res is None: res=seg[-1][1]
        out.append(((res-entry)*100*q, t['day']))
    return out
days=sorted({t['day'] for t in trades})
mid=days[len(days)//2]
A={d for d in days if d<mid}; B={d for d in days if d>=mid}
print(f"days={len(days)}  split at {mid}  (A={len(A)} days, B={len(B)} days)\n")
CONFIGS=[("current",dict()),
         ("deadband 0.5",dict(deadband=0.5)),
         ("deadband 1.0",dict(deadband=1.0)),
         ("deadband 1.5",dict(deadband=1.5)),
         ("no invalidation",dict(use_inval=False)),
         ("time stop 120m no-inval",dict(use_inval=False,time_stop=120)),
         ("profit target 35%",dict(target=0.35)),
         ("stop loss 75%",dict(stop=0.75))]
print(f"{'policy':26} {'FULL':>9} {'first half':>11} {'2nd half':>10} {'ex-top3':>9} {'med/trade':>10}")
for name,kw in CONFIGS:
    full=[x[0] for x in per_trade(**kw)]
    a=[x[0] for x in per_trade(subset=A,**kw)]; b=[x[0] for x in per_trade(subset=B,**kw)]
    ex3=sum(sorted(full)[:-3])
    print(f"  {name:24} {sum(full):>+9,.0f} {sum(a):>+11,.0f} {sum(b):>+10,.0f} {ex3:>+9,.0f} {st.median(full):>+10,.0f}")
