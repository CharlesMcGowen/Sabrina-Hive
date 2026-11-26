"""Durable recruiter ledger and exact, explicitly authorized outreach outbox."""
from collections import Counter
import json
import os
from pathlib import Path
import re
import sqlite3

from tools.messaging_store import ConversationStore, ROOT, digest, now

PROFILE_PATH = ROOT / 'data/messaging_profile.json'
SNAPSHOT_PATH = ROOT / 'data/social/recruiter-tracker.json'
NO_CONTACT = re.compile(r"\b(?:do not|don't|stop|remove me).{0,25}(?:contact|messag|reach|mail|list)", re.I)
RECRUITING = re.compile(
    r'\brecruit(?:er|ing|ment)\b|\btalent acquisition\b|\bHR team\b|'
    r'\b(?:we|i|our team)\b.{0,50}\bhiring\b|'
    r'\b(?:role|position|job|opportunity)\b.{0,200}\b(?:client|contract|full.time|resume)\b|'
    r'\b(?:client|contract|full.time|resume)\b.{0,200}\b(?:role|position|job|opportunity)\b', re.I | re.S)


def classify(snapshot):
    """Keep a verbatim source, rather than inferring recruiting from a name."""
    external = [m for m in snapshot['messages'] if m['ownership'] == 'external']
    for message in external:
        if NO_CONTACT.search(message['text']):
            return 'do_not_contact', {'source_id': message['id'], 'quote': NO_CONTACT.search(message['text']).group()}
    for message in external:
        match = RECRUITING.search(message['text'])
        if match:
            return 'recruiter', {'source_id': message['id'], 'quote': match.group()}
    return 'needs_classification', {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class RecruiterTracker:
    def __init__(self, store=None):
        self.store = store or ConversationStore()
        self.db = self.store.db
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recruiter_action_events'").fetchone()
        if not exists:
            backup = self.store.path.with_name(self.store.path.name + '.before-recruiter-tracker-'+now().replace(':','-')+'.bak')
            target=sqlite3.connect(backup)
            try:
                self.db.backup(target)
            finally:
                target.close()
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS recruiter_campaigns(
            id TEXT PRIMARY KEY, account TEXT NOT NULL, exact_text TEXT NOT NULL,
            authorization TEXT NOT NULL, created TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE IF NOT EXISTS recruiter_contacts(
            account TEXT NOT NULL, thread TEXT NOT NULL, peer TEXT NOT NULL, name TEXT NOT NULL,
            classification TEXT NOT NULL, evidence TEXT NOT NULL, observed TEXT NOT NULL,
            PRIMARY KEY(account,thread));
          CREATE TABLE IF NOT EXISTS recruiter_actions(
            id INTEGER PRIMARY KEY, campaign TEXT NOT NULL, account TEXT NOT NULL, thread TEXT NOT NULL,
            peer TEXT NOT NULL, context_version INTEGER NOT NULL, text TEXT NOT NULL,
            authorization TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            receipt TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(campaign,account,peer));
          CREATE TABLE IF NOT EXISTS recruiter_inventory(
            fingerprint TEXT PRIMARY KEY, name TEXT NOT NULL, preview TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'unread', thread TEXT, error TEXT, updated TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS recruiter_action_events(
            id INTEGER PRIMARY KEY, action_id INTEGER NOT NULL, phase TEXT NOT NULL,
            detail TEXT NOT NULL, created TEXT NOT NULL);
        ''')
        self.db.commit()

    def campaign(self, campaign_id, account, text, authorization):
        if not account or not 15 <= len(text) <= 1400 or not authorization.strip():
            raise ValueError('Exact text, account identity, and explicit user authorization required')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO recruiter_campaigns(id,account,exact_text,authorization,created) VALUES(?,?,?,?,?)',
                            (campaign_id, account, text, authorization, now()))
            row = self.db.execute('SELECT * FROM recruiter_campaigns WHERE id=?', (campaign_id,)).fetchone()
            if (row['account'], row['exact_text'], row['authorization']) != (account, text, authorization):
                raise ValueError('Campaign scope cannot change after authorization')
        return dict(row)

    def observe(self, snapshot):
        version, _ = self.store.observe(snapshot)
        classification, evidence = classify(snapshot)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO recruiter_contacts VALUES(?,?,?,?,?,?,?)',
                            (snapshot['account_id'], snapshot['thread_id'], snapshot['peer_id'].rstrip('/'),
                             snapshot['peer_name'], classification, json.dumps(evidence), now()))
        # Every normalized capture updates the evidence graph before any
        # reviewer sees it.  This keeps discovery, scheduled review, and manual
        # refreshes on one source of relationship state.
        from tools.messaging_skills import load_profile
        from tools.recruiter_graph import RecruiterGraph
        RecruiterGraph(self.store).observe({**snapshot, 'version': version}, load_profile())
        return {**snapshot, 'version': version}

    def queue(self, campaign_id, snapshot):
        account, thread, peer = snapshot['account_id'], snapshot['thread_id'], snapshot['peer_id'].rstrip('/')
        campaign = self.db.execute('SELECT * FROM recruiter_campaigns WHERE id=?', (campaign_id,)).fetchone()
        contact = self.db.execute('SELECT * FROM recruiter_contacts WHERE account=? AND thread=?', (account, thread)).fetchone()
        if not campaign or not campaign['active'] or campaign['account'] != account:
            raise ValueError('Campaign is inactive or belongs to another account')
        if not contact or contact['classification'] != 'recruiter' or not peer:
            return None
        status, reason = ('authorized', '') if snapshot['complete'] else ('needs_review', '; '.join(snapshot['completeness_reasons']))
        if any(m['ownership'] == 'self' and m['text'].strip() == campaign['exact_text'] for m in snapshot['messages']):
            status, reason = 'already_contacted', 'Exact outreach is already present in this conversation'
        if self._uncertain(account, thread):
            status, reason = 'needs_review', 'A prior send has uncertain delivery; verify its outcome first'
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO recruiter_actions(campaign,account,thread,peer,context_version,text,authorization,status,reason,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (campaign_id, account, thread, peer, snapshot['version'], campaign['exact_text'],
                             campaign['authorization'], status, reason, now(), now()))
        return self.db.execute('SELECT id FROM recruiter_actions WHERE campaign=? AND account=? AND peer=?',
                               (campaign_id, account, peer)).fetchone()['id']

    def _uncertain(self, account, thread, exclude=None):
        old = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_outbox'").fetchone()
        if old and self.db.execute("SELECT 1 FROM message_outbox WHERE account=? AND thread=? AND status IN ('sending','uncertain')", (account, thread)).fetchone():
            return True
        return bool(self.db.execute("SELECT 1 FROM recruiter_actions WHERE account=? AND thread=? AND id!=? AND status IN ('sending','uncertain')",
                                    (account, thread, exclude or -1)).fetchone())

    def get(self, action_id):
        row = self.db.execute('SELECT * FROM recruiter_actions WHERE id=?', (action_id,)).fetchone()
        if not row:
            raise KeyError(action_id)
        return dict(row)

    def refresh_before_send(self, action_id, fresh):
        """New inbound messages require assessment; do not silently overwrite consent context."""
        entry = self.get(action_id)
        if fresh['account_id'] != entry['account'] or fresh['thread_id'] != entry['thread'] or fresh['peer_id'].rstrip('/') != entry['peer']:
            raise ValueError('Account or recipient changed')
        if fresh['version'] != entry['context_version']:
            raise ValueError('Conversation changed since outreach preparation; review required')

    def prepare(self, action_id, fresh):
        """Prepare the still-unattempted exact campaign against freshly reviewed context.

        This is separate from submission. It cannot rearm any attempted, uncertain,
        opted-out, incomplete or operator-blocked action.
        """
        with self.db:
            self.db.execute('UPDATE recruiter_actions SET updated=updated WHERE id=?',(action_id,))
            entry=self.get(action_id)
            if entry['status']!='authorized':raise ValueError('Only unattempted outreach may be prepared')
            if (fresh['account_id']!=entry['account'] or fresh['thread_id']!=entry['thread']
                    or fresh['peer_id'].rstrip('/')!=entry['peer']):raise ValueError('Account or recipient changed')
            if not fresh['complete'] or classify(fresh)[0]!='recruiter':raise ValueError('Recruiter context requires review')
            if self._uncertain(entry['account'],entry['thread'],action_id):raise ValueError('Prior uncertain delivery requires review')
            if any(m['ownership']=='self' and m['text'].strip()==entry['text'] for m in fresh['messages']):raise ValueError('Exact introduction already exists')
            self.db.execute('INSERT INTO recruiter_action_events(action_id,phase,detail,created) VALUES(?,?,?,?)',
                            (action_id,'prepared',json.dumps({'prior_version':entry['context_version'],'context_version':fresh['version'],'source_message_ids':[m['id'] for m in fresh['messages']]}),now()))
            self.db.execute('UPDATE recruiter_actions SET context_version=?,updated=? WHERE id=? AND status=\'authorized\'',(fresh['version'],now(),action_id))

    def claim(self, action_id, expected):
        with self.db:
            self.db.execute('UPDATE recruiter_actions SET updated=updated WHERE id=?', (action_id,))
            entry = self.get(action_id)
            if entry['status'] != 'authorized':
                raise ValueError('Already attempted, sent, uncertain, or awaiting review')
            self.refresh_before_send(action_id, expected)
            current = self.store.snapshot(entry['account'], entry['thread'])
            self.refresh_before_send(action_id, current)
            campaign = self.db.execute('SELECT * FROM recruiter_campaigns WHERE id=?', (entry['campaign'],)).fetchone()
            if not campaign['active'] or entry['text'] != campaign['exact_text'] or entry['authorization'] != campaign['authorization']:
                raise ValueError('Exact campaign authorization is no longer valid')
            if not current['complete'] or classify(current)[0] != 'recruiter':
                raise ValueError('Incomplete context, recruiter not established, or contact opted out')
            if self._uncertain(entry['account'], entry['thread'], action_id):
                raise ValueError('Prior uncertain send requires outcome verification')
            if any(m['ownership'] == 'self' and m['text'].strip() == entry['text'] for m in current['messages']):
                raise ValueError('Exact outreach already exists; do not duplicate it')
            self.db.execute('INSERT INTO recruiter_action_events(action_id,phase,detail,created) VALUES(?,?,?,?)',
                            (action_id,'submission_started',json.dumps({'context_version':current['version'],'before_ids':[m['id'] for m in current['messages']]}),now()))
            self.db.execute("UPDATE recruiter_actions SET status='sending',updated=? WHERE id=? AND status='authorized'", (now(), action_id))
        return entry

    def finish(self, action_id, receipt):
        entry = self.get(action_id)
        if entry['status'] != 'sending' or receipt.get('status') not in ('sent', 'uncertain'):
            raise ValueError('No active send claim or invalid receipt')
        if receipt['status'] == 'sent' and (not receipt.get('message_id') or receipt.get('account_id') != entry['account'] or receipt.get('thread_id') != entry['thread'] or receipt.get('text') != entry['text']):
            raise ValueError('Delivery evidence does not match exact authorization')
        with self.db:
            self.db.execute('UPDATE recruiter_actions SET status=?,receipt=?,reason=?,updated=? WHERE id=? AND status=\'sending\'',
                            (receipt['status'], json.dumps(receipt), receipt.get('reason', ''), now(), action_id))

    def block(self, action_id, reason):
        with self.db:
            self.db.execute("UPDATE recruiter_actions SET status='needs_review',reason=?,updated=? WHERE id=? AND status='authorized'", (str(reason), now(), action_id))

    def model_review(self,account,thread,version,profile):
        path=self.store.path.with_name('messaging_blackboard.sqlite3')
        if not path.exists():return None
        db=None
        try:
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
            db.row_factory=sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_review_runs'").fetchone():return None
            row=db.execute('SELECT * FROM conversation_review_runs WHERE account=? AND thread=? ORDER BY created DESC LIMIT 1',(account,thread)).fetchone()
            if not row:return None
            value=dict(row);value['packet']=json.loads(value['packet']);value['models']=json.loads(value['models'])
            value['decision']=json.loads(value['decision']) if value['decision'] else None
            value['artifacts']=[]
            for artifact in db.execute('SELECT * FROM conversation_review_artifacts WHERE review_id=? ORDER BY created',(row['id'],)):
                item=dict(artifact);item['parsed']=json.loads(item['parsed']) if item['parsed'] else None
                value['artifacts'].append(item)
            value['source_matches_current']=row['context_version']==version and row['profile_hash']==digest(profile)
            if not value['source_matches_current']:
                value['status']='stale';value['error']='Conversation or approved profile changed since this review'
            return value
        except (sqlite3.Error,ValueError):return {'status':'unavailable','error':'Conversation blackboard is temporarily unavailable'}
        finally:
            if db:db.close()

    def snapshot(self, worker=None):
        from tools.messaging_skills import load_profile
        from tools.recruiter_graph import RecruiterGraph
        profile=load_profile()
        graph=RecruiterGraph(self.store)
        rows = []
        for contact in self.db.execute("SELECT * FROM recruiter_contacts ORDER BY observed DESC"):
            thread = self.store.snapshot(contact['account'], contact['thread'])
            model_review=self.model_review(contact['account'],contact['thread'],thread['version'],profile)
            if contact['classification'] not in {'recruiter','do_not_contact'} and not model_review:continue
            action = self.db.execute('SELECT * FROM recruiter_actions WHERE account=? AND thread=? ORDER BY id DESC LIMIT 1', (contact['account'], contact['thread'])).fetchone()
            related = None if action else self.db.execute('SELECT * FROM recruiter_actions WHERE account=? AND peer=? ORDER BY id DESC LIMIT 1', (contact['account'], contact['peer'])).fetchone()
            latest = thread['messages'][-1]
            graph_dossier = graph.dossier(thread, profile)
            state = action['status'] if action else 'covered_in_another_thread' if related else 'not_queued'
            if state == 'sent':
                receipt = json.loads(action['receipt'] or '{}')
                state = 'reply_received' if latest['ownership'] == 'external' and latest['id'] != receipt.get('message_id') else 'awaiting_reply'
            if state == 'sending':
                state = 'delivery_verification_required'
            decision = self.db.execute("SELECT content,status FROM decisions WHERE account=? AND thread=? AND context_version=? AND status!='stale' ORDER BY created DESC LIMIT 1", (contact['account'], contact['thread'], thread['version'])).fetchone()
            next_action = {'authorized': 'Send the exact approved introduction', 'awaiting_reply': 'Wait for recruiter response',
                           'reply_received': 'Review the new reply using the saved career profile', 'needs_review': 'User review required',
                           'uncertain': 'Verify delivery before any further send', 'delivery_verification_required': 'Verify interrupted submission; never automatically retry',
                           'covered_in_another_thread': 'This recruiter has another tracked conversation; only one introduction is authorized per recruiter'}.get(state, 'Review conversation')
            assessment=json.loads(decision['content']) if decision else None
            if model_review and model_review.get('source_matches_current') and model_review.get('decision'):
                assessment=model_review['decision']
                next_action=assessment['next_action']
            elif model_review:
                assessment=None
                next_action='Inspect model evidence; no proposal is accepted yet' if model_review['status'] in {'blocked','stale','unavailable'} else 'Model review is in progress; no outgoing reply is proposed'
            rows.append({'name': contact['name'], 'account_id': contact['account'], 'thread_id': contact['thread'],
                         'peer_id': contact['peer'],
                         'url': thread['url'], 'status': state, 'classification': contact['classification'],
                         'evidence': json.loads(contact['evidence']), 'latest_message': latest['text'],
                         'latest_owner': latest['ownership'], 'latest_message_id': latest['id'],
                         'message_count': len(thread['messages']), 'context_complete': thread['complete'],
                         'context_issues': thread['completeness_reasons'], 'next_action': next_action,
                         'source_messages':thread['messages'],'model_review':model_review,
                         'graph': graph_dossier,
                         'relationship_state': graph_dossier.get('conversation_state',{}).get('state','new'),
                         'current_objective': graph_dossier.get('conversation_state',{}).get('current_objective'),
                         'open_questions': graph_dossier.get('open_questions',[]),
                         'autonomous_eligibility': (assessment or {}).get('auto_reply_eligible',False) if assessment else False,
                         'autonomy_enabled_for_thread': graph.autonomy_allowed(thread),
                         'autonomous_reason': (assessment or {}).get('autonomous_reason','') if assessment else '',
                         'reason': action['reason'] if action else '', 'outreach_text': action['text'] if action else None,
                         'receipt': json.loads(action['receipt']) if action and action['receipt'] else None,
                         'assessment': assessment, 'observed': contact['observed']})
        inventory = dict(self.db.execute('SELECT state,count(*) FROM recruiter_inventory GROUP BY state').fetchall())
        return {'updated': now(), 'source_host': 'Windows desktop', 'worker': worker or {}, 'profile': profile,
                'total_recruiters': len({(r['account_id'],r['peer_id']) for r in rows if r['classification']=='recruiter'}), 'total_conversations':len(rows),
                'counts': dict(Counter(r['status'] for r in rows)), 'inventory': inventory,
                'contacts': rows, 'campaigns': [dict(r) for r in self.db.execute('SELECT id,exact_text,created,active FROM recruiter_campaigns')]}


TRACKER_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sabrina recruiter conversations</title><style>
body{margin:0;background:#0b1120;color:#e2e8f0;font:15px system-ui}main{max-width:1500px;margin:auto;padding:28px}h1{margin-bottom:6px}a{color:#93c5fd}button,input,select{font:inherit;padding:9px;background:#162238;color:#fff;border:1px solid #475569;border-radius:6px}section,.card{background:#111c30;padding:18px;border:1px solid #28384f;border-radius:10px;margin:16px 0}.muted,small{color:#a3b2c6}.pill{display:inline-block;border-radius:20px;background:#213754;padding:5px 10px;margin:4px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px 8px;border-bottom:1px solid #29384e;vertical-align:top}td{min-width:150px}th{color:#a3b2c6}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;max-height:340px;overflow:auto}.message{max-width:320px}.bar{display:flex;gap:12px;flex-wrap:wrap;align-items:center}details{margin:10px 0}summary{cursor:pointer;color:#bfdbfe}.source-message{padding:12px;margin:10px 0;border:1px solid #334155;border-radius:6px}.flow{display:flex;gap:10px;flex-wrap:wrap}.flow span{background:#1c2f4a;padding:10px;border-radius:6px}
</style><script src="/assets/recruiter-tracker.js" defer></script></head><body><main>
<a href="/">← Sabrina dashboard</a><h1>Recruiter conversations</h1>
<p class="muted">Captured messages, model understanding, and the next step for each conversation.</p>
<div id="health" class="card">Loading tracker…</div>
<section><h2>How Sabrina reviews a conversation</h2>
<div class="flow"><span>Captured chat + your approved profile</span><span>→ Private shared blackboard</span><span>→ Llama reader</span><span>→ Qwen drafter</span><span>→ Granite independent reviewer</span><span>→ Your review</span></div>
<p>Every model receives the original captured chat and approved profile. Each stage saves its actual response and source evidence. A review can recommend waiting, requesting information, or a contextual draft. It cannot authorize a send.</p>
<p class="muted">Expand a conversation to see what was collected, what each model read, and why it chose the next step. Unchanged reviews are reused; changed chats or profile facts invalidate them.</p></section>
<section><h2>Your saved context</h2><div id="profile"></div><p class="muted">Your approved facts stay separate from recruiter claims and promises made in chat.</p></section>
<section><h2>Outreach template library</h2><div id="templates"></div><p class="muted">Templates are drafts. Sabrina may preview them, but each exact rendered message still requires approved profile facts and explicit user authorization.</p></section>
<section><div class="bar"><h2>Conversations</h2><input id="search" placeholder="Search a recruiter or message">
<select id="filter"><option value="">All statuses</option><option value="draft_ready">Draft ready for review</option><option value="needs_input">Needs your information</option><option value="no_reply">No reply due</option><option value="blocked">Review blocked</option><option value="uncertain">Unconfirmed delivery</option><option value="awaiting_reply">Earlier outreach verified</option></select><button id="refresh">Refresh</button></div>
<div id="counts"></div><div style="overflow:auto"><table><thead><tr><th>Contact</th><th>Earlier outreach</th><th>Captured conversation</th><th>Model review</th><th>Next step</th></tr></thead><tbody id="rows"></tbody></table></div></section>
</main></body></html>'''
