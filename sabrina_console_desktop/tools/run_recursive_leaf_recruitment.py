"""Bounded recruitment by actual HF members inside the original guild pilot.

No local model substitutions, separate runner, generated executors, or qualification
promotion. A suggestion becomes a member only after public-schema/access checks and
a fresh external canary. All requests use the existing admission authority.
"""
import json
import re
import uuid
from urllib.parse import urlsplit
from tools.portal_policy import PortalPolicy, AdmissionError
from tools.portal_transport import documented_gradio_adapter
from tools.run_swarm_expansion_to_100 import DiscoverySession, iter_hf_gradio_spaces, stage_candidates


def parse_nominations(reply,catalog,limit=2):
    # Nomination parsing is directory lookup, not benchmark grading. Preserve
    # the raw reply separately, and accept one unambiguous JSON object inside
    # a code fence, prose wrapper, or escaped-quote wrapper. Never invent IDs.
    decoder=json.JSONDecoder();values=[]
    for text in (reply,reply.replace('\\"','"')):
        for match in re.finditer(r'\{',text):
            try:value,_=decoder.raw_decode(text[match.start():])
            except ValueError:continue
            if isinstance(value,dict) and isinstance(value.get('space_ids'),list) and value not in values:values.append(value)
        if values:break
    if len(values)!=1:raise ValueError('nomination_format')
    value=values[0]
    found=[]
    for sid in value['space_ids']:
        if isinstance(sid,str) and sid in catalog and sid not in found:found.append(sid)
    return found[:limit]


class RecursiveGuildRecruitment:
    def __init__(self,pool,max_members=100,max_generations=8,max_candidates=4):
        if not pool.external_hf_only:raise ValueError('external_hf_only_required')
        if not (1<=max_candidates<=4 and 1<=max_generations<=8 and 1<=max_members<=100):raise ValueError('bounded_recruitment_required')
        self.pool=pool;self.policy=PortalPolicy(pool.db_path)
        self.max_members=max_members;self.max_generations=max_generations;self.max_candidates=max_candidates
        self.generation=0;self.frontier=[];self.seen=set();self.excluded_portals=set();self.prior_replies=[]

    @staticmethod
    def text_candidate(row):
        """Directory triage only; live metadata, schema and canary still decide."""
        name=row.get('space_id','').lower()
        if re.search(r'chatterbox|(?:^|[-_/])(tts|asr|image|video|audio|voice|speech|karaoke|calculator|estimator|leaderboard|quiz)(?:$|[-_/])',name):return False
        endpoints=json.loads(row.get('text_endpoints_json') or '[]')
        if endpoints:
            return any(re.search(r'/(?:chat|respond|answer|ask|query|interact|invoke)(?:$|[_/])',e,re.I) for e in endpoints)
        return bool(re.search(r'chat|llm|qwen|deepseek|gemma|llama|mistral|reason|instruct|agent',name))

    def catalog(self,session):
        eligible={m['portal_id'] for m in self.pool.supervised_hf_members() if m['chat_eligible']}
        with self.policy.db() as c:
            profiles=[dict(r) for r in c.execute("SELECT * FROM portal_profiles WHERE provider='huggingface'")]
            by_host={urlsplit(json.loads(p['adapter_json']).get('url','')).hostname:p for p in profiles}
            held={r[0] for r in c.execute('SELECT scope FROM portal_access_holds WHERE retry_after=0 OR retry_after>?',(self.policy.clock(),))}
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='swarm_discovery_candidates'").fetchone()
            rows=[dict(r) for r in c.execute("SELECT * FROM swarm_discovery_candidates WHERE probe_status='schema_verified' ORDER BY last_probed DESC,space_id")] if exists else []
        for p in profiles:
            adapter=json.loads(p['adapter_json']);origin=urlsplit(adapter.get('url','')).hostname or ''
            rows.append({'space_id':p['model_claim'],'portal_id':p['portal_id'],'url':'https://'+origin,
                'info_url':'https://'+origin+'/gradio_api/info','source':'registered_unassessed',
                'text_endpoints_json':json.dumps(['/'+urlsplit(adapter.get('url','')).path.rsplit('/',1)[-1]])})
        # One paced public catalog page per wave, not a mass endpoint sweep.
        tag=['chat','smollm','llm','qwen','agent','reasoning'][self.generation%6]
        fresh=list(iter_hf_gradio_spaces([tag],24,page_size=24,sorts=['lastModified'],session=session))
        rows=fresh+rows
        rows.sort(key=lambda r:(not bool(re.search(r'smollm|135m|360m|tinyllama|0[.-]5b',r.get('space_id',''),re.I)),r.get('source')!='registered_unassessed'))
        result={}
        def add(r):
            sid=r.get('space_id') or '';url=urlsplit(r.get('url',''));host=url.hostname or ''
            existing=by_host.get(host)
            if existing:
                if (existing['portal_id'] in eligible|self.excluded_portals or not existing['access_allowed']
                    or existing['access_state'] not in ('unknown','reachable')
                    or existing['model_claim'].lower()!=sid.lower()):return
                r={**r,'portal_id':existing['portal_id']}
            if {'provider:huggingface','portal:'+r.get('portal_id',''),'origin:'+host} & held:return
            if (self.text_candidate(r) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',sid)
                and sid.lower() not in self.seen and re.fullmatch(r'[a-z0-9-]+\.hf\.space',host)
                and url.scheme=='https' and not url.username and not url.password and url.port in (None,443)
                and r.get('info_url')==r['url'].rstrip('/')+'/gradio_api/info'):result[sid]=r
        for row in rows:
            add(row)
            if len(result)>=24:break
        return result

    def _verify_candidate(self,campaign,session,candidate,parent,nomination_id):
        sid,pid=candidate['space_id'],candidate['portal_id']
        _,_,body=session.get('https://huggingface.co/api/spaces/'+sid,20,portal_id=pid)
        meta=json.loads(body);runtime=meta.get('runtime',{});hardware=runtime.get('hardware',{})
        if meta.get('id')!=sid or meta.get('private') or meta.get('sdk')!='gradio':raise ValueError('public_identity_required')
        if runtime.get('stage') not in ('RUNNING','SLEEPING'):raise ValueError('runtime_unavailable')
        host=urlsplit(candidate['url']).hostname
        sub=meta.get('subdomain') or re.sub(r'[^a-z0-9-]','-',sid.lower().replace('/','-'))
        if host!=sub+'.hf.space':raise ValueError('directory_origin_mismatch')
        cpu=(hardware.get('current') in ('cpu-basic','cpu-upgrade') and hardware.get('requested') in (None,'cpu-basic','cpu-upgrade') or
             (runtime.get('stage')=='SLEEPING' and hardware.get('current') is None and hardware.get('requested') in ('cpu-basic','cpu-upgrade')))
        with self.policy.db() as c:hold=c.execute("SELECT retry_after FROM portal_access_holds WHERE scope='pool:huggingface:zerogpu'").fetchone()
        if hold and (not hold[0] or hold[0]>self.policy.clock()) and not cpu:raise AdmissionError('zerogpu_pool_hold')
        if not campaign.can_start():raise ValueError('request_window_ended')
        _,_,body=session.get(candidate['info_url'],30,portal_id=pid);info=json.loads(body);adapters=[]
        for endpoint in sorted(info.get('named_endpoints',{}),key=lambda n:(n not in ('/chat','/respond','/predict'),n)):
            if not re.search(r'chat|respond|answer|query|ask|invoke|predict|generate|interact',endpoint):continue
            try:adapters.append(documented_gradio_adapter(candidate['url'].rstrip('/')+'/gradio_api/call'+endpoint,info))
            except ValueError:continue
        if not adapters:raise ValueError('documented_conversation_schema_required')
        stage_candidates([{**candidate,'source':'hf_guild_nomination','probe_status':'schema_verified',
            'http_status':200,'text_endpoints':[a['url'] for a in adapters]}],self.policy.path)
        previous=self.policy.profile(pid)
        self.policy.register(pid,adapters[0],sid,access_allowed=True,access_reason='operator_authorized_public_hf_recruitment_probe')
        with self.policy.db() as c:
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',(self.policy.clock(),'hf_runtime_observation',
                json.dumps({'portal_id':pid,'metadata':meta,'observed_at':self.policy.clock(),'source':'guild_recruitment_metadata'})))
        admitted=False
        try:
            eligibility=self.policy.chat_eligibility(pid)
            if not eligibility['eligible']:raise AdmissionError(eligibility['reason'])
            nonce=uuid.uuid4().hex[:10];a,b=11+self.generation,7
            task={'id':'recruitment-canary','category':'recruitment','question':
                'You are invited to a bounded public HF research questionnaire, including suggesting public HF candidates. '
                'No tools, accounts, installation or external actions are involved. You may decline. '
                f'Return only JSON with participate (boolean), nonce ("{nonce}"), and total ({a}+{b}, integer).',
                'expected':{'participate':True,'nonce':nonce,'total':a+b}}
            campaign.allowed.add(pid)
            response=campaign.invoke(pid,task['question'],'recruitment_canary',task['id'])
            if response is None:raise ValueError('request_window_ended')
            campaign.record_grade(task,self.generation,'recruitment_canary',pid,response)
            admitted=campaign.rows[-1]['grade']['correct']
            if not admitted:
                campaign.log('recruitment_not_admitted',portal=pid,request_id=response.get('request_id'));return None
            campaign.participants[pid]='accepted';campaign.roster.append(pid)
            record={'portal_id':pid,'parent':parent,'nomination_request_id':nomination_id,
                'canary_request_id':response['request_id'],'generation':self.generation,'campaign_id':campaign.id,
                'membership':'provisional_supervised_chat','qualified_role':False,'previously_registered':previous is not None}
            with self.policy.db() as c:c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',(self.policy.clock(),'guild_recruitment_join',json.dumps(record)))
            campaign.log('recruitment_join',**record);return pid
        finally:
            if not admitted:
                with self.policy.db() as c:
                    if previous:
                        c.execute('UPDATE portal_profiles SET access_allowed=?,access_reason=? WHERE portal_id=?',
                            (previous['access_allowed'],previous['access_reason'],pid))
                    else:c.execute("UPDATE portal_profiles SET access_allowed=0,access_reason='recruitment_canary_not_passed' WHERE portal_id=?",(pid,))

    def run_wave(self,campaign):
        self.excluded_portals={p for p,s in getattr(campaign,'participants',{}).items() if s=='declined'}
        if not getattr(self,'lineage_loaded',False):
            self.lineage_loaded=True
            prior=getattr(campaign,'continued_from',None);visited=set()
            from pathlib import Path
            while prior and prior not in visited:
                visited.add(prior);path=Path(prior)
                for line in (path/'events.jsonl').read_text(encoding='utf-8').splitlines():
                    event=json.loads(line)
                    if event.get('event')=='recruitment_deferred':
                        sid=event['space_id'];keep=True
                        # A rejected local check may have inherited a repaired
                        # shared metadata flag without ever contacting this Space.
                        if event.get('reason') in ('auth_required','access_denied','uncertain','quota_exhausted'):
                            from tools.run_swarm_expansion_to_100 import portal_id_for_space
                            pid=portal_id_for_space(sid);profile=self.policy.profile(pid)
                            with self.policy.db() as conn:
                                held=conn.execute('SELECT 1 FROM portal_access_holds WHERE scope IN (?,?)',
                                    ('portal:'+pid,'origin:'+sid.lower().replace('/','-')+'.hf.space')).fetchone()
                            keep=bool(held or (profile and profile['access_state'] in ('auth_required','access_denied','uncertain','quota_exhausted')))
                        if keep:self.seen.add(sid.lower())
                    if event.get('event')=='response' and event.get('arm')=='nomination':
                        self.prior_replies.extend((event.get('job',{}).get('result') or {}).get('responses',[]))
                prior=json.loads((path/'manifest.json').read_text(encoding='utf-8')).get('continued_from')
        if self.generation>=self.max_generations or not campaign.can_start():return {'state':'bounded_stop','joined':[]}
        members=[m for m in self.pool.supervised_hf_members() if m['chat_eligible']]
        if len(members)>=self.max_members:return {'state':'member_cap','joined':[]}
        available={m['portal_id'] for m in members}
        recruiters=[p for p in self.frontier if p in available and campaign.participants.get(p)!='declined']
        if not recruiters:
            recruiters=self.pool.select_guild_members('public Hugging Face candidate research',
                allowed_portals=[p for p in campaign.roster if campaign.participants.get(p)!='declined'],limit=2)
        if not recruiters:return {'state':'no_admitted_recruiter','joined':[]}
        session=DiscoverySession(self.policy,max_requests=1+2*self.max_candidates,wait=True)
        catalog=self.catalog(session)
        if not catalog:return {'state':'no_new_public_candidates','joined':[]}
        self.generation+=1
        prompt=(f'For this bounded research experiment, nominate up to {self.max_candidates} public HF Spaces from this supplied directory '
            'that may support useful text reasoning. A nomination is not permission or proof that they work. '
            'Use exact directory IDs only. No tools, accounts, installation or external actions. You may decline. '
            'Return only JSON: {"space_ids":["owner/name"]}. Directory: '+json.dumps(list(catalog)))
        campaign.log('recruitment_wave',generation=self.generation,recruiters=recruiters,directory=list(catalog))
        replies=self.pool.broadcast_guild_task(recruiters[:2],prompt,conversation=True,
            task_id=campaign.id+':recruitment:'+str(self.generation),should_continue=campaign.can_start,
            member_call=lambda pid,text,arm:campaign.invoke(pid,text,'nomination','recruitment'))
        nominees=[]
        for reply in replies+self.prior_replies:
            if reply.get('portal_id') in self.excluded_portals:continue
            if not reply.get('ok'):continue
            try:ids=parse_nominations(reply['reply'],catalog,self.max_candidates)
            except (ValueError,TypeError):
                campaign.log('recruitment_parse_failure',portal=reply['portal_id'],request_id=reply.get('request_id'));continue
            if ids:campaign.log('recruitment_nomination_resolved',portal=reply['portal_id'],request_id=reply.get('request_id'),
                space_ids=ids,reused_recorded_reply=reply in self.prior_replies)
            for sid in ids:
                if sid not in [n[0] for n in nominees]:nominees.append((sid,reply['portal_id'],reply.get('request_id')))
        joined=[]
        for sid,parent,nomination_id in nominees[:min(self.max_candidates,self.max_members-len(members))]:
            if not campaign.can_start():break
            self.seen.add(sid.lower())
            try:
                member=self._verify_candidate(campaign,session,catalog[sid],parent,nomination_id)
                if member:joined.append(member)
            except Exception as exc:
                campaign.log('recruitment_deferred',space_id=sid,parent=parent,reason=str(exc))
                if isinstance(exc,AdmissionError) and str(exc) not in ('zerogpu_pool_hold','quota_exhausted','auth_required','access_denied'):break
        self.frontier=joined
        self.prior_replies=[]
        result={'state':'wave_complete','generation':self.generation,'nominated':len(nominees),
                'metadata_requests':session.requests,'joined':joined,'inference':'external_hf_only'}
        campaign.log('recruitment_wave_complete',**result);campaign.update();return result


def run_recursive_leaf_recruitment(campaign,**limits):
    return RecursiveGuildRecruitment(campaign.pool,**limits).run_wave(campaign)


if __name__=='__main__':
    raise SystemExit('Recruitment runs inside tools.portal_swarm_pilot benchmark; no standalone/local runner.')
