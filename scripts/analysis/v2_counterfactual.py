"""V2 COUNTERFACTUAL — what SCALP-v2 would have done with the SAME attempt stream.
For every recorded attempt: enter at DECISION time at the natural
(mid + aggressiveness*spread), assume the IOC fills (upper bound; a fill-prob
knob exists), apply the $1 floor, per-ticker dedup, max_open 7, 10 fills /
30 attempts per day, then replay the UNCHANGED exit rules on the real minute
path with the recorded 5-min vwap signals. Score on the truth ruler:
  truth   = exit_mid - entry_natural (entry half-spread is REAL cost, counted)
  realistic = truth - exit half-spread (measured median 0.039) - commissions
Compared to v1 ACTUAL truth on the same days. UNVALIDATED — PAPER ONLY.
NOT gate evidence. In-sample caveat: the floor/cap values were chosen on this data."""
import pickle, sys, bisect, json, random, statistics as st, collections
sys.path.insert(0,'/Users/you/projects/OptionsAgent'); sys.path.insert(0,'/Users/you/projects/OptionsAgent/scripts')
from enrich_trades_thetadata import pt_to_et_minute
from src.engine.exit_manager import evaluate_exit
from datetime import datetime, time
CFG={"flat_time_et":"15:45","stop_loss_pct":0.50,"profit_target_pct":0.50,"single_leg_target_gain_pct":0.50}
EXIT_HALF=0.039; COMM=0.65
d=pickle.load(open(sys.argv[1],'rb')); A,P,V=d['attempts'],d['paths'],d['vwap']
def vw_at(day,tk,et):
    s=V.get((day,tk))
    if not s: return None
    i=bisect.bisect_right([x[0] for x in s],et)-1
    return s[i][1] if i>=0 else None
def natural(t,agg):
    legs=t['legs']
    if len(legs)==1:
        l=legs[0]; return round(float(l['mid'])+agg*(float(l['ask'])-float(l['bid'])),2)
    return round(float(t['net_debit'])+agg*sum(float(l['ask'])-float(l['bid']) for l in legs),2)
def replay(a,entry,agg):
    key=(tuple(x['symbol'] for x in a['trade']['legs']),tuple(x['action'] for x in a['trade']['legs']),a['day'])
    p=P.get(key)
    if not p: return None
    e0=pt_to_et_minute(a['ts']); seg=[(k,v) for k,v in p if k>=e0]
    if not seg: return None
    t=a['trade']; pos={"direction":t.get('direction'),"entry_price":entry,"max_profit":t.get('max_profit')}
    out=None
    for k,v in seg:
        hh,mm=int(k[:2]),int(k[3:5])
        if mm%5: continue
        if (hh,mm)>=(15,45): out=("time_flat",v,k); break
        vd=vw_at(a['day'],a['ticker'],k)
        r=evaluate_exit(pos,v,{"vwap_dist_atr":vd},datetime.combine(datetime.strptime(a['day'],"%Y-%m-%d").date(),time(hh,mm)),CFG)
        if r: out=(r.split(' (')[0],v,k); break
    if out is None: out=("eod",seg[-1][1],seg[-1][0])
    return out
def run(agg=0.5,floor=1.0,max_fill=10,max_att=30,max_open=7,fill_prob=1.0,seed=7,days=None):
    rng=random.Random(seed); rows=[]; byday=collections.defaultdict(list)
    for a in A: byday[a['day']].append(a)
    for day in sorted(byday):
        if days is not None and day not in days: continue
        fills=att=0; open_pos=[]  # (ticker, exit_et)
        for a in sorted(byday[day],key=lambda x:x['ts']):
            t=a['trade']
            if float(t.get('net_debit') or 0)<floor: continue
            et=pt_to_et_minute(a['ts']); open_pos=[(tk,x) for tk,x in open_pos if x>et]
            if any(tk==a['ticker'] for tk,_ in open_pos): continue
            if len(open_pos)>=max_open or att>=max_att or fills>=max_fill: continue
            att+=1
            if rng.random()>fill_prob: continue
            entry=natural(t,agg); out=replay(a,entry,agg)
            if out is None: continue
            fills+=1; q=int(a.get('qty') or 1); nlegs=len(t['legs'])
            truth=(out[1]-entry)*100*q; real=truth-(EXIT_HALF*100*q)-(COMM*q*nlegs*2)
            rows.append(dict(day=a['day'],tk=a['ticker'],strat=a['strategy'],q=q,entry=entry,exit=out[1],reason=out[0],truth=truth,real=real,rid=a['rec_id']))
            open_pos.append((a['ticker'],out[2]))
    return rows
def v1_actual(days):
    enr={}
    for l in open('/Users/you/projects/OptionsAgent/logs/observation/enriched_trades_v1.jsonl'):
        r=json.loads(l)
        if r.get('rec_id') and r.get('status')=='ok': enr[r['rec_id']]=r
    ev=[json.loads(l) for l in open('/Users/you/projects/OptionsAgent/logs/observation/fills.jsonl') if l.strip()]
    rows=[]
    for e in ev:
        if e.get('event')!='paper_exit': continue
        rid=e['rec_id']; d=str(e['ts'])[:10]
        if days is not None and d not in days: continue
        if '-SCALP-' not in rid and '-TEST-' not in rid: continue
        r=enr.get(rid)
        if not r: continue
        rows.append(dict(day=d,truth=float(r['true_pnl_mid']),booked=float(e.get('pnl_total') or 0),
                         real=float(r['true_pnl_mid'])-(0.074*100*int(r.get('quantity') or 1))-(COMM*int(r.get('quantity') or 1)*2*2)))
    return rows
if __name__=="__main__":
    live_days={r['day'] for r in v1_actual(None)}      # days v1 actually closed trades
    def summ(rows,key='truth'):
        if not rows: return "n=0"
        v=[r[key] for r in rows]; days=len({r['day'] for r in rows})
        return f"n={len(rows):>4} days={days:>2} total {sum(v):>+9,.0f}  /trade {sum(v)/len(v):>+6.1f}  med {st.median(v):>+5.0f}  win {100*sum(1 for x in v if x>0)/len(v):3.0f}%  ex-top5 {sum(sorted(v)[:-5]):>+8,.0f}"
    print("=== HEADLINE: days on which v1 actually traded ===")
    v1=v1_actual(live_days); v2=run(days=live_days)
    print(f"  v1 ACTUAL  truth     {summ(v1)}")
    print(f"  v1 ACTUAL  realistic {summ(v1,'real')}")
    print(f"  v2 COUNTER truth     {summ(v2)}")
    print(f"  v2 COUNTER realistic {summ(v2,'real')}")
    print("\n=== by era (live days) ===")
    for name,f in (('Tradier',lambda d:d<'2026-08-05'),('IBKR',lambda d:d>='2026-08-05')):
        dd={d for d in live_days if f(d)}
        print(f"  {name:8} v1 truth {summ(v1_actual(dd))}\n           v2 truth {summ(run(days=dd))}")
    print("\n=== v2 exit mix (live days) ===", dict(collections.Counter(r['reason'] for r in v2)))
    print("\n=== sensitivity (v2 truth, live days) ===")
    for lab,kw in (('agg 0.25',dict(agg=0.25)),('agg 1.0 (pay full spread)',dict(agg=1.0)),('no $1 floor',dict(floor=0)),('no caps',dict(max_fill=999,max_att=999)),('fill prob 0.7',dict(fill_prob=0.7)),('fill prob 0.5',dict(fill_prob=0.5))):
        print(f"  {lab:26} {summ(run(days=live_days,**kw))}")
    print("\n=== ALL recorded days incl. broker-dead (v2 gets days v1 could not trade) ===")
    print(f"  v2 truth     {summ(run())}")
    print(f"  v2 realistic {summ(run(),'real')}")
    print("\n=== split-half stability (v2 truth, live days) ===")
    ds=sorted(live_days); h=len(ds)//2
    print(f"  first half  {summ(run(days=set(ds[:h])))}\n  second half {summ(run(days=set(ds[h:])))}")
    json.dump({'v1':v1,'v2':v2},open(sys.argv[2],'w')) if len(sys.argv)>2 else None
