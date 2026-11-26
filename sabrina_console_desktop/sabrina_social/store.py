"""Durable audit log, operation journal, and approval queue for Sabrina Social."""
from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import sqlite3
import time
import uuid


class SocialStore:
    def __init__(self, path: str | Path):
        from .availability_support import backup_database
        backup_database(path, migration='availability-v1')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, decided REAL,
                    result TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
                    detail TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contacts (
                    contact_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    profile_url TEXT, stage TEXT NOT NULL DEFAULT 'new',
                    notes TEXT NOT NULL DEFAULT '', updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, content_hash TEXT, result TEXT, error TEXT,
                    linkedin_activity_id TEXT, linkedin_url TEXT, verified_text_hash TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(id),
                    phase TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operation_events_operation
                    ON operation_events(operation_id,id);
                CREATE TABLE IF NOT EXISTS leases (
                    name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_intents (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, phase TEXT NOT NULL,
                    operation_id TEXT, executor TEXT, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS action_targets (
                    target TEXT PRIMARY KEY, intent_id TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pulse_rotation_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cursor_index INTEGER NOT NULL DEFAULT 0,
                    last_task TEXT,
                    last_status TEXT,
                    last_reason TEXT,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_applications (
                    job_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    job_url TEXT NOT NULL,
                    easy_apply INTEGER NOT NULL DEFAULT 0,
                    match_score INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    draft_note TEXT,
                    error TEXT,
                    receipt TEXT,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS operation_events_no_update
                    BEFORE UPDATE ON operation_events BEGIN
                    SELECT RAISE(ABORT, 'operation events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS operation_events_no_delete
                    BEFORE DELETE ON operation_events BEGIN
                    SELECT RAISE(ABORT, 'operation events are append-only'); END;
                """
            )
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            # Opening a journal is not evidence that another controller has died.
            # Ambiguous operations remain recorded for explicit reconciliation.

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        if not row:
            return None
        value = dict(row)
        for field in ("payload", "result", "detail"):
            if value.get(field):
                value[field] = json.loads(value[field])
        return value

    def audit(self, event: str, detail: dict):
        with self.db() as db:
            db.execute("INSERT INTO audit(event,detail,created) VALUES(?,?,?)",
                       (event, json.dumps(detail, ensure_ascii=False), time.time()))

    @staticmethod
    def text_hash(text: str | None) -> str | None:
        return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None

    def begin_operation(self, kind: str, payload: dict, content: str | None = None) -> dict:
        operation_id, now = uuid.uuid4().hex, time.time()
        with self.db() as db:
            db.execute(
                "INSERT INTO operations(id,kind,status,payload,content_hash,created,updated) "
                "VALUES(?,?,?,?,?,?,?)",
                (operation_id, kind, "running", json.dumps(payload, ensure_ascii=False),
                 self.text_hash(content), now, now),
            )
            db.execute(
                "INSERT INTO operation_events(operation_id,phase,detail,created) VALUES(?,?,?,?)",
                (operation_id, "operation_started", json.dumps({"kind": kind}), now),
            )
        return self.operation(operation_id)

    def claim_intent(self, kind, key, payload):
        from tools.worker_identity import executor_identity
        ident = self.text_hash(kind + ':' + key)
        fingerprint = self.text_hash(json.dumps(payload, sort_keys=True, ensure_ascii=True))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM action_intents WHERE id=?', (ident,)).fetchone()
            if old and (old['fingerprint'] != fingerprint or old['phase'] != 'not_submitted'):
                return {'id': ident, 'claimed': False, 'phase': old['phase']}
            db.execute('INSERT OR REPLACE INTO action_intents VALUES(?,?,?,NULL,?,?)',
                       (ident, fingerprint, 'prepared', json.dumps(executor_identity(key)), time.time()))
        return {'id': ident, 'claimed': True, 'phase': 'prepared'}

    def intent_phase(self, ident, phase, operation_id=None):
        with self.db() as db:
            db.execute('UPDATE action_intents SET phase=?,operation_id=COALESCE(?,operation_id),updated=? WHERE id=?',
                       (phase, operation_id, time.time(), ident))

    def claim_action_target(self, target, intent_id):
        """Written immediately before submission; even a new pulse cannot repeat this target."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM action_targets WHERE target=?', (target,)).fetchone():
                raise RuntimeError('Social action target already attempted; outcome verification required')
            db.execute('INSERT INTO action_targets VALUES(?,?,?)', (target, intent_id, time.time()))

    def operation_event(self, operation_id: str, phase: str, detail: dict | None = None):
        """Append and fsync a phase before the corresponding browser side effect."""
        now = time.time()
        with self.db() as db:
            exists = db.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not exists:
                raise KeyError(operation_id)
            db.execute(
                "INSERT INTO operation_events(operation_id,phase,detail,created) VALUES(?,?,?,?)",
                (operation_id, phase, json.dumps(detail or {}, ensure_ascii=False), now),
            )
            db.execute("UPDATE operations SET updated=? WHERE id=?", (now, operation_id))

    def finish_operation(self, operation_id: str, status: str, result: dict | None = None,
                         error: str | None = None):
        if status not in {"completed", "failed", "unverified", "interrupted"}:
            raise ValueError("Invalid terminal operation status")
        result = result or {}
        now = time.time()
        activity_id = result.get("linkedin_activity_id")
        linkedin_url = result.get("linkedin_url")
        verified_hash = result.get("verified_text_hash")
        with self.db() as db:
            db.execute(
                "UPDATE operations SET status=?,result=?,error=?,linkedin_activity_id=?,"
                "linkedin_url=?,verified_text_hash=?,updated=? WHERE id=? AND status='running'",
                (status, json.dumps(result, ensure_ascii=False), error, activity_id, linkedin_url,
                 verified_hash, now, operation_id),
            )
            db.execute(
                "INSERT INTO operation_events(operation_id,phase,detail,created) VALUES(?,?,?,?)",
                (operation_id, "operation_finished",
                 json.dumps({"status": status, "error": error}, ensure_ascii=False), now),
            )

    def operation(self, operation_id: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            events = db.execute(
                "SELECT id,phase,detail,created FROM operation_events WHERE operation_id=? ORDER BY id",
                (operation_id,),
            ).fetchall()
        value = self._decode(row)
        if value is not None:
            value["events"] = [self._decode(event) for event in events]
        return value

    def operations(self, limit: int = 100, status: str | None = None) -> list[dict]:
        query, args = "SELECT id FROM operations", []
        if status:
            query += " WHERE status=?"
            args.append(status)
        query += " ORDER BY created DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self.db() as db:
            ids = [row["id"] for row in db.execute(query, args).fetchall()]
        return [self.operation(value) for value in ids]

    def claim_lease(self, name: str, seconds: int) -> str | None:
        owner, now = uuid.uuid4().hex, time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM leases WHERE name=?", (name,)).fetchone()
            if current and current["expires"] > now:
                return None
            db.execute("INSERT OR REPLACE INTO leases(name,owner,expires) VALUES(?,?,?)",
                       (name, owner, now + max(30, int(seconds))))
        return owner

    def renew_lease(self, name: str, owner: str, seconds: int) -> bool:
        with self.db() as db:
            changed = db.execute(
                "UPDATE leases SET expires=? WHERE name=? AND owner=? AND expires>?",
                (time.time() + max(30, int(seconds)), name, owner, time.time()),
            )
        return changed.rowcount == 1

    def release_lease(self, name: str, owner: str):
        with self.db() as db:
            db.execute("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))

    def request(self, action: str, payload: dict) -> dict:
        request_id = uuid.uuid4().hex
        now = time.time()
        with self.db() as db:
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)",
                       (request_id, action, json.dumps(payload), "pending", now, None, None, None))
        self.audit("approval_requested", {"id": request_id, "action": action})
        return self.get(request_id)

    def request_scheduled(self, action: str, payload: dict, job_id: str) -> dict:
        """Record an explicit one-time approval attached to one scheduled job."""
        request_id, now = uuid.uuid4().hex, time.time()
        value = {**payload, "scheduled_job_id": job_id}
        with self.db() as db:
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)",
                       (request_id, action, json.dumps(value, ensure_ascii=False),
                        "scheduled_approved", now, now, None, None))
        self.audit("scheduled_action_approved", {
            "id": request_id, "action": action, "scheduled_job_id": job_id,
            "payload_hash": self.text_hash(json.dumps(value, sort_keys=True, ensure_ascii=False)),
        })
        return self.get(request_id)

    def get(self, request_id: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (request_id,)).fetchone()
        return self._decode(row)

    def list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        query, args = "SELECT * FROM approvals", []
        if status:
            query += " WHERE status=?"
            args.append(status)
        query += " ORDER BY created DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self.db() as db:
            return [self._decode(row) for row in db.execute(query, args).fetchall()]

    def claim(self, request_id: str, expected_status: str = "pending") -> dict | None:
        """Atomically claim a pending request so it can execute only once."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE approvals SET status='executing',decided=? WHERE id=? AND status=?",
                (time.time(), request_id, expected_status),
            )
            row = db.execute("SELECT * FROM approvals WHERE id=?", (request_id,)).fetchone()
        return self._decode(row) if changed.rowcount == 1 else None

    def reject(self, request_id: str) -> bool:
        with self.db() as db:
            changed = db.execute(
                "UPDATE approvals SET status='rejected',decided=? WHERE id=? AND status='pending'",
                (time.time(), request_id),
            )
        if changed.rowcount:
            self.audit("approval_rejected", {"id": request_id})
        return changed.rowcount == 1

    def cancel_scheduled_approval(self, request_id: str) -> bool:
        with self.db() as db:
            changed = db.execute(
                "UPDATE approvals SET status='rejected',decided=?,error=? "
                "WHERE id=? AND status='scheduled_approved'",
                (time.time(), "Scheduled operation cancelled", request_id),
            )
        if changed.rowcount:
            self.audit("scheduled_action_cancelled", {"id": request_id})
        return changed.rowcount == 1

    def finish(self, request_id: str, result: dict | None = None, error: str | None = None):
        status = "failed" if error else "completed"
        with self.db() as db:
            db.execute("UPDATE approvals SET status=?,result=?,error=? WHERE id=? AND status='executing'",
                       (status, json.dumps(result) if result is not None else None, error, request_id))
        self.audit("approval_executed", {"id": request_id, "status": status})

    def get_rotation_state(self, task_list: list[str] | None = None) -> dict:
        tasks = task_list or [
            "engage_feed", "x_creator_pulse", "jobs_apply", "x_creator_pulse",
            "cybersecurity_scout", "x_creator_pulse", "triage_inbox", "x_creator_pulse",
            "ai_eval_scout", "x_creator_pulse", "jobs_apply", "x_creator_pulse",
            "jobs_discover", "create_post",
        ]
        with self.db() as db:
            row = db.execute("SELECT * FROM pulse_rotation_state WHERE id=1").fetchone()
        if not row:
            return {
                "cursor_index": 0,
                "next_task": tasks[0] if tasks else "engage_feed",
                "last_task": None,
                "last_status": None,
                "last_reason": None,
                "updated": None,
                "task_wheel": tasks,
            }
        idx = int(row["cursor_index"] or 0) % max(1, len(tasks))
        return {
            "cursor_index": idx,
            "next_task": tasks[idx] if tasks else "engage_feed",
            "last_task": row["last_task"],
            "last_status": row["last_status"],
            "last_reason": row["last_reason"],
            "updated": row["updated"],
            "task_wheel": tasks,
        }

    def advance_rotation_state(self, task_list: list[str], executed_task: str,
                               last_status: str, last_reason: str = "") -> dict:
        now = time.time()
        state = self.get_rotation_state(task_list)
        next_idx = (int(state["cursor_index"]) + 1) % max(1, len(task_list))
        with self.db() as db:
            db.execute(
                """INSERT INTO pulse_rotation_state(id, cursor_index, last_task, last_status, last_reason, updated)
                   VALUES(1, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       cursor_index=excluded.cursor_index,
                       last_task=excluded.last_task,
                       last_status=excluded.last_status,
                       last_reason=excluded.last_reason,
                       updated=excluded.updated""",
                (next_idx, executed_task, last_status, last_reason, now),
            )
        return self.get_rotation_state(task_list)

    def upsert_job_application(self, job: dict) -> dict:
        now = time.time()
        job_id = str(job["job_id"]).strip()
        receipt = json.dumps(job["receipt"], ensure_ascii=False) if isinstance(job.get("receipt"), dict) else job.get("receipt")
        with self.db() as db:
            db.execute(
                """INSERT INTO job_applications(
                       job_id, title, company, location, job_url, easy_apply,
                       match_score, status, draft_note, error, receipt, created, updated
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET
                       title=excluded.title,
                       company=excluded.company,
                       location=COALESCE(excluded.location, job_applications.location),
                       job_url=excluded.job_url,
                       easy_apply=excluded.easy_apply,
                       match_score=MAX(excluded.match_score, job_applications.match_score),
                       status=excluded.status,
                       draft_note=COALESCE(excluded.draft_note, job_applications.draft_note),
                       error=excluded.error,
                       receipt=COALESCE(excluded.receipt, job_applications.receipt),
                       updated=excluded.updated""",
                (
                    job_id,
                    str(job.get("title") or "Untitled Role")[:300],
                    str(job.get("company") or "Unknown Company")[:200],
                    str(job.get("location") or "")[:200],
                    str(job.get("job_url") or "")[:500],
                    1 if job.get("easy_apply") else 0,
                    int(job.get("match_score") or 0),
                    str(job.get("status") or "discovered"),
                    job.get("draft_note"),
                    job.get("error"),
                    receipt,
                    float(job.get("created") or now),
                    now,
                ),
            )
            row = db.execute("SELECT * FROM job_applications WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else {}

    def list_job_applications(self, status: str | None = None, limit: int = 100) -> list[dict]:
        query, args = "SELECT * FROM job_applications", []
        if status:
            query += " WHERE status=?"
            args.append(status)
        query += " ORDER BY updated DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self.db() as db:
            rows = [dict(r) for r in db.execute(query, args).fetchall()]
        for r in rows:
            if r.get("receipt") and isinstance(r["receipt"], str):
                try:
                    r["receipt"] = json.loads(r["receipt"])
                except Exception:
                    pass
        return rows

    def job_funnel_counts(self) -> dict:
        counts = {"discovered": 0, "staged": 0, "approved": 0, "submitted": 0, "skipped": 0, "failed": 0, "total": 0}
        with self.db() as db:
            for row in db.execute("SELECT status, COUNT(*) AS cnt FROM job_applications GROUP BY status").fetchall():
                st = str(row["status"] or "discovered")
                cnt = int(row["cnt"] or 0)
                counts[st] = counts.get(st, 0) + cnt
                counts["total"] += cnt
        return counts


