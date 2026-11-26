"""Bridge checkpoints and an adapter to agent_runtime.Store/Runtime.

Separate database prevents other controllers from claiming private lab-only work.
This module never queues scanner jobs or starts an unrestricted agent loop.
"""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from pathlib import Path
from tools.agent_runtime import OllamaFleet, Runtime, Store
from tools.run_measurement import ensure_measurement_schema, evaluate_and_save_run, expire_measurement_rows
from tools.sabrina_host_execution import ensure_host_exec_schema, expire_host_exec_rows


class RunAlreadyActiveError(ValueError):
    """Raised when attempting to resume a run that is actively held by a lease owner."""
    def __init__(self, run_id: str, owner: str, lease_until: float, retry_after_seconds: float):
        self.run_id = run_id
        self.owner = owner
        self.lease_until = lease_until
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__(
            f"Research run {run_id!r} is already active (owner {owner[:8] if owner else 'unknown'}; "
            f"expires in {self.retry_after_seconds:.1f}s)"
        )

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "status": "busy",
            "error": "RUN_ALREADY_ACTIVE",
            "run_id": self.run_id,
            "lease_owner": self.owner,
            "lease_until": self.lease_until,
            "retry_after_seconds": round(self.retry_after_seconds, 1),
            "message": str(self),
        }
class Coordination:
    def __init__(self, path, runtime_factory=None):
        self.remote_enabled = runtime_factory is None
        self.store = Store(path)
        self.runtime_factory = runtime_factory or (lambda: Runtime(db_path=path, fleet_factory=OllamaFleet))
        self._runtime = None
        with self.store.connect(True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS assistant_sessions (
                    id TEXT PRIMARY KEY, task TEXT NOT NULL, context TEXT NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    messages TEXT NOT NULL DEFAULT '[]', refs TEXT NOT NULL DEFAULT '[]',
                    progress TEXT NOT NULL DEFAULT '', answer TEXT NOT NULL DEFAULT '',
                    owner TEXT, lease_until REAL NOT NULL DEFAULT 0, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_submissions (
                    operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL, task_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_decision_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    created REAL NOT NULL, completed REAL, request TEXT NOT NULL,
                    metadata TEXT NOT NULL, response TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS assistant_decision_run ON assistant_decision_audit(run_id,id);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(assistant_sessions)')}
            additions = {
                'model': "TEXT NOT NULL DEFAULT ''",
                'requested_device': "TEXT NOT NULL DEFAULT ''",
                'activity': "TEXT NOT NULL DEFAULT ''",
                'verified_result': "TEXT NOT NULL DEFAULT ''",
                'verification_evidence': "TEXT NOT NULL DEFAULT ''",
                'created': "REAL NOT NULL DEFAULT 0",
                'desktop_audit_retention_days': "INTEGER NOT NULL DEFAULT 0",
                'desktop_audit_expires': "REAL NOT NULL DEFAULT 0",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE assistant_sessions ADD COLUMN {name} {declaration}")
            db.execute("UPDATE assistant_sessions SET created=updated WHERE created=0")
            ensure_measurement_schema(db)
            ensure_host_exec_schema(db)

    @staticmethod
    def _expire_audit_rows(db, run_ids, now):
        if not run_ids:
            return
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for run_id in run_ids:
            db.execute('''UPDATE assistant_sessions SET state='expired',task='',context='',
                messages='[]',refs='[]',progress='',answer='',owner=NULL,lease_until=0,
                verified_result='',verification_evidence='',activity='Desktop audit expired; raw content erased',
                updated=? WHERE id=?''', (now, run_id))
            for table in ('assistant_desktop_audit_events', 'assistant_desktop_refs',
                          'assistant_desktop_windows', 'assistant_desktop_elements',
                          'assistant_desktop_proposals', 'assistant_desktop_approvals',
                          'assistant_decision_audit'):
                if table in tables:
                    db.execute(f'DELETE FROM {table} WHERE run_id=?', (run_id,))
            if 'agent_events' in tables:
                db.execute('DELETE FROM agent_events WHERE task_id=?', (run_id,))
            if 'assistant_cross_app_checkpoints' in tables:
                db.execute("UPDATE assistant_cross_app_checkpoints SET content='',evidence_refs='[]',checklist_json='[]' WHERE run_id=?", (run_id,))
            if 'assistant_cross_app_routes' in tables:
                db.execute("UPDATE assistant_cross_app_routes SET rationale='',expected_contribution='' WHERE run_id=?", (run_id,))
            if 'assistant_cross_app_handoffs' in tables:
                db.execute("UPDATE assistant_cross_app_handoffs SET prompt='',summary='',error_detail='',conversation_key='' WHERE run_id=?", (run_id,))
            if 'assistant_cross_app_reviews' in tables:
                db.execute("UPDATE assistant_cross_app_reviews SET state='expired',updated=? WHERE run_id=?", (now, run_id))
        from tools.agent_desktop_extended import expire_artifacts
        expire_artifacts(db, run_ids)
        expire_measurement_rows(db, run_ids, now)
        expire_host_exec_rows(db, run_ids, now)
        from tools.sabrina_antigravity_cli import expire_cli_rows
        expire_cli_rows(db, run_ids)

    def expire_desktop_audits(self, now=None):
        now = time.time() if now is None else float(now)
        with self.store.connect(True) as db:
            rows = db.execute('''SELECT id FROM assistant_sessions
                WHERE desktop_audit_expires>0 AND desktop_audit_expires<=? AND state!='expired' ''',
                (now,)).fetchall()
            run_ids = [row['id'] for row in rows]
            self._expire_audit_rows(db, run_ids, now)
        return run_ids

    def ensure_desktop_audit_retention(self, run_id, days=None):
        if days is not None and (type(days) is not int or not 1 <= days <= 365):
            raise ValueError('desktop audit retention must be between 1 and 365 days')
        now = time.time()
        with self.store.connect(True) as db:
            row = db.execute('''SELECT state,desktop_audit_retention_days,desktop_audit_expires
                FROM assistant_sessions WHERE id=?''', (run_id,)).fetchone()
            if row is None or row['state'] == 'expired':
                raise ValueError('Research run is unavailable for desktop audit')
            if row['desktop_audit_retention_days']:
                if days is not None and days != row['desktop_audit_retention_days']:
                    raise ValueError('Desktop audit retention is fixed when the run starts')
                return row['desktop_audit_retention_days'], row['desktop_audit_expires']
            selected_days = days or 7
            expires = now + selected_days * 86400
            db.execute('''UPDATE assistant_sessions SET desktop_audit_retention_days=?,desktop_audit_expires=?
                WHERE id=?''', (selected_days, expires, run_id))
            return selected_days, expires

    def decision_start(self, session, payload, metadata):
        """Save the exact bridge request before dispatch; coordinator options may differ."""
        with self.store.connect(True) as db:
            row = db.execute('SELECT state,owner,desktop_audit_expires FROM assistant_sessions WHERE id=?',
                             (session['id'],)).fetchone()
            if row is None or not row['desktop_audit_expires']:
                return None
            if row['state'] == 'expired' or row['desktop_audit_expires'] <= time.time() or row['owner'] != session['owner']:
                raise ValueError('Decision audit requires an active, unexpired run')
            return db.execute('''INSERT INTO assistant_decision_audit(run_id,created,request,metadata)
                VALUES(?,?,?,?)''', (session['id'],time.time(),json.dumps(payload,ensure_ascii=False),
                                    json.dumps(metadata,ensure_ascii=False))).lastrowid

    def decision_finish(self, event_id, response=None, error=None):
        if event_id is None:
            return
        response = response or {}
        message = response.get('message', {})
        # Record observable output/tool choices and performance counters only.
        # Hidden reasoning fields are deliberately excluded from this audit.
        visible = {key: response[key] for key in ('model','created_at','done','done_reason',
            'total_duration','load_duration','prompt_eval_count','prompt_eval_duration',
            'eval_count','eval_duration') if key in response}
        visible['message'] = {key: message[key] for key in ('role','content','tool_calls') if key in message}
        with self.store.connect(True) as db:
            db.execute('UPDATE assistant_decision_audit SET completed=?,response=?,error=? WHERE id=?',
                       (time.time(),json.dumps(visible,ensure_ascii=False),error,event_id))

    def decision_events(self, run_id, after_id=0, limit=1):
        self.expire_desktop_audits()
        with self.store.connect() as db:
            row = db.execute('SELECT state,desktop_audit_expires FROM assistant_sessions WHERE id=?', (run_id,)).fetchone()
            if row is None or row['state'] == 'expired' or row['desktop_audit_expires'] <= time.time():
                raise ValueError('Decision audit is unavailable or expired')
            rows = db.execute('''SELECT * FROM assistant_decision_audit
                WHERE run_id=? AND id>? ORDER BY id LIMIT ?''',
                (run_id,max(0,int(after_id)),max(1,min(5,int(limit))))).fetchall()
        return [{**dict(row), **{key:json.loads(row[key]) if row[key] else None
                 for key in ('request','response','metadata')}} for row in rows]

    def verify_external_result(self, run_id, summary, evidence_path):
        """External supervisor only; deliberately absent from model tools."""
        path = Path(evidence_path).resolve()
        root = Path(__file__).resolve().parents[1] / 'artifacts'
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Verification requires a saved observation under project artifacts')
        evidence = json.loads(path.read_text(encoding='utf-8'))
        if evidence.get('run_id') != run_id or evidence.get('observation',{}).get('ok') is not True:
            raise ValueError('Evidence must be a successful observation for this research run')
        if not summary or len(summary)>2000:
            raise ValueError('A bounded supervisor verification summary is required')
        with self.store.connect(True) as db:
            row = db.execute('SELECT state FROM assistant_sessions WHERE id=?',(run_id,)).fetchone()
            if row is None or row['state'] in {'running', 'expired'}:
                raise ValueError('Research must be stopped before external verification')
            if db.execute("SELECT 1 FROM assistant_desktop_proposals WHERE run_id=? AND state IN ('pending','executing')",(run_id,)).fetchone():
                raise ValueError('Resolve outstanding desktop actions before verification')
            db.execute("UPDATE assistant_sessions SET state='completed',activity='External result verified',verified_result=?,verification_evidence=?,updated=? WHERE id=?",
                       (summary,str(path),time.time(),run_id))
            self.store.event(db,run_id,'external_result_verified',{'summary':summary,'evidence':str(path)})
            evaluate_and_save_run(db, run_id, Path(__file__).resolve().parents[1])
        return {'run_id':run_id,'verified_result':summary,'evidence':str(path)}

    def activity(self, session, model, device, activity):
        with self.store.connect(True) as db:
            db.execute('UPDATE assistant_sessions SET model=?,requested_device=?,activity=?,updated=? WHERE id=? AND owner=?',
                       (model, device, activity[:500], time.time(), session['id'], session['owner']))

    def pause_for_operator(self, run_id, reason):
        if not reason or len(reason)>1000:
            raise ValueError('A bounded operator blocker is required')
        with self.store.connect(True) as db:
            changed=db.execute("UPDATE assistant_sessions SET state='paused',activity=?,updated=? WHERE id=? AND owner IS NULL AND verified_result='' AND state!='expired'",
                               ('Blocked: '+reason,time.time(),run_id)).rowcount
            if changed != 1:
                raise ValueError('Run must be stopped and unverified before marking a blocker')
            self.store.event(db,run_id,'operator_blocker',{'reason':reason})
            evaluate_and_save_run(db, run_id, Path(__file__).resolve().parents[1])
        return {'run_id':run_id,'state':'paused','operator_blocker':reason}

    def begin(self, task, context, run_id=None, *, desktop_audit_retention_days=None):
        owner = uuid.uuid4().hex
        self.expire_desktop_audits()
        with self.store.connect(True) as db:
            if run_id:
                row = db.execute('SELECT * FROM assistant_sessions WHERE id=?', (run_id,)).fetchone()
                if row is None:
                    raise ValueError('Unknown research run_id')
                if row['state'] == 'expired':
                    raise ValueError('Research run expired; raw desktop content was erased and this run cannot be resumed')
                if (task and task != row['task']) or (context and context != row['context']):
                    raise ValueError('Resume requires the original task/context or empty values')
                if row['state'] == 'completed':
                    return dict(row)
                if desktop_audit_retention_days is not None and row['desktop_audit_retention_days']:
                    if int(desktop_audit_retention_days) != row['desktop_audit_retention_days']:
                        raise ValueError('Desktop audit retention is fixed when the run starts')
                if row['owner'] and row['lease_until'] > time.time():
                    raise RunAlreadyActiveError(
                        run_id=run_id,
                        owner=row['owner'],
                        lease_until=float(row['lease_until']),
                        retry_after_seconds=float(row['lease_until']) - time.time(),
                    )
            else:
                if not task:
                    raise ValueError('A task is required for a new research run')
                if desktop_audit_retention_days is not None and not 1 <= int(desktop_audit_retention_days) <= 365:
                    raise ValueError('desktop audit retention must be between 1 and 365 days')
                run_id = uuid.uuid4().hex
                now = time.time()
                retention_days = int(desktop_audit_retention_days or 0)
                audit_expires = now + retention_days * 86400 if retention_days else 0
                db.execute('''INSERT INTO assistant_sessions
                    (id,task,context,state,updated,created,desktop_audit_retention_days,desktop_audit_expires)
                    VALUES(?,?,?,?,?,?,?,?)''',
                    (run_id, task, context, 'paused', now, now, retention_days, audit_expires))
            now = time.time()
            db.execute("UPDATE assistant_sessions SET owner=?,lease_until=?,state='running',activity='resumed',updated=? WHERE id=?",
                       (owner, now + 1800, now, run_id))
            return dict(db.execute('SELECT * FROM assistant_sessions WHERE id=?', (run_id,)).fetchone())

    def save(self, session, messages, refs, state='running', answer=''):
        serialized = json.dumps(messages, ensure_ascii=False)
        if len(serialized) > 120_000:
            raise ValueError('Research history budget exhausted; start a new run with a summary')
        with self.store.connect(True) as db:
            changed = db.execute('''UPDATE assistant_sessions SET messages=?,refs=?,state=?,answer=?,
                revision=revision+1,owner=?,lease_until=?,updated=?
                WHERE id=? AND revision=? AND owner=?''',
                (serialized, json.dumps(refs), state, answer,
                 session['owner'] if state == 'running' else None,
                 time.time() + 1800 if state == 'running' else 0, time.time(),
                 session['id'], session['revision'], session['owner'])).rowcount
            if changed != 1:
                raise ValueError('Research checkpoint conflict; reload the run')
            if state != 'running':
                db.execute('UPDATE assistant_sessions SET activity=? WHERE id=?', (state, session['id']))
            session['revision'] += 1
            self.store.event(db, session['id'], 'research_checkpoint',
                             {'revision': session['revision'], 'state': state})
            if state != 'running':
                evaluate_and_save_run(db, session['id'], Path(__file__).resolve().parents[1])

    def release(self, session):
        with self.store.connect(True) as db:
            db.execute("UPDATE assistant_sessions SET owner=NULL,lease_until=0,state='paused' WHERE id=? AND owner=?",
                       (session['id'], session['owner']))

    def ledger_events(self, run_id, limit=50):
        with self.store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'agent_events' not in tables:
                return []
            rows = db.execute(
                'SELECT id, task_id, time, kind, detail FROM agent_events WHERE task_id=? ORDER BY id DESC LIMIT ?',
                (run_id, max(1, min(200, int(limit)))),
            ).fetchall()
        events = []
        for row in reversed(rows):
            item = dict(row)
            try:
                item['detail'] = json.loads(item['detail']) if item.get('detail') else {}
            except Exception:
                pass
            events.append(item)
        return events

    def approval_records(self, run_id):
        with self.store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'assistant_desktop_approvals' not in tables:
                return []
            rows = db.execute(
                'SELECT * FROM assistant_desktop_approvals WHERE run_id=? ORDER BY id',
                (run_id,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item['target_paths'] = json.loads(item['target_paths']) if item.get('target_paths') else []
            item['dialog_text'] = json.loads(item['dialog_text']) if item.get('dialog_text') else []
            item['one_time_selected'] = bool(item.get('one_time_selected'))
            out.append(item)
        return out

    def record_monitor_event(self, run_id, status, detail=None):
        now = time.time()
        payload = {'status': status, **(detail or {})}
        with self.store.connect(True) as db:
            self.store.event(db, run_id, 'desktop_monitor_status', payload)
            db.execute(
                "UPDATE assistant_sessions SET activity=?, updated=? WHERE id=? AND state!='expired'",
                (f"Monitor: {status}"[:500], now, run_id),
            )
        return payload

    def read(self, run_id):
        self.expire_desktop_audits()
        with self.store.connect() as db:
            row = db.execute('SELECT id,state,revision,refs,progress,updated,created,desktop_audit_retention_days,desktop_audit_expires FROM assistant_sessions WHERE id=?',
                             (run_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown research run_id')
            result = dict(row)
        result['evidence_references'] = json.loads(result.pop('refs'))
        result['run_id'] = result.pop('id')
        result['resumable'] = result['state'] not in {'completed', 'expired'}
        result['ledger_events'] = self.ledger_events(run_id, limit=25)
        result['approvals'] = self.approval_records(run_id)
        return result

    def note(self, run_id, owner, progress, detail=None):
        if not isinstance(progress, str) or not 1 <= len(progress) <= 2000:
            raise ValueError('Progress must contain 1-2000 characters of metadata, no source or secrets')
        with self.store.connect(True) as db:
            changed = db.execute('''UPDATE assistant_sessions SET progress=?,updated=?
                WHERE id=? AND owner=? AND state='running' AND lease_until>?''',
                (progress, time.time(), run_id, owner, time.time())).rowcount
            if changed != 1:
                raise ValueError('No active research lease for this checkpoint')
            event_payload = {'saved': True, 'progress': progress[:500]}
            if isinstance(detail, dict):
                event_payload.update(detail)
            self.store.event(db, run_id, 'research_progress', event_payload)
        return {'run_id': run_id, 'progress_saved': True}

    @property
    def runtime(self):
        if self._runtime is None:
            self._runtime = self.runtime_factory()
        return self._runtime

    def submit(self, goal, context, operation_id, tier='fast', device='auto',
               client_id='assistant-bridge', project='research', depends_on=None, success_criteria=None):
        if self.remote_enabled:
            from tools.control_client import configured
            client = configured()
            if client:
                client.client = client_id
                client.project = project
                extra = {}
                if depends_on is not None: extra['depends_on'] = depends_on
                if success_criteria is not None: extra['success_criteria'] = success_criteria
                return client.submit(goal, context, operation_id, tier, device, **extra)
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            raise ValueError('operation_id must contain 1-128 characters')
        runtime = self.runtime
        if tier not in {'tiny', 'fast', 'smart'} or device not in {'auto', 'cpu', 'gpu'}:
            raise ValueError('Invalid tier or device')
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
            raise ValueError('Goal must contain 1-2000 characters')
        if not isinstance(context, str) or len(context) > runtime.config['limits']['max_prompt_chars'] // 2:
            raise ValueError('Evidence exceeds context budget')
        goal = goal.strip()
        digest = hashlib.sha256(json.dumps([goal, context, tier, device]).encode()).hexdigest()
        with self.store.connect(True) as db:
            existing = db.execute('SELECT * FROM assistant_submissions WHERE operation_id=?', (operation_id,)).fetchone()
            if existing:
                if existing['digest'] != digest:
                    raise ValueError('operation_id was already used with different arguments')
                task_id = existing['task_id']
            else:
                run_id = uuid.uuid4().hex
                limits = runtime.config['limits']
                db.execute('INSERT INTO agent_runs(id,deadline,limits) VALUES(?,?,?)',
                           (run_id, time.time() + limits['deadline_seconds'], json.dumps(limits)))
                task_id = self.store._insert(db, run_id, None, 0, goal, context, tier, device)
                db.execute('INSERT INTO assistant_submissions VALUES(?,?,?)', (operation_id, digest, task_id))
        return self.status(task_id)

    def _owned_task(self, task_id):
        task = self.store.get(task_id)
        with self.store.connect() as db:
            owned = db.execute('''SELECT 1 FROM assistant_submissions s JOIN agent_tasks t ON t.id=s.task_id
                WHERE t.run_id=?''', (task['run_id'],)).fetchone()
        if not owned:
            raise ValueError('Task was not submitted through this bridge')
        return task

    def status(self, run_or_task_id: str, include_result: bool = False) -> dict:
        """Read-only status for an assistant research run or runtime task."""
        if str(run_or_task_id).startswith('obj_'):
            from tools.control_client import configured
            client = configured()
            if not client:
                raise ValueError('Shared coordinator is not configured')
            result = client.status(run_or_task_id)
            if not include_result:
                result.pop('result', None)
                result['tasks'] = [{k:v for k,v in t.items() if k not in {'result','goal'}} for t in result['tasks']]
            return result
        # Check assistant_sessions first for research runs
        with self.store.connect() as db:
            row = db.execute(
                'SELECT id, state, owner, lease_until, activity, task, model, requested_device, '
                'progress, revision, updated, created, desktop_audit_retention_days, desktop_audit_expires '
                'FROM assistant_sessions WHERE id=?',
                (run_or_task_id,),
            ).fetchone()
        if row is not None:
            now = time.time()
            data = dict(row)
            lease_until = float(data.get('lease_until') or 0.0)
            is_active = bool(data.get('owner') and lease_until > now and data.get('state') == 'running')
            retry_after = max(0.0, lease_until - now) if is_active else 0.0
            return {
                "ok": True,
                "kind": "assistant_session",
                "run_id": data["id"],
                "state": data["state"],
                "active": is_active,
                "lease_owner": data["owner"] if is_active else None,
                "lease_until": lease_until if is_active else 0.0,
                "retry_after_seconds": round(retry_after, 1),
                "activity": data["activity"],
                "progress": data["progress"],
                "revision": data["revision"],
                "model": data["model"],
                "requested_device": data["requested_device"],
                "updated": data["updated"],
                "created": data["created"],
                "desktop_audit_retention_days": data["desktop_audit_retention_days"],
                "desktop_audit_expires": data["desktop_audit_expires"],
            }
        root = self._owned_task(run_or_task_id)
        keys = ('id', 'run_id', 'parent', 'state', 'rounds', 'tier', 'device')
        tasks = [{key: task[key] for key in keys} for task in self.store.snapshot(root['run_id'])]
        result = {'task_id': run_or_task_id, 'run_id': root['run_id'], 'state': root['state'],
                  'tasks': tasks, 'execution': 'bounded_reasoning', 'inference': 'lab_local_only'}
        if include_result:
            result['result'] = root['result']
        return result

    def step(self, task_id):
        if task_id.startswith('obj_'):
            return self.status(task_id)
        task = self._owned_task(task_id)
        if task['state'] not in {'completed', 'failed', 'cancelled'}:
            self.runtime.step(task['run_id'])
        return self.status(task_id)

    def cancel(self, task_id):
        if task_id.startswith('obj_'):
            from tools.control_client import configured
            client = configured()
            if not client:
                raise ValueError('Shared coordinator is not configured')
            return client.cancel(task_id)
        self._owned_task(task_id)
        self.store.cancel(task_id)
        return self.status(task_id)
