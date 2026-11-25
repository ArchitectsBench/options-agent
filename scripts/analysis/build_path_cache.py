"""Cache FULL-DAY minute paths + per-cycle vwap_dist for every closed trade.
One-time build so the exit-policy sweep can run in memory.
UNVALIDATED — PAPER ONLY. Read-only w.r.t. all engine state."""
import json, glob, re, sys, os, pickle
sys.path.insert(0,'/Users/you/projects/OptionsAgent')
sys.path.insert(0,'/Users/you/projects/OptionsAgent/scripts')
from enrich_trades_thetadata import pt_to_et_minute, structure_path
ROOT='/Users/you/projects/OptionsAgent'
OUT=sys.argv[1]

# 1. trade specs (legs, qty, direction, max_profit) from observation logs
specs={}
vwap={}   # (day,ticker) -> [(et_hhmm, vwap_dist_atr)]
for f in sorted(glob.glob(f'{ROOT}/logs/observation/2026-*.jsonl')):
    for l in open(f):
        try: r=json.loads(l)
        except: continue
        ts=r.get('ts')
        if ts:
            day=str(ts)[:10]; et=pt_to_et_minute(ts)
            for tk,s in (r.get('signals') or {}).items():
                if isinstance(s,dict) and isinstance(s.get('vwap_dist_atr'),(int,float)):
                    vwap.setdefault((day,tk),[]).append((et,float(s['vwap_dist_atr'])))
        for a in (r.get('paper_execution_tests') or []):
            if a.get('rec_id') and a.get('trade'):
                specs[a['rec_id']]={'trade':a['trade'],'qty':a.get('quantity'),'ts':a.get('ts') or ts}
for k in vwap: vwap[k].sort()
print(f"specs={len(specs)}  vwap series={len(vwap)}", flush=True)

# 2. closed trades with a truth entry
enr=[json.loads(l) for l in open(f'{ROOT}/logs/observation/enriched_trades_v1.jsonl') if l.strip()]
trades=[]
for r in enr:
    if r.get('status')!='ok': continue
    s=specs.get(r['rec_id'])
    if not s or not r.get('true_mid_entry'): continue
    d=r['rec_id'][:8]
    trades.append({'rid':r['rec_id'],'day':f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                   'tk':r['ticker'],'spec':s,'entry':float(r['true_mid_entry']),
                   'actual':float(r['true_pnl_mid']),'qty':int(s['qty'] or 1)})
print(f"candidate trades: {len(trades)}", flush=True)

paths={}
done=0
for t in trades:
    key=(tuple(l['symbol'] for l in t['spec']['trade']['legs']),
         tuple(l['action'] for l in t['spec']['trade']['legs']), t['day'])
    if key in paths: continue
    legs=[{'symbol':l['symbol'],'side':l['action']} for l in t['spec']['trade']['legs']]
    try:
        p=structure_path(legs,[t['day']])
        paths[key]=None if p is None else [(k[11:], float(v)) for k,v in p['net_mid'].items()]
    except Exception:
        paths[key]=None
    done+=1
    if done%50==0: print(f"  fetched {done}", flush=True)
pickle.dump({'trades':trades,'paths':paths,'vwap':vwap}, open(OUT,'wb'))
ok=sum(1 for v in paths.values() if v)
print(f"DONE: {len(paths)} paths ({ok} non-empty) -> {OUT}", flush=True)
