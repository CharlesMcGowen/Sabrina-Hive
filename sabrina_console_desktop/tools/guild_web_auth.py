"""Opt-in account operations for the audited portal broker.

The model selects operations and refs, never credentials, email contents or URLs
from verification messages. No disposable identities, OAuth consent or purchases.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / 'config/portal_accounts.json'
AUTH_GATES = {'authentication_required', 'authentication_or_personal_data_required'}
TOOLS = {
    'auth_begin': {'mode': 'sign_in or register', 'account': 'authorized account alias', 'revision': 'current revision'},
    'auth_fill': {'ref': 'current input index', 'field': 'email, password or code', 'revision': 'current revision'},
    'auth_click': {'ref': 'current sign-in/registration/verification button', 'revision': 'current revision'},
    'auth_check_email': {},
    'auth_open_verification': {},
}


def origin(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('HTTPS account origin required')
    return 'https://' + p.netloc.lower()


class AccountPolicy:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        if value.get('version') != 1 or value.get('enabled') is not True:
            raise ValueError('Account policy disabled or unsupported')
        self.fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        self.id = value['id']
        for alias, account in value['accounts'].items():
            if not re.fullmatch(r'[a-z0-9_-]{1,50}', alias):raise ValueError('Invalid account alias')
            if not re.fullmatch(r'[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', account['email']):
                raise ValueError('Invalid authorized mailbox')
            for site in account['origins']:
                if origin(site) != site:raise ValueError('Exact origin required')
            if not set(account.get('registration_origins', [])) <= set(account['origins']):
                raise ValueError('Registration cannot expand account scope')

    @classmethod
    def load(cls, grant=None):
        policy = cls(json.loads(POLICY_PATH.read_text(encoding='utf-8')))
        if grant and grant != policy.grant():raise ValueError('Account policy changed; revalidate')
        policy.managed = True
        return policy

    def grant(self):return {'id': self.id, 'sha256': self.fingerprint}

    def account(self, alias, site, mode):
        account = self.value['accounts'].get(alias)
        if not account or site not in account['origins']:raise ValueError('Account not authorized for this origin')
        if mode not in ('sign_in', 'register'):raise ValueError('Unknown account operation')
        if mode == 'register' and site not in account.get('registration_origins', []):
            raise ValueError('Registration not authorized for this origin')
        return account

    def public(self, site):
        return {'policy': self.grant(), 'accounts': [
            {'alias': alias, 'sign_in': True, 'register': site in a.get('registration_origins', [])}
            for alias, a in self.value['accounts'].items() if site in a['origins']],
            'verification': 'Scoped existing Gmail session; credentials and codes stay host-side',
            'unsupported': ['OAuth consent', 'phone/SMS', 'CAPTCHA solving', 'payments', 'terms acceptance']}


class Vault:
    """Windows credential storage only; no plaintext/env fallback for account passwords."""
    def __init__(self):
        import keyring
        if type(keyring.get_keyring()).__module__ != 'keyring.backends.Windows':
            raise RuntimeError('Windows credential vault unavailable')
        self.backend = keyring

    def key(self, alias, site):return alias + ':' + hashlib.sha256(site.encode()).hexdigest()
    def get(self, alias, site):return self.backend.get_password('sabrina.portal.accounts', self.key(alias, site))
    def create(self, alias, site):
        if self.get(alias, site):raise ValueError('Existing portal credential; choose sign_in')
        value = secrets.token_urlsafe(24) + '!aA1'
        self.backend.set_password('sabrina.portal.accounts', self.key(alias, site), value)
        return value


def verification_from_messages(messages, account, site, sent_at, now):
    """Only one recent, attributable verification message; never run email instructions."""
    host = urlsplit(site).hostname
    domains = account.get('sender_domains', {}).get(site, [host])
    matches = []
    for m in messages:
        sender = m.get('sender', '').lower().rsplit('@', 1)
        if len(sender) != 2 or sender[1] not in domains:continue
        if m.get('recipient', '').lower() != account['email'].lower():continue
        if not sent_at <= m.get('timestamp', 0) <= now + 30 or now - m['timestamp'] > 900:continue
        if not m.get('id') or not re.search(r'verif|confirm|sign.?in|log.?in|one.time|magic', m.get('subject', ''), re.I):continue
        if re.search(r'reset|recover|change password', m.get('subject', ''), re.I):continue
        codes = set(re.findall(r'(?:verification|security|sign.?in|one.time|confirmation|otp|code)[^\d]{0,60}(\d{4,8})(?!\d)', m.get('text', ''), re.I))
        links = set()
        for link in m.get('links', []):
            try:
                if origin(link['url']) != site:continue
            except ValueError:continue
            if re.search(r'verif|confirm|sign.?in|log.?in|magic', link.get('label', ''), re.I):links.add(link['url'])
        if len(codes) > 1 or len(links) > 1:raise ValueError('Ambiguous verification message')
        if codes or links:
            matches.append({'message_id': m['id'], 'code': next(iter(codes), None), 'url': next(iter(links), None)})
    if len(matches) > 1:raise ValueError('Multiple verification messages; reconcile before use')
    return matches[0] if matches else None


class AuthSession:
    def __init__(self, broker, policy, vault=None, mailbox=None):
        self.broker, self.policy = broker, policy
        self.vault = vault
        self.mailbox = mailbox
        self.current = None
        self.sensitive = set(a['email'] for a in policy.value['accounts'].values())
        self.code = self.link = None
        self.last_mail_check = 0
        self.filled = set()
        broker.journal.conn.executescript('''
CREATE TABLE IF NOT EXISTS guild_web_auth_attempts (
 id TEXT PRIMARY KEY, run_id TEXT, origin TEXT, account TEXT, mode TEXT, created REAL, state TEXT);
CREATE TABLE IF NOT EXISTS guild_web_auth_actions (
 attempt_id TEXT, phase TEXT, created REAL, state TEXT, PRIMARY KEY(attempt_id, phase));
''')
        broker.journal.event(broker.run_id, 'account_authorization', policy.grant())

    def redacted(self, value):
        def clean(v):
            if isinstance(v, dict):
                return {k: (safe_url(x) if k in ('url', 'href') and isinstance(x, str) else clean(x)) for k, x in v.items()}
            if isinstance(v, list):return [clean(x) for x in v]
            if not isinstance(v, str):return v
            for secret in sorted(self.sensitive, key=len, reverse=True):
                if secret:v = v.replace(secret, '[credential redacted]')
            return re.sub(r'https://[^\s<>"\']+', lambda m: safe_url(m[0]), v)
        def safe_url(v):
            try:
                p = urlsplit(v)
                if v in self.sensitive:return urlunsplit((p.scheme, p.netloc, '/[verification-redacted]', '', ''))
                private = any(re.search(r'token|secret|password|email|code|otp|auth|session|state|key', k, re.I)
                              or x in self.sensitive for k, x in parse_qsl(p.query))
                return urlunsplit((p.scheme, p.netloc, p.path, '', '')) if private or p.fragment else v
            except ValueError:return '[invalid URL]'
        return clean(value)

    def status(self, url):
        try:site = origin(url)
        except ValueError:site = ''
        status = self.policy.public(site)
        if self.current:
            status['attempt'] = {k: self.current[k] for k in ('id', 'origin', 'account', 'mode', 'state')}
            status.update(fields_filled=sorted(self.filled), code_available=bool(self.code), link_available=bool(self.link))
        return status

    def _event(self, kind, detail):
        self.broker.journal.event(self.broker.run_id, kind, self.redacted(detail))

    def _current(self):
        if not self.current:raise ValueError('Choose auth_begin first')
        if origin(self.broker.page.url) != self.current['origin']:raise ValueError('Account origin changed')
        return self.current

    def execute(self, action, now=None):
        b = self.broker; kind = action['action']
        if getattr(self.policy, 'managed', False):AccountPolicy.load(self.policy.grant())
        if kind == 'auth_begin':
            site = origin(now['url'])
            account = self.policy.account(action['account'], site, action['mode'])
            if self.current and self.current['origin'] == site:raise ValueError('Account attempt already exists; inspect its state')
            prior = b.journal.conn.execute("SELECT id FROM guild_web_auth_attempts WHERE origin=? AND account=? AND state NOT IN ('access_observed','abandoned_before_submission')", (site, action['account'])).fetchone()
            if prior:raise ValueError('Unresolved account attempt; reconcile before starting another')
            self.vault = self.vault or Vault()
            password = self.vault.get(action['account'], site)
            if action['mode'] == 'register' and password:raise ValueError('Existing portal credential; choose sign_in')
            if action['mode'] == 'register':password = self.vault.create(action['account'], site)
            if password:self.sensitive.add(password)
            self.filled = set(); self.code = self.link = None
            self.current = dict(id=uuid.uuid4().hex, run_id=b.run_id, origin=site, account=action['account'], mode=action['mode'], created=time.time(), state='prepared')
            with b.journal.conn:
                b.journal.conn.execute('INSERT INTO guild_web_auth_attempts VALUES (:id,:run_id,:origin,:account,:mode,:created,:state)', self.current)
            self._event('auth_decision', self.current)
            return
        current = self._current()
        account = self.policy.account(current['account'], current['origin'], current['mode'])
        if kind == 'auth_check_email':
            if time.time() - self.last_mail_check < 15:raise ValueError('Wait before another mailbox check')
            sent = b.journal.conn.execute("SELECT MIN(created) FROM guild_web_auth_actions WHERE attempt_id=? AND phase!='verification_link'", (current['id'],)).fetchone()[0]
            if not sent:raise ValueError('No recorded authentication submission')
            if self.mailbox is None:
                from tools.portal_verification_mail import GmailVerificationReader
                self.mailbox = GmailVerificationReader(b.client)
            self.last_mail_check = time.time()
            messages = self.mailbox.read(account, current['origin'], sent)
            found = verification_from_messages(messages, account, current['origin'], sent, time.time())
            if found:
                self.code, self.link = found['code'], found['url']
                self.sensitive.update(x for x in (self.code, self.link) if x)
                self._event('auth_email_matched', {'attempt_id': current['id'], 'message_id_sha256': hashlib.sha256(found['message_id'].encode()).hexdigest(), 'code_available': bool(self.code), 'link_available': bool(self.link)})
            else:self._event('auth_email_pending', {'attempt_id': current['id']})
            return
        if kind == 'auth_open_verification':
            if not self.link:raise ValueError('No matched verification link')
            from tools.guild_web_broker import public_url
            if public_url(self.link) != current['origin']:raise ValueError('Verification origin mismatch')
            self._intent('verification_link')
            b.page.goto(self.link, wait_until='domcontentloaded', timeout=30000)
            self._outcome('verification_link'); self.link = None
            return
        control = b.refs.get(action.get('ref'))
        if not control or control['disabled']:raise ValueError('Current enabled account control required')
        locator = b.control_element(control)
        if kind == 'auth_fill':
            field = action['field']; name = control['name']
            if control['tag'] != 'input':raise ValueError('Account input required')
            if field == 'email' and (control['type'] == 'email' or re.search(r'email', name, re.I)):
                value = account['email']
            elif field == 'password' and control['type'] == 'password':
                value = self.vault.get(current['account'], current['origin'])
                if not value:raise ValueError('No authorized password; use email sign-in or operator provisioning')
            elif field == 'code' and re.search(r'verif|code|otp|one.time', name, re.I) and self.code:
                value = self.code
            else:raise ValueError('Field is not an authorized credential input')
            self.sensitive.add(value)
            # Do not overwrite an unrelated populated field.
            existing = locator.input_value(timeout=5000)
            if existing and existing != value:raise ValueError('Account field is already populated; reconcile')
            locator.fill(value, timeout=5000); self.filled.add(field)
            self._event('auth_field_filled', {'attempt_id': current['id'], 'field': field})
            return
        if kind != 'auth_click':raise ValueError('Unknown account operation')
        label = control['name'].strip()
        if control.get('href') or control['tag'] not in ('button', 'input') and control.get('role') != 'button':
            raise ValueError('Observed authentication button required')
        if not re.fullmatch(r'(?:Continue(?: with email)?|Next|Sign in|Log in|Login|Sign up|Register|Create (?:an )?account|Send (?:code|link|verification email|magic link)|Verify(?: email| code)?|Confirm(?: email| code)?)', label, re.I):
            raise ValueError('Unsupported authentication action; no OAuth, consent, payment or reset')
        if not self.filled:raise ValueError('No host-filled account fields; inspect before submitting')
        # Autofilled credentials also require exact ownership before any submit.
        for c in now['controls']:
            field = ('password' if c['type'] == 'password' else
                     'email' if c['type'] == 'email' or re.search(r'email', c['name'], re.I) else
                     'code' if re.search(r'verif|code|otp|one.time', c['name'], re.I) else None)
            if c['tag'] != 'input' or field is None:continue
            expected = account['email'] if field == 'email' else self.code if field == 'code' else self.vault.get(current['account'], current['origin'])
            actual = b.control_element(c).input_value(timeout=5000)
            if actual and actual != expected:raise ValueError('Unowned autofilled credential; submission blocked')
        phase = '+'.join(sorted(self.filled))
        self._intent(phase)
        try:locator.click(timeout=5000)
        except Exception:
            self._event('auth_submission_uncertain', {'attempt_id': current['id'], 'phase': phase})
            raise RuntimeError('Authentication submission uncertain; inspect or check email, never resend') from None
        self._outcome(phase)

    def _intent(self, phase):
        c = self.current; b = self.broker
        with b.journal.conn:
            try:b.journal.conn.execute('INSERT INTO guild_web_auth_actions VALUES (?,?,?,?)', (c['id'], phase, time.time(), 'intent'))
            except __import__('sqlite3').IntegrityError:raise ValueError('Authentication step already attempted; reconcile, never resend') from None
            b.journal.conn.execute("UPDATE guild_web_auth_attempts SET state='submission_pending' WHERE id=?", (c['id'],))
        c['state'] = 'submission_pending'
        self._event('auth_submission_intent', {'attempt_id': c['id'], 'phase': phase})

    def _outcome(self, phase):
        c = self.current
        with self.broker.journal.conn:
            self.broker.journal.conn.execute("UPDATE guild_web_auth_actions SET state='clicked' WHERE attempt_id=? AND phase=?", (c['id'], phase))
        self._event('auth_submission_clicked', {'attempt_id': c['id'], 'phase': phase})

    def access_observed(self, site, event_id):
        if self.current and self.current['origin'] == site:
            with self.broker.journal.conn:
                self.broker.journal.conn.execute("UPDATE guild_web_auth_attempts SET state='access_observed' WHERE id=?", (self.current['id'],))
            self.current['state'] = 'access_observed'
            self._event('auth_portal_access_observed', {'attempt_id': self.current['id'], 'smoke_observation': event_id, 'account_identity_verified': False})
