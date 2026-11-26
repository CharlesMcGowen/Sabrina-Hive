"""Live UI adapter and existing evaluator integration. No model-written verdicts."""
from __future__ import annotations
from contextlib import contextmanager
import contextvars
import json
import time
from urllib.parse import urlsplit

from tools.guild_web_broker import WebJournal, GuildWebBroker, public_url
from tools.guild_web_growth import run_cycle
from tools.portal_policy import PortalPolicy, adapter_fingerprint
from tools.portal_evaluation import Evaluator
from harnesses.external.collective_pool import KindCollectivePool

DEADLINE=contextvars.ContextVar('web_qualification_deadline',default=None)


@contextmanager
def qualification_deadline(deadline):
    token=DEADLINE.set(deadline)
    try:yield
    finally:DEADLINE.reset(token)


def web_chat_exchange(policy,portal_id,request_id,adapter,prompt):
    """Called exclusively through the admitted, single-attempt portal transport."""
    with policy.db() as db:
        admitted=db.execute("SELECT 1 FROM portal_requests WHERE request_id=? AND portal_id=? AND state='active'",
                            (request_id,portal_id)).fetchone()
    if not admitted:raise ValueError('Active portal admission receipt required')
    if adapter.get('verified_protocol') is not True:
        raise ValueError('Observed browser protocol required')
    profile=policy.profile(portal_id)
    if not profile or profile['adapter_hash']!=adapter_fingerprint(adapter):
        raise ValueError('Browser adapter changed; revalidate')
    deadline=min(time.time()+300,DEADLINE.get() or float('inf'))
    if deadline-time.time()<30:
        return {'ok':False,'failure':'campaign_budget_exhausted','delivery':'missing','raw_answer':''}
    run_id='web-eval-'+request_id
    journal=WebJournal(policy.path)
    journal.start(run_id,{'purpose':'frozen_structured_trial','portal_id':portal_id,
                          'request_id':request_id,'adapter_hash':profile['adapter_hash'],
                          'url':adapter['url'],'prompt':prompt},deadline-time.time())
    journal.event(run_id,'external_request',{'request_id':request_id,'portal_id':portal_id})
    pool=KindCollectivePool(db_path=policy.path,external_hf_only=True,enable_semantic_cache=False)
    result={'ok':False,'delivery':'missing','failure':'reply_not_collected','raw_answer':''}
    try:
        from tools.control_client import caller
        account_policy = None
        if adapter.get('account_grant'):
            from tools.guild_web_auth import AccountPolicy
            account_policy = AccountPolicy.load(adapter['account_grant'])
        with caller(run_id,'portal-qualification'),GuildWebBroker(run_id,journal,start_browser=True,
                task_prompt=prompt,task_url=adapter['url'],account_policy=account_policy) as broker:
            broker.allowed_urls.add(adapter['url'])
            summary=run_cycle(pool,broker,journal,run_id,
                'Open the authorized portal '+adapter['url']+'. Establish a fresh conversation. '
                'Use submit_test to deliver the host-owned frozen task once. Observe until response_collected, then finish.',
                max_steps=12,controller='local14b')
            if broker.observed_reply:
                from tools.cyber_compliance_sentinel import CyberComplianceSentinel
                report=CyberComplianceSentinel().inspect_and_report_threat(
                    source_identifier=portal_id,content=broker.observed_reply,
                    context={'source':'audited_web_ui','run_id':run_id,'request_id':request_id})
                if report:
                    return {'ok':False,'delivery':'observed','failure':'access_denied',
                            'error':'response_quarantined','raw_answer':'','quarantine_report_id':report['report_id']}
                result={'ok':True,'delivery':'observed','raw_answer':broker.observed_reply,
                        'raw_response':{'run_id':run_id,'observation_event_id':broker.response_event_id},
                        'browser_run_id':run_id,'response_event_id':broker.response_event_id}
            elif broker.blocked_origins:
                reasons={x for v in broker.blocked_origins.values() for x in v}
                failure=('captcha' if 'human_verification_required' in reasons else
                         'auth_required' if any('auth' in x or 'personal_data' in x for x in reasons) else
                         'quota_exhausted' if 'usage_limit_observed' in reasons else 'access_denied')
                result.update(failure=failure)
            else:
                sent=journal.conn.execute('SELECT state FROM guild_web_submissions WHERE run_id=?',(run_id,)).fetchall()
                if sent:result.update(failure='uncertain',delivery='uncertain')
        return result
    except Exception as exc:
        sent=journal.conn.execute('SELECT state FROM guild_web_submissions WHERE run_id=?',(run_id,)).fetchall()
        result.update(failure='uncertain' if sent else 'browser_controller_unavailable',
                      delivery='uncertain' if sent else 'missing',error=type(exc).__name__)
        journal.finish(run_id,'blocked',{'reason':result['failure'],'error':type(exc).__name__})
        return result
    finally:
        pool.blackboard.close();journal.conn.close()


def prepare_candidate(policy,candidate_id):
    """Only independently observed UI candidates receive evaluation profiles."""
    with policy.db() as db:
        row=db.execute('SELECT * FROM swarm_discovery_candidates WHERE portal_id=?',(candidate_id,)).fetchone()
    if not row or row['probe_status']!='ui_single_smoke_pass_unqualified':
        raise ValueError('Candidate lacks a verified UI smoke')
    parts=row['source'].split(':')
    if len(parts)!=4 or parts[0]!='guild_web_growth' or parts[2]!='observation':
        raise ValueError('Missing run/observation provenance')
    journal=WebJournal(policy.path)
    try:
        report=journal.report(parts[1])
        if report['run']['state']=='expired':raise ValueError('Candidate evidence expired')
        from tools.guild_web_growth import stage_verified
        verified=stage_verified(journal,parts[1])
        if not any(r['portal_id']==candidate_id and r['observation_event_id']==int(parts[3]) for r in verified):
            raise ValueError('Smoke provenance mismatch')
        raw=json.loads(next(e['raw'] for e in report['events'] if e['id']==int(parts[3])))
        if public_url(raw['url'])!=row['url']:raise ValueError('Candidate origin mismatch')
        adapter={'kind':'audited_web_ui','url':raw['url'],'verified_protocol':True,
                 'protocol':'fresh_chat_visible_json_block_v1','smoke_source':row['source']}
        if raw.get('account_policy'):
            from tools.guild_web_auth import AccountPolicy
            adapter['account_grant'] = AccountPolicy.load(raw['account_policy']).grant()
        prior=policy.profile(candidate_id)
        if prior and json.loads(prior['adapter_json'])!=adapter:
            raise ValueError('Existing profile differs; explicit adapter review required')
        policy.register(candidate_id,adapter,'Public UI; underlying model unverified',
                        access_allowed=True,access_reason='operator_authorized_live_qualification')
        return adapter
    finally:journal.conn.close()


def activate_qualified(policy,portal_id,run_id):
    """Publish only the existing evaluator's current live structured role."""
    summary=Evaluator(policy).summarize(run_id)
    with policy.db() as db:
        run=db.execute('SELECT * FROM portal_eval_runs WHERE run_id=?',(run_id,)).fetchone()
    if not run or run['portal_id']!=portal_id or run['role']!='structured_extraction' or not summary['qualified']:
        return {'activated':False,'reason':'qualification_not_passed','summary':summary}
    eligibility=policy.eligibility(portal_id,'structured_extraction')
    if not eligibility['eligible'] or eligibility['responsibility']!='qualified':
        return {'activated':False,'reason':eligibility['reason'],'summary':summary}
    profile=policy.profile(portal_id);adapter=json.loads(profile['adapter_json']);now=policy.clock()
    with policy.db() as db:
        db.execute('''INSERT INTO assistant_external_portals
          (portal_id,name,region,category,url,auth_mode,status,catalog_state,reachability_state,tested_state,
           qualification_state,execution_mode,actual_model,discovered_at,last_benchmarked,qualified_roles)
          VALUES (?,?,?,'public_web_ui',?,?,'active','cataloged','reachable','tested','qualified','live','unknown',?,?,?)
          ON CONFLICT(portal_id) DO UPDATE SET tested_state='tested',qualification_state='qualified',
          auth_mode=excluded.auth_mode,last_benchmarked=excluded.last_benchmarked,qualified_roles=excluded.qualified_roles''',
          (portal_id,urlsplit(adapter['url']).hostname,'unverified',adapter['url'],
           'authorized_account_scope' if adapter.get('account_grant') else 'zero_auth',now,now,json.dumps(['structured_extraction'])))
        db.execute("UPDATE swarm_discovery_candidates SET state='qualified' WHERE portal_id=?",(portal_id,))
        db.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                   (now,'qualified_web_member_added',json.dumps({'portal_id':portal_id,'run_id':run_id,'role':'structured_extraction'})))
    return {'activated':True,'portal_id':portal_id,'role':'structured_extraction','summary':summary}
