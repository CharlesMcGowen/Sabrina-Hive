"""Run-owned web tools; account access requires an explicit scoped grant.

Uses the existing browser manager, a separate persistent research profile and a
coordinator lease. A model proposes actions; this broker validates every action.
"""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus, urlsplit


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def ensure_research_browser():
    """Reuse the installed Chrome and research profile without changing PS policy."""
    if os.name != 'nt':
        raise RuntimeError('Research browser belongs on Windows; no lab GUI fallback')
    import psutil
    profile = Path(os.environ['LOCALAPPDATA']) / 'SabrinaResearchProfile'
    def owner():
        listeners=[c for c in psutil.net_connections(kind='tcp') if c.status=='LISTEN' and c.laddr.port==9224]
        if not listeners:
            return None
        process=psutil.Process(listeners[0].pid)
        args=process.cmdline()
        if process.name().lower()!='chrome.exe' or not any(str(profile).lower() in a.lower() and a.startswith('--user-data-dir=') for a in args):
            raise RuntimeError('Port 9224 is not the dedicated Chrome research profile')
        return process.pid
    found=owner()
    if found:return found
    choices=[Path(os.environ.get(key,''))/'Google/Chrome/Application/chrome.exe'
             for key in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA')]
    executable=next((p for p in choices if p.is_file()),None)
    if not executable:raise RuntimeError('Installed Google Chrome unavailable')
    profile.mkdir(parents=True,exist_ok=True)
    startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=subprocess.SW_HIDE
    subprocess.Popen([str(executable),'--remote-debugging-address=127.0.0.1','--remote-debugging-port=9224',
        '--user-data-dir='+str(profile),'--no-first-run','--no-default-browser-check','--disable-extensions','about:blank'],
        startupinfo=startup,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        found=owner()
        if found:return found
        time.sleep(.5)
    raise RuntimeError('Research browser startup timeout; inspect before retrying')


def public_url(url, resolver=socket.getaddrinfo):
    p = urlsplit(url)
    if (p.scheme != 'https' or not p.hostname or p.username or p.password
            or p.port not in (None, 443) or len(url) > 2400):
        raise ValueError('Public HTTPS URL without credentials required')
    host = p.hostname.rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.local', '.localhost', '.internal', '.lan')):
        raise ValueError('Private host prohibited')
    addresses = {x[4][0] for x in resolver(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError('Non-public address prohibited')
    return p.scheme + '://' + p.netloc.lower()


def parse_action(text):
    """Allow one JSON object with an optional prose/fence wrapper, never code."""
    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    candidates = []
    for match in re.finditer(r'\{', text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and 'action' in value:
            candidates.append(value)
    if len(candidates) != 1:
        raise ValueError('Exactly one action object required')
    action = candidates[0]
    schemas = {'search': {'query', 'engine'}, 'open': {'url'}, 'observe': set(), 'consult': {'question'},
               'click': {'ref', 'revision'}, 'submit_test': {'composer', 'send', 'revision'},
               'finish': set()}
    from tools.guild_web_auth import TOOLS as AUTH_TOOLS
    schemas.update({k: set(v) for k, v in AUTH_TOOLS.items()})
    kind = action.get('action')
    if not isinstance(kind, str) or kind not in schemas or set(action) - schemas[kind] - {'action', 'reason'}:
        raise ValueError('Unsupported action or fields')
    required = schemas[kind] - ({'engine'} if kind == 'search' else set())
    if not required <= set(action) or any(not isinstance(v, str) for v in action.values()):
        raise ValueError('Missing or non-string action fields')
    if len(action.get('reason', '')) > 1000:
        raise ValueError('Public rationale too long')
    return action


class WebJournal:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.conn = sqlite3.connect(str(path), timeout=20)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
CREATE TABLE IF NOT EXISTS guild_web_campaigns (
 id TEXT PRIMARY KEY, created REAL, expires REAL, deadline REAL, state TEXT, spec_hash TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS guild_web_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, created REAL, kind TEXT, metadata TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS guild_web_submissions (
 run_id TEXT, origin TEXT, operation_id TEXT UNIQUE, state TEXT, marker TEXT, expected INTEGER,
 PRIMARY KEY(run_id, origin));
''')
        self.conn.commit()
        self.expire()

    def expire(self):
        with self.conn:
            expired=[r[0] for r in self.conn.execute('SELECT id FROM guild_web_campaigns WHERE expires<=? AND state!=?', (self.clock(),'expired'))]
            tables={r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for ident in expired:
                # Erase copies written by the existing transport too, scoped only
                # to this campaign's task IDs/request receipts.
                ids={r[0] for r in self.conn.execute("SELECT json_extract(metadata,'$.request_id') FROM guild_web_events WHERE run_id=? AND kind IN ('model_output','external_request')",(ident,)) if r[0]}
                if 'portal_audit' in tables:
                    rows=self.conn.execute("SELECT id,detail FROM portal_audit WHERE json_valid(detail) AND substr(json_extract(detail,'$.task_id'),1,?)=?",(len(ident)+1,ident+':')).fetchall()
                    for row in rows:
                        value=json.loads(row['detail'])
                        if value.get('request_id'):ids.add(value['request_id'])
                        clean={k:value[k] for k in ('task_id','portal_id','request_id','engine') if k in value}
                        clean['retention_expired']=True
                        self.conn.execute('UPDATE portal_audit SET detail=? WHERE id=?',(json.dumps(clean),row['id']))
                    for rid in ids:
                        rows=self.conn.execute("SELECT id,detail FROM portal_audit WHERE json_valid(detail) AND json_extract(detail,'$.request_id')=?",(rid,)).fetchall()
                        for row in rows:
                            value=json.loads(row['detail'])
                            clean={k:value[k] for k in ('task_id','portal_id','request_id','engine') if k in value}
                            clean['retention_expired']=True
                            self.conn.execute('UPDATE portal_audit SET detail=? WHERE id=?',(json.dumps(clean),row['id']))
                if 'portal_requests' in tables:
                    for rid in ids:self.conn.execute('UPDATE portal_requests SET result=NULL WHERE request_id=?',(rid,))
                if 'portal_eval_cases' in tables:
                    for rid in ids:
                        rows=self.conn.execute("SELECT run_id,case_id,attempt,response_json FROM portal_eval_cases WHERE json_extract(response_json,'$.request_id')=?",(rid,)).fetchall()
                        for row in rows:
                            value=json.loads(row['response_json'])
                            for key in ('reply','raw_answer','raw_response'):value.pop(key,None)
                            value['retention_expired']=True
                            self.conn.execute('UPDATE portal_eval_cases SET response_json=? WHERE run_id=? AND case_id=? AND attempt=?',
                                (json.dumps(value),row['run_id'],row['case_id'],row['attempt']))
            self.conn.execute("UPDATE guild_web_events SET raw=NULL WHERE run_id IN (SELECT id FROM guild_web_campaigns WHERE expires<=?)", (self.clock(),))
            self.conn.execute("UPDATE guild_web_campaigns SET state='expired' WHERE expires<=?", (self.clock(),))
            # Retain operation/uncertainty metadata; remove raw nonce material.
            self.conn.execute("UPDATE guild_web_submissions SET marker=NULL,expected=NULL WHERE run_id IN (SELECT id FROM guild_web_campaigns WHERE expires<=?)", (self.clock(),))

    def start(self, ident, spec, seconds=900, retention_days=7):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident):
            raise ValueError('Invalid run identifier')
        if not 1 <= retention_days <= 365 or not 0 < seconds <= 3600:
            raise ValueError('Invalid retention or runtime budget')
        self.expire()
        with self.conn:
            self.conn.execute('INSERT INTO guild_web_campaigns VALUES (?,?,?,?,?,?,?)',
                              (ident, self.clock(), self.clock()+86400*retention_days,
                               self.clock()+seconds, 'running', digest(spec), '{}'))
        self.event(ident, 'contract', {'spec_hash': digest(spec)}, spec)

    def active(self, ident):
        self.expire()
        row = self.conn.execute('SELECT * FROM guild_web_campaigns WHERE id=?', (ident,)).fetchone()
        if not row or row['state'] != 'running' or self.clock() >= row['deadline']:
            raise ValueError('Run expired, stopped, unknown, or out of time; no replay')
        return row

    def event(self, ident, kind, metadata=None, raw=None):
        self.active(ident)
        with self.conn:
            cur = self.conn.execute('INSERT INTO guild_web_events(run_id,created,kind,metadata,raw) VALUES (?,?,?,?,?)',
                (ident, self.clock(), kind, json.dumps(metadata or {}), None if raw is None else json.dumps(raw, ensure_ascii=False)))
            return cur.lastrowid

    def finish(self, ident, state, summary):
        self.expire()
        with self.conn:
            self.conn.execute("UPDATE guild_web_campaigns SET state=?,summary=? WHERE id=? AND state='running'",
                              (state, json.dumps(summary), ident))

    def report(self, ident):
        self.expire()
        run = self.conn.execute('SELECT * FROM guild_web_campaigns WHERE id=?', (ident,)).fetchone()
        if not run:
            raise ValueError('Unknown run')
        return {'run': dict(run), 'events': [dict(r) for r in self.conn.execute(
            'SELECT * FROM guild_web_events WHERE run_id=? ORDER BY id', (ident,))]}


DOM = r'''() => {
 const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
 const controls = [...document.querySelectorAll('a[href],button,input,textarea,[contenteditable="true"],[role="button"]')].map((e,i)=>({
   index:i, tag:e.tagName.toLowerCase(), type:e.getAttribute('type')||'', role:e.getAttribute('role')||'',
   name:e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.title||e.innerText||'',
   href:e.tagName==='A'?e.href:'', editable:e.isContentEditable,
   disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true', visible:visible(e),
   value:('value' in e && !['password','email','tel'].includes(e.type))?e.value:'',
   in_dialog:!!e.closest('[role="dialog"],dialog')
 })).filter(e=>e.visible);
 const challenge=[...document.querySelectorAll('iframe')].filter(visible).some(e=>/challenge|captcha/i.test(e.title||'') && e.getBoundingClientRect().height>60);
 const search_results=[...document.querySelectorAll('li.b_algo h2 a,a:has(h3),a.result__a')].filter(visible).map(e=>({title:e.innerText,url:e.href}));
 const code_blocks=[...document.querySelectorAll('pre')].filter(visible).map(e=>e.innerText);
 return {url:location.href,title:document.title,text:document.body?document.body.innerText:'',controls,challenge,search_results,code_blocks};
}'''
CONTROL_SELECTOR = 'a[href],button,input,textarea,[contenteditable="true"],[role="button"]'


def gates(snapshot):
    text = snapshot['text']
    controls = snapshot['controls']
    result = []
    if snapshot.get('challenge') or re.search(r"(?:^|\n)\s*(?:verify (?:that )?you(?:'|’)?re human|verify you are human|unusual traffic|complete the captcha)\b", text, re.I):
        result.append('human_verification_required')
    if any(re.fullmatch(r'\s*(?:I agree(?:,? continue)?|Accept (?:the )?terms(?: &.*)?|Agree and continue)\s*', c['name'], re.I) for c in controls):
        result.append('terms_approval_required')
    composers = [c for c in controls if is_composer(c)]
    if any(c['type'] in ('password','email','tel') and (c['in_dialog'] or not composers) for c in controls):
        result.append('authentication_or_personal_data_required')
    elif (not composers and re.search(r'/(?:auth|login|signin)(?:/|$)', urlsplit(snapshot.get('url','')).path, re.I)
          and any(re.match(r'(?:Continue|Sign in|Log in)(?: with|$)', c['name'], re.I) for c in controls)):
        result.append('authentication_required')
    if re.search(r'(?:daily (?:message |prompt )?limit reached|quota exceeded|too many requests|rate limit exceeded)', text, re.I):
        result.append('usage_limit_observed')
    return result


def is_composer(control):
    return (control['tag'] == 'textarea' or control.get('editable') or
            (control['tag'] == 'input' and control['type'] in ('', 'text'))) and not re.search(
                r'search|email|phone|password|name|address|token|api.?key', control['name'], re.I)


def search_view(raw):
    """Return actual result links, not search-engine navigation or AI summaries."""
    parsed=urlsplit(raw['url'])
    if parsed.hostname not in ('www.bing.com','www.google.com','duckduckgo.com','html.duckduckgo.com'):
        return None
    from ai_systems.web_research import _unwrap_bing_redirect, _unwrap_ddg_redirect
    results=[]
    for row in raw.get('search_results',[]):
        url=_unwrap_ddg_redirect(_unwrap_bing_redirect(row['url']))
        if url.startswith('https://') and not any(r['url']==url for r in results):
            results.append({'title':row['title'][:250],'url':url})
    return results[:12]


def test_reply(text, marker, expected):
    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    found = []
    for match in re.finditer(r'\{', text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get('marker') == marker:
            # A toast/title can mirror the same answer shown in the transcript.
            # Conflicting values remain ambiguous; identical UI copies aren't
            # additional submissions (those have separate durable operation IDs).
            if not any(json.dumps(value,sort_keys=True)==json.dumps(v,sort_keys=True) for v in found):
                found.append(value)
    return (len(found) == 1 and set(found[0]) == {'marker', 'sum'} and
            type(found[0]['sum']) is int and found[0]['sum'] == expected)


def _rendered_json_values(text):
    decoder=json.JSONDecoder(object_pairs_hook=unique_object)
    offset=0
    while True:
        match=re.search(r'[\{\[]',text[offset:])
        if not match:return
        start=offset+match.start()
        try:value,end=decoder.raw_decode(text[start:])
        except ValueError:
            # Do not salvage a nested object from a malformed outer response.
            raise
        yield text[start:start+end],value
        offset=start+end


def visible_structured_reply(raw, prompt, baseline_text=None):
    """Extract one rendered answer block after the exact submitted user prompt.

    No expected answer, synthesis, JSON repair, or selection among alternatives.
    Ambiguous/missing attribution remains uncollected.
    """
    if not prompt or raw['text'].count(prompt)!=1:
        return None
    tail=raw['text'].split(prompt,1)[1]
    if any(re.fullmatch(r'\s*(?:Stop|Stop generating|Stop response)\s*',c['name'],re.I) for c in raw['controls']):
        return None
    blocks=[block for block in raw.get('code_blocks',[]) if block.strip() and block in tail and block not in prompt
            and (baseline_text is None or block not in baseline_text)]
    if blocks:
        if len(blocks)!=1:return None
        try:json.loads(blocks[0],object_pairs_hook=unique_object)
        except (ValueError,TypeError):return None
        return blocks[0]
    # Some chats render JSON as plain text. Attribute only a new complete value
    # after the exact prompt, excluding values present before submission (e.g.
    # API examples in the footer). Never select using the expected answer.
    if baseline_text is None:return None
    canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'))
    new={}
    try:
        old={canonical(value) for _,value in _rendered_json_values(baseline_text)}
        for text,value in _rendered_json_values(tail):
            key=canonical(value)
            if key not in old:new.setdefault(key,text)
    except ValueError:return None
    return next(iter(new.values())) if len(new)==1 else None


class GuildWebBroker:
    def __init__(self, run_id, journal, cdp_url='http://127.0.0.1:9224', start_browser=False, task_prompt=None, task_url=None, account_policy=None):
        if cdp_url != 'http://127.0.0.1:9224':
            raise ValueError('Only the separate public research profile is permitted')
        self.run_id, self.journal, self.cdp_url = run_id, journal, cdp_url
        self.start_browser = start_browser
        self.manager = self.page = self.lease = None
        self.allowed_urls, self.refs, self.blocked_origins = set(), {}, {}
        self.snapshot = None
        self.revision, self.observed_at, self.epoch = '', 0, 0
        self.verifications, self.searches = [], 0
        self.new_chats = set()
        self.network_denials, self.host_cache = [], {}
        self.task_prompt=task_prompt
        self.task_origin=public_url(task_url) if task_prompt and task_url else None
        if task_prompt and not self.task_origin:raise ValueError('Task URL required for frozen trials')
        self.submitted_prompt=None
        self.observed_reply=None
        self.submission_baseline=None
        self.response_event_id=None
        self._reply_sample=None
        self._action_handles = []
        self.auth = None
        if account_policy is not None:
            from tools.guild_web_auth import AuthSession
            self.auth = AuthSession(self, account_policy)

    def __enter__(self):
        from tools.control_client import configured
        from ai_systems.browser_utils import PlaywrightBrowserManager
        client = configured()
        if client is None:
            raise RuntimeError('Coordinator required')
        self.client = client
        self.lease = client.lease('browser:research-public', wait_seconds=10)
        self.identity = self.lease.__enter__()
        try:
            if self.start_browser:
                pid=ensure_research_browser()
                self.journal.event(self.run_id,'browser_owner',{'pid':pid,'port':9224,'profile':'SabrinaResearchProfile'})
            self.manager = PlaywrightBrowserManager()
            self.page = self.manager.attach_isolated_cdp(self.cdp_url)
            self.page.route('**/*', self._route)
            self.page.on('download', lambda download: download.cancel())
            self.page.on('popup', lambda popup: popup.close())
            self.page.on('dialog', lambda dialog: dialog.dismiss())
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        # Close only the task-created page; never the profile or user's tabs.
        try:
            if self.page and not self.page.is_closed():
                self.page.close()
        finally:
            if self.manager:
                self.manager.disconnect_cdp()
            if self.lease:
                self.lease.__exit__(None, None, None)

    def _route(self, route):
        try:
            url = route.request.url
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443):
                raise ValueError('Public HTTPS required')
            if self.auth and self.auth.current:
                payload = url + '\n' + (route.request.post_data or '')
                sensitive = any(s and (s in payload or quote_plus(s) in payload) for s in self.auth.sensitive)
                if sensitive and parsed.scheme+'://'+parsed.netloc.lower() != self.auth.current['origin']:
                    raise ValueError('Credential-bearing request outside authorized account origin')
            host = urlsplit(url).hostname
            if self.host_cache.get(host, 0) < time.monotonic():
                public_url(url)
                self.host_cache[host] = time.monotonic() + 30
            elif urlsplit(url).scheme != 'https' or urlsplit(url).port not in (None,443):
                raise ValueError('HTTPS required')
            route.continue_()
        except Exception:
            self.network_denials.append(urlsplit(route.request.url).hostname)
            route.abort()

    def _alive(self):
        self.journal.active(self.run_id)
        self.client.request('/v1/leases/renew', self.identity)

    def observe(self, wait=False):
        self._alive()
        if wait:
            self.page.wait_for_timeout(2500)
        raw = self.page.evaluate(DOM)
        if self.auth:
            raw = self.auth.redacted(raw)
            raw['account_policy'] = self.auth.policy.grant()
        self.refs = {str(c['index']): c for c in raw['controls']}
        results=search_view(raw)
        if results is not None:
            self.allowed_urls.update(r['url'] for r in results)
        else:
            for c in raw['controls']:
                if c['href'].startswith('https://'):
                    self.allowed_urls.add(c['href'])
        raw['gates'] = gates(raw)
        self.revision = digest([self.run_id, self.epoch, raw])
        self.snapshot, self.observed_at = raw, time.monotonic()
        origin = 'https://' + (urlsplit(raw['url']).netloc or '')
        hard_gates = self._hard_gates(raw['gates'])
        if hard_gates:
            self.blocked_origins[origin] = hard_gates
        pending = self.journal.conn.execute('SELECT * FROM guild_web_submissions WHERE run_id=? AND origin=?',
                                          (self.run_id, origin)).fetchone()
        if pending and pending['marker'] and pending['expected'] is not None and test_reply(raw['text'], pending['marker'], pending['expected']):
            if not any(v['origin'] == origin for v in self.verifications):
                verdict = {'origin': origin, 'operation_id': pending['operation_id'],
                           'criterion': 'unique_marker_exact_integer_sum', 'passed': True,
                           'qualification': False, 'actual_model': 'unverified'}
                self.verifications.append(verdict)
                with self.journal.conn:
                    self.journal.conn.execute("UPDATE guild_web_submissions SET state='observed_pass' WHERE operation_id=?", (pending['operation_id'],))
                self.journal.event(self.run_id, 'verification', verdict)
        event = self.journal.event(self.run_id, 'observation', {'url':raw['url'], 'revision':self.revision,
            'gates': raw['gates'], 'characters':len(raw['text'])}, raw)
        if self.auth and any(v['origin'] == origin for v in self.verifications):
            self.auth.access_observed(origin, event)
        if self.task_prompt and self.submitted_prompt and pending and not raw['gates']:
            reply=visible_structured_reply(raw,self.submitted_prompt,self.submission_baseline)
            if reply and self._reply_sample and self._reply_sample[0]==reply and time.monotonic()-self._reply_sample[1]>=2:
                self.observed_reply=reply;self.response_event_id=event
                with self.journal.conn:
                    self.journal.conn.execute("UPDATE guild_web_submissions SET state='reply_collected' WHERE operation_id=?",(pending['operation_id'],))
            elif reply:self._reply_sample=(reply,time.monotonic())
            else:self._reply_sample=None
        # Full copy stays in the retention-controlled audit; model gets bounded UI.
        response_excerpt = ''
        if self.submitted_prompt and raw['text'].count(self.submitted_prompt)==1:
            response_excerpt = raw['text'].split(self.submitted_prompt,1)[1][:3000]
        return {'event_id':event, 'url':raw['url'], 'title':raw['title'], 'revision':self.revision,
                'text':('Search returned '+str(len(results))+' result links. Choose open with a result URL; no portal has been tested.' if results is not None else raw['text'][:10000]),
                'text_truncated':results is not None or len(raw['text'])>10000,
                'search_results':results or [],
                'controls':([] if results is not None else [{k:c[k] for k in ('index','tag','type','name','href','disabled','editable')} for c in raw['controls'][:100]]),
                'gates':raw['gates'], 'verified_tests':self.verifications,
                'response_collected':self.observed_reply is not None,
                'task_response_excerpt':response_excerpt,
                'blocked_origins':self.blocked_origins}

    def _hard_gates(self, found):
        from tools.guild_web_auth import AUTH_GATES
        return [g for g in found if not self.auth or g not in AUTH_GATES]

    def _fresh(self, revision):
        if revision != self.revision or time.monotonic()-self.observed_at > 180:
            raise ValueError('Stale reference: observe again')
        now = self.page.evaluate(DOM)
        if self.auth:
            now = self.auth.redacted(now)
            now['account_policy'] = self.auth.policy.grant()
        now['gates'] = gates(now)
        if digest([self.run_id,self.epoch,now]) != revision:
            raise ValueError('Page changed: observe again')
        if self._hard_gates(now['gates']):
            raise ValueError('Page gate: '+','.join(self._hard_gates(now['gates'])))
        return now

    def control_element(self, control):
        """Resolve the exact DOM-query ref once; never re-resolve an ordinal in a different tree."""
        try:
            handle = self.page.evaluate_handle('''spec => {
              const e=document.querySelectorAll(spec.selector)[spec.expected.index];
              if(!e || !e.getClientRects().length) throw Error('changed');
              const actual={tag:e.tagName.toLowerCase(),type:e.getAttribute('type')||'',
                role:e.getAttribute('role')||'',
                name:e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.title||e.innerText||'',
                href:e.tagName==='A'?e.href:'',editable:e.isContentEditable,
                disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true'};
              for(const [key,value] of Object.entries(spec.expected)) {
                if(key!=='index' && actual[key]!==value) throw Error('changed');
              }
              return e;
            }''', {'selector': CONTROL_SELECTOR, 'expected': {k:control[k] for k in
                    ('index','tag','type','role','name','href','editable','disabled') if k in control}})
            element = handle.as_element()
            if element is None:
                handle.dispose();raise ValueError('Control is no longer an element')
            self._action_handles.append(handle)
            return element
        except Exception:
            raise ValueError('Page changed: observed control identity no longer matches; observe again') from None

    def execute(self, action):
        try:return self._execute(action)
        finally:
            for handle in self._action_handles:
                try:handle.dispose()
                except Exception:pass
            self._action_handles.clear()

    def _execute(self, action):
        self._alive()
        action = parse_action(json.dumps(action))
        kind = action['action']
        if kind == 'observe':
            return self.observe(wait=True)
        if kind.startswith('auth_'):
            if not self.auth:raise ValueError('No run-scoped account authorization')
            now = self._fresh(action['revision']) if 'revision' in action else self.page.evaluate(DOM)
            if self._hard_gates(gates(now)):raise ValueError('Human verification, consent or usage gate remains blocked')
            try:self.auth.execute(action, now)
            except ValueError:raise
            except Exception as exc:
                # Credential entry diagnostics may contain field values.
                detail = ': ' + self.auth.redacted(str(exc)) if isinstance(exc, RuntimeError) else ''
                raise RuntimeError('Account operation blocked: ' + type(exc).__name__ + detail) from None
            self.epoch += 1
            return self.observe(wait=True)
        if kind == 'search':
            if self.task_prompt:raise ValueError('Frozen trial cannot search or change portals')
            if self.searches >= 4 or not 3 <= len(action['query']) <= 180:
                raise ValueError('Search budget or query length exceeded')
            engine = action.get('engine','bing')
            bases = {'bing':'https://www.bing.com/search?q=', 'google':'https://www.google.com/search?q=',
                     'duckduckgo':'https://duckduckgo.com/?q='}
            if engine not in bases:
                raise ValueError('Unsupported search provider')
            url = bases[engine] + quote_plus(action['query'])
            self.searches += 1
        elif kind == 'open':
            url = action['url']
            if url not in self.allowed_urls:
                raise ValueError('URL must come from observed search results or page links')
        else:
            now = self._fresh(action.get('revision',''))
            if kind == 'click':
                control = self.refs.get(action['ref'])
                if not control:
                    raise ValueError('Unknown run-owned reference')
                if control['href']:
                    if control['href'] not in self.allowed_urls:raise ValueError('Observed link required')
                    target = public_url(control['href'])
                    if self.task_origin and target!=self.task_origin:raise ValueError('Frozen trial portal mismatch')
                    if target in self.blocked_origins:raise ValueError('Previously gated origin')
                    # Native click preserves JS handlers for sign-in dialogs and client routing.
                    self.control_element(control).click(timeout=5000)
                    public_url(self.page.url)
                    self.epoch += 1
                    return self.observe(wait=True)
                permitted = r'\s*(?:Maybe later|Not now|Close|Dismiss|New chat|Start chat(?:ting)?|Chat now|Try now)\s*'
                if self.auth:
                    permitted = r'\s*(?:Maybe later|Not now|Close|Dismiss|Continue as guest|Skip|New chat|Start chat(?:ting)?|Chat now|Try now|Sign in|Log in|Sign up|Register|Create (?:an )?account)\s*'
                    if re.search(r'Sign|Log in|Register|Create.*account', control['name'], re.I) and any(c.get('value') or c.get('type') in ('email','password') for c in now['controls'] if c['tag']=='input'):
                        raise ValueError('Use the scoped account tools for authentication forms')
                if not re.fullmatch(permitted,control['name'],re.I):
                    raise ValueError('Button unsupported; no auth, terms, download, or purchase actions')
                self.control_element(control).click(timeout=5000)
                if re.fullmatch(r'\s*New chat\s*',control['name'],re.I):
                    self.new_chats.add(public_url(now['url']))
                self.epoch += 1
                return self.observe()
            if kind != 'submit_test':
                raise ValueError('Unsupported browser action')
            if now.get('gates', gates(now)):raise ValueError('Resolve authentication before submitting a model test')
            composer, send = self.refs.get(action['composer']), self.refs.get(action['send'])
            if not composer or not send or not is_composer(composer) or composer['disabled'] or composer['value']:
                raise ValueError('Empty observed composer required')
            if send['tag'] not in ('button','input') and send['role'] != 'button':
                raise ValueError('Observed send button required')
            if not re.fullmatch(r'\s*(?:Send(?: message| prompt)?|Submit(?: prompt| message)?|Ask|Run)\s*',send['name'],re.I):
                raise ValueError('Unambiguous send control required')
            origin = public_url(now['url'])
            if self.task_origin and origin!=self.task_origin:raise ValueError('Frozen trial portal mismatch')
            if any(re.fullmatch(r'\s*(?:Stop|Stop generating|Stop response)\s*',c['name'],re.I) for c in now['controls']):
                raise ValueError('Conversation is busy; do not submit')
            if any(re.fullmatch(r'\s*New chat\s*',c['name'],re.I) for c in now['controls']) and origin not in self.new_chats:
                raise ValueError('Use the observed New chat control before submitting')
            if re.search(r'growth-[0-9a-f]{16}',now['text']) and origin not in self.new_chats:
                raise ValueError('Existing conversation detected; establish a fresh chat')
            old = self.journal.conn.execute("SELECT 1 FROM guild_web_submissions WHERE origin=? AND (run_id=? OR state IN ('intent','uncertain'))", (origin,self.run_id)).fetchone()
            if old:
                raise ValueError('Duplicate or unresolved submission; do not resend')
            marker = 'growth-' + uuid.uuid4().hex[:16]
            a, b = 23 + int(marker[-2:],16)%40, 17
            prompt = f'Public chat smoke test. Return only a JSON object with keys marker and sum. marker must be "{marker}". sum is the integer result of {a} + {b}.'
            if self.task_prompt:prompt=self.task_prompt
            from tools.cyber_compliance_sentinel import CyberComplianceSentinel
            clean,detail=CyberComplianceSentinel().sanitize_outbound_query(prompt)
            if not clean:raise ValueError(detail)
            # Resolve before recording submission intent; a stale input has no side effects.
            composer_element = self.control_element(composer)
            op = self.run_id + ':' + uuid.uuid4().hex
            with self.journal.conn:
                self.journal.conn.execute('INSERT INTO guild_web_submissions VALUES (?,?,?,?,?,?)',
                                          (self.run_id,origin,op,'intent',marker,None if self.task_prompt else a+b))
            self.journal.event(self.run_id,'submission_intent',{'origin':origin,'operation_id':op},{'prompt':prompt,'action':action})
            self.submission_baseline=now['text']
            try:
                composer_element.fill(prompt,timeout=5000)
                fresh = self.page.evaluate(DOM)
                if gates(fresh) or public_url(fresh['url']) != origin:
                    raise ValueError('Gate or origin changed before send')
                # Re-find by observed exact label after filling; never stale ordinal input.
                names = [c for c in fresh['controls'] if c['name']==send['name'] and c['tag']==send['tag']]
                if len(names)!=1 or names[0]['disabled']:
                    raise ValueError('Send control is ambiguous or disabled')
                self.control_element(names[0]).click(timeout=5000)
                self.submitted_prompt=prompt
                self.journal.event(self.run_id,'submission_clicked',{'origin':origin,'operation_id':op})
            except Exception:
                with self.journal.conn:
                    self.journal.conn.execute("UPDATE guild_web_submissions SET state='uncertain' WHERE operation_id=?",(op,))
                raise
            self.epoch += 1
            return self.observe(wait=True)
        origin = public_url(url)
        if self.task_origin and origin!=self.task_origin:raise ValueError('Frozen trial portal mismatch')
        if origin in self.blocked_origins:
            raise ValueError('Previously gated origin; no repeated access attempts')
        self.journal.event(self.run_id,'navigation_intent',{'action':kind,'url':url})
        self.page.goto(url,wait_until='domcontentloaded',timeout=30000)
        public_url(self.page.url)
        self.epoch += 1
        return self.observe(wait=True)
