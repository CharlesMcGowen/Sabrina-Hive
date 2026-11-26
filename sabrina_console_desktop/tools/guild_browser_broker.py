"""Narrow Ai2 chat broker using Sabrina's existing browser manager and lab lease."""
import hashlib
import json
import time
from pathlib import Path
from tools.guild_browser_loop import origin


class GuildBrowserBroker:
    def __init__(self, allowed_url, output, cdp_url='http://127.0.0.1:9222'):
        if origin(allowed_url) != origin('https://playground.allenai.org/'):
            raise ValueError('Only the observed Ai2 chat protocol is implemented')
        if cdp_url != 'http://127.0.0.1:9222':
            raise ValueError('Only the configured local browser endpoint is permitted')
        self.url=allowed_url; self.output=Path(output); self.cdp_url=cdp_url
        self.manager=None; self.page=None; self.lease=None; self.sent=False; self.operations=set()

    def __enter__(self):
        from tools.control_client import configured
        from ai_systems.browser_utils import PlaywrightBrowserManager
        client=configured()
        if client is None: raise RuntimeError('Lab resource coordinator is required')
        for lease in client.request('/v1/status').get('leases',[]):
            if lease.get('resource')=='browser:linkedin-primary' and lease.get('state')=='uncertain':
                raise RuntimeError('Shared browser lease is uncertain; verify previous executor stopped before reconciliation')
        self.client=client
        self.lease=client.lease('browser:linkedin-primary')
        self.identity=self.lease.__enter__()
        try:
            self.manager=PlaywrightBrowserManager()
            self.page=self.manager.attach_isolated_cdp(self.cdp_url)
            self.page.goto(self.url,wait_until='domcontentloaded',timeout=30000)
            self.page.get_by_role('button',name='Submit prompt',exact=True).or_(
                self.page.get_by_role('button',name='Accept terms & use Playground',exact=True)).first.wait_for(timeout=15000)
            return self
        except Exception:
            self.__exit__(None,None,None)
            raise

    def __exit__(self,*args):
        if self.manager: self.manager.disconnect_cdp()
        if self.lease: self.lease.__exit__(*args)

    def observe(self):
        self.client.request('/v1/leases/renew',self.identity)
        if origin(self.page.url)!=origin(self.url): raise ValueError('Unexpected page origin')
        visible=self.page.locator('body').inner_text(timeout=5000)
        composer=self.page.get_by_role('textbox')
        send=self.page.get_by_role('button',name='Submit prompt',exact=True)
        terms=self.page.get_by_role('button',name='Accept terms & use Playground',exact=True)
        gates=[]
        if terms.count() and terms.is_visible(): gates.append('terms_require_human_approval')
        if self.page.locator('iframe[title*="challenge"]').count():
            for frame in self.page.locator('iframe[title*="challenge"]').all():
                if frame.is_visible(): gates.append('captcha_challenge')
        for marker in ('Too many requests','Rate limit exceeded','quota exceeded','Sign in to continue','Log in to continue'):
            if marker.lower() in visible.lower(): gates.append(marker)
        writable=composer.count()==1 and composer.is_visible() and composer.is_enabled()
        can_send=send.count()==1 and send.is_visible() and send.is_enabled()
        if not writable or not can_send: gates.append('chat_controls_unavailable')
        state={'url':self.page.url,'visible':visible[-14000:],'blocked':bool(gates),'gates':gates,
               'capabilities':{'observe':True,'submit_once':writable and can_send and not gates and not self.sent,
                 'browser_attached':True,'existing_session':True,'local_inference':False,
                 'terms_acceptance':False,'account_creation':False},'submitted':self.sent}
        state['revision']=hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()[:20]
        self.output.mkdir(parents=True,exist_ok=True)
        self.page.screenshot(path=str(self.output/'browser-latest.png'))
        return state

    def execute(self,action,*,operation_id):
        current=self.observe()
        if operation_id in self.operations: raise RuntimeError('Action may already have executed')
        if current['blocked'] or action['revision']!=current['revision']:
            return {'ok':True,'observation':current,'executed':False,'reason':'blocked_or_stale'}
        if origin(action['allowed_url'])!=origin(self.url): raise ValueError('Action scope mismatch')
        self.operations.add(operation_id)
        if action['action']=='observe': return {'ok':True,'observation':current,'executed':False}
        if action['action']!='submit_once' or self.sent: raise ValueError('Unsupported or duplicate submission')
        composer=self.page.get_by_role('textbox')
        composer.fill(action['text'],timeout=5000)
        if self.observe()['blocked']: raise ValueError('Gate appeared before submit')
        self.sent=True
        self.page.get_by_role('button',name='Submit prompt',exact=True).click(timeout=5000)
        deadline=time.monotonic()+45
        # A cleared composer and a new thread are observable submission evidence;
        # the following worker round decides whether a real answer is present.
        while time.monotonic()<deadline:
            time.sleep(1)
            fresh=self.observe()
            if fresh['blocked']: return {'ok':True,'observation':fresh,'executed':True}
            if '/thread/' in fresh['url'] and not self.page.get_by_role('textbox').input_value():
                time.sleep(2)
                return {'ok':True,'observation':self.observe(),'executed':True}
        return {'ok':False,'reason':'submission_response_timeout_no_replay','observation':self.observe()}
