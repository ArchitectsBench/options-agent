"""COUNTERFACTUAL: what if the VWAP invalidation rule had never fired?

Truncation control for the 'never developed' cohort. MFE in enriched_trades is
bounded by the exit being evaluated, so a trade stopped at minute 6 with
MFE<=2% cannot be distinguished from a dead entry. Replay against FULL-DAY
paths with vwap_dist_atr=None (invalidation branch can never fire) while
stop_loss / profit_target / time_flat stay live.

Per-trade; concurrency caps and the daily breaker are ignored.
NOT GATE EVIDENCE — UNVALIDATED — PAPER ONLY.
"""
import json, glob, re, sys
from datetime import datetime, time
sys.path.insert(0,'/Users/you/projects/OptionsAgent')
sys.path.insert(0,'/Users/you/projects/OptionsAgent/scripts')
from enrich_trades_thetadata import pt_to_et_minute, structure_path
from src.engine.exit_manager import evaluate_exit

ROOT='/Users/you/projects/OptionsAgent'
CFG={"flat_time_et":"15:45","stop_loss_pct":0.50,"profit_target_pct":0.50,
     "single_leg_target_gain_pct":0.50}

specs={}   # rec_id -> trade spec from observation logs
for f in sorted(glob.glob(f'{ROOT}/logs/observation/2026-*.jsonl')):
    for l in open(f):
        try: r=json.loads(l)
        except: continue
        for a in (r.get('paper_execution_tests') or []):
            if a.get('rec_id') and a.get('trade'):
                specs[a['rec_id']]={'trade':a['trade'],'qty':a.get('quantity'),'ts':a.get('ts') or r.get('ts')}

exits={}
for l in open(f'{ROOT}/logs/observation/fills.jsonl'):
    e=json.loads(l)
    if e.get('event')=='paper_exit': exits[e['rec_id']]=e

enr=[json.loads(l) for l in open(f'{ROOT}/logs/observation/enriched_trades_v1.jsonl') if l.strip()]
cohort=[]
for r in enr:
    if r.get('status')!='ok' or not r.get('n_path_minutes'): continue
    en,mfe=r.get('true_mid_entry'),r.get('mfe_mid_1m')
    if None in (en,mfe) or en<=0: continue
    if (mfe-en)/en > 0.02: continue          # never-developed cohort only
    s=specs.get(r['rec_id'])
    if not s: continue
    cohort.append((r,s))
print(f"never-developed trades with a recoverable spec: {len(cohort)}", flush=True)

res=[]
for i,(r,s) in enumerate(cohort,1):
    day=r['rec_id'][:8]; day=f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    t=s['trade']
    legs=[{"symbol":l["symbol"],"side":l["action"]} for l in t["legs"]]
    try: path=structure_path(legs,[day])
    except Exception: path=None
    if path is None or not len(path): continue
    ent_et=pt_to_et_minute(s['ts'])
    sub=path[path.index>=f"{day}T{ent_et}"]
    if not len(sub): continue
    entry=float(r['true_mid_entry'])
    pos={"legs":t["legs"],"direction":t.get("direction"),"entry_price":entry,
         "max_profit":t.get("max_profit"),"quantity":int(s['qty'] or 1)}
    qty=int(s['qty'] or 1)
    # walk the 5-minute cycle grid, invalidation DISABLED
    out=None
    for key in sub.index:
        hh,mm=int(key[11:13]),int(key[14:16])
        if mm % 5: continue
        val=float(sub.loc[key,'net_mid']) if not hasattr(sub.loc[key,'net_mid'],'iloc') else float(sub.loc[key,'net_mid'].iloc[0])
        now=datetime.combine(datetime.strptime(day,"%Y-%m-%d").date(), time(hh,mm))
        reason=evaluate_exit(pos,val,{"vwap_dist_atr":None},now,CFG)
        if reason:
            out=(reason.split(' (')[0], val, key[11:]); break
    if out is None:
        val=float(sub['net_mid'].iloc[-1]); out=("eod",val,sub.index[-1][11:])
    sim=round((out[1]-entry)*100*qty,2)
    res.append(dict(rid=r['rid'] if 'rid' in r else r['rec_id'],
                    actual=float(r['true_pnl_mid']), sim=sim,
                    reason=out[0], actual_reason=str((exits.get(r['rec_id']) or {}).get('close_reason') or '')[:24]))
    if i%25==0: print(f"  {i}/{len(cohort)}", flush=True)

a=sum(x['actual'] for x in res); b=sum(x['sim'] for x in res)
print(f"\n=== NEVER-DEVELOPED COHORT, n={len(res)} ===")
print(f"  ACTUAL (invalidation fired) : ${a:>+10,.0f}   ({a/len(res):+.0f}/trade)")
print(f"  SIM (invalidation disabled) : ${b:>+10,.0f}   ({b/len(res):+.0f}/trade)")
print(f"  => invalidation rule effect : ${a-b:>+10,.0f}  ({'SAVED' if a>b else 'COST'} money)")
import collections
print("\n  simulated exit mix:",dict(collections.Counter(x['reason'] for x in res)))
better=sum(1 for x in res if x['sim']>x['actual'])
print(f"  trades that would have ended BETTER without invalidation: {better}/{len(res)} ({100*better/len(res):.0f}%)")
json.dump(res,open(sys.argv[1],'w'),indent=1)
