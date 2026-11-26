"""Versioned portal evidence, access policy and cross-process request admission.

This database is the authority for external requests, including explicit selections.
Legacy benchmark scores are historical evidence, never capability credentials.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get('SABRINA_ASSISTANT_DB', ROOT / 'data/sabrina_assistant_fresh.sqlite3'))
ROLES = ('grounded_summarization', 'structured_extraction', 'translation', 'code_generation', 'critique')
API_TIER_PROVIDERS = (
    'generativelanguage.googleapis.com',
    'router.huggingface.co',
    'api.openai.com',
    'api.anthropic.com',
    'api.groq.com',
    'integrate.api.nvidia.com',
    'models.inference.ai.azure.com',
)
VERSION = 'respectful-portals-v1'
TTL = 30 * 86400


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def model_identity(profile):
    return profile['model_claim'] + ('|verified:' + profile['verified_model'] if profile.get('verified_model') else '|unverified')


def adapter_fingerprint(adapter):
    sources = {name:hashlib.sha256((ROOT/'tools'/name).read_bytes()).hexdigest()
               for name in ('portal_policy.py','portal_transport.py')}
    if adapter.get('kind')=='audited_web_ui':
        sources.update({name:hashlib.sha256((ROOT/'tools'/name).read_bytes()).hexdigest()
                        for name in ('guild_web_qualification.py','guild_web_broker.py','guild_web_growth.py','control_client.py')})
    return digest({'adapter':adapter,'transport_version':VERSION,'sources':sources})


def verified_https_adapter(adapter):
    if adapter.get('verified_protocol') is not True:
        return False
    try:
        url = urlsplit(adapter.get('url', ''))
        return (url.scheme == 'https' and bool(url.hostname) and not url.username
                and not url.password and url.port in (None, 443))
    except ValueError:
        return False


class AdmissionError(RuntimeError):
    pass


class PortalPolicy:
    def __init__(self, db_path=DEFAULT_DB, clock=time.time):
        self.path = Path(db_path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript('''
CREATE TABLE IF NOT EXISTS portal_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_profiles (
 portal_id TEXT PRIMARY KEY, provider TEXT NOT NULL, adapter_json TEXT NOT NULL,
 adapter_hash TEXT NOT NULL, model_claim TEXT NOT NULL, verified_model TEXT,
 access_allowed INTEGER NOT NULL DEFAULT 0, access_reason TEXT NOT NULL,
 access_state TEXT NOT NULL DEFAULT 'unknown', retry_after REAL NOT NULL DEFAULT 0,
 cooldown REAL NOT NULL DEFAULT 10, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS portal_eval_runs (
 run_id TEXT PRIMARY KEY, portal_id TEXT NOT NULL, role TEXT NOT NULL,
 suite_hash TEXT NOT NULL, adapter_hash TEXT NOT NULL, model_claim TEXT NOT NULL,
 grader_version TEXT NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL,
 created REAL NOT NULL, completed REAL, summary_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS portal_eval_cases (
 run_id TEXT NOT NULL, case_id TEXT NOT NULL, attempt INTEGER NOT NULL,
 prompt_hash TEXT NOT NULL, response_json TEXT NOT NULL, grade_json TEXT NOT NULL,
 created REAL NOT NULL, PRIMARY KEY(run_id, case_id, attempt));
CREATE TABLE IF NOT EXISTS portal_role_qualifications (
 portal_id TEXT NOT NULL, role TEXT NOT NULL, responsibility TEXT NOT NULL,
 run_id TEXT, adapter_hash TEXT NOT NULL, model_claim TEXT NOT NULL,
 tools_json TEXT NOT NULL DEFAULT '[]', qualified_at REAL NOT NULL,
 expires REAL NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(portal_id, role));
CREATE TABLE IF NOT EXISTS portal_requests (
 request_id TEXT PRIMARY KEY, portal_id TEXT NOT NULL, provider TEXT NOT NULL,
 purpose TEXT NOT NULL, day TEXT NOT NULL, started REAL NOT NULL,
 state TEXT NOT NULL, completed REAL, result TEXT);
CREATE TABLE IF NOT EXISTS portal_access_holds (
 scope TEXT PRIMARY KEY, reason TEXT NOT NULL, retry_after REAL NOT NULL);
CREATE INDEX IF NOT EXISTS portal_requests_provider ON portal_requests(provider, started);
CREATE TABLE IF NOT EXISTS portal_route_counts (
 scope TEXT PRIMARY KEY, total INTEGER NOT NULL DEFAULT 0, reserves INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS portal_audit (
 id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_response_cache (
 cache_key TEXT PRIMARY KEY, response_json TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS portal_tool_operations (
 operation_id TEXT PRIMARY KEY, portal_id TEXT NOT NULL, request_id TEXT NOT NULL,
 action_hash TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT, created REAL NOT NULL);
''')
            for k, v in [('research_enabled', False), ('tools_enabled', False),
                         ('research_candidates_enabled', False), ('research_candidate_daily_limit', 6),
                         ('research_candidate_portal_daily_limit', 2),
                         ('daily_limit', 250), ('portal_daily_limit', 20),
                         ('chat_daily_limit', 50), ('chat_budget_mode', 'shared'),
                         ('api_tiers_enabled', False)]:
                c.execute('INSERT OR IGNORE INTO portal_settings VALUES (?,?)', (k, json.dumps(v)))

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(str(self.path), timeout=20)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA foreign_keys=ON')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def setting(self, key):
        with self.db() as c:
            row = c.execute('SELECT value FROM portal_settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_enabled(self, *, research=False, tools=False):
        if tools and not research:
            raise ValueError('Tool routing requires research routing')
        with self.db() as c:
            for k, v in [('research_enabled', research), ('tools_enabled', tools)]:
                c.execute('UPDATE portal_settings SET value=? WHERE key=?', (json.dumps(bool(v)), k))
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                      (self.clock(), 'feature_flags', json.dumps({'research': research, 'tools': tools})))

    def set_chat_budget_mode(self, mode):
        """Set local fallback accounting without changing provider enforcement."""
        if mode not in ('shared', 'per_portal', 'unlimited'):
            raise ValueError('chat_budget_mode must be shared, per_portal, or unlimited')
        with self.db() as c:
            c.execute('INSERT INTO portal_settings(key,value) VALUES (?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      ('chat_budget_mode', json.dumps(mode)))
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                      (self.clock(), 'chat_budget_mode', json.dumps({
                          'mode': mode, 'scope': 'local_fallback_only',
                          'provider_holds_unchanged': True,
                          'provider_rules_unchanged': True})))

    def set_research_candidates_enabled(self, enabled: bool):
        """Permit bounded, unqualified text research without changing tool or chat grants."""
        if enabled and not self.setting('research_enabled'):
            raise ValueError('Research routing must be enabled first')
        with self.db() as c:
            c.execute('UPDATE portal_settings SET value=? WHERE key=?',
                      (json.dumps(bool(enabled)), 'research_candidates_enabled'))
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                      (self.clock(), 'research_candidate_flag', json.dumps({'enabled': bool(enabled)})))

    def set_api_tiers_enabled(self, enabled: bool):
        """Toggle API-key tiers (OpenAI, Anthropic, Gemini, HF Router, Groq, NIM, GitHub Models) vs. open web portals."""
        flag = bool(enabled)
        placeholders = ','.join('?' for _ in API_TIER_PROVIDERS)
        reason = 'operator_enabled_api_tier' if flag else 'api_tier_disabled_by_operator'
        with self.db() as c:
            c.execute('INSERT INTO portal_settings(key,value) VALUES (?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      ('api_tiers_enabled', json.dumps(flag)))
            c.execute(f'UPDATE portal_profiles SET access_allowed=?, access_reason=?, updated=? '
                      f'WHERE provider IN ({placeholders})',
                      (int(flag), reason, self.clock(), *API_TIER_PROVIDERS))
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                      (self.clock(), 'api_tiers_flag', json.dumps({'enabled': flag, 'providers': list(API_TIER_PROVIDERS)})))

    def register(self, portal_id, adapter, model_claim, *, access_allowed=False, access_reason='unreviewed', verified_model=None):
        host = urlsplit(adapter.get('url', '')).hostname or ''
        # Multiple aliases and models sharing a provider share one concurrency limit.
        provider = 'huggingface' if host.endswith('.hf.space') else host
        h = adapter_fingerprint(adapter)
        with self.db() as c:
            c.execute('''INSERT INTO portal_profiles
 (portal_id,provider,adapter_json,adapter_hash,model_claim,verified_model,access_allowed,access_reason,updated)
 VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(portal_id) DO UPDATE SET
 provider=excluded.provider,adapter_json=excluded.adapter_json,adapter_hash=excluded.adapter_hash,
 model_claim=excluded.model_claim,verified_model=excluded.verified_model,
 access_allowed=excluded.access_allowed,access_reason=excluded.access_reason,updated=excluded.updated''',
                      (portal_id, provider, json.dumps(adapter), h, model_claim, verified_model,
                       int(access_allowed), access_reason, self.clock()))
        return h

    def profile(self, portal_id):
        with self.db() as c:
            r = c.execute('SELECT * FROM portal_profiles WHERE portal_id=?', (portal_id,)).fetchone()
        return dict(r) if r else None

    def migrate_legacy(self):
        """Idempotent migration; preserve legacy benchmark_json and every portal."""
        count = 0
        with self.db() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='assistant_external_portals'").fetchone():
                return 0
            rows = c.execute('SELECT portal_id,benchmark_json FROM assistant_external_portals').fetchall()
            for r in rows:
                for role in ROLES:
                    cur = c.execute('''INSERT OR IGNORE INTO portal_role_qualifications
 VALUES (?,?,'supervised_reserve',NULL,'','', '[]',0,0,'legacy_evidence_requires_clean_revalidation')''', (r[0], role))
                    count += cur.rowcount
            c.execute("UPDATE assistant_external_portals SET qualification_state='revalidation_required', requires_revalidation=1 WHERE qualification_state='qualified'")
            orphans=c.execute("""SELECT q.* FROM portal_role_qualifications q
 WHERE q.responsibility='qualified' AND NOT EXISTS
 (SELECT 1 FROM portal_eval_runs r WHERE r.run_id=q.run_id AND r.portal_id=q.portal_id
 AND r.role=q.role AND r.mode='live' AND r.state='complete')""").fetchall()
            for orphan in orphans:
                c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                          (self.clock(),'unprovenanced_role_preserved',json.dumps(dict(orphan))))
                c.execute("UPDATE portal_role_qualifications SET responsibility='supervised_reserve',expires=0,tools_json='[]',reason='missing_live_role_evidence' WHERE portal_id=? AND role=?",
                          (orphan['portal_id'],orphan['role']))
        return count

    def origin_hold(self, profile):
        origin = urlsplit(json.loads(profile['adapter_json']).get('url', '')).hostname or ''
        with self.db() as c:
            hold = c.execute('SELECT reason,retry_after FROM portal_access_holds WHERE scope=?',
                             ('origin:' + origin,)).fetchone()
            pool = c.execute("SELECT reason,retry_after FROM portal_access_holds WHERE scope='pool:huggingface:zerogpu'").fetchone() if profile['provider']=='huggingface' else None
            if pool and (not pool['retry_after'] or pool['retry_after']>self.clock()):
                runtime=c.execute("SELECT detail FROM portal_audit WHERE kind='hf_runtime_observation' AND json_extract(detail,'$.portal_id')=? ORDER BY id DESC LIMIT 1",(profile['portal_id'],)).fetchone()
                evidence=json.loads(runtime[0]) if runtime else {}
                metadata=evidence.get('metadata',{})
                runtime_state=metadata.get('runtime',{})
                hardware=runtime_state.get('hardware',{})
                current,requested=hardware.get('current'),hardware.get('requested')
                cpu_types=('cpu-basic','cpu-upgrade')
                # A sleeping CPU Space has no current allocation. Its configured
                # CPU hardware is not part of the ZeroGPU pool. Other holds still
                # apply; this permits a normal availability check, not a promotion.
                cpu_confirmed=(metadata.get('id')==profile['model_claim']
                    and 0 <= self.clock()-evidence.get('observed_at',0) < 86400
                    and ((current in cpu_types and requested in (None,*cpu_types))
                         or (current is None and runtime_state.get('stage')=='SLEEPING' and requested in cpu_types)))
                if not cpu_confirmed:
                    return pool['reason']
        return hold['reason'] if hold and (not hold['retry_after'] or hold['retry_after'] > self.clock()) else ''

    def eligibility(self, portal_id, role, tools=(), *, allow_reserve=False):
        result = {'eligible': False, 'responsibility': 'unassessed', 'reason': 'unknown_portal', 'tools': []}
        p = self.profile(portal_id)
        if not p:
            return result
        if p['provider'] in API_TIER_PROVIDERS and not self.setting('api_tiers_enabled'):
            return {**result, 'reason': 'api_tier_disabled_by_operator'}
        held = self.origin_hold(p)
        if held:
            return {**result, 'reason': held}
        if not p['access_allowed'] or p['access_state'] in ('auth_required', 'captcha', 'access_denied', 'quota_exhausted', 'uncertain'):
            return {**result, 'reason': p['access_state'] if p['access_allowed'] else p['access_reason']}
        if p['retry_after'] > self.clock():
            return {**result, 'reason': 'cooldown'}
        if not self.setting('research_enabled'):
            return {**result, 'reason': 'research_disabled'}
        adapter = json.loads(p['adapter_json'])
        candidate = (self.setting('research_candidates_enabled') and role in ROLES and not tools
                     and p['access_state'] == 'reachable' and verified_https_adapter(adapter))
        candidate_result = {**result, 'eligible': True, 'responsibility': 'experimental_research',
                            'reason': 'reachable_protocol_verified_unqualified', 'tools': []}
        with self.db() as c:
            q = c.execute('SELECT * FROM portal_role_qualifications WHERE portal_id=? AND role=?', (portal_id, role)).fetchone()
        if not q:
            return candidate_result if candidate else {**result, 'reason': 'unassessed_role'}
        result.update(dict(q))
        result['tools'] = json.loads(q['tools_json'])
        valid = (q['responsibility'] == 'qualified' and q['expires'] > self.clock()
                 and q['adapter_hash'] == p['adapter_hash'] == adapter_fingerprint(json.loads(p['adapter_json']))
                 and q['model_claim'] == model_identity(p))
        if valid:
            from tools.portal_evaluation import suite, grader_fingerprint
            with self.db() as c:
                run=c.execute('SELECT * FROM portal_eval_runs WHERE run_id=?',(q['run_id'],)).fetchone()
            valid=bool(run and run['mode']=='live' and run['state']=='complete'
                       and run['portal_id']==portal_id and run['role']==role
                       and run['adapter_hash']==q['adapter_hash'] and run['model_claim']==q['model_claim']
                       and run['suite_hash']==suite('qualification',role)[0]
                       and run['grader_version']==grader_fingerprint())
        if valid:
            if role == 'tool_use' and (not self.setting('tools_enabled') or not tools or not set(tools) <= set(result['tools'])):
                return {**result, 'eligible': False, 'reason': 'tool_scope_not_qualified'}
            if role != 'tool_use' and tools:
                return {**result, 'eligible': False, 'reason': 'research_has_no_tools'}
            return {**result, 'eligible': True, 'reason': 'current_role_evidence'}
        if candidate:
            return candidate_result
        return {**result, 'eligible': bool(allow_reserve and role in ROLES and not tools),
                'responsibility': 'supervised_reserve', 'reason': 'clean_revalidation_required', 'tools': []}

    def research_slot(self, want_reserve=False):
        """A reserve consumes a slot only after 19 ordinary eligible research calls."""
        day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO portal_route_counts(scope) VALUES (?)', (day,))
            r = c.execute('SELECT * FROM portal_route_counts WHERE scope=?', (day,)).fetchone()
            reserve = want_reserve and (r['reserves'] + 1) * 20 <= r['total'] + 1
            if want_reserve and not reserve:
                return False
            c.execute('UPDATE portal_route_counts SET total=total+1,reserves=reserves+? WHERE scope=?', (int(reserve), day))
        return True

    def chat_eligibility(self, portal_id):
        """Operator-enabled conversation access never grants a qualified role."""
        result = {'eligible': False, 'responsibility': 'supervised_chat', 'tools': []}
        p = self.profile(portal_id)
        if p and p['provider'] in API_TIER_PROVIDERS and not self.setting('api_tiers_enabled'):
            return {**result, 'reason': 'api_tier_disabled_by_operator'}
        if not self.setting('supervised_chat_enabled') or not self.setting('research_enabled'):
            return {**result, 'reason': 'supervised_chat_disabled'}
        if not p or not p['access_allowed']:
            return {**result, 'reason': 'access_not_authorized'}
        held = self.origin_hold(p)
        if held:
            return {**result, 'reason': held}
        adapter = json.loads(p['adapter_json'])
        u = urlsplit(adapter.get('url', ''))
        if (adapter.get('kind') != 'gradio' or not adapter.get('verified_protocol')
                or u.scheme != 'https' or not (u.hostname or '').endswith('.hf.space')
                or u.username or u.password or u.port not in (None, 443)):
            return {**result, 'reason': 'public_chat_protocol_required'}
        endpoint = u.path.rsplit('/', 1)[-1].lower()
        schema=adapter.get('schema',{})
        inputs,outputs=schema.get('parameters',[]),schema.get('returns',[])
        prompt_index,output_index=adapter.get('prompt_index',-1),adapter.get('output_index',-1)
        documented_chat=(isinstance(prompt_index,int) and 0<=prompt_index<len(inputs)
            and inputs[prompt_index].get('type',{}).get('type')=='string'
            and isinstance(output_index,int) and 0<=output_index<len(outputs)
            and (outputs[output_index].get('component')=='Chatbot'
                 or (outputs[output_index].get('component')=='Json'
                     and outputs[output_index].get('label','').lower() in ('response','answer')
                     and adapter.get('prompt_parameter')==inputs[prompt_index].get('parameter_name'))
                 or (outputs[output_index].get('component') in ('Markdown','Textbox')
                     and {'system_prompt','max_new_tokens'} <= {p.get('parameter_name') for p in inputs})))
        if not re.search(r'chat|respond|answer|query|ask|invoke|predict|generate', endpoint) and not documented_chat:
            return {**result, 'reason': 'conversation_endpoint_required'}
        claim = (portal_id + ' ' + p['model_claim']).lower()
        if any(term in claim for term in ('text-to-speech','speech-to-text','text-processor','text-analysis')):
            return {**result, 'reason': 'non_conversational_service'}
        outputs = adapter.get('schema', {}).get('returns', [])
        index = adapter.get('output_index', 0)
        if index < len(outputs) and outputs[index].get('component') in ('Audio','Video','Image','File','Gallery'):
            return {**result, 'reason': 'non_conversational_output'}
        if p['access_state'] in ('auth_required','captcha','access_denied','quota_exhausted','uncertain'):
            return {**result, 'reason': p['access_state']}
        if p['retry_after'] > self.clock():
            return {**result, 'reason': 'cooldown'}
        return {**result, 'eligible': True, 'reason': 'operator_supervised_conversation'}

    def admit(self, portal_id, purpose):
        if purpose not in ('evaluation', 'research', 'research_reserve', 'research_candidate', 'tool', 'repair', 'supervised_chat'):
            raise ValueError('Unknown request purpose')
        p = self.profile(portal_id)
        if not p or not p['access_allowed']:
            raise AdmissionError('access_not_authorized')
        try:
            return self._admit(p, purpose)
        except AdmissionError as exc:
            self.record_usage_denial(p, purpose, str(exc))
            raise

    def record_usage_denial(self, profile, purpose, reason):
        with self.db() as c:
            from tools.portal_usage_rules import assess, public_evidence
            rules, _ = assess(c, profile, self.clock()) if purpose != 'discovery' else ([], '')
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                      (self.clock(), 'usage_decision', json.dumps({
                          'portal_id': profile['portal_id'], 'purpose': purpose,
                          'decision': 'deferred_or_denied', 'reason': reason,
                          'rules': public_evidence(rules),
                          'source': 'local_safeguard' if reason in ('daily_budget_exhausted', 'chat_budget_exhausted', 'provider_cooldown', 'provider_busy') else 'access_or_documented_policy',
                          'request_sent': False})))

    def admit_discovery(self, portal_id, url):
        """Permit public HF metadata only; never register a candidate as a member."""
        u = urlsplit(url)
        host = u.hostname or ''
        if (u.scheme != 'https' or u.username or u.password or u.port not in (None, 443)
                or not ((host == 'huggingface.co' and (u.path == '/api/spaces'
                            or re.fullmatch(r'/api/spaces/[\w.-]+/[\w.-]+', u.path)))
                        or (host.endswith('.hf.space') and u.path == '/gradio_api/info'))):
            raise AdmissionError('discovery_url_not_permitted')
        p = {'portal_id': portal_id, 'provider': 'huggingface', 'cooldown': 10, 'origin': host}
        try:
            return self._admit(p, 'discovery')
        except AdmissionError as exc:
            self.record_usage_denial(p, 'discovery', str(exc))
            raise

    def _admit(self, p, purpose):
        portal_id = p['portal_id']
        now = self.clock()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if purpose == 'supervised_chat':
                evidence = self.chat_eligibility(portal_id)
                if not evidence['eligible']:
                    raise AdmissionError(evidence['reason'])
            if purpose != 'discovery':
                p = dict(c.execute('SELECT * FROM portal_profiles WHERE portal_id=?', (portal_id,)).fetchone())
                scoped_hold = self.origin_hold(p)
                if scoped_hold:
                    raise AdmissionError(scoped_hold)
                if not p['access_allowed']:
                    raise AdmissionError('access_not_authorized')
                if p['access_state'] in ('auth_required', 'captcha', 'access_denied', 'quota_exhausted', 'uncertain'):
                    raise AdmissionError(p['access_state'])
                if p['retry_after'] > now:
                    raise AdmissionError('cooldown')
                if purpose == 'research_candidate':
                    adapter = json.loads(p['adapter_json'])
                    settings = {r[0]: json.loads(r[1]) for r in c.execute('SELECT key,value FROM portal_settings')}
                    if (not settings.get('research_enabled') or not settings.get('research_candidates_enabled')
                            or p['access_state'] != 'reachable' or not verified_https_adapter(adapter)):
                        raise AdmissionError('research_candidate_not_eligible')
                    total, own = c.execute('''SELECT COUNT(*),COALESCE(SUM(portal_id=?),0)
 FROM portal_requests WHERE day=? AND purpose='research_candidate' ''', (portal_id, day)).fetchone()
                    if (total >= settings.get('research_candidate_daily_limit', 6)
                            or own >= settings.get('research_candidate_portal_daily_limit', 2)):
                        raise AdmissionError('research_candidate_budget_exhausted')
            else:
                if c.execute("SELECT 1 FROM portal_profiles WHERE access_reason='standby_host_live_access_disabled' LIMIT 1").fetchone():
                    raise AdmissionError('standby_host_live_access_disabled')
                # Runtime metadata can reconcile inherited inference flags. Explicit
                # provider/origin holds below still govern discovery admission.
                blocked = c.execute("SELECT access_state,adapter_json FROM portal_profiles WHERE provider=? AND (access_state IN ('quota_exhausted','uncertain') OR retry_after>?)", (p['provider'], now)).fetchall()
                for row in blocked:
                    if p.get('origin') == 'huggingface.co':
                        continue
                    origin = urlsplit(json.loads(row['adapter_json']).get('url', '')).hostname or ''
                    # A saved profile flag applies to that origin, including
                    # aliases. Shared provider holds and unresolved requests are
                    # independently enforced below; a historical profile flag
                    # must not become a blanket denial of unrelated schemas.
                    if p.get('origin') == origin:
                        raise AdmissionError(row['access_state'])
            origin = p.get('origin') or urlsplit(json.loads(p.get('adapter_json', '{}')).get('url', '')).hostname or ''
            for hold in c.execute('SELECT reason,retry_after FROM portal_access_holds WHERE scope IN (?,?,?)',
                                  ('provider:'+p['provider'], 'portal:'+portal_id, 'origin:'+origin)):
                if hold['retry_after'] == 0 or hold['retry_after'] > now:
                    raise AdmissionError(hold['reason'])
            active = c.execute("""SELECT request_id,provider,state,started,completed,result
                                FROM portal_requests WHERE state IN ('active','uncertain')""").fetchall()
            # A completed client attempt with a proven, held HF origin remains
            # unresolved at that origin. It is not an active request at unrelated
            # origins. Unscoped uncertainty still blocks the provider.
            active=[r for r in active if not (r['state']=='uncertain' and r['provider']=='huggingface'
                and c.execute("SELECT 1 FROM portal_audit a JOIN portal_access_holds h ON h.scope=json_extract(a.detail,'$.scope') WHERE a.kind='hf_origin_uncertainty' AND json_extract(a.detail,'$.request_id')=? AND h.reason='uncertain' AND h.retry_after=0",(r['request_id'],)).fetchone())]
            # A finished non-HF client attempt with an indefinite provider hold
            # stays blocked at that provider. It does not occupy another
            # provider's global execution capacity. Without a recorded finish
            # and a matching hold, an uncertain request still counts globally.
            quarantines = {r['scope'] for r in c.execute("""SELECT scope FROM portal_access_holds
                WHERE scope LIKE 'provider:%' AND reason='uncertain' AND retry_after=0""")}
            active_clients = sum(not (
                r['state']=='uncertain' and r['result']=='uncertain'
                and r['completed'] is not None and r['completed']>=r['started']
                and r['provider'] not in ('', 'huggingface')
                and 'provider:'+r['provider'] in quarantines
            ) for r in active)
            if active_clients >= 2 or any(r['provider'] == p['provider'] for r in active):
                raise AdmissionError('provider_busy')
            latest = c.execute('SELECT MAX(started) FROM portal_requests WHERE provider=?', (p['provider'],)).fetchone()[0]
            provider_cooldown = c.execute('SELECT MAX(cooldown) FROM portal_profiles WHERE provider=?', (p['provider'],)).fetchone()[0] or 10
            if latest is not None and now - latest < max(10, p['cooldown'], provider_cooldown):
                raise AdmissionError('provider_cooldown')
            from tools.portal_usage_rules import assess, public_evidence
            rules, rule_denial = assess(c, p, now) if purpose != 'discovery' else ([], '')
            if rule_denial:
                raise AdmissionError(rule_denial)
            if not rules and purpose in ('evaluation', 'repair', 'discovery', 'supervised_chat'):
                settings = {r[0]: json.loads(r[1]) for r in c.execute('SELECT * FROM portal_settings')}
                unlimited_chat = settings.get('chat_budget_mode') == 'unlimited'
                if purpose == 'supervised_chat' and unlimited_chat:
                    # Public-portal chat has no Sabrina volume ceiling. Provider
                    # rules, access holds, concurrency and cooldown still apply.
                    pass
                else:
                    purposes = ("'evaluation','repair','discovery'" if unlimited_chat
                                else "'evaluation','repair','discovery','supervised_chat'")
                    total, own = c.execute("SELECT COUNT(*),COALESCE(SUM(portal_id=?),0) FROM portal_requests WHERE day=? AND purpose IN (" + purposes + ")", (portal_id, day)).fetchone()
                    per_portal_chat = (purpose == 'supervised_chat'
                                       and settings.get('chat_budget_mode', 'shared') == 'per_portal')
                    if (not per_portal_chat and total >= settings['daily_limit']) or own >= settings['portal_daily_limit']:
                        raise AdmissionError('daily_budget_exhausted')
                    if purpose == 'supervised_chat':
                        chat_total, chat_own = c.execute("SELECT COUNT(*),COALESCE(SUM(portal_id=?),0) FROM portal_requests WHERE day=? AND purpose='supervised_chat'", (portal_id, day)).fetchone()
                        if ((not per_portal_chat and chat_total >= settings.get('chat_daily_limit', 50))
                                or chat_own >= settings.get('chat_portal_daily_limit', 5)):
                            raise AdmissionError('chat_budget_exhausted')
            if purpose in ('research','research_reserve'):
                c.execute('INSERT OR IGNORE INTO portal_route_counts(scope) VALUES (?)',(day,))
                counter=c.execute('SELECT * FROM portal_route_counts WHERE scope=?',(day,)).fetchone()
                reserve=purpose=='research_reserve'
                if reserve and (counter['reserves']+1)*20 > counter['total']+1:
                    raise AdmissionError('reserve_allowance_exhausted')
                c.execute('UPDATE portal_route_counts SET total=total+1,reserves=reserves+? WHERE scope=?',(int(reserve),day))
            rid = uuid.uuid4().hex
            c.execute('INSERT INTO portal_requests VALUES (?,?,?,?,?,?,?,NULL,NULL)', (rid, portal_id, p['provider'], purpose, day, now, 'active'))
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                      (now, 'usage_decision', json.dumps({'portal_id': portal_id, 'request_id': rid,
                       'purpose': purpose, 'decision': 'admitted',
                       'source': 'reviewed_inference_rules' if rules else 'local_safeguards_not_provider_entitlement',
                       'rules': public_evidence(rules), 'request_sent': False,
                       'note': 'Admission receipt; subsequent transport records establish delivery.'})))
        return rid

    def finish(self, request_id, response):
        state = response.get('failure', '')
        uncertain = state == 'uncertain'
        with self.db() as c:
            r = c.execute('SELECT portal_id,provider,purpose FROM portal_requests WHERE request_id=?', (request_id,)).fetchone()
            if not r:
                raise ValueError('Unknown request')
            c.execute('UPDATE portal_requests SET state=?,completed=?,result=? WHERE request_id=?',
                      ('uncertain' if uncertain else 'complete', self.clock(), state, request_id))
            blocked = state in ('auth_required','captcha','access_denied','quota_exhausted','uncertain')
            delay = float(response.get('retry_after_seconds') or (3600 if state == 'rate_limited' else 0))
            if (uncertain and r['provider']=='huggingface' and r['purpose']!='discovery'
                and response.get('uncertainty_scope')=='origin'
                and response.get('uncertainty_cause')=='client_transport_timeout'):
                profile=c.execute('SELECT adapter_json FROM portal_profiles WHERE portal_id=?',(r['portal_id'],)).fetchone()
                host=urlsplit(json.loads(profile[0])['url']).hostname if profile else None
                if host and host.endswith('.hf.space') and host==response.get('request_origin'):
                    c.execute('INSERT OR REPLACE INTO portal_access_holds VALUES (?,?,0)',('origin:'+host,'uncertain'))
                    for sibling in c.execute('SELECT portal_id,adapter_json FROM portal_profiles WHERE provider=?',(r['provider'],)).fetchall():
                        if urlsplit(json.loads(sibling['adapter_json'])['url']).hostname==host:
                            c.execute("UPDATE portal_profiles SET access_state='uncertain' WHERE portal_id=?",(sibling['portal_id'],))
                    c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                        (self.clock(),'hf_origin_uncertainty',json.dumps({'request_id':request_id,'scope':'origin:'+host,
                         'remote_outcome':'unresolved_do_not_replay','client_attempt_finished':True})))
                    return
            if (state=='quota_exhausted' and response.get('quota_scope')=='zerogpu'
                    and response.get('quota_upstream')=='huggingface_zerogpu' and r['provider']=='huggingface'):
                # A timed compute-pool hold does not poison unrelated Space profiles.
                c.execute('INSERT OR REPLACE INTO portal_access_holds VALUES (?,?,?)',
                    ('pool:huggingface:zerogpu',state,self.clock()+delay if delay else 0))
                return
            if blocked or delay:
                if r['purpose'] == 'discovery':
                    scope = ('portal:'+r['portal_id'] if state in ('auth_required','captcha','access_denied') else 'provider:'+r['provider'])
                    c.execute('INSERT OR REPLACE INTO portal_access_holds VALUES (?,?,?)',
                              (scope, state, self.clock()+delay if delay and not blocked else 0))
                scoped_quota = (state == 'quota_exhausted' and response.get('quota_scope') == 'origin'
                                and response.get('quota_upstream') == 'generativelanguage.googleapis.com'
                                and r['provider'] == 'huggingface')
                if state in ('auth_required','captcha','access_denied') or scoped_quota:
                    # A maintainer's expired key is not an outage of every HF Space.
                    profile=c.execute('SELECT adapter_json FROM portal_profiles WHERE portal_id=?',(r['portal_id'],)).fetchone()
                    if not profile:
                        return
                    origin=urlsplit(json.loads(profile[0])['url']).hostname
                    if scoped_quota:
                        if not (origin or '').endswith('.hf.space'):
                            raise ValueError('Scoped quota requires an HF Space origin')
                        # Persistent until evidence-based reconciliation; retry_after on the
                        # profile records the earliest provider-advertised review time.
                        c.execute('INSERT OR REPLACE INTO portal_access_holds VALUES (?,?,0)',
                                  ('origin:'+origin, 'quota_exhausted'))
                    siblings=c.execute('SELECT portal_id,adapter_json FROM portal_profiles WHERE provider=?',(r['provider'],)).fetchall()
                    for sibling in siblings:
                        if urlsplit(json.loads(sibling['adapter_json'])['url']).hostname==origin:
                            c.execute('UPDATE portal_profiles SET access_state=?,retry_after=? WHERE portal_id=?',(state,self.clock()+delay,sibling['portal_id']))
                else:
                    # Shared quotas/uncertainty suspend the provider, including aliases.
                    c.execute('INSERT OR REPLACE INTO portal_access_holds VALUES (?,?,?)',
                              ('provider:'+r['provider'], state, 0 if blocked else self.clock()+delay))
                    c.execute('UPDATE portal_profiles SET access_state=?,retry_after=? WHERE provider=?',
                              (state, self.clock() + delay, r['provider']))
            elif response.get('delivery') == 'observed':
                c.execute("UPDATE portal_profiles SET access_state='reachable' WHERE portal_id=?", (r['portal_id'],))

    def reconcile(self, provider, evidence):
        if not evidence.strip():
            raise ValueError('Evidence that previous work stopped/access was restored is required')
        with self.db() as c:
            c.execute("UPDATE portal_requests SET state='reconciled',completed=? WHERE provider=? AND state IN ('active','uncertain')", (self.clock(), provider))
            c.execute("UPDATE portal_profiles SET access_state='unknown',retry_after=0 WHERE provider=?", (provider,))
            c.execute('DELETE FROM portal_access_holds WHERE scope=?', ('provider:'+provider,))
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)', (self.clock(), 'operator_reconciliation', json.dumps({'provider': provider, 'evidence': evidence})))
