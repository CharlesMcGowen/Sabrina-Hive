"""Durable, account-scoped conversation snapshots and a review-only queue."""
import hashlib
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def now():
    return datetime.now(timezone.utc).isoformat()

class ConversationStore:
    def __init__(self, path=None):
        self.path = Path(path or ROOT / 'data/messaging.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS threads (
          account TEXT NOT NULL, thread TEXT NOT NULL, version INTEGER NOT NULL,
          hash TEXT NOT NULL, snapshot TEXT NOT NULL, observed TEXT NOT NULL,
          PRIMARY KEY(account,thread));
        CREATE TABLE IF NOT EXISTS messages (
          account TEXT NOT NULL, thread TEXT NOT NULL, id TEXT NOT NULL,
          content TEXT NOT NULL, observed TEXT NOT NULL, PRIMARY KEY(account,thread,id));
        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
          context_version INTEGER NOT NULL, profile_hash TEXT NOT NULL, status TEXT NOT NULL,
          content TEXT NOT NULL, created TEXT NOT NULL,
          UNIQUE(account,thread,context_version,profile_hash));
        CREATE TABLE IF NOT EXISTS opportunities (
          account TEXT NOT NULL, thread TEXT NOT NULL, identity TEXT NOT NULL,
          content TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(account,thread,identity));
        CREATE TABLE IF NOT EXISTS leases (
          name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def acquire(self, name, owner, ttl=600):
        import time
        with self.db:
            self.db.execute('DELETE FROM leases WHERE name=? AND expires<?', (name,time.time()))
            self.db.execute('INSERT OR IGNORE INTO leases VALUES(?,?,?)',(name,owner,time.time()+ttl))
            row=self.db.execute('SELECT owner FROM leases WHERE name=?',(name,)).fetchone()
            return row['owner']==owner

    def release(self, name, owner):
        with self.db:
            self.db.execute('DELETE FROM leases WHERE name=? AND owner=?',(name,owner))

    def observe(self, snapshot):
        account,thread=snapshot['account_id'],snapshot['thread_id']
        if not account or not thread or not snapshot.get('messages'):
            raise ValueError('Stable account/thread IDs and messages required')
        ids=[m['id'] for m in snapshot['messages']]
        if any(not i for i in ids) or len(ids)!=len(set(ids)):
            raise ValueError('Message IDs must be unique and nonempty')
        # Exclude transient observation times; edits, ownership, ordering and completeness matter.
        stable={k:v for k,v in snapshot.items() if k not in ('observed_at','version')}
        key=digest(stable)
        with self.db:
            old=self.db.execute('SELECT version,hash FROM threads WHERE account=? AND thread=?',(account,thread)).fetchone()
            changed=old is None or old['hash']!=key
            version=(old['version']+1 if changed else old['version']) if old else 1
            if changed:
                self.db.execute("UPDATE decisions SET status='stale' WHERE account=? AND thread=? AND status IN ('draft','needs_input','no_reply')",(account,thread))
            self.db.execute('INSERT OR REPLACE INTO threads VALUES(?,?,?,?,?,?)',
                            (account,thread,version,key,json.dumps(stable,ensure_ascii=False),now()))
            for message in snapshot['messages']:
                # Preserve when a stable message ID was first observed. Repeated
                # polling may refresh normalized content, but it must not move
                # follow-up or response-time anchors forward forever.
                self.db.execute(
                    '''INSERT INTO messages(account,thread,id,content,observed) VALUES(?,?,?,?,?)
                       ON CONFLICT(account,thread,id) DO UPDATE SET content=excluded.content''',
                    (account,thread,message['id'],json.dumps(message,ensure_ascii=False),now()))
        return version,changed

    def snapshot(self,account,thread):
        row=self.db.execute('SELECT version,snapshot FROM threads WHERE account=? AND thread=?',(account,thread)).fetchone()
        if not row: raise KeyError(thread)
        return {**json.loads(row['snapshot']),'version':row['version']}

    def has_decision(self,account,thread,version,profile_hash):
        return self.db.execute("SELECT 1 FROM decisions WHERE account=? AND thread=? AND context_version=? AND profile_hash=? AND status!='stale'",
                               (account,thread,version,profile_hash)).fetchone() is not None

    def save_decision(self, account, thread, version, profile_hash, decision):
        status={'draft_reply':'draft','ask_human':'needs_input','no_reply':'no_reply'}[decision['action']]
        with self.db:
            current=self.db.execute('SELECT version FROM threads WHERE account=? AND thread=?',(account,thread)).fetchone()
            if not current or current['version']!=version:
                raise ValueError('Conversation changed while drafting')
            self.db.execute("UPDATE decisions SET status='stale' WHERE account=? AND thread=? AND profile_hash!=?",(account,thread,profile_hash))
            self.db.execute('INSERT OR REPLACE INTO decisions(account,thread,context_version,profile_hash,status,content,created) VALUES(?,?,?,?,?,?,?)',
                            (account,thread,version,profile_hash,status,json.dumps(decision,ensure_ascii=False),now()))
            for opportunity in decision.get('opportunities',[]):
                identity=digest([opportunity.get('company',''),opportunity.get('role',''),opportunity.get('job_url','')])
                self.db.execute('INSERT OR REPLACE INTO opportunities VALUES(?,?,?,?,?)',
                                (account,thread,identity,json.dumps(opportunity,ensure_ascii=False),now()))

    def review(self,account=None):
        query='SELECT d.*,t.snapshot FROM decisions d JOIN threads t ON d.account=t.account AND d.thread=t.thread WHERE d.status!=\'stale\' AND d.context_version=t.version'
        params=()
        if account:query+=' AND d.account=?';params=(account,)
        rows=self.db.execute(query+' ORDER BY d.created DESC',params).fetchall()
        return [{**{k:r[k] for k in ('id','account','thread','context_version','status','created')},
                 'decision':json.loads(r['content']),'conversation':json.loads(r['snapshot'])} for r in rows]

    def opportunities(self,account=None):
        query='SELECT * FROM opportunities';params=()
        if account:query+=' WHERE account=?';params=(account,)
        return [{**dict(r),'content':json.loads(r['content'])} for r in self.db.execute(query,params)]
