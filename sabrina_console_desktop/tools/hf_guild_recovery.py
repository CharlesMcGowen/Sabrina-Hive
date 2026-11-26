"""Audited recovery of collateral ZeroGPU flags using live HF runtime metadata."""
import json
import urllib.error
from urllib.parse import urlsplit
from tools.portal_policy import PortalPolicy, AdmissionError
from tools.portal_transport import read_json, classify, retry_seconds


def scope_recorded_hf_timeout(policy, request_id, before_profiles, controller_receipt):
    """Isolate one finished client attempt; keep its remote outcome unresolved.

    Restore only the proven collateral profile stamp from a prior registry snapshot.
    Never remove provider quota/rate holds or declare the remote request completed.
    """
    responses=(controller_receipt.get('result') or {}).get('responses',[])
    matches=[r for r in responses if r.get('request_id')==request_id]
    if (controller_receipt.get('state') not in ('failed','complete') or len(matches)!=1
        or matches[0].get('failure')!='uncertain' or matches[0].get('delivery')!='uncertain'
        or matches[0].get('execution_mode')!='live' or matches[0].get('http_status')):
        raise ValueError('finished_client_timeout_receipt_required')
    prior={r['portal_id']:r for r in before_profiles}
    with policy.db() as c:
        c.execute('BEGIN IMMEDIATE')
        source=c.execute('SELECT * FROM portal_requests WHERE request_id=?',(request_id,)).fetchone()
        if not source or source['provider']!='huggingface' or source['state']!='uncertain' or not source['completed']:
            raise ValueError('recorded_hf_timeout_required')
        profile=c.execute('SELECT adapter_json FROM portal_profiles WHERE portal_id=?',(source['portal_id'],)).fetchone()
        host=urlsplit(json.loads(profile[0])['url']).hostname if profile else None
        if not host or not host.endswith('.hf.space'):raise ValueError('hf_origin_required')
        other=c.execute("SELECT request_id FROM portal_requests WHERE provider='huggingface' AND state IN ('active','uncertain') AND request_id<>?",(request_id,)).fetchall()
        if other:raise ValueError('other_unresolved_provider_work')
        hold=c.execute("SELECT reason,retry_after FROM portal_access_holds WHERE scope='provider:huggingface'").fetchone()
        if not hold or tuple(hold)!=('uncertain',0):raise ValueError('provider_hold_not_this_timeout')
        changes=[]
        for row in c.execute("SELECT portal_id,adapter_json,access_state,retry_after FROM portal_profiles WHERE provider='huggingface'").fetchall():
            if row['access_state']!='uncertain' or abs(row['retry_after']-source['completed'])>2:continue
            if row['portal_id'] not in prior:raise ValueError('missing_prior_profile')
            source_origin=urlsplit(json.loads(row['adapter_json'])['url']).hostname==host
            state='uncertain' if source_origin else prior[row['portal_id']]['access_state']
            retry=0 if source_origin else prior[row['portal_id']]['retry_after']
            c.execute('UPDATE portal_profiles SET access_state=?,retry_after=? WHERE portal_id=?',(state,retry,row['portal_id']))
            changes.append({'portal_id':row['portal_id'],'after':state,'retry_after':retry})
        c.execute('INSERT OR REPLACE INTO portal_access_holds VALUES (?,?,0)',('origin:'+host,'uncertain'))
        c.execute("DELETE FROM portal_access_holds WHERE scope='provider:huggingface' AND reason='uncertain' AND retry_after=0")
        record={'request_id':request_id,'scope':'origin:'+host,'remote_outcome':'unresolved_do_not_replay',
            'client_attempt_finished':True,'changes':changes,'provider_quotas_unchanged':True}
        c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',(policy.clock(),'hf_origin_uncertainty',json.dumps(record)))
    return record

def migrate_legacy_zerogpu(policy, trigger_id):
    """Replace a proven old blanket stamp with the original timed pool hold."""
    from tools.portal_transport import quota_scope_evidence
    with policy.db() as c:
        c.execute('BEGIN IMMEDIATE')
        source=c.execute('SELECT * FROM portal_requests WHERE request_id=?',(trigger_id,)).fetchone()
        if not source or source['state']!='complete':raise ValueError('completed_source_required')
        raw=c.execute("SELECT detail FROM portal_audit WHERE kind='supervised_chat_response' AND json_extract(detail,'$.request_id')=?",(trigger_id,)).fetchone()
        profile=c.execute('SELECT adapter_json FROM portal_profiles WHERE portal_id=?',(source['portal_id'],)).fetchone()
        evidence=quota_scope_evidence(json.loads(profile[0]),json.loads(raw[0])) if raw and profile else {}
        if evidence.get('quota_scope')!='zerogpu' or not evidence.get('retry_after_seconds'):raise ValueError('timed_zerogpu_evidence_required')
        retry_at=source['completed']+evidence['retry_after_seconds']
        existing=c.execute("SELECT retry_after FROM portal_access_holds WHERE scope='pool:huggingface:zerogpu'").fetchone()
        if existing and existing[0]==0:raise ValueError('independent_indefinite_pool_hold')
        retry_at=max(retry_at,existing[0] if existing else 0)
        c.execute('INSERT OR REPLACE INTO portal_access_holds VALUES (?,?,?)',('pool:huggingface:zerogpu','quota_exhausted',retry_at))
        rows=c.execute("SELECT portal_id,access_state,retry_after FROM portal_profiles WHERE provider='huggingface' AND access_state='quota_exhausted' AND abs(retry_after-?)<2",(source['completed'],)).fetchall()
        changes=[]
        for row in rows:
            # Do not erase independently observed authentication or timeout barriers.
            last=c.execute("SELECT result FROM portal_requests WHERE portal_id=? AND request_id<>? AND purpose<>'discovery' ORDER BY started DESC LIMIT 1",(row['portal_id'],trigger_id)).fetchone()
            barrier=last[0] if last and last[0] in ('auth_required','captcha','access_denied','uncertain','quota_exhausted') else 'unknown'
            c.execute('UPDATE portal_profiles SET access_state=?,retry_after=0 WHERE portal_id=?',(barrier,row['portal_id']))
            changes.append({'portal_id':row['portal_id'],'before':dict(row),'after':barrier})
        record={'trigger_id':trigger_id,'retry_not_before':retry_at,'changes':changes,'holds_removed':[],'historical_responses_unchanged':True}
        c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',(policy.clock(),'legacy_zerogpu_pool_migration',json.dumps(record)))
    return record

def observe_runtime(policy, portal_id):
    profile=policy.profile(portal_id)
    space=profile['model_claim']
    url='https://huggingface.co/api/spaces/'+space
    rid=policy.admit_discovery('runtime:'+portal_id,url)
    try:
        metadata=read_json(url)
        if metadata.get('id') != space:
            raise ValueError('runtime_identity_mismatch')
    except Exception as exc:
        result={'failure':'metadata_error','delivery':'missing','error':type(exc).__name__}
        if isinstance(exc, urllib.error.HTTPError):
            result.update(failure=classify('',exc.code),http_status=exc.code,
                retry_after_seconds=retry_seconds(exc.headers.get('Retry-After')) if exc.code==429 else 0,
                response_headers={key:exc.headers[key] for key in ('Retry-After','Content-Type','X-Request-Id') if key in exc.headers})
        policy.finish(rid,result)
        with policy.db() as c:
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                (policy.clock(),'hf_runtime_error',json.dumps({'portal_id':portal_id,'url':url,'request_id':rid,**result})))
        raise
    evidence={'portal_id':portal_id,'url':url,'metadata':metadata,'observed_at':policy.clock(),'request_id':rid}
    policy.finish(rid,{'delivery':'observed'})
    with policy.db() as c:
        c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
            (policy.clock(),'hf_runtime_observation',json.dumps(evidence)))
    return evidence

def release_collateral_cpu(policy, portal_id, observation_id, trigger_id):
    """Does not clear holds, mark reachable, or touch qualifications/other profiles."""
    with policy.db() as c:
        c.execute('BEGIN IMMEDIATE')
        profile=dict(c.execute('SELECT * FROM portal_profiles WHERE portal_id=?',(portal_id,)).fetchone())
        source=c.execute('SELECT * FROM portal_requests WHERE request_id=?',(trigger_id,)).fetchone()
        observed=c.execute("SELECT detail FROM portal_audit WHERE kind='hf_runtime_observation' AND json_extract(detail,'$.request_id')=?",(observation_id,)).fetchone()
        raw=c.execute("SELECT detail FROM portal_audit WHERE kind='supervised_chat_response' AND json_extract(detail,'$.request_id')=?",(trigger_id,)).fetchone()
        if not source or not raw or not observed:raise ValueError('missing_provenance')
        evidence=json.loads(observed[0]);meta=evidence['metadata'];runtime=meta.get('runtime',{})
        if (evidence['portal_id']!=portal_id or meta.get('id')!=profile['model_claim']
            or policy.clock()-evidence['observed_at']>600 or runtime.get('stage')!='RUNNING'
            or runtime.get('hardware',{}).get('current') not in ('cpu-basic','cpu-upgrade')):
            raise ValueError('running_cpu_evidence_required')
        response=json.loads(raw[0])
        if (source['portal_id']==portal_id or source['result']!='quota_exhausted'
            or response.get('failure')!='quota_exhausted' or 'zerogpu' not in raw[0].lower()
            or profile['access_state']!='quota_exhausted'
            or abs(profile['retry_after']-source['completed'])>2):
            raise ValueError('not_a_collateral_zerogpu_flag')
        origin=urlsplit(json.loads(profile['adapter_json'])['url']).hostname
        scopes=('provider:'+profile['provider'],'origin:'+origin,'portal:'+portal_id)
        if c.execute('SELECT 1 FROM portal_access_holds WHERE scope IN (?,?,?) AND (retry_after=0 OR retry_after>?)',(*scopes,policy.clock())).fetchone():
            raise ValueError('independent_hold_preserved')
        if c.execute("SELECT 1 FROM portal_requests WHERE portal_id=? AND (state IN ('active','uncertain') OR result IN ('quota_exhausted','auth_required','captcha','access_denied','uncertain'))",(portal_id,)).fetchone():
            raise ValueError('independent_failure_preserved')
        c.execute("UPDATE portal_profiles SET access_state='unknown',retry_after=0 WHERE portal_id=?",(portal_id,))
        c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',(policy.clock(),'cpu_guild_scope_recovery',json.dumps({'portal_id':portal_id,'before_state':profile['access_state'],'before_retry_after':profile['retry_after'],'observation_id':observation_id,'trigger_id':trigger_id,'after':'unknown_not_yet_inference_tested'})))
    return {'portal_id':portal_id,'released':True,'state':'unknown'}
