"""Bounded operator CLI for portal migration, qualification, review and rollout."""
from __future__ import annotations
import argparse
import json
import sqlite3
import time
from pathlib import Path
from tools.portal_policy import DEFAULT_DB, ROOT, ROLES, PortalPolicy
from tools.portal_evaluation import Evaluator
from tools.portal_transport import refresh_protocol


def audit(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    c.row_factory=sqlite3.Row
    try:
        integrity=c.execute('PRAGMA quick_check').fetchone()[0]
        if integrity!='ok':
            raise RuntimeError(integrity)
        rows=c.execute('SELECT portal_id,qualification_state,benchmark_json FROM assistant_external_portals').fetchall()
        counts={'registered':len(rows),'legacy_marked_qualified':0,'full_delivery':0,'partial_delivery':0,'zero_delivery':0,'unbenchmarked':0}
        queue=[]
        for r in rows:
            b=json.loads(r['benchmark_json'] or '{}');d=b.get('delivery_reliability')
            counts['legacy_marked_qualified']+=r['qualification_state']=='qualified'
            counts['unbenchmarked' if d is None else 'full_delivery' if d==1 else 'zero_delivery' if d==0 else 'partial_delivery']+=1
            priority=0 if b.get('answer_correctness')==.5 else 1 if b.get('answer_correctness')==1 else 2 if d else 3
            queue.append((priority,r['portal_id']))
        return {'integrity':integrity,'counts':counts,'qualification_queue':[pid for _,pid in sorted(queue)],
                'note':'Historical delivery is not current availability; zero scores do not establish formatting failure.'}
    finally:
        c.close()


def migrate(policy):
    definitions=json.loads((ROOT/'config/portal_adapters.json').read_text(encoding='utf-8'))['profiles']
    for p in definitions:
        # Re-running migration must preserve independently validated schema and newer profiles.
        if not policy.profile(p['portal_id']):
            policy.register(**p)
    return {'profiles':len(definitions),'legacy_roles_preserved':policy.migrate_legacy()}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',default=str(DEFAULT_DB))
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('audit');sub.add_parser('migrate');sub.add_parser('queue')
    refresh=sub.add_parser('refresh');refresh.add_argument('portal')
    evaluate=sub.add_parser('evaluate');evaluate.add_argument('portal');evaluate.add_argument('--role',choices=(*ROLES,'tool_use'),required=True)
    evaluate.add_argument('--max-requests',type=int,default=20);evaluate.add_argument('--wait-seconds',type=int,default=0)
    resume=sub.add_parser('resume');resume.add_argument('run_id');resume.add_argument('--max-requests',type=int,default=20);resume.add_argument('--wait-seconds',type=int,default=0)
    for name in ('report','review-packet'):
        child=sub.add_parser(name);child.add_argument('run_id')
    review=sub.add_parser('review');review.add_argument('run_id');review.add_argument('--file',required=True)
    enable=sub.add_parser('enable');enable.add_argument('--tools',action='store_true')
    sub.add_parser('disable')
    reconcile=sub.add_parser('reconcile');reconcile.add_argument('provider');reconcile.add_argument('--evidence',required=True)
    args=p.parse_args(argv)
    if args.command in ('audit','queue'):
        result=audit(args.db)
        if args.command=='queue': result=result['qualification_queue']
    else:
        policy=PortalPolicy(args.db);ev=Evaluator(policy)
        if args.command=='migrate': result=migrate(policy)
        elif args.command=='refresh': result=refresh_protocol(policy,args.portal)
        elif args.command in ('evaluate','resume'):
            if not 1<=args.max_requests<=250 or not 0<=args.wait_seconds<=3600:
                p.error('max-requests must be 1..250; wait-seconds must be 0..3600')
            rid=ev.start(args.portal,args.role) if args.command=='evaluate' else args.run_id
            print(json.dumps({'run_id':rid}),flush=True)
            deadline=time.monotonic()+args.wait_seconds;sent=0
            while True:
                result=ev.run(rid,args.max_requests-sent);sent+=result['sent']
                if sent>=args.max_requests or time.monotonic()>=deadline or result['state']=='complete': break
                if result.get('deferred') not in ('provider_cooldown','provider_busy','format_retry_pending','batch_limit'): break
                time.sleep(min(10,max(0,deadline-time.monotonic())))
            result['batch_sent']=sent
        elif args.command=='report': result=ev.summarize(args.run_id)
        elif args.command=='review-packet': result=ev.review_packet(args.run_id)
        elif args.command=='review':
            verdicts=json.loads(Path(args.file).read_text(encoding='utf-8'))
            for v in verdicts: result=ev.review(args.run_id,**v)
        elif args.command in ('enable','disable'):
            policy.set_enabled(research=args.command=='enable',tools=getattr(args,'tools',False));result={'research_enabled':policy.setting('research_enabled'),'tools_enabled':policy.setting('tools_enabled')}
        elif args.command=='reconcile': policy.reconcile(args.provider,args.evidence);result={'provider':args.provider,'reconciled':True}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
