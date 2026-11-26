"""Durable chat receipts; polling never resubmits inference."""
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class ChatJobs:
    def __init__(self, path, runner):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT, state TEXT, progress TEXT, created REAL, result TEXT)')
            c.execute("UPDATE jobs SET state='interrupted', progress='Service restarted; previous outcome requires review. No automatic retry.' WHERE state='running'")

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def get(self, job_id):
        with self.db() as c:
            row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return {**dict(row), 'payload': json.loads(row['payload']),
                'result': json.loads(row['result']) if row['result'] else None}

    def submit(self, job_id, message, mode, target):
        if not re.fullmatch(r'[a-zA-Z0-9-]{16,80}', job_id):
            raise ValueError('A valid operation ID is required.')
        if not message.strip() or len(message) > 20000:
            raise ValueError('Enter a message of 1–20000 characters.')
        if mode not in {'hive_council', 'external_council', 'direct_node'}:
            raise ValueError('Unknown chat mode.')
        payload = json.dumps(dict(message=message, mode=mode, target=target), sort_keys=True)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
            if old:
                if old['payload'] != payload:
                    raise ValueError('Operation ID already belongs to another message.')
                return self.get(job_id)
            active = c.execute("SELECT id FROM jobs WHERE state='running' LIMIT 1").fetchone()
            if active:
                raise RuntimeError('A chat is still running: ' + active['id'])
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
                      (job_id, payload, 'running', 'Accepted; waiting for model work', time.time(), None))
        threading.Thread(target=self._run, args=(job_id, json.loads(payload)), daemon=True).start()
        return self.get(job_id)

    def _run(self, job_id, payload):
        def progress(message):
            with self.db() as c:
                c.execute('UPDATE jobs SET progress=? WHERE id=?', (message, job_id))
        try:
            result = self.runner(**payload, progress=progress)
            success = any(r.get('ok') for r in result.get('responses', []))
            state = 'complete' if success else 'failed'
            note = 'Response ready' if success else 'No model returned an answer; see recorded errors.'
        except Exception as exc:
            result, state, note = None, 'failed', str(exc)
        with self.db() as c:
            c.execute('UPDATE jobs SET state=?, progress=?, result=? WHERE id=?',
                      (state, note, json.dumps(result, ensure_ascii=False), job_id))
