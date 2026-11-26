"""Durable LinkedIn thread snapshots and an outbox. No browser or model side effects."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from urllib.parse import urlparse

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data/linkedin_conversations.sqlite3"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def valid_graph(snapshot):
    messages = {m.get("id"): m for m in snapshot.get("messages", [])}
    post_id = snapshot.get("post_id")
    for message in messages.values():
        current, seen = message.get("id"), set()
        while current != post_id:
            if not current or current in seen or current not in messages:
                return False
            seen.add(current)
            current = messages[current].get("parent_id")
    return True


class ConversationStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    identity_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
                    original TEXT NOT NULL, snapshot TEXT NOT NULL,
                    version TEXT NOT NULL, checked REAL NOT NULL,
                    turns INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
                    source_key TEXT NOT NULL, version TEXT NOT NULL,
                    decision TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(thread_id, source_key));
                CREATE TABLE IF NOT EXISTS handled (
                    thread_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    text_hash TEXT NOT NULL, PRIMARY KEY(thread_id,message_id,text_hash));
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(id),
                    thread_id TEXT NOT NULL REFERENCES threads(id), status TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS leases (
                    scope TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (
                    thread_id TEXT NOT NULL REFERENCES threads(id), version TEXT NOT NULL,
                    snapshot TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(thread_id,version));
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def lease(self, scope="linkedin-browser", seconds=300):
        owner = uuid.uuid4().hex
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM leases WHERE scope=?", (scope,)).fetchone()
            if current and current["expires"] > now:
                return None
            db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)", (scope, owner, now + seconds))
        return owner

    def release(self, owner, scope="linkedin-browser"):
        with self.db() as db:
            db.execute("DELETE FROM leases WHERE scope=? AND owner=?", (scope, owner))

    def renew(self, owner, scope="linkedin-browser", seconds=600):
        with self.db() as db:
            updated = db.execute("UPDATE leases SET expires=? WHERE scope=? AND owner=? AND expires>?",
                                 (time.time() + seconds, scope, owner, time.time()))
            if updated.rowcount != 1:
                raise RuntimeError("Browser lease expired; stop this pass")

    def track(self, snapshot):
        snapshot = json.loads(json.dumps(snapshot))
        if not snapshot.get("account_id"):
            raise ValueError("Account identity is required")
        messages = snapshot.get("messages", [])
        ids = [m.get("id") for m in messages]
        if len(ids) != len(set(ids)):
            raise ValueError("Ambiguous comment identities")
        root = next((m for m in messages if m.get("id") == snapshot.get("root_id")), None)
        complete = bool(snapshot.get("complete") and snapshot.get("post_id") and
                        snapshot.get("post_url") and root and
                        root.get("author_id") == snapshot["account_id"] and
                        all(m.get("id") and m.get("author_id") and m.get("text") for m in messages)
                        and snapshot.get("post_text") and valid_graph(snapshot))
        snapshot["complete"] = complete
        identity = digest([snapshot["account_id"], snapshot.get("post_id") or snapshot.get("post_text"),
                           snapshot.get("root_id") or snapshot.get("own_comment")])
        thread_id = identity[:32]
        version = digest(snapshot)
        with self.db() as db:
            db.execute("INSERT INTO threads(id,account_id,identity_key,state,original,snapshot,version,checked) "
                       "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "snapshot=excluded.snapshot,version=excluded.version,state=excluded.state,checked=excluded.checked",
                       (thread_id, snapshot["account_id"], identity,
                        "active" if complete else "needs_identity_or_context",
                        json.dumps(snapshot), json.dumps(snapshot), version, time.time()))
            db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?)",
                       (thread_id, version, json.dumps(snapshot), time.time()))
        return thread_id

    def refresh(self, thread_id, snapshot):
        old = self.get(thread_id)
        if snapshot.get("account_id") != old["account_id"]:
            raise ValueError("Browser account changed")
        prior = old["snapshot"]
        for field in ("post_id", "root_id"):
            if snapshot.get(field) != prior.get(field):
                raise ValueError("Thread identity changed: " + field)
        if not snapshot.get("complete") or not valid_graph(snapshot):
            with self.db() as db:
                db.execute("UPDATE threads SET state=?,checked=? WHERE id=?",
                           ("incomplete_context", time.time(), thread_id))
            return self.get(thread_id)
        root = next((m for m in snapshot.get("messages", []) if m.get("id") == snapshot["root_id"]), None)
        ids = [m.get("id") for m in snapshot.get("messages", [])]
        if (not root or root.get("author_id") != old["account_id"] or len(ids) != len(set(ids))
                or any(not m.get("id") or not m.get("author_id") for m in snapshot.get("messages", []))):
            raise ValueError("Incomplete or ambiguous message identity")
        with self.db() as db:
            original = old["original"] if old["original"].get("complete") else snapshot
            db.execute("UPDATE threads SET original=?,snapshot=?,version=?,state='active',checked=? WHERE id=?",
                       (json.dumps(original), json.dumps(snapshot), digest(snapshot), time.time(), thread_id))
            db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?)",
                       (thread_id, digest(snapshot), json.dumps(snapshot), time.time()))
        return self.get(thread_id)

    def get(self, thread_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not row:
            raise KeyError(thread_id)
        result = dict(row)
        for key in ("original", "snapshot"):
            result[key] = json.loads(result[key])
        return result

    def threads(self, limit=5):
        with self.db() as db:
            rows = db.execute("SELECT id FROM threads ORDER BY checked ASC LIMIT ?",
                              (max(1, min(int(limit), 20)),)).fetchall()
        return [self.get(row["id"]) for row in rows]

    def pending(self, thread):
        snapshot = thread["snapshot"]
        messages = {m["id"]: m for m in snapshot.get("messages", []) if m.get("id")}
        root_id = snapshot.get("root_id")
        def descends(message_id, ancestor):
            seen = set()
            while message_id in messages and message_id not in seen:
                seen.add(message_id)
                parent = messages[message_id].get("parent_id")
                if parent == ancestor:
                    return True
                message_id = parent
            return False
        with self.db() as db:
            handled = {(r[0], r[1]) for r in db.execute(
                "SELECT message_id,text_hash FROM handled WHERE thread_id=?", (thread["id"],))}
        pending = []
        for m in messages.values():
            if m["author_id"] == thread["account_id"] or not descends(m["id"], root_id):
                continue
            if (m["id"], digest(m.get("text", ""))) in handled:
                continue
            # A human or Sabrina already answered this particular branch.
            if any(other["author_id"] == thread["account_id"] and descends(other["id"], m["id"])
                   for other in messages.values()):
                continue
            pending.append(m)
        return pending

    def save_decision(self, thread, sources, decision):
        source_key = digest([thread["version"], sorted((m["id"], digest(m["text"])) for m in sources)])
        decision_id = digest([thread["id"], source_key])[:32]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT version,state FROM threads WHERE id=?", (thread["id"],)).fetchone()
            if current["version"] != thread["version"] or current["state"] != "active":
                raise ValueError("Context changed while drafting")
            db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?)",
                       (decision_id, thread["id"], source_key, thread["version"], json.dumps(decision), time.time()))
        return decision_id

    def decision(self, decision_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        result = dict(row)
        result["decision"] = json.loads(result["decision"])
        return result

    def mark_handled(self, thread_id, sources):
        with self.db() as db:
            db.executemany("INSERT OR IGNORE INTO handled VALUES(?,?,?)",
                           [(thread_id, m["id"], digest(m["text"])) for m in sources])

    def claim_send(self, decision_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            d = db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
            t = db.execute("SELECT * FROM threads WHERE id=?", (d["thread_id"],)).fetchone()
            if t["version"] != d["version"] or t["state"] != "active":
                raise ValueError("Draft is stale")
            if t["turns"] >= 2:
                raise ValueError("Automatic conversation limit reached")
            if json.loads(d["decision"])["action"] != "reply":
                raise ValueError("Decision does not authorize a reply")
            if db.execute("SELECT 1 FROM outbox WHERE thread_id=? AND status IN ('sending','uncertain')",
                          (d["thread_id"],)).fetchone():
                raise ValueError("An earlier send needs reconciliation")
            # Unique decision_id and an immediate transaction prevent double sends.
            db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?)",
                       (decision_id, decision_id, d["thread_id"], "sending", time.time(), time.time(), "{}"))
        return decision_id

    def finish_send(self, outbox_id, evidence):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
            d = db.execute("SELECT decision,version FROM decisions WHERE id=?", (row["decision_id"],)).fetchone()
            t = db.execute("SELECT account_id,snapshot FROM threads WHERE id=?", (row["thread_id"],)).fetchone()
            decision = json.loads(d["decision"])
            before = db.execute("SELECT snapshot FROM snapshots WHERE thread_id=? AND version=?",
                                (row["thread_id"], d["version"])).fetchone()
            before_ids = {m["id"] for m in json.loads(before[0])["messages"]}
            expected_url = urlparse(json.loads(t["snapshot"])["post_url"])
            actual_url = urlparse(evidence.get("reply_url") or "")
            verified = bool(evidence.get("status") == "sent" and evidence.get("reply_id") and
                            evidence["reply_id"] not in before_ids and
                            evidence.get("parent_id") == decision.get("target_id") and evidence.get("reply_url") and
                            evidence.get("reply_text") == decision.get("comment") and
                            evidence.get("author_id") == t["account_id"] and
                            actual_url.scheme == "https" and actual_url.hostname == expected_url.hostname and
                            actual_url.path == expected_url.path)
            if row["status"] == "sent":
                return
            db.execute("UPDATE outbox SET status=?,updated=?,evidence=? WHERE id=?",
                       ("sent" if verified else "uncertain", time.time(), json.dumps(evidence), outbox_id))
            if verified:
                db.execute("UPDATE threads SET turns=turns+1 WHERE id=?", (row["thread_id"],))
        return verified

    def unresolved(self, thread_id):
        with self.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM outbox WHERE thread_id=? AND status IN ('sending','uncertain')", (thread_id,))]

    def recently_sent(self, thread_id, seconds=600):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM outbox WHERE thread_id=? AND status='sent' AND updated>?",
                                   (thread_id, time.time() - seconds)).fetchone())

    def sources_for_decision(self, decision_id):
        d = self.decision(decision_id)
        with self.db() as db:
            row = db.execute("SELECT snapshot FROM snapshots WHERE thread_id=? AND version=?",
                             (d["thread_id"], d["version"])).fetchone()
        return [m for m in json.loads(row[0])["messages"] if m["id"] in d["decision"]["source_ids"]]

    def has_edited_handled_message(self, thread_id, pending):
        with self.db() as db:
            for message in pending:
                if db.execute("SELECT 1 FROM handled WHERE thread_id=? AND message_id=? AND text_hash<>?",
                              (thread_id, message["id"], digest(message["text"]))).fetchone():
                    return True
        return False

    def status(self):
        with self.db() as db:
            return {"threads": [dict(r) for r in db.execute(
                "SELECT id,account_id,state,checked,turns FROM threads ORDER BY checked DESC")],
                "decisions": [{**dict(r), "decision": json.loads(r["decision"])} for r in db.execute(
                    "SELECT id,thread_id,decision,created FROM decisions ORDER BY created DESC LIMIT 30")],
                "outbox": [dict(r) for r in db.execute(
                    "SELECT * FROM outbox ORDER BY created DESC LIMIT 30")]}
