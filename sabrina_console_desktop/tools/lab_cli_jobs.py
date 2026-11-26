"""Lab-only, bounded read-only jobs over Sabrina's existing audited CLI adapters.

Staged operational entrypoint; never starts a daemon or a desktop application.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import re
import signal
import socket
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MODEL = "huihui_ai/qwen3-abliterated:14b"
CODEX = Path('/home/ego/.local/bin/codex')
AGY = Path('/home/ego/.local/bin/agy')
MAX_PASSES = 3


def mode_options(mode):
    if mode == 'exchange':
        return dict(codex_cli_access='enabled', codex_cli_model='gpt-6-luna',
                    antigravity_cli_access='enabled', antigravity_cli_suite='', cli_exchange_access='enabled')
    if mode == 'coding':
        return dict(codex_cli_access='none', codex_cli_model='',
                    antigravity_cli_access='enabled', antigravity_cli_suite='duration-v1', cli_exchange_access='none')
    raise ValueError('Unknown job mode')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def host_guard():
    if platform.system() != 'Linux' or socket.gethostname().split('.')[0] != 'ego':
        raise ValueError('Lab-only execution requires Linux on ego; no desktop fallback')
    for binary in (CODEX, AGY):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(f'Required CLI missing: {binary}; no fallback')


def fingerprint():
    files = []
    for folder in ('tools', 'missions'):
        for path in sorted((ROOT / folder).rglob('*.py')):
            files.append((str(path.relative_to(ROOT)), hashlib.sha256(path.read_bytes()).hexdigest()))
    for binary in (CODEX, AGY):
        files.append((str(binary), hashlib.sha256(binary.read_bytes()).hexdigest()))
    config = ROOT / 'control-reference.json'
    files.append(('control-reference.json', hashlib.sha256(config.read_bytes()).hexdigest()))
    return digest(files)


@contextlib.contextmanager
def exclusive(path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another managed lab job owns the process lock') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Jobs:
    def __init__(self, dbpath):
        self.path = Path(dbpath)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS managed_cli_jobs(
                operation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                run_id TEXT, source_hash TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, deadline REAL NOT NULL, passes INTEGER NOT NULL DEFAULT 0,
                settings TEXT NOT NULL, error TEXT)''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def get(self, op):
        with self.connect() as db:
            row = db.execute('SELECT * FROM managed_cli_jobs WHERE operation_id=?', (op,)).fetchone()
        if not row:
            raise ValueError('Unknown operation_id')
        return dict(row)

    def reserve(self, op, objective, source, settings, now=None):
        now = time.time() if now is None else now
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', op):
            raise ValueError('operation_id requires 1-100 letters, digits, underscores or hyphens')
        if not objective.strip() or len(objective) > 2000:
            raise ValueError('Objective must contain 1-2000 characters')
        request = digest({'objective': objective, 'settings': settings})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM managed_cli_jobs WHERE operation_id=?', (op,)).fetchone()
            if row:
                if row['request_hash'] != request:
                    raise ValueError('operation_id already exists with different inputs')
                return False
            if db.execute("SELECT 1 FROM managed_cli_jobs WHERE state IN ('creating','running','blocked')").fetchone():
                raise ValueError('Existing job ownership unresolved; inspect before creating another job')
            db.execute('INSERT INTO managed_cli_jobs VALUES(?,?,NULL,?,?, ?,?,0,?,NULL)',
                       (op, request, source, 'creating', now, now + 900, json.dumps(settings)))
        return True

    def update(self, op, **fields):
        if not set(fields) <= {'run_id', 'state', 'passes', 'error'}:
            raise ValueError('Invalid job update')
        with self.connect() as db:
            db.execute('UPDATE managed_cli_jobs SET ' + ','.join(k + '=?' for k in fields)
                       + ' WHERE operation_id=?', (*fields.values(), op))

    def preflight(self, op, source, now=None):
        now = time.time() if now is None else now
        job = self.get(op)
        if job['state'] != 'paused' or not job['run_id']:
            raise ValueError('Only an explicitly paused initialized job may run; reconcile interruptions separately')
        if job['source_hash'] != source:
            raise ValueError('Frozen source, coordinator config or CLI binary changed')
        if job['deadline'] <= now or job['passes'] >= MAX_PASSES:
            raise ValueError('Job time/pass budget exhausted')
        with self.connect() as db:
            if db.execute("SELECT 1 FROM managed_cli_jobs WHERE operation_id!=? AND state IN ('creating','running','blocked')", (op,)).fetchone():
                raise ValueError('Another managed job requires ownership reconciliation')
            run = db.execute('SELECT * FROM assistant_sessions WHERE id=?', (job['run_id'],)).fetchone()
            if not run or run['state'] != 'paused' or run['owner']:
                raise ValueError('Run missing, not paused, or still owned; no lease stealing')
            if not run['desktop_audit_expires'] or run['desktop_audit_expires'] <= now:
                raise ValueError('Run evidence expired; cannot resume')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'delegation_assignments' in tables and db.execute(
                    "SELECT 1 FROM delegation_assignments WHERE objective_id=? AND delivery='uncertain'",
                    (job['run_id'],)).fetchone():
                raise ValueError('Uncertain provider delivery; inspect evidence, never automatically resend')
        return job


def prepare_bridge(dbpath):
    # Applied before import and explicitly after operator configuration loading.
    os.environ.update(SABRINA_ASSISTANT_DB=str(dbpath),
                      SABRINA_CONTROL_CONFIG=str(ROOT / 'control-reference.json'),
                      SABRINA_OLLAMA_BASE_URL='http://127.0.0.1:11434',
                      SABRINA_CODEX_BINARY=str(CODEX), SABRINA_ANTIGRAVITY_CLI=str(AGY),
                      SABRINA_LAB_ASSISTANT_THINK='true', SABRINA_LAB_ASSISTANT_DEVICE='gpu')
    os.environ.pop('SABRINA_CONTROL_URL', None)
    sys.path.insert(0, str(ROOT))
    from tools import sabrina_lab_assistant_mcp as bridge, control_client
    os.environ['SABRINA_LAB_ASSISTANT_THINK'] = 'true'
    os.environ['SABRINA_LAB_ASSISTANT_DEVICE'] = 'gpu'
    if control_client.configured('http://127.0.0.1:11434') is None:
        raise ValueError('Coordinator must manage the explicit local inference endpoint')
    return bridge


def execute_child(dbpath, op):
    os.setsid()
    jobs = Jobs(dbpath)
    try:
        host_guard()
        bridge = prepare_bridge(dbpath)
        job = jobs.get(op)
        if fingerprint() != job['source_hash']:
            raise ValueError('Source changed before execution')
        settings = json.loads(job['settings'])
        mode = settings.get('mode', 'exchange')
        os.environ['SABRINA_VERIFIER_LOCAL'] = '1' if mode == 'coding' else '0'
        bridge.ask_sabrina_lab_assistant(
            run_id=job['run_id'], model=MODEL, max_tokens=4096,
            desktop_access='none', host_access='none',
            **mode_options(mode))
        with jobs.connect() as db:
            run = db.execute('SELECT state,owner FROM assistant_sessions WHERE id=?', (job['run_id'],)).fetchone()
        state = run['state'] if run and not run['owner'] else 'blocked'
        jobs.update(op, state=state if state in ('paused', 'completed', 'expired') else 'blocked')
    except Exception as exc:
        # Do not create a second raw-content retention store from provider exceptions.
        jobs.update(op, state='blocked', error=f'{type(exc).__name__}; inspect retained run audit')


def run_pass(jobs, op):
    job = jobs.preflight(op, fingerprint())
    jobs.update(op, state='running', passes=job['passes'] + 1)
    child = multiprocessing.get_context('fork').Process(target=execute_child, args=(jobs.path, op))
    child.start()
    child.join(max(0, job['deadline'] - time.time()))
    if child.is_alive():
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.join(3)
            if child.is_alive():
                os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.join(5)
        jobs.update(op, state='blocked', error='Deadline exceeded; reconcile worker and provider uncertainty before any further work')
    elif child.exitcode or jobs.get(op)['state'] == 'running':
        jobs.update(op, state='blocked', error='Worker interrupted; no automatic replay or lease release')


def status(jobs, op, include_result=False):
    job = jobs.get(op)
    result = {k: job[k] for k in ('operation_id', 'run_id', 'state', 'passes', 'deadline', 'source_hash', 'settings', 'error')}
    result.update(objective_verification='not_independently_verified', requested_device='gpu', observed_placement='unavailable')
    if job['run_id']:
        with jobs.connect() as db:
            run = db.execute('SELECT * FROM assistant_sessions WHERE id=?', (job['run_id'],)).fetchone()
        if run:
            expired = run['state'] == 'expired' or run['desktop_audit_expires'] <= time.time()
            result.update(run_state=run['state'], run_owned=bool(run['owner']), raw_evidence_available=not expired)
            if expired:
                result.update(state='expired', error=None)
            if include_result and not expired:
                result['report'] = run['answer']
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=['submit', 'resume', 'status', 'result'])
    ap.add_argument('--operation-id', required=True)
    ap.add_argument('--objective-file', type=Path)
    ap.add_argument('--mode', choices=['exchange', 'coding'], default='exchange')
    a = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        host_guard()  # No imports/model/subprocess/DB work on Windows or the wrong node.
        state = ROOT / 'managed-state'
        state.mkdir(exist_ok=True)
        jobs = Jobs(state / 'assistant.sqlite3')
        if a.command in ('status', 'result'):
            bridge = prepare_bridge(jobs.path)
            bridge._get_coordination().expire_desktop_audits()
            result = status(jobs, a.operation_id, a.command == 'result')
        else:
            with exclusive(state / 'worker.lock'):
                source = fingerprint()
                if a.command == 'submit':
                    if not a.objective_file:
                        raise ValueError('submit requires --objective-file')
                    objective = a.objective_file.read_text(encoding='utf-8').strip()
                    settings = dict(model=MODEL, think=True, codex_model='gpt-6-luna',
                                    antigravity_model='provider_default', codex=str(CODEX), antigravity=str(AGY),
                                    owner_node='ego', retention_days=7, desktop_access='none', host_access='none')
                    if a.mode == 'coding':
                        settings.update(mode='coding', suite='duration-v1', verifier_transport='local_fixed_module')
                    fresh = jobs.reserve(a.operation_id, objective, source, settings)
                    if fresh:
                        board = prepare_bridge(jobs.path)._get_coordination()
                        session = board.begin(objective, '', desktop_audit_retention_days=7)
                        board.release(session)
                        jobs.update(a.operation_id, run_id=session['id'], state='paused')
                        run_pass(jobs, a.operation_id)
                else:
                    run_pass(jobs, a.operation_id)
                result = status(jobs, a.operation_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result['state'] in ('paused', 'completed') else 2
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'error': f'{type(exc).__name__}: {exc}'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
