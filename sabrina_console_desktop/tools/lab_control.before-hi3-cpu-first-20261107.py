"""One lab-owned queue for inference, durable reasoning objectives, and resource leases.

All state lives on the service host. Domain journals remain separate. This service
does not give reasoning agents browser, shell, publishing, or source-editing tools.
"""
from __future__ import annotations
import argparse
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from tools.agent_runtime import Store, Runtime, CapacityBusy, load_config, response_schema
from tools.lab_http import LabHTTP

TERMINAL = {'completed', 'failed', 'cancelled'}


def bounded(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{name} requires 1–{maximum} characters')
    return value.strip()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class Control:
    def __init__(self, path, backend=None, config=None, clock=time.time):
        self.store = Store(path)
        self.config = config or load_config()
        self.backend = backend or LabHTTP('http://127.0.0.1:11434', managed=False)
        self.clock = clock
        self.stop = threading.Event()
        self.threads = []
        with self.store.connect(True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS control_requests (
                  id TEXT PRIMARY KEY, client TEXT, project TEXT, operation_id TEXT,
                  digest TEXT, body TEXT, state TEXT, created REAL, deadline REAL,
                  owner TEXT, token TEXT, result TEXT, error TEXT DEFAULT '',
                  available REAL DEFAULT 0, attempts INTEGER DEFAULT 0,
                  UNIQUE(client,project,operation_id));
                CREATE TABLE IF NOT EXISTS control_clients (
                  client TEXT PRIMARY KEY, served REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS control_objectives (
                  id TEXT PRIMARY KEY, client TEXT, project TEXT, operation_id TEXT,
                  digest TEXT, task_id TEXT, run_id TEXT, dependencies TEXT,
                  criteria TEXT, served REAL DEFAULT 0, UNIQUE(client,project,operation_id));
                CREATE TABLE IF NOT EXISTS control_leases (
                  resource TEXT PRIMARY KEY, owner TEXT, token TEXT, expires REAL, state TEXT);
                CREATE TABLE IF NOT EXISTS control_waiters (
                  resource TEXT, owner TEXT, created REAL, expires REAL,
                  PRIMARY KEY(resource,owner));
            ''')

    def event(self, db, item, kind, detail):
        self.store.event(db, item, 'control_' + kind, detail)

    def submit_request(self, body):
        body = dict(body)
        for key in ('client', 'project', 'operation_id'):
            body[key] = bounded(body.get(key), key)
        if body.get('path') not in {'/api/chat', '/api/generate', '/api/embed', '/api/embeddings'}:
            raise ValueError('Unsupported inference operation')
        payload = body.get('payload')
        if not isinstance(payload, dict) or len(encoded(payload)) > 120000:
            raise ValueError('Inference payload must be an object of at most 120000 characters')
        bounded(payload.get('model'), 'model')
        if payload.get('stream') is True:
            raise ValueError('Use queued non-streaming inference')
        options = payload.get('options', {})
        if not isinstance(options, dict):
            raise ValueError('options must be an object')
        for key, default, maximum in [('num_ctx', 8192, 32768), ('num_predict', 1024, 4096)]:
            value = options.get(key, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f'{key} must be between 1 and {maximum}')
        if payload.get('keep_alive') in (0, '0', '0s', -1, '-1'):
            raise ValueError('Model residency is controlled by the shared service')
        seconds = body.setdefault('request_seconds', 150)
        if not isinstance(seconds, (int, float)) or not 10 <= seconds <= 300:
            raise ValueError('request_seconds must be between 10 and 300')
        text = encoded(body)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.store.connect(True) as db:
            old = db.execute('SELECT id,digest FROM control_requests WHERE client=? AND project=? AND operation_id=?',
                             (body['client'], body['project'], body['operation_id'])).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('Operation ID already has different arguments')
                request_id = old['id']
            else:
                pending = db.execute("SELECT count(*) FROM control_requests WHERE state IN ('queued','running','cancelling')").fetchone()[0]
                if pending >= 256:
                    raise ValueError('Lab queue is full; retry this operation ID later')
                request_id = 'inf_' + uuid.uuid4().hex
                db.execute('INSERT OR IGNORE INTO control_clients(client) VALUES(?)', (body['client'],))
                db.execute('''INSERT INTO control_requests(id,client,project,operation_id,digest,body,state,created,deadline)
                    VALUES(?,?,?,?,?,?,'queued',?,?)''',
                    (request_id, body['client'], body['project'], body['operation_id'], digest, text,
                     self.clock(), self.clock() + 1800))
                self.event(db, request_id, 'submitted', {'client': body['client'], 'project': body['project']})
        return self.request_status(request_id)

    def request_status(self, request_id):
        with self.store.connect() as db:
            row = db.execute('SELECT id,client,project,state,created,deadline,result,error,attempts FROM control_requests WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown inference request')
            result = dict(row)
            result['result'] = json.loads(result['result']) if result['result'] else None
            return result

    def cancel_request(self, request_id):
        with self.store.connect(True) as db:
            db.execute("UPDATE control_requests SET state=CASE WHEN state='running' THEN 'cancelling' ELSE 'cancelled' END WHERE id=? AND state IN ('queued','running')", (request_id,))
            self.event(db, request_id, 'cancel_requested', {})
        return self.request_status(request_id)

    def _expire_leases(self, db):
        db.execute("UPDATE control_leases SET state='uncertain' WHERE state='held' AND expires<?", (self.clock(),))
        db.execute('DELETE FROM control_waiters WHERE expires<?', (self.clock(),))

    def acquire(self, resource, owner):
        resource, owner = bounded(resource, 'resource'), bounded(owner, 'owner')
        if resource != 'compute:lab' and not resource.startswith(('browser:', 'workspace:', 'account:')):
            raise ValueError('Unsupported resource')
        with self.store.connect(True) as db:
            self._expire_leases(db)
            db.execute('''INSERT INTO control_waiters VALUES(?,?,?,?) ON CONFLICT(resource,owner)
                DO UPDATE SET expires=excluded.expires''', (resource, owner, self.clock(), self.clock() + 60))
            held = db.execute('SELECT * FROM control_leases WHERE resource=?', (resource,)).fetchone()
            front = db.execute('SELECT owner FROM control_waiters WHERE resource=? ORDER BY created,owner LIMIT 1', (resource,)).fetchone()
            if held or front['owner'] != owner:
                return {'state': 'waiting', 'reason': held['state'] if held else 'earlier waiter'}
            token = uuid.uuid4().hex
            db.execute("INSERT INTO control_leases VALUES(?,?,?,?,'held')", (resource, owner, token, self.clock()+90))
            db.execute('DELETE FROM control_waiters WHERE resource=? AND owner=?', (resource, owner))
            self.event(db, resource, 'lease_acquired', {'owner': owner})
            return {'state': 'held', 'token': token}

    def lease_action(self, action, body):
        resource, owner = body['resource'], body['owner']
        with self.store.connect(True) as db:
            if action == 'withdraw':
                db.execute('DELETE FROM control_waiters WHERE resource=? AND owner=?', (resource, owner))
                return {'state': 'withdrawn'}
            row = db.execute('SELECT * FROM control_leases WHERE resource=? AND owner=? AND token=?',
                             (resource, owner, body['token'])).fetchone()
            if not row:
                raise ValueError('Stale or incorrect lease token')
            if action == 'renew':
                if row['state'] != 'held' or row['expires'] < self.clock():
                    raise ValueError('Expired lease requires reconciliation')
                db.execute('UPDATE control_leases SET expires=? WHERE resource=?', (self.clock()+90, resource))
            elif action == 'release':
                db.execute('DELETE FROM control_leases WHERE resource=?', (resource,))
            elif action == 'quarantine':
                db.execute("UPDATE control_leases SET state='uncertain' WHERE resource=?", (resource,))
            else:
                raise ValueError('Unknown lease action')
            self.event(db, resource, 'lease_' + action, {'owner': owner})
        return {'state': action}

    def claim_request(self):
        with self.store.connect(True) as db:
            self._expire_leases(db)
            db.execute("UPDATE control_requests SET state='failed',error='Queue deadline exceeded' WHERE state='queued' AND deadline<?", (self.clock(),))
            if db.execute("SELECT 1 FROM control_leases WHERE resource='compute:lab'").fetchone():
                return None
            if db.execute("SELECT 1 FROM control_waiters WHERE resource='compute:lab'").fetchone():
                return None
            row = db.execute('''SELECT r.* FROM control_requests r JOIN control_clients c ON c.client=r.client
                WHERE r.state='queued' AND r.available<=? ORDER BY c.served,r.created,r.id LIMIT 1''', (self.clock(),)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE control_requests SET state='running',owner=?,token=?,attempts=attempts+1 WHERE id=?", (token, token, row['id']))
            db.execute('UPDATE control_clients SET served=? WHERE client=?', (self.clock(), row['client']))
            seconds = json.loads(row['body'])['request_seconds']
            db.execute("INSERT INTO control_leases VALUES('compute:lab',?,?,?,'held')", (row['id'], token, self.clock()+seconds+90))
            self.event(db, row['id'], 'claimed', {'client': row['client']})
            return {**dict(row), 'token': token}

    def finish_request(self, request, state, result=None, error=''):
        with self.store.connect(True) as db:
            old = db.execute('SELECT state,token FROM control_requests WHERE id=?', (request['id'],)).fetchone()
            if not old or old['token'] != request['token'] or old['state'] not in {'running', 'cancelling'}:
                return False
            if old['state'] == 'cancelling' and state != 'uncertain':
                state, result = 'cancelled', None
            db.execute('UPDATE control_requests SET state=?,result=?,error=?,available=? WHERE id=?',
                       (state, encoded(result) if result is not None else None, error[:1000], self.clock()+2, request['id']))
            if state == 'uncertain':
                db.execute("UPDATE control_leases SET state='uncertain' WHERE owner=? AND token=?", (request['id'], request['token']))
            else:
                db.execute('DELETE FROM control_leases WHERE owner=? AND token=?', (request['id'], request['token']))
            self.event(db, request['id'], state, {'error': error[:1000]})
            return True

    def admit(self, body):
        """Choose placement conservatively; never explicitly evict another model."""
        payload = json.loads(encoded(body['payload']))
        installed = self.backend.request('/api/tags', timeout=8)['models']
        model = next((m for m in installed if m['name'] == payload['model']), None)
        if model is None:
            raise ValueError('Requested model is not installed')
        residents = self.backend.request('/api/ps', timeout=8)['models']
        options = payload.setdefault('options', {})
        options.setdefault('num_ctx', 8192)
        options.setdefault('num_predict', 1024)
        memory = model['size']/1024**3 * 1.15 + 1.5 * options['num_ctx']/8192
        node = self.config['nodes'][0]
        other_gpu = sum(m.get('size_vram', 0)/1024**3 for m in residents if m['name'] != model['name'])
        device = body.get('device', 'cpu' if options.get('num_gpu') == 0 else 'auto')
        if device not in {'auto', 'cpu', 'gpu'}:
            raise ValueError('Invalid device')
        resident = next((m for m in residents if m['name'] == model['name']), None)
        if resident and not resident.get('size_vram', 0):
            if device == 'gpu':
                raise CapacityBusy('Waiting for existing CPU residency to expire before GPU placement')
            if device == 'auto':
                if memory > node.get('auto_cpu_max_gib', 8):
                    raise CapacityBusy('Large CPU resident is outside automatic CPU policy')
                device = 'cpu'
        # On the lab, include unrelated physical usage in admission decisions.
        gpu_limit, ram_limit = node['gpu_gib'], node['ram_gib']
        if os.name != 'nt' and Path('/proc/meminfo').exists():
            import subprocess
            ram = {line.split(':')[0]: line.split(':')[1].strip() for line in Path('/proc/meminfo').read_text().splitlines()}
            ram_limit = min(ram_limit, int(ram['MemAvailable'].split()[0])/1024**2)
            gpu = subprocess.run(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
            if gpu.returncode == 0 and gpu.stdout.strip():
                resident_gpu = sum(m.get('size_vram', 0)/1024**3 for m in residents)
                gpu_limit = min(gpu_limit, float(gpu.stdout.splitlines()[0])/1024 + resident_gpu)
            else:
                gpu_limit = 0
        if device != 'cpu' and other_gpu + memory <= gpu_limit:
            options['num_gpu'] = 999
        elif device == 'cpu' or (device == 'auto' and memory <= node.get('auto_cpu_max_gib', 8)):
            if memory > ram_limit:
                raise CapacityBusy('Waiting for RAM capacity')
            # A resident GPU copy may be active in an unmanaged caller. Do not migrate it.
            if any(m['name'] == model['name'] and m.get('size_vram', 0) for m in residents):
                raise CapacityBusy('Waiting for existing GPU residency to expire before CPU placement')
            options['num_gpu'] = 0
            options['num_thread'] = node['cpu_threads']
        else:
            raise CapacityBusy('Waiting for GPU capacity; no eligible CPU fallback')
        payload['stream'] = False
        payload['keep_alive'] = '60s'
        return payload

    def inference_step(self):
        request = self.claim_request()
        if request is None:
            return False
        body = json.loads(request['body'])
        try:
            payload = self.admit(body)
        except (CapacityBusy, OSError) as exc:
            self.finish_request(request, 'queued', error=str(exc))
            return False
        except Exception as exc:
            self.finish_request(request, 'failed', error=str(exc))
            return True
        try:
            with self.store.connect(True) as db:
                self.event(db, request['id'], 'placement_selected', {
                    'model': payload['model'], 'num_gpu': payload['options']['num_gpu'],
                    'num_ctx': payload['options']['num_ctx']})
            result = self.backend.request(body['path'], payload, timeout=body['request_seconds'])
        except Exception as exc:
            # A client timeout is not proof that Ollama stopped computing.
            self.finish_request(request, 'uncertain', error=str(exc))
            return True
        self.finish_request(request, 'completed', result=result)
        return True

    def submit_objective(self, body):
        body = dict(body)
        for key in ('client', 'project', 'operation_id'):
            body[key] = bounded(body.get(key), key)
        body['goal'] = bounded(body.get('goal'), 'goal', 2000)
        context = body.setdefault('context', '')
        if not isinstance(context, str) or len(context) > self.config['limits']['max_prompt_chars']//2:
            raise ValueError('Context exceeds objective budget')
        tier, device = body.setdefault('tier', 'fast'), body.setdefault('device', 'auto')
        if tier not in {'tiny', 'fast', 'smart'} or device not in {'auto', 'gpu', 'cpu'}:
            raise ValueError('Invalid model tier or device')
        dependencies, criteria = body.setdefault('depends_on', []), body.setdefault('success_criteria', [])
        if not isinstance(dependencies, list) or len(dependencies) > 20 or not all(isinstance(v, str) for v in dependencies):
            raise ValueError('depends_on requires up to 20 objective IDs')
        if not isinstance(criteria, list) or len(criteria) > 10 or not all(isinstance(v, str) and 1 <= len(v) <= 300 for v in criteria):
            raise ValueError('success_criteria requires up to 10 bounded strings')
        digest = hashlib.sha256(encoded(body).encode()).hexdigest()
        with self.store.connect(True) as db:
            old = db.execute('SELECT * FROM control_objectives WHERE client=? AND project=? AND operation_id=?',
                             (body['client'], body['project'], body['operation_id'])).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('Objective operation ID already has different arguments')
                objective_id = old['id']
            else:
                for dependency in dependencies:
                    if not db.execute('SELECT 1 FROM control_objectives WHERE id=? AND project=?', (dependency, body['project'])).fetchone():
                        raise ValueError('Dependencies must be existing objectives in this project')
                active = db.execute("SELECT count(*) FROM control_objectives o JOIN agent_tasks t ON t.id=o.task_id WHERE t.state NOT IN ('completed','failed','cancelled')").fetchone()[0]
                if active >= 128:
                    raise ValueError('Objective queue is full')
                objective_id, run_id = 'obj_' + uuid.uuid4().hex, uuid.uuid4().hex
                limits = self.config['limits']
                db.execute('INSERT INTO agent_runs(id,deadline,limits) VALUES(?,?,?)',
                           (run_id, self.clock()+limits['deadline_seconds'], encoded(limits)))
                task_id = self.store._insert(db, run_id, None, 0, body['goal'], context, tier, device)
                db.execute('INSERT INTO control_objectives VALUES(?,?,?,?,?,?,?,?,?,0)',
                    (objective_id, body['client'], body['project'], body['operation_id'], digest, task_id, run_id,
                     encoded(dependencies), encoded(criteria)))
                self.event(db, objective_id, 'objective_submitted', {'root_task': task_id})
        return self.objective_status(objective_id)

    def objective_status(self, objective_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM control_objectives WHERE id=?', (objective_id,)).fetchone()
            if not row:
                raise ValueError('Unknown objective')
            root = self.store.get(row['task_id'])
            pending = [dict(r) for r in db.execute('''SELECT r.id,r.state,r.error FROM control_requests r
                JOIN agent_tasks t ON r.operation_id=t.control_key WHERE t.run_id=?''', (row['run_id'],))]
            run = dict(db.execute('SELECT calls,deadline,limits FROM agent_runs WHERE id=?', (row['run_id'],)).fetchone())
        run['limits'] = json.loads(run['limits'])
        return {'task_id': objective_id, 'root_task_id': root['id'], 'run_id': row['run_id'],
                'client': row['client'], 'project': row['project'], 'state': root['state'],
                'result': root['result'], 'depends_on': json.loads(row['dependencies']),
                'success_criteria': json.loads(row['criteria']), 'tasks': self.store.snapshot(row['run_id']),
                'inference': pending, 'budget': run, 'execution': 'bounded_reasoning',
                'verification': 'Result requires domain verification; completed means reasoning returned'}

    def objective_step(self, runtime):
        with self.store.connect(True) as db:
            db.execute("UPDATE agent_tasks SET state='failed',result='Run deadline exceeded' WHERE state IN ('queued','waiting') AND run_id IN (SELECT id FROM agent_runs WHERE deadline<?)", (self.clock(),))
            db.execute("""UPDATE control_requests SET state=CASE WHEN state='running' THEN 'cancelling' ELSE 'cancelled' END
                WHERE state IN ('queued','running') AND operation_id IN
                (SELECT t.control_key FROM agent_tasks t JOIN agent_runs r ON r.id=t.run_id WHERE r.deadline<?)""", (self.clock(),))
            rows = db.execute('''SELECT o.*,t.state FROM control_objectives o JOIN agent_tasks t ON t.id=o.task_id
                WHERE t.state NOT IN ('completed','failed','cancelled') ORDER BY o.served,o.id''').fetchall()
            chosen = None
            for row in rows:
                dependencies = json.loads(row['dependencies'])
                states = [db.execute('SELECT t.state FROM control_objectives o JOIN agent_tasks t ON t.id=o.task_id WHERE o.id=?', (d,)).fetchone()[0] for d in dependencies]
                if any(s in {'failed', 'cancelled'} for s in states):
                    db.execute("UPDATE agent_tasks SET state='failed',result='Dependency did not complete successfully' WHERE id=?", (row['task_id'],))
                    continue
                if any(s != 'completed' for s in states):
                    continue
                if not db.execute("SELECT 1 FROM agent_tasks WHERE run_id=? AND state IN ('queued','waiting')", (row['run_id'],)).fetchone():
                    continue
                chosen = dict(row)
                db.execute('UPDATE control_objectives SET served=? WHERE id=?', (self.clock(), row['id']))
                break
        if chosen:
            return runtime.step(chosen['run_id'])
        return False

    def cancel_objective(self, objective_id):
        status = self.objective_status(objective_id)
        self.store.cancel(status['root_task_id'])
        for request in status['inference']:
            self.cancel_request(request['id'])
        return self.objective_status(objective_id)

    def recover(self):
        """Called only under the singleton server lock, never by an ordinary reader."""
        with self.store.connect(True) as db:
            rows = db.execute("SELECT id FROM control_requests WHERE state IN ('running','cancelling')").fetchall()
            for row in rows:
                db.execute("UPDATE control_requests SET state='uncertain',error='Service restarted during inference; reconcile before retry' WHERE id=?", (row['id'],))
                db.execute("UPDATE control_leases SET state='uncertain' WHERE owner=?", (row['id'],))
                self.event(db, row['id'], 'interrupted', {})
            # Reattach reasoning to its durable inference key. Never repeat a completed call.
            db.execute("UPDATE agent_tasks SET state='queued',owner=NULL WHERE state='running' AND run_id IN (SELECT run_id FROM control_objectives)")

    def reconcile(self, body):
        if body.get('confirmed_stopped') is not True:
            raise ValueError('Confirm the previous executor has stopped before releasing its reservation')
        resource = bounded(body.get('resource'), 'resource')
        with self.store.connect(True) as db:
            row = db.execute("SELECT * FROM control_leases WHERE resource=? AND state='uncertain'", (resource,)).fetchone()
            if not row:
                raise ValueError('No uncertain reservation for this resource')
            db.execute("UPDATE control_requests SET state='queued',available=?,error='',token=NULL WHERE id=? AND state='uncertain'", (self.clock(), row['owner']))
            db.execute('DELETE FROM control_leases WHERE resource=?', (resource,))
            self.event(db, resource, 'operator_reconciled', {'owner': row['owner']})
        return {'state': 'released'}

    def status(self):
        with self.store.connect() as db:
            requests = []
            for row in db.execute('SELECT id,client,project,state,error,created,attempts,body FROM control_requests ORDER BY created DESC LIMIT 100'):
                value = dict(row)
                body = json.loads(value.pop('body'))
                value.update(model=body['payload'].get('model'), requested_device=body.get('device', 'auto'),
                             task_label=body.get('task_label', ''), placement='not selected')
                event = db.execute("SELECT detail FROM agent_events WHERE task_id=? AND kind='control_placement_selected' ORDER BY id DESC LIMIT 1", (value['id'],)).fetchone()
                if event:
                    placement = json.loads(event['detail'])
                    value['placement'] = 'GPU' if placement.get('num_gpu', 0) else 'CPU'
                requests.append(value)
            return {'requests': requests,
                    'objectives': [dict(r) for r in db.execute('SELECT o.id,o.client,o.project,t.goal,t.state FROM control_objectives o JOIN agent_tasks t ON t.id=o.task_id ORDER BY t.created DESC LIMIT 100')],
                    'leases': [dict(r) for r in db.execute('SELECT resource,owner,expires,state FROM control_leases')],
                    'inference_concurrency': 1, 'unmanaged_callers_isolated': False}

    def start(self):
        self.recover()
        def infer_loop():
            while not self.stop.is_set():
                try:
                    self.inference_step()
                except Exception as exc:
                    with self.store.connect(True) as db:
                        self.event(db, 'service', 'worker_error', {'error': str(exc)[:1000]})
                self.stop.wait(0.2)
        def objective_loop():
            runtime = Runtime(self.config, self.store.path, fleet_factory=lambda c,s: GatewayFleet(self,c,s))
            while not self.stop.is_set():
                try:
                    self.objective_step(runtime)
                except Exception as exc:
                    with self.store.connect(True) as db:
                        self.event(db, 'service', 'objective_worker_error', {'error': str(exc)[:1000]})
                self.stop.wait(0.2)
        for target in [infer_loop] + [objective_loop]*3:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)


class GatewayFleet:
    """Run existing reasoning trees through the same queue as every other caller."""
    def __init__(self, control, config, store):
        self.control, self.config, self.store = control, config, store

    def generate(self, task, messages):
        with self.store.connect(True) as db:
            current = db.execute('SELECT * FROM agent_tasks WHERE id=?', (task['id'],)).fetchone()
            run = db.execute('SELECT * FROM agent_runs WHERE id=?', (task['run_id'],)).fetchone()
            objective = db.execute('SELECT * FROM control_objectives WHERE run_id=?', (task['run_id'],)).fetchone()
            if current['state'] != 'running':
                raise RuntimeError('Task cancelled')
            key = current['control_key']
            if not key:
                limits = json.loads(run['limits'])
                if run['calls'] >= limits['max_calls'] or run['deadline'] < self.control.clock():
                    raise ValueError('Reasoning budget exhausted')
                key = task['id'] + ':' + str(current['rounds']+1)
                db.execute('UPDATE agent_tasks SET control_key=?,rounds=rounds+1 WHERE id=?', (key,task['id']))
                db.execute('UPDATE agent_runs SET calls=calls+1 WHERE id=?', (task['run_id'],))
        limits = self.config['limits']
        model = self.config['tiers'][task['tier']][0]
        evidence = json.loads(messages[-1]['content'])
        evidence['success_criteria'] = json.loads(objective['criteria'])
        evidence['dependency_results'] = [self.control.objective_status(d)['result'] for d in json.loads(objective['dependencies'])]
        messages = [messages[0], {'role':'user', 'content': encoded(evidence)}]
        if len(encoded(messages)) > limits['max_prompt_chars']:
            raise ValueError('Combined dependency evidence exceeds prompt budget')
        if len(encoded(messages).encode('utf-8')) + limits['num_predict'] + 512 > limits['num_ctx']:
            raise ValueError('Combined dependency evidence exceeds context window')
        receipt = self.control.submit_request({'client': objective['client'], 'project': objective['project'],
            'operation_id': key, 'path': '/api/chat', 'device': task['device'],
            'request_seconds': limits['request_seconds'], 'payload': {'model': model,
                'messages': messages, 'format': response_schema(task['tier']), 'stream': False,
                'think': False, 'options': {'num_ctx': limits['num_ctx'], 'num_predict': limits['num_predict'], 'temperature': 0.1}}})
        if receipt['state'] == 'completed':
            result = receipt['result']
            if not result.get('done') or result.get('done_reason') == 'length':
                raise ValueError('Model response incomplete')
            return result['message']['content']
        if receipt['state'] in {'failed','cancelled'}:
            raise RuntimeError('Inference failed: ' + receipt['error'])
        # Yield the reasoning worker while inference is queued; all objectives can advance.
        raise CapacityBusy('Waiting for shared inference ' + receipt['id'] + ': ' + receipt['state'])


def create_app(control, token, run_workers=True):
    @asynccontextmanager
    async def lifespan(app):
        if run_workers:
            control.start()
        yield
        control.stop.set()
    app = FastAPI(title='Sabrina Lab Coordinator', lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)
    def authorize(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
        if not credentials or not hmac.compare_digest(credentials.credentials, token):
            raise HTTPException(401, 'Coordinator token required')
    def invoke(fn, *args):
        try:
            return fn(*args)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
    @app.get('/health')
    def health():
        return {'service': 'sabrina-lab-coordinator', 'version': 1}
    protected = [Depends(authorize)]
    @app.get('/v1/status', dependencies=protected)
    def status():
        return control.status()
    @app.get('/v1/code/search', dependencies=protected)
    def code_search(language: str | None = None, kind: str | None = None,
                    nested: bool | None = None, min_parameters: int | None = None,
                    max_parameters: int | None = None, async_function: bool | None = None,
                    limit: int = Query(20, ge=1, le=50)):
        from tools.sanitized_code_index import search
        if language not in {None, 'python', 'go', 'javascript', 'yaml'}:
            raise HTTPException(400, 'Unsupported language filter')
        if kind not in {None, 'class', 'function', 'mapping', 'sequence', 'scalar'}:
            raise HTTPException(400, 'Unsupported declaration kind')
        if (min_parameters is not None and min_parameters < 0) or (max_parameters is not None and max_parameters < 0):
            raise HTTPException(400, 'Parameter counts must be nonnegative')
        return {'records': search(language, kind, nested, min_parameters, max_parameters,
                                  async_function, limit)}
    @app.post('/v1/inference', dependencies=protected)
    def infer(body: dict):
        return invoke(control.submit_request, body)
    @app.get('/v1/inference/{request_id}', dependencies=protected)
    def inference_status(request_id: str):
        return invoke(control.request_status, request_id)
    @app.post('/v1/inference/{request_id}/cancel', dependencies=protected)
    def cancel_inference(request_id: str):
        return invoke(control.cancel_request, request_id)
    @app.post('/v1/objectives', dependencies=protected)
    def objective(body: dict):
        return invoke(control.submit_objective, body)
    @app.get('/v1/objectives/{objective_id}', dependencies=protected)
    def objective_status(objective_id: str):
        return invoke(control.objective_status, objective_id)
    @app.post('/v1/objectives/{objective_id}/cancel', dependencies=protected)
    def cancel_objective(objective_id: str):
        return invoke(control.cancel_objective, objective_id)
    @app.post('/v1/leases/acquire', dependencies=protected)
    def acquire(body: dict):
        return invoke(control.acquire, body.get('resource'), body.get('owner'))
    @app.post('/v1/leases/reconcile', dependencies=protected)
    def reconcile(body: dict):
        return invoke(control.reconcile, body)
    @app.post('/v1/leases/{action}', dependencies=protected)
    def lease_action(action: str, body: dict):
        return invoke(control.lease_action, action, body)
    return app


def main():
    if os.name == 'nt':
        raise SystemExit('Run the coordinator on the Linux lab host; Windows uses control_cli')
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8090)
    parser.add_argument('--db', default='data/control/coordinator.sqlite3')
    parser.add_argument('--token-file', default='data/control/client.token')
    args = parser.parse_args()
    token_path = Path(args.token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if not token_path.exists():
        with token_path.open('x') as stream:
            stream.write(secrets.token_urlsafe(32))
        if os.name != 'nt':
            token_path.chmod(0o600)
    # Prevent a second server from performing startup recovery on live tasks.
    lock = Path(args.db).with_suffix('.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        import uvicorn
        uvicorn.run(create_app(Control(args.db), token_path.read_text().strip()), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
