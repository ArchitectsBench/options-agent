"""Cache FULL-DAY minute paths for EVERY recorded SCALP entry attempt (filled or
not) plus per-cycle vwap_dist_atr series. Input for the v2 counterfactual.
Read-only w.r.t. engine state. UNVALIDATED — PAPER ONLY."""
import json, glob, sys, pickle
sys.path.insert(0,'/Users/you/projects/OptionsAgent'); sys.path.insert(0,'/Users/you/projects/OptionsAgent/scripts')
from enrich_trades_thetadata import pt_to_et_minute, structure_path
ROOT='/Users/you/projects/OptionsAgent'; OUT=sys.argv[1]
attempts=[]; vwap={}
for f in sorted(glob.glob(f'{ROOT}/logs/observation/2026-*.jsonl')):
    day=f.split('/')[-1][:-6]
    for l in open(f):
        try: r=json.loads(l)
        except: continue
        ts=r.get('ts')
        if ts:
            et=pt_to_et_minute(ts)
            for tk,s in (r.get('signals') or {}).items():
                if isinstance(s,dict) and isinstance(s.get('vwap_dist_atr'),(int,float)):
                    vwap.setdefault((day,tk),[]).append((et,float(s['vwap_dist_atr'])))
        for a in (r.get('paper_execution_tests') or []):
            t=a.get('trade') or {}
            if t.get('legs') and all('bid' in x and 'ask' in x for x in t['legs']):
                attempts.append({'day':day,'ts':a.get('ts') or ts,'rec_id':a.get('rec_id'),'ticker':a.get('ticker'),
                                 'strategy':a.get('strategy'),'mode':a.get('mode'),'qty':a.get('quantity'),'trade':t,
                                 'submitted':(a.get('paper_order') or {}).get('submitted'),
                                 'resp':str(((a.get('paper_order') or {}).get('response') or {}).get('error') or '')[:40]})
for k in vwap: vwap[k].sort()
print(f"attempts={len(attempts)} vwap series={len(vwap)}", flush=True)
paths={}; done=0
for a in attempts:
    key=(tuple(x['symbol'] for x in a['trade']['legs']), tuple(x['action'] for x in a['trade']['legs']), a['day'])
    if key in paths: continue
    legs=[{'symbol':x['symbol'],'side':x['action']} for x in a['trade']['legs']]
    try:
        p=structure_path(legs,[a['day']])
        paths[key]=None if p is None else [(k[11:],float(v)) for k,v in p['net_mid'].items()]
    except Exception: paths[key]=None
    done+=1
    if done%100==0: print(f"  fetched {done}", flush=True)
pickle.dump({'attempts':attempts,'paths':paths,'vwap':vwap},open(OUT,'wb'))
print(f"DONE {len(paths)} paths ({sum(1 for v in paths.values() if v)} non-empty) -> {OUT}", flush=True)
