"""Neutral, single-attempt transports. No answer repair or access-barrier retries."""
from __future__ import annotations
import copy
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from tools.portal_policy import DEFAULT_DB, VERSION, PortalPolicy, AdmissionError, digest

MAX_BYTES = 2_000_000
NEUTRAL = 'Please answer within your supported capabilities. If this task is outside your scope, say so.\n'


def normalize(text):
    transformations = []
    answer = str(text).strip()
    if answer != text:
        transformations.append('trim_outer_whitespace')
    match = re.fullmatch(r'```(?:python|json|text)?\s*\n(.*?)\n```', answer, re.S | re.I)
    if match:
        answer = match.group(1)
        transformations.append('remove_single_outer_code_fence')
    return answer, transformations


def classify(text, status=None):
    low = text.lower()
    if 'verify you are human' in low or 'complete the captcha' in low or 'captcha required' in low:
        return 'captcha'
    if status == 401:
        return 'auth_required'
    if status == 403:
        return 'access_denied'
    # Gradio can complete successfully while its upstream model API rejects access.
    plain = re.sub(r'<[^>]+>', '', low).strip().lstrip('🔒❌⚠️ ').strip()
    plain = re.sub(r'^(?:\*{1,2}|_{1,2}|`{1,3})', '', plain).lstrip()
    service_error = bool(re.match(r'(?:error(?:\s+(?:generating response|during chat|parsing chat messages|fetching url))?\s*:|an error occurred while)', plain))
    if re.match(r'(?:please\s+)?(?:sign in|log in)\b', plain):
        return 'auth_required'
    if re.match(r"\{['\"]success['\"]:\s*false\b", plain) and 'api key is required' in plain:
        return 'auth_required'
    if service_error and ('401 unauthorized' in low or 'oauth token has expired' in low):
        return 'auth_required'
    if service_error and '403 forbidden' in low:
        return 'access_denied'
    if any(x in low for x in ('quota exceeded', 'quota exhausted', 'insufficient credits', 'zero gpu quota', 'zerogpu quota', 'exceeded your gpu quota')):
        return 'quota_exhausted'
    if status == 429:
        return 'rate_limited'
    if status == 404:
        return 'endpoint_missing'
    if status and status >= 400:
        return 'provider_error'
    if service_error or re.search(r'(?:^|\n)\s*❌?\s*the council failed to generate a response\.', low):
        return 'provider_error'
    # Only a short direct refusal is a transport-level scope result. Quoted
    # language, critiques and substantive continuations require task grading.
    direct_refusal = re.match(r"(?:this(?: task)? is outside my (?:scope|expertise)|i (?:cannot|can't) help with this|i cannot write code|i don't have information about that in the notes|i (?:can )?only answer questions about)\b", plain)
    if direct_refusal and len(plain) <= 300 and not re.search(r'\b(?:however|but|here is|here are|for example)\b', plain):
        return 'unsupported_task'
    return ''


def retry_seconds(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 3600


def quota_scope_evidence(adapter, response):
    """Scope explicit ZeroGPU service errors and hosted Gemini project quotas.

    Unknown errors retain shared-provider scope. Reported retry delays never
    authorize replay of a request with an uncertain delivery outcome.
    """
    host = urlsplit(adapter.get('url', '')).hostname or ''
    low = response.get('reply', '').lower().strip()
    if (response.get('failure') == 'quota_exhausted' and adapter.get('kind') == 'gradio'
            and host.endswith('.hf.space') and response.get('http_status') is None):
        for event in response.get('raw_response', []) if isinstance(response.get('raw_response'), list) else []:
            data = event.get('data', {}) if isinstance(event, dict) else {}
            error = data.get('error', '') if isinstance(data, dict) else ''
            match = re.match(r'You have exceeded your ZeroGPU quota .*?Try again in (\d+):(\d+):(\d+)', str(error), re.I)
            if match:
                hours, minutes, seconds = map(int, match.groups())
                return {'quota_scope':'zerogpu', 'quota_upstream':'huggingface_zerogpu',
                        'quota_scope_reason':'observed_zerogpu_service_error',
                        'retry_after_seconds':hours*3600+minutes*60+seconds}
    if (response.get('failure') != 'quota_exhausted' or adapter.get('kind') != 'gradio'
            or not host.endswith('.hf.space') or response.get('http_status') is not None
            or not re.match(r"(?:oops, something went wrong:\s*)?error calling model\b", low)
            or 'resource_exhausted' not in low
            or 'generativelanguage.googleapis.com/generate_content_free_tier_requests' not in low
            or 'generaterequestsperdayperprojectpermodel-freetier' not in low):
        return {}
    match = re.search(r'''["']retrydelay["']\s*:\s*["'](\d+(?:\.\d+)?)s["']''', low)
    return {'quota_scope': 'origin', 'quota_upstream': 'generativelanguage.googleapis.com',
            'quota_scope_reason': 'hosted_upstream_project_daily_quota',
            'retry_after_seconds': float(match.group(1)) if match else 0}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect_requires_review', headers, fp)


def fetch(url, payload=None, headers=None):
    if urlsplit(url).scheme != 'https':
        raise ValueError('HTTPS required')
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'SabrinaResearch/1.0', **(headers or {})})
    return urllib.request.build_opener(NoRedirect()).open(req, timeout=45)


def read_json(url, payload=None, headers=None):
    with fetch(url, payload, headers) as response:
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('response_too_large')
        return json.loads(raw)


def assistant_text(data):
    """Recognize explicit assistant messages, a text output, or Gradio chat pairs.

    Do not recursively select arbitrary strings (errors, labels, and prompt echoes).
    """
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        if data.get('role') == 'assistant':
            content = data.get('content', '')
            if isinstance(content, list):
                return '\n'.join(x['text'] for x in content if isinstance(x, dict)
                                 and x.get('type') == 'text' and isinstance(x.get('text'), str))
            return content if isinstance(content, str) else ''
        if data.get('role'):
            return ''
        for key in ('response', 'answer', 'generated_text', 'output', 'text', 'content'):
            if isinstance(data.get(key), str):
                return data[key]
        return ''
    if isinstance(data, list) and data:
        messages = [x for x in data if isinstance(x, dict) and x.get('role') == 'assistant']
        if messages:
            return assistant_text(messages[-1])
        if all(isinstance(x, list) and len(x) == 2 for x in data):
            return assistant_text(data[-1][1])
        if len(data) == 1:
            return assistant_text(data[0])
    return ''


def documented_gradio_adapter(url, info, bindings=None, output_label=None, prompt_parameter=None):
    """Use documented defaults; ambiguous or required non-prompt inputs stop repair."""
    base, endpoint = url.rsplit('/call', 1)
    spec = info.get('named_endpoints', {}).get(endpoint)
    if not spec:
        raise ValueError('documented_endpoint_missing')
    params = spec.get('parameters', [])
    bindings = bindings or {}
    named = [i for i, p in enumerate(params) if p.get('parameter_name', '').lower() in ('prompt', 'message', 'query', 'question', 'text', 'input', 'user_message', 'input_text', 'task_prompt', 'user_input')]
    if prompt_parameter is not None:
        named=[i for i,p in enumerate(params) if p.get('parameter_name')==prompt_parameter]
    if len(named) != 1:
        raise ValueError('ambiguous_prompt_parameter')
    index = named[0]
    template = []
    prompt_format='string'
    for i, p in enumerate(params):
        if i == index:
            ptype = p.get('type', {}).get('type')
            shape=p.get('type',{})
            multimodal_text=(ptype=='object' and p.get('component','').lower()=='multimodaltextbox'
                and shape.get('properties',{}).get('text',{}).get('type')=='string'
                and shape.get('properties',{}).get('files',{}).get('type')=='array'
                and set(shape.get('required',[]))<= {'text','files'})
            if multimodal_text:
                prompt_format='multimodal_text'
            elif ptype != 'string':
                raise ValueError('unsupported_prompt_schema')
            template.append(None)
        elif p.get('parameter_name') in bindings:
            value = bindings[p['parameter_name']]
            if p.get('type', {}).get('type') != 'string' or not isinstance(value,str):
                raise ValueError('binding_schema_mismatch')
            template.append(value)
        elif 'parameter_default' in p:
            template.append(p['parameter_default'])
        else:
            raise ValueError('required_parameter_needs_review')
    returns = spec.get('returns', [])
    eligible = [i for i, r in enumerate(returns) if r.get('component') in ('Chatbot', 'Textbox', 'Markdown') or r.get('type', {}).get('type') == 'string']
    if output_label:
        eligible = [i for i,r in enumerate(returns) if r.get('label')==output_label]
    elif len(returns)==1 and returns[0].get('component')=='Json':
        eligible = [0]
    elif len([r for r in returns if r.get('component')=='Chatbot'])==1:
        eligible = [i for i,r in enumerate(returns) if r.get('component')=='Chatbot']
    if len(eligible) != 1:
        raise ValueError('ambiguous_output_schema')
    token_indices = [i for i,p in enumerate(params) if p.get('parameter_name','').lower() in ('max_tokens','max_new_tokens','max_output_tokens')]
    return {'kind': 'gradio', 'url': url, 'parameters': template, 'prompt_index': index, 'output_index': eligible[0],
            'token_limit_index': token_indices[0] if len(token_indices)==1 else None, 'schema': spec,
            'bindings':bindings, 'output_label':output_label, 'prompt_parameter':prompt_parameter,
            'prompt_format':prompt_format, 'verified_protocol': True}


def refresh_protocol(policy, portal_id):
    p = policy.profile(portal_id)
    adapter = json.loads(p['adapter_json'])
    if adapter.get('kind') != 'gradio':
        return {'ok': False, 'failure': 'unsupported_protocol_repair'}
    try:
        rid = policy.admit(portal_id, 'repair')
    except AdmissionError as exc:
        return {'ok': False, 'failure': str(exc), 'deferred': True}
    try:
        info = read_json(adapter['url'].rsplit('/call', 1)[0] + '/info')
        with policy.db() as c:
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                      (policy.clock(),'protocol_fixture',json.dumps({'portal_id':portal_id,'info':info})))
        replacement = documented_gradio_adapter(adapter['url'], info, adapter.get('bindings'), adapter.get('output_label'), adapter.get('prompt_parameter'))
        policy.register(portal_id, replacement, p['model_claim'], access_allowed=bool(p['access_allowed']), access_reason=p['access_reason'], verified_model=p['verified_model'])
        result = {'ok': True, 'delivery': 'observed', 'adapter': replacement}
    except urllib.error.HTTPError as exc:
        result = {'ok': False, 'failure': classify('', exc.code), 'retry_after_seconds': retry_seconds(exc.headers.get('Retry-After')) if exc.code == 429 else 0}
    except Exception as exc:
        result = {'ok': False, 'failure': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
    policy.finish(rid, result)
    return result


def _exchange(adapter, prompt, model_claim, max_tokens=1024):
    kind = adapter.get('kind')
    if kind == 'gradio':
        if not adapter.get('verified_protocol'):
            return {'ok': False, 'failure': 'protocol_validation_required', 'delivery': 'missing'}
        params = copy.deepcopy(adapter['parameters'])
        params[adapter['prompt_index']] = ({'text':prompt,'files':[]}
            if adapter.get('prompt_format')=='multimodal_text' else prompt)
        limit_index = adapter.get('token_limit_index')
        if limit_index is not None:
            default = params[limit_index]
            if isinstance(default, (int,float)) and not isinstance(default,bool) and default > 0:
                params[limit_index] = min(int(default),max_tokens)
        initial = read_json(adapter['url'], {'data': params})
        event = initial.get('event_id')
        if not isinstance(event, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', event):
            return {'ok': False, 'failure': 'malformed_event_id', 'raw_response': initial, 'delivery': 'missing'}
        raw_events, data_lines, event_type, total = [], [], '', 0
        stream_started = time.monotonic()
        with fetch(adapter['url'] + '/' + event) as stream:
            for line_bytes in stream:
                if time.monotonic() - stream_started > 60:
                    raise TimeoutError('stream_deadline_exceeded')
                total += len(line_bytes)
                if total > MAX_BYTES:
                    raise ValueError('response_too_large')
                line = line_bytes.decode('utf-8').rstrip('\r\n')
                if line.startswith('event:'):
                    event_type = line[6:].strip()
                elif line.startswith('data:'):
                    data_lines.append(line[5:].strip())
                elif not line and data_lines:
                    data = json.loads('\n'.join(data_lines)); data_lines = []
                    raw_events.append({'event': event_type, 'data': data})
                    if event_type == 'error':
                        return {'ok': False, 'failure': classify(str(data)) or 'provider_error', 'raw_response': raw_events, 'delivery': 'missing'}
                    if event_type == 'complete':
                        idx = adapter['output_index']
                        item = data[idx] if isinstance(data,list) and idx < len(data) else None
                        text = assistant_text(item)
                        outputs = adapter.get('schema',{}).get('returns',[])
                        if not text and idx < len(outputs) and outputs[idx].get('component')=='Json' and isinstance(item,(dict,list)):
                            text = json.dumps(item,ensure_ascii=False)
                        return {'raw_response': raw_events, 'raw_answer': text, 'delivery': 'observed' if text else 'missing', 'failure': '' if text else 'unrecognized_response'}
        return {'ok': False, 'failure': 'incomplete_stream', 'raw_response': raw_events, 'delivery': 'missing'}
    if kind == 'google':
        from tools.secret_store import get_secret
        key = get_secret('GEMINI_API_KEY')
        if not key:
            return {'ok': False, 'failure': 'auth_required', 'delivery': 'missing'}
        data = read_json(adapter['url'], {'contents': [{'parts': [{'text': prompt}]}],
                                        'generationConfig': {'temperature': 0, 'maxOutputTokens': max_tokens}}, {'x-goog-api-key': key})
        candidates = data.get('candidates') or []
        parts = candidates[0].get('content', {}).get('parts', []) if candidates else []
        text = '\n'.join(p['text'] for p in parts if p.get('text') and not p.get('thought'))
        return {'raw_response': data, 'raw_answer': text, 'delivery': 'observed' if text else 'missing', 'observed_model': data.get('modelVersion')}
    return {'ok': False, 'failure': 'unsupported_adapter', 'delivery': 'missing'}


def request(policy, portal_id, prompt, *, purpose, max_tokens=1024, exchange=None):
    p = policy.profile(portal_id)
    if not p:
        return {'ok': False, 'failure': 'unknown_portal', 'deferred': True}
    adapter = json.loads(p['adapter_json'])
    if adapter.get('kind') == 'gradio' and not adapter.get('verified_protocol'):
        return {'ok': False, 'failure': 'protocol_validation_required', 'deferred': True}
    try:
        rid = policy.admit(portal_id, purpose)
    except AdmissionError as exc:
        return {'ok': False, 'failure': str(exc), 'deferred': True}
    started = time.monotonic()
    full_prompt = NEUTRAL + prompt
    try:
        if exchange:
            result=exchange(adapter, full_prompt, p['model_claim'], max_tokens)
        elif adapter.get('kind')=='audited_web_ui':
            from tools.guild_web_qualification import web_chat_exchange
            result=web_chat_exchange(policy,portal_id,rid,adapter,full_prompt)
        else:
            result=_exchange(adapter, full_prompt, p['model_claim'], max_tokens)
        raw = result.get('raw_answer', '')
        reply, transformations = normalize(raw)
        failure = result.get('failure') or classify(raw)
        if raw.strip() in (prompt.strip(), full_prompt.strip()) or (full_prompt and full_prompt in raw):
            failure = 'prompt_echo'
        result.update(reply=reply, transformations=transformations, failure=failure,
                      ok=bool(reply and not failure))
    except urllib.error.HTTPError as exc:
        body = exc.read(16000).decode('utf-8', 'replace')
        result = {'ok': False, 'failure': classify(body, exc.code), 'http_status': exc.code, 'raw_response': body,
                  'retry_after_seconds': retry_seconds(exc.headers.get('Retry-After')) if exc.code == 429 else 0, 'delivery': 'missing', 'reply': ''}
    except (TimeoutError, urllib.error.URLError):
        result = {'ok': False, 'failure': 'uncertain', 'delivery': 'uncertain', 'reply': ''}
        host=urlsplit(adapter.get('url','')).hostname or ''
        if adapter.get('kind')=='gradio' and host.endswith('.hf.space'):
            result.update(uncertainty_scope='origin',uncertainty_cause='client_transport_timeout',request_origin=host)
    except Exception as exc:
        result = {'ok': False, 'failure': 'adapter_error:' + type(exc).__name__, 'delivery': 'missing', 'reply': ''}
    result.update(portal_id=portal_id, request_id=rid, adapter_hash=p['adapter_hash'],
                  claimed_model=p['model_claim'], actual_model=p['verified_model'] or 'unknown',
                  execution_mode='live', is_simulated=False, cache_hit=False,
                  provenance_ref='portal_request:' + rid, latency_ms=round((time.monotonic() - started) * 1000, 2))
    result['output_limit_requested'] = max_tokens
    result['output_limit_enforceable'] = adapter.get('kind')=='google' or adapter.get('token_limit_index') is not None
    result.update(quota_scope_evidence(adapter, result))
    if adapter.get('kind')=='google' and result.get('observed_model'):
        with policy.db() as c:
            c.execute('UPDATE portal_profiles SET verified_model=? WHERE portal_id=?', (result['observed_model'],portal_id))
        result['actual_model'] = result['observed_model']
    policy.finish(rid, result)
    return result


def invoke_portal(portal_id, prompt, task_id='', *, db_path=DEFAULT_DB,
                  required_capability='grounded_summarization', tool_allowlist=(), supervised=False, use_cache=False,
                  conversation=False):
    policy = PortalPolicy(db_path)
    if conversation:
        if not supervised or tool_allowlist or required_capability == 'tool_use' or use_cache:
            return {'ok': False, 'failure': 'chat_has_no_tool_or_cache_permissions', 'reply': ''}
        evidence = policy.chat_eligibility(portal_id)
        if not evidence['eligible']:
            return {'ok': False, 'failure': evidence['reason'], 'reply': '', 'qualification': evidence}
        result = request(policy, portal_id, prompt, purpose='supervised_chat', max_tokens=512)
        result.update(qualification=evidence, responsibility='supervised_chat',
                      requires_controller_review=True, tool_allowlist=[], fallback_reason=evidence['reason'])
        # Preserve raw responses and the exact currently executing adapter fingerprint.
        from tools.portal_policy import adapter_fingerprint
        result['adapter_hash'] = adapter_fingerprint(json.loads(policy.profile(portal_id)['adapter_json']))
        with policy.db() as c:
            c.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                      (policy.clock(), 'supervised_chat_response', json.dumps(result, ensure_ascii=False)))
        return result
    evidence = policy.eligibility(portal_id, required_capability, tool_allowlist, allow_reserve=supervised)
    if not evidence['eligible']:
        return {'ok': False, 'failure': evidence['reason'], 'reply': '', 'qualification': evidence}
    reserve = evidence['responsibility'] == 'supervised_reserve'
    candidate = evidence['responsibility'] == 'experimental_research'
    profile = policy.profile(portal_id)
    cache_key = digest([portal_id, prompt, task_id, required_capability, profile['adapter_hash'],
                        profile['model_claim'], evidence.get('run_id')])
    cacheable = use_cache and not reserve and not candidate and not tool_allowlist and required_capability != 'tool_use'
    if cacheable:
        with policy.db() as c:
            cached = c.execute('SELECT response_json FROM portal_response_cache WHERE cache_key=? AND expires>?',
                               (cache_key,policy.clock())).fetchone()
        if cached:
            result = json.loads(cached[0])
            return {**result, 'cache_hit': True, 'delivery': 'cached', 'qualification': evidence}
    purpose = ('tool' if required_capability == 'tool_use' else 'research_reserve' if reserve
               else 'research_candidate' if candidate else 'research')
    result = request(policy, portal_id, prompt, purpose=purpose)
    result.update(qualification=evidence, responsibility=evidence['responsibility'],
                  requires_controller_review=reserve, tool_allowlist=list(tool_allowlist),
                  fallback_reason=evidence['reason'])
    if cacheable and result.get('ok'):
        with policy.db() as c:
            c.execute('INSERT OR REPLACE INTO portal_response_cache VALUES (?,?,?)',
                      (cache_key,json.dumps(result),min(policy.clock()+600,evidence['expires'])))
    return result
