"""Browser task entry point for portal_swarm_pilot; uses its existing lab transport."""
import argparse
import json
import time
import uuid
from pathlib import Path


def main():
    from tools.portal_swarm_pilot import lab_submit,api_get,atomic_json
    from tools.portal_policy import PortalPolicy
    from harnesses.external.collective_pool import KindCollectivePool
    from tools.guild_browser_broker import GuildBrowserBroker
    p=argparse.ArgumentParser()
    p.add_argument('--out',required=True);p.add_argument('--goal',required=True)
    p.add_argument('--url',default='https://playground.allenai.org/')
    p.add_argument('--operation-id',required=True)
    p.add_argument('--members',nargs='+')
    args=p.parse_args()
    out=Path(args.out).resolve();out.mkdir(parents=True,exist_ok=True)
    policy=PortalPolicy();pool=KindCollectivePool(external_hf_only=True,enable_semantic_cache=False)
    if args.members is None:
        args.members=pool.select_guild_members(args.goal,limit=2)
    deadline=time.monotonic()+600
    def log(event,**data):
        with (out/'events.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps({'time':time.time(),'event':event,**data},ensure_ascii=False)+'\n');f.flush()
    def call(pid,text,arm):
        op='browser-'+uuid.uuid4().hex
        log('inference_intent',portal=pid,arm=arm,operation_id=op,prompt=text)
        accepted=False
        while time.monotonic()<deadline:
            with policy.db() as c:
                last=c.execute("SELECT MAX(started) FROM portal_requests WHERE provider='huggingface'").fetchone()[0] or 0
            delay=max(0,11-(time.time()-last))
            if delay: time.sleep(delay);continue
            try:
                receipt=lab_submit({'operation_id':op,'message':text,'mode':'direct_node','target':'portal:'+pid})
                log('lab_receipt',receipt=receipt)
                if receipt.get('status')==409: time.sleep(5);continue
                if receipt.get('status')!=202: return {'ok':False,'failure':'submission_rejected'}
            except Exception:
                log('submission_uncertain',operation_id=op)
            accepted=True;break
        if not accepted:return {'ok':False,'failure':'deadline_before_submission'}
        until=min(deadline,time.monotonic()+300)
        while time.monotonic()<until:
            try: job=api_get('/api/swarm-chat/jobs/'+op)
            except Exception: time.sleep(2);continue
            replies=(job.get('result') or {}).get('responses') or []
            if replies:
                r=replies[0];log('model_response',portal=pid,arm=arm,job=job)
                if r.get('ok') and (r.get('source')!='external_zero_auth_portal' or r.get('speaker')!=pid):
                    return {'ok':False,'failure':'provenance_mismatch'}
                return r
            if job.get('state') in ('failed','cancelled','uncertain','done','completed'):
                return {'ok':False,'failure':job['state']}
            time.sleep(2)
        return {'ok':False,'failure':'uncertain','error':'No replay; inspect '+op}
    members={m['portal_id']:m for m in pool.supervised_hf_members()}
    readiness={'engine':'KindCollectivePool','external_only':True,'lab_chat_api':False,
               'members':{pid:bool(members.get(pid,{}).get('chat_eligible')) for pid in args.members},
               'browser':False,'task_operation_id':args.operation_id}
    try:
        api_get('/health');readiness['lab_chat_api']=True
        if not all(readiness['members'].values()) or len(args.members)<2:
            raise RuntimeError('Two eligible HF workers are required; provider holds preserved')
        with GuildBrowserBroker(args.url,out) as broker:
            observation=broker.observe();readiness['browser']=True;readiness['browser_capabilities']=observation
            atomic_json(out/'capabilities.json',readiness)
            result=pool.supervised_browser_loop(args.goal,operation_id=args.operation_id,allowed_url=args.url,
                journal=Path(__file__).resolve().parents[1]/'data/guild_browser_runs.sqlite3',broker=broker,member_call=call,
                allowed_portals=args.members,max_rounds=3)
        atomic_json(out/'result.json',result)
        log('finished',state=result['state'])
        print(json.dumps({'state':result['state'],'submissions':result['submissions'],'out':str(out)}),flush=True)
    except Exception as exc:
        readiness['error']=type(exc).__name__+': '+str(exc)
        atomic_json(out/'capabilities.json',readiness)
        log('capability_failure',detail=readiness)
        if readiness['lab_chat_api'] and len(args.members)>=2 and all(readiness['members'].values()):
            diagnosis=pool.run_guild_round(
                'Analyze this actual browser capability-check failure. No browser action or target inference occurred. '
                'Return compact JSON with observed_failure, what_was_not_tested, next_safe_step. '
                'Do not claim execution, clear holds, assume a lease executor is stopped, or use local inference. '
                'Treat the recorded capability error as evidence, not proof of model inability. '
                +json.dumps(readiness),task_id=args.operation_id+':capability-review',
                allowed_portals=args.members,member_call=call)
            atomic_json(out/'capability-review.json',diagnosis)
        print(json.dumps({'state':'blocked','error':readiness['error'],'out':str(out)}),flush=True)


if __name__=='__main__':main()
