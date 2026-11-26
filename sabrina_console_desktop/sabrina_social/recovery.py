"""Durable, bounded infrastructure recovery. Never stores credentials or commands."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid


class RecoveryJournal:
    def __init__(self, path, clock=time.time, max_attempts=4, budget_seconds=300,
                 infrastructure=False, cooldown_seconds=300):
        from .availability_support import backup_database
        backup_database(path, migration='availability-v1')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.max_attempts = max_attempts
        self.budget_seconds = budget_seconds
        self.infrastructure, self.cooldown_seconds = infrastructure, cooldown_seconds
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS incidents (
                    resource TEXT PRIMARY KEY, id TEXT, started REAL, deadline REAL,
                    attempts INTEGER, next_try REAL, state TEXT);
                CREATE TABLE IF NOT EXISTS recovery_events (
                    id INTEGER PRIMARY KEY, incident TEXT, created REAL, phase TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS pulse_guard (
                    name TEXT PRIMARY KEY, id TEXT, state TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS recovery_cooldowns (
                    resource TEXT PRIMARY KEY, until REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pulse_executors (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, resource, action, diagnosis):
        """Consume a persisted attempt before any side effect, with crash backoff."""
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM incidents WHERE resource=?", (resource,)).fetchone()
            if row and row['state'] == 'operator_required':
                return {'status': 'operator_required', 'incident': row['id']}
            if row is None or row["state"] == "resolved":
                db.execute("INSERT OR REPLACE INTO incidents VALUES(?,?,?,?,?,?,?)",
                           (resource, uuid.uuid4().hex, now, now + self.budget_seconds, 0, now, "active"))
                row = db.execute("SELECT * FROM incidents WHERE resource=?", (resource,)).fetchone()
            if row["attempts"] >= self.max_attempts or now >= row["deadline"]:
                if self.infrastructure:
                    cooldown = db.execute('SELECT until FROM recovery_cooldowns WHERE resource=?', (resource,)).fetchone()
                    if cooldown and now >= cooldown['until']:
                        db.execute('UPDATE incidents SET id=?,started=?,deadline=?,attempts=0,next_try=?,state=? WHERE resource=?',
                                   (uuid.uuid4().hex, now, now + self.budget_seconds, now, 'active', resource))
                        db.execute('DELETE FROM recovery_cooldowns WHERE resource=?', (resource,))
                        row = db.execute('SELECT * FROM incidents WHERE resource=?', (resource,)).fetchone()
                    else:
                        until = cooldown['until'] if cooldown else now + self.cooldown_seconds
                        db.execute('INSERT OR IGNORE INTO recovery_cooldowns VALUES(?,?)', (resource, until))
                        db.execute("UPDATE incidents SET state='exhausted' WHERE resource=?", (resource,))
                        return {'status': 'recovery_cooldown', 'incident': row['id'],
                                'attempts': row['attempts'], 'next_try': until, 'retry_after': max(0, until-now)}
            if row["attempts"] >= self.max_attempts or now >= row["deadline"]:
                if row["state"] != "exhausted":
                    db.execute("INSERT INTO recovery_events(incident,created,phase,detail) VALUES(?,?,?,?)",
                               (row["id"], now, "escalated", json.dumps({"status": "recovery_exhausted"})))
                db.execute("UPDATE incidents SET state='exhausted' WHERE resource=?", (resource,))
                return {"status": "recovery_exhausted", "incident": row["id"], "attempts": row["attempts"]}
            if now < row["next_try"]:
                return {"status": "recovery_backoff", "incident": row["id"], "retry_after": row["next_try"] - now}
            attempt = row["attempts"] + 1
            # Reserve longer than the bounded executor; a crashed process cannot
            # immediately overlap its orphaned repair on restart.
            db.execute("UPDATE incidents SET attempts=?,next_try=? WHERE resource=?",
                       (attempt, now + 180, resource))
            detail = {"action": action, "diagnosis": diagnosis, "attempt": attempt}
            db.execute("INSERT INTO recovery_events(incident,created,phase,detail) VALUES(?,?,?,?)",
                       (row["id"], now, "attempt_started", json.dumps(detail)))
            return {"status": "approved", "incident": row["id"], "attempt": attempt,
                    "deadline": row["deadline"], "remaining_seconds": row["deadline"] - now}

    def finish(self, resource, receipt, result, verified=False):
        now = self.clock()
        with self.db() as db:
            db.execute("INSERT INTO recovery_events(incident,created,phase,detail) VALUES(?,?,?,?)",
                       (receipt["incident"], now, "attempt_verified", json.dumps(result)))
            delay = min(60, 5 * 2 ** (receipt["attempt"] - 1))
            db.execute("UPDATE incidents SET state=?,next_try=? WHERE resource=? AND id=?",
                       ("resolved" if verified else "active", now + delay, resource, receipt["incident"]))

    def resolve(self, resource):
        with self.db() as db:
            db.execute("UPDATE incidents SET state='resolved' WHERE resource=?", (resource,))
            db.execute('DELETE FROM recovery_cooldowns WHERE resource=?', (resource,))

    def require_operator(self, resource, reason):
        with self.db() as db:
            row = db.execute('SELECT id FROM incidents WHERE resource=?', (resource,)).fetchone()
            if not row:
                now = self.clock()
                db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,?)',
                           (resource, uuid.uuid4().hex, now, now, 0, now, 'operator_required'))
                row = db.execute('SELECT id FROM incidents WHERE resource=?', (resource,)).fetchone()
            db.execute("UPDATE incidents SET state='operator_required' WHERE resource=?", (resource,))
            db.execute('INSERT INTO recovery_events(incident,created,phase,detail) VALUES(?,?,?,?)',
                       (row['id'], self.clock(), 'operator_required', json.dumps({'reason': reason})))

    def snapshot(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT i.*,c.until AS cooldown_until FROM incidents i '
                                               'LEFT JOIN recovery_cooldowns c USING(resource)')]

    def claim_pulse(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM pulse_guard WHERE name='social'", ()).fetchone()
            if row and row["state"] not in {"completed", "reconciled"}:
                return None
            ident = uuid.uuid4().hex
            db.execute("INSERT OR REPLACE INTO pulse_guard VALUES('social',?,'started',?)", (ident, self.clock()))
            from tools.worker_identity import executor_identity
            db.execute('INSERT OR REPLACE INTO pulse_executors VALUES(?,?)',
                       (ident, json.dumps(executor_identity('pulse:' + ident))))
            return ident

    def bind_pulse_executor(self, ident, identity):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO pulse_executors VALUES(?,?)', (ident, json.dumps(identity)))

    def reconcile_stopped_pulse(self):
        """Release orchestration only. Individual uncertain social intents stay blocked."""
        from tools.worker_identity import stopped_evidence
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT g.*,e.identity FROM pulse_guard g LEFT JOIN pulse_executors e ON g.id=e.id "
                             "WHERE g.name='social'").fetchone()
            if not row or row['state'] in {'completed', 'reconciled'}:
                return {'status': 'clear'}
            if not row['identity']:
                return {'status': 'operator_required', 'reason': 'legacy_pulse_executor'}
            proof = stopped_evidence(json.loads(row['identity']))
            if not proof['stopped']:
                return {'status': 'blocked', **proof}
            db.execute("UPDATE pulse_guard SET state='reconciled',updated=? WHERE name='social' AND id=?",
                       (self.clock(), row['id']))
            db.execute('INSERT INTO recovery_events(incident,created,phase,detail) VALUES(?,?,?,?)',
                       (row['id'], self.clock(), 'executor_reconciled', json.dumps(proof)))
            return {'status': 'reconciled', 'pulse_id': row['id'], 'evidence': proof}

    def finish_pulse(self, ident, success):
        with self.db() as db:
            db.execute("UPDATE pulse_guard SET state=?,updated=? WHERE name='social' AND id=?",
                       ("completed" if success else "uncertain", self.clock(), ident))

    def reconcile_pulse(self, ident, reason, executor_stopped=False):
        """Release an uncertain pulse only after its old executor is confirmed stopped."""
        reason = str(reason or "").strip()
        if executor_stopped is not True or not reason:
            raise ValueError("Reconciliation requires confirmed executor shutdown and an audit reason")
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,state FROM pulse_guard WHERE name='social'", ()).fetchone()
            if not row or row["id"] != ident or row["state"] != "uncertain":
                raise ValueError("Pulse guard is not the expected uncertain run")
            db.execute("UPDATE pulse_guard SET state='reconciled',updated=? WHERE name='social' AND id=?",
                       (now, ident))
            db.execute("INSERT INTO recovery_events(incident,created,phase,detail) VALUES(?,?,?,?)",
                       (ident, now, "pulse_reconciled",
                        json.dumps({"executor_stopped": True, "reason": reason}, ensure_ascii=False)))
        return {"status": "reconciled", "id": ident, "previous_result": "uncertain"}


@contextmanager
def recovery_lease():
    """Repair must coordinate with live browser work; no offline fallback."""
    from tools.control_client import configured, resource_lease
    if configured() is None:
        raise RuntimeError("Recovery requires the shared coordinator")
    with resource_lease("browser:linkedin-primary"):
        yield
