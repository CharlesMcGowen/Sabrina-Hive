"""Checkpointed discovery -> existing role qualification -> swarm admission."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from tools.guild_web_broker import WebJournal, GuildWebBroker
from tools.guild_web_growth import run_cycle
from tools.guild_web_qualification import prepare_candidate, activate_qualified, qualification_deadline
from tools.portal_evaluation import Evaluator
from tools.portal_policy import PortalPolicy, DEFAULT_DB
from tools.portal_swarm_pilot import campaign_lock
from harnesses.external.collective_pool import KindCollectivePool

ROOT=Path(__file__).resolve().parents[1]
SOURCES=('tools/control_client.py','tools/guild_web_broker.py','tools/guild_web_growth.py',
         'tools/guild_web_campaign.py','tools/guild_web_qualification.py','tools/portal_policy.py',
         'tools/portal_transport.py','tools/portal_evaluation.py','harnesses/external/collective_pool.py',
         'tools/guild_web_auth.py','tools/portal_verification_mail.py')


def fingerprints():
    return {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCES}


def save(path,value):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2),encoding='utf-8');os.replace(temp,path)


def run(out,minutes=60,cycles=4,db_path=DEFAULT_DB):
    if not 1<=minutes<=240 or not 1<=cycles<=20:raise ValueError('Invalid campaign budget')
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    receipt=out/'campaign.json'
    if receipt.exists():raise ValueError('Preserved campaign exists; no deadline reset or competing resume')
    ident='portal-growth-'+uuid.uuid4().hex[:16]
    start=time.time();deadline=start+minutes*60
    frozen=fingerprints()
    state={'id':ident,'pid':os.getpid(),'started':start,'deadline':deadline,'state':'starting',
           'source_hashes':frozen,'discovery':[],'qualifications':[],'activations':[],
           'supervisor_browser_actions':0,'stop_file':str(out/'STOP')}
    save(receipt,state)
    policy=PortalPolicy(db_path);journal=WebJournal(db_path)
    attempted=set()
    def checkpoint():
        save(receipt,state)
        if (out/'STOP').exists():raise InterruptedError('operator_stop')
        if time.time()>=deadline:raise InterruptedError('campaign_deadline')
        if fingerprints()!=frozen:raise InterruptedError('harness_changed')
    try:
        from tools.control_client import caller
        with campaign_lock(ROOT/'data/collective_guild_benchmark.lock'),qualification_deadline(deadline):
            for cycle in range(cycles):
                checkpoint()
                with policy.db() as conn:
                    known=[r['url'] for r in conn.execute("SELECT url FROM swarm_discovery_candidates WHERE probe_status='ui_single_smoke_pass_unqualified'")]
                goal=('Find and test a new useful public AI chat portal through your own web searches. '
                      'Choose the search queries and sites yourself. Prefer a reachable chat returning structured JSON. '
                      'Establish a fresh chat and submit the host smoke test. Finish after one independently verified response. '
                      'Already tested portals (seek a different one): '+json.dumps(known[:20]))
                run_id=ident+'-discover-'+str(cycle)
                state.update(state='discovering',current_run=run_id);checkpoint()
                journal.start(run_id,{'goal':goal,'source_hashes':frozen,'campaign_id':ident},min(600,deadline-time.time()))
                pool=KindCollectivePool(db_path=Path(db_path),external_hf_only=True,enable_semantic_cache=False)
                try:
                    with caller(run_id,'portal-growth'),GuildWebBroker(run_id,journal,start_browser=True) as broker:
                        result=run_cycle(pool,broker,journal,run_id,goal,max_steps=24,controller='local14b')
                    state['discovery'].append(result)
                except Exception as exc:
                    journal.finish(run_id,'blocked',{'error':type(exc).__name__+': '+str(exc)})
                    state['discovery'].append({'run_id':run_id,'state':'blocked','error':type(exc).__name__})
                    # Ownership or coordinator failures stop; they do not trigger a competing worker.
                    raise
                finally:pool.blackboard.close()
                checkpoint()
                with policy.db() as conn:
                    candidates=[r['portal_id'] for r in conn.execute("SELECT portal_id FROM swarm_discovery_candidates WHERE probe_status='ui_single_smoke_pass_unqualified' AND state='staged' ORDER BY last_probed DESC")]
                for pid in candidates:
                    if pid in attempted:continue
                    attempted.add(pid);checkpoint()
                    entry={'portal_id':pid,'state':'preparing'};state['qualifications'].append(entry)
                    state.update(state='qualifying',current_portal=pid);checkpoint()
                    try:
                        prepare_candidate(policy,pid)
                        evaluator=Evaluator(policy)
                        eval_id=evaluator.start(pid,'structured_extraction')
                        entry.update(run_id=eval_id,state='running');checkpoint()
                        while time.time()<deadline:
                            result=evaluator.run(eval_id,max_requests=1)
                            entry.update(state=result['state'],summary=result);checkpoint()
                            if result['state']=='complete':
                                admission=activate_qualified(policy,pid,eval_id)
                                entry['admission']=admission
                                if admission['activated']:state['activations'].append(admission)
                                break
                            blocked=result.get('deferred')
                            if blocked not in (None,'batch_limit','format_retry_pending','provider_cooldown'):
                                entry.update(state='blocked',blocker=blocked);break
                            if blocked=='provider_cooldown':time.sleep(10)
                        if entry.get('blocker') in ('uncertain','quota_exhausted','rate_limited','daily_budget_exhausted'):
                            raise InterruptedError('qualification_'+entry['blocker'])
                    except (ValueError,RuntimeError) as exc:
                        entry.update(state='blocked',blocker=str(exc));checkpoint()
                checkpoint()
            state['state']='checkpoint_complete'
    except InterruptedError as exc:
        state.update(state='paused',reason=str(exc))
    except Exception as exc:
        state.update(state='blocked',reason=type(exc).__name__+': '+str(exc))
    finally:
        state['finished']=time.time();save(receipt,state);journal.conn.close()
    return state


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',required=True);p.add_argument('--minutes',type=int,default=60)
    p.add_argument('--cycles',type=int,default=4)
    args=p.parse_args()
    if not 1<=args.minutes<=240 or not 1<=args.cycles<=20:p.error('Minutes 1..240, cycles 1..20')
    result=run(args.out,args.minutes,args.cycles)
    print(json.dumps({'id':result['id'],'state':result['state'],'activations':len(result['activations']),
                      'reason':result.get('reason')}),flush=True)


if __name__=='__main__':main()
