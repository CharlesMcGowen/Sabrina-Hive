"""Durable coordination over existing research runs and audited handoffs.

Only WorkflowWorker invokes the trusted verifier. Model-facing operations never
accept a verdict. Desktop apps return proposed artifacts, not workspace edits.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager

from tools.sabrina_workflow_verifier import digest, public_contract


APPS = ("Claude.exe", "Antigravity.exe", "Codex.exe")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data/sabrina_assistant.sqlite3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_objectives (
 id TEXT PRIMARY KEY, goal TEXT NOT NULL, state TEXT NOT NULL,
 contract TEXT NOT NULL, contract_hash TEXT NOT NULL, created REAL NOT NULL,
 deadline REAL NOT NULL, expires REAL NOT NULL, next_wake REAL NOT NULL DEFAULT 0,
 experimental INTEGER NOT NULL, external_stop TEXT NOT NULL DEFAULT 'not_requested');
CREATE TABLE IF NOT EXISTS workflow_tasks (
 id TEXT PRIMARY KEY, objective_id TEXT NOT NULL, contract TEXT NOT NULL,
 contract_hash TEXT NOT NULL, dependencies TEXT NOT NULL, optional INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL, verification_state TEXT NOT NULL DEFAULT 'unverified',
 skip_reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_attempts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT UNIQUE, parent_id TEXT,
 state TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'not_submitted',
 operation_id TEXT UNIQUE NOT NULL, marker TEXT UNIQUE NOT NULL,
 handoff_id TEXT UNIQUE, app TEXT, conversation_key TEXT,
 expected_prompt TEXT NOT NULL, prompt_hash TEXT NOT NULL,
 revision_hash TEXT, manifest TEXT, artifact_path TEXT, reply_event_id INTEGER,
 correction TEXT NOT NULL DEFAULT '{}', failure_kind TEXT,
 playbook_version TEXT NOT NULL DEFAULT 'experimental-proposed-artifact-v1',
 harness_hash TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL,
 tool_calls INTEGER NOT NULL DEFAULT 0, submit_event_id INTEGER);
CREATE TABLE IF NOT EXISTS workflow_verifications (
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, artifact_hash TEXT NOT NULL,
 suite_hash TEXT NOT NULL, verdict TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, objective_id TEXT NOT NULL,
 task_id TEXT, attempt_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS workflow_event_objective ON workflow_events(objective_id,id);
CREATE TABLE IF NOT EXISTS workflow_leases (
 resource TEXT PRIMARY KEY, owner TEXT NOT NULL, objective_id TEXT NOT NULL,
 until REAL NOT NULL, uncertain INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS workflow_evidence (
 id TEXT PRIMARY KEY, objective_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 transport TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 request_id TEXT, prompt_hash TEXT NOT NULL, raw_response TEXT NOT NULL,
 raw_response_hash TEXT NOT NULL, delivery_status TEXT NOT NULL,
 error TEXT, parent_evidence_id TEXT, created REAL NOT NULL, completed REAL);
CREATE INDEX IF NOT EXISTS workflow_evidence_attempt ON workflow_evidence(attempt_id, id);
"""


def harness_hash():
    names = ("sabrina_workflow.py", "sabrina_workflow_verifier.py", "agent_desktop_broker.py",
             "sabrina_lab_assistant_mcp.py", "sabrina_coordination.py")
    import hashlib
    return digest({name: hashlib.sha256((ROOT / "tools" / name).read_bytes()).hexdigest() for name in names})


class WorkflowStore:
    def __init__(self, path=DEFAULT_DB, artifacts=None, clock=time.time):
        self.path = Path(path).resolve()
        self.artifacts = Path(artifacts or ROOT / "artifacts/workflows").resolve()
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('BEGIN IMMEDIATE;\n'+SCHEMA+'\nCOMMIT;')
            columns = {r[1] for r in db.execute('PRAGMA table_info(workflow_attempts)')}
            if 'metrics' not in columns:
                db.execute("ALTER TABLE workflow_attempts ADD COLUMN metrics TEXT NOT NULL DEFAULT '{}'")
            if 'reconciliation_event_id' not in columns:
                db.execute('ALTER TABLE workflow_attempts ADD COLUMN reconciliation_event_id INTEGER')
            lease_columns = {r[1] for r in db.execute('PRAGMA table_info(workflow_leases)')}
            for name, kind in [('pid','INTEGER'),('process_started','REAL')]:
                if name not in lease_columns:
                    db.execute(f'ALTER TABLE workflow_leases ADD COLUMN {name} {kind}')
            ev_columns = {r[1] for r in db.execute('PRAGMA table_info(workflow_evidence)')}
            for name, kind in [
                ('execution_mode', "TEXT NOT NULL DEFAULT 'live'"),
                ('requested_provider', "TEXT NOT NULL DEFAULT ''"),
                ('requested_model', "TEXT NOT NULL DEFAULT ''"),
                ('requested_interface', "TEXT NOT NULL DEFAULT ''"),
                ('actual_provider', "TEXT NOT NULL DEFAULT ''"),
                ('actual_model', "TEXT NOT NULL DEFAULT 'unknown'"),
                ('actual_interface', "TEXT NOT NULL DEFAULT ''"),
                ('fallback_occurred', "INTEGER NOT NULL DEFAULT 0"),
            ]:
                if name not in ev_columns:
                    db.execute(f'ALTER TABLE workflow_evidence ADD COLUMN {name} {kind}')

    def submit_evidence(
        self,
        *,
        objective_id: str,
        attempt_id: str,
        transport: str,
        requested_provider: str,
        requested_model: str = "provider_default",
        requested_interface: str = "cli",
        actual_provider: str | None = None,
        actual_model: str = "unknown",
        actual_interface: str | None = None,
        fallback_occurred: bool = False,
        raw_response: str = "",
        delivery_status: str = "received",
        execution_mode: str = "live",
        is_simulated: bool = False,
        error: str | None = None,
        request_id: str | None = None,
        parent_evidence_id: str | None = None,
    ) -> dict:
        """Record transport-neutral worker evidence linked to its objective and attempt."""
        if execution_mode not in {"live", "simulation", "replay"}:
            raise ValueError("execution_mode must be 'live', 'simulation', or 'replay'")
        if execution_mode == "live" and is_simulated:
            raise ValueError("Simulated response presented as live in workflow evidence")
        act_prov = (actual_provider or requested_provider or "").strip()
        act_mod = (actual_model or "unknown").strip() or "unknown"
        act_iface = (actual_interface or requested_interface or transport or "").strip()
        if act_prov.lower() in {"gemini", "google"} and (
            "ollama" in act_iface.lower() or "qwen" in act_mod.lower() or "local:" in act_mod.lower()
        ):
            raise ValueError("Local fallback cannot be attributed to Gemini")
        ev_id = "ev_" + uuid.uuid4().hex[:16]
        now = self.clock()
        resp_hash = digest(raw_response)
        with self.connect() as db:
            a = db.execute("SELECT prompt_hash FROM workflow_attempts WHERE id=?", (attempt_id,)).fetchone()
            p_hash = a["prompt_hash"] if a else digest("")
            db.execute(
                """INSERT INTO workflow_evidence
                   (id,objective_id,attempt_id,transport,provider,model,request_id,prompt_hash,
                    raw_response,raw_response_hash,delivery_status,error,parent_evidence_id,
                    created,completed,execution_mode,requested_provider,requested_model,
                    requested_interface,actual_provider,actual_model,actual_interface,fallback_occurred)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ev_id,
                    objective_id,
                    attempt_id,
                    transport,
                    act_prov,
                    act_mod,
                    request_id,
                    p_hash,
                    raw_response,
                    resp_hash,
                    delivery_status,
                    error,
                    parent_evidence_id,
                    now,
                    now if delivery_status == "received" else None,
                    execution_mode,
                    requested_provider,
                    requested_model,
                    requested_interface,
                    act_prov,
                    act_mod,
                    act_iface,
                    1 if fallback_occurred else 0,
                ),
            )
            self._event(
                db,
                objective_id,
                "evidence_recorded",
                {
                    "evidence_id": ev_id,
                    "execution_mode": execution_mode,
                    "requested_provider": requested_provider,
                    "actual_provider": act_prov,
                    "actual_model": act_mod,
                    "fallback_occurred": bool(fallback_occurred),
                    "delivery_status": delivery_status,
                },
                None,
                attempt_id,
            )
        return {
            "evidence_id": ev_id,
            "objective_id": objective_id,
            "attempt_id": attempt_id,
            "execution_mode": execution_mode,
            "requested_provider": requested_provider,
            "requested_model": requested_model,
            "requested_interface": requested_interface,
            "actual_provider": act_prov,
            "actual_model": act_mod,
            "actual_interface": act_iface,
            "fallback_occurred": bool(fallback_occurred),
            "delivery_status": delivery_status,
        }

    def get_coding_pool(self, *, providers=None, profiles=None, collector=None):
        """Return a 4-worker CodingPool bound to this workflow store's SQLite DB."""
        from tools.sabrina_coding_pool import CodingPool
        return CodingPool(
            self.path,
            self.artifacts / "coding_pool_workspaces",
            providers=providers,
            profiles=profiles,
            collector=collector,
            clock=self.clock,
        )

    def get_reasoning_allocator(self, *, providers=None, profiles=None, collector=None):
        """Return Sabrina's 7-action reasoning allocator bound to this workflow store."""
        from tools.sabrina_coding_pool import SabrinaReasoningAllocator
        return SabrinaReasoningAllocator(
            self.get_coding_pool(providers=providers, profiles=profiles, collector=collector)
        )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _event(self, db, objective, kind, detail, task=None, attempt=None):
        db.execute("INSERT INTO workflow_events(objective_id,task_id,attempt_id,kind,detail,created) VALUES(?,?,?,?,?,?)",
                   (objective, task, attempt, kind, json.dumps(detail), self.clock()))

    def _metrics(self, db, aid):
        a = db.execute('SELECT * FROM workflow_attempts WHERE id=?',(aid,)).fetchone()
        if not a or not a['run_id']:
            return
        rows = db.execute('SELECT response,metadata FROM assistant_decision_audit WHERE run_id=?',(a['run_id'],)).fetchall()
        counts = [json.loads(r['response']) if r['response'] else {} for r in rows]
        known = bool(counts) and all(isinstance(r.get('prompt_eval_count'),int) and isinstance(r.get('eval_count'),int) for r in counts)
        metrics = {'elapsed_seconds':max(0,self.clock()-a['created']), 'desktop_calls':a['tool_calls'],
                   'model_calls':len(rows), 'prompt_tokens':sum(r['prompt_eval_count'] for r in counts) if known else None,
                   'completion_tokens':sum(r['eval_count'] for r in counts) if known else None,
                   'external_tokens':None,'external_cost':None,'confirmed_placement':None}
        db.execute('UPDATE workflow_attempts SET metrics=? WHERE id=?',(json.dumps(metrics),aid))

    def create(self, goal, suite_id="duration-v1", *, experimental=True, retention_days=7):
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 2000:
            raise ValueError("goal must contain 1-2000 characters")
        if not 1 <= retention_days <= 365:
            raise ValueError("retention must be 1-365 days")
        contract = public_contract(suite_id)
        contract.update({"apps": list(APPS), "mode": "proposed_artifacts", "max_attempts": 3,
                         "prohibited": ["commands", "file_edits", "approval_clicks", "deploy", "install"]})
        now, oid = self.clock(), uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO workflow_objectives(id,goal,state,contract,contract_hash,created,deadline,expires,experimental) VALUES(?,?,'ready',?,?,?,?,?,?)",
                       (oid, goal.strip(), json.dumps(contract), digest(contract), now, now+3600,
                        now+retention_days*86400, int(experimental)))
            self._event(db, oid, "objective_created", {"contract_hash": digest(contract)})
        self.add_task(oid, contract)
        return self.status(oid)

    def _live(self, db, oid):
        row = db.execute("SELECT * FROM workflow_objectives WHERE id=?", (oid,)).fetchone()
        if row is None:
            raise ValueError("unknown objective")
        if row["expires"] <= self.clock() or row["state"] == "expired":
            raise ValueError("workflow evidence expired; create a fresh objective")
        if row["state"] in {"verified", "cancelled", "failed"}:
            raise ValueError("objective is terminal")
        if row["deadline"] <= self.clock():
            raise ValueError("objective budget exhausted")
        if digest(json.loads(row["contract"])) != row["contract_hash"]:
            raise ValueError("objective contract changed")
        return row

    def add_task(self, oid, contract, dependencies=(), optional=False):
        """Bounded v1: tasks use the frozen objective contract, never new criteria."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            obj = self._live(db, oid)
            if obj["state"] != "ready":
                raise ValueError("task graph freezes before execution")
            if digest(contract) != obj["contract_hash"]:
                raise ValueError("task cannot widen scope or change acceptance criteria")
            rows = db.execute("SELECT id FROM workflow_tasks WHERE objective_id=?", (oid,)).fetchall()
            ids = {r[0] for r in rows}
            if len(ids) >= 8 or not set(dependencies) <= ids:
                raise ValueError("invalid dependencies or task budget; dependencies must already exist")
            tid = uuid.uuid4().hex
            db.execute("INSERT INTO workflow_tasks(id,objective_id,contract,contract_hash,dependencies,optional,state,created) VALUES(?,?,?,?,?,?,'ready',?)",
                       (tid, oid, json.dumps(contract), digest(contract), json.dumps(list(dependencies)), int(optional), self.clock()))
            self._event(db, oid, "task_created", {"contract_hash": digest(contract)}, tid)
        return tid

    def claim(self, oid, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            obj = self._live(db, oid)
            if obj["state"] in {"paused", "blocked"}:
                raise ValueError("objective requires explicit resume")
            for resource in ("workflow:"+oid, "desktop:primary", "workspace:"+oid):
                lease = db.execute("SELECT * FROM workflow_leases WHERE resource=?", (resource,)).fetchone()
                if lease and lease["owner"] != owner and self._owner_may_be_alive(lease):
                    raise ValueError("workflow or desktop already owned")
                if lease and lease['owner'] != owner and lease['objective_id'] == oid:
                    self._recover_local_run_lease(db,oid,lease)
            try:
                import psutil
                started=psutil.Process().create_time()
            except ImportError:
                started=None
            for resource in ("workflow:"+oid, "desktop:primary", "workspace:"+oid):
                db.execute("INSERT OR REPLACE INTO workflow_leases(resource,owner,objective_id,until,uncertain,pid,process_started) VALUES(?,?,?,?,0,?,?)",
                           (resource, owner, oid, self.clock()+1200,os.getpid(),started))
            db.execute("UPDATE workflow_objectives SET state='running' WHERE id=?", (oid,))

    @staticmethod
    def _owner_may_be_alive(lease):
        if not lease['pid'] or lease['process_started'] is None:
            return True  # Unknown ownership is not evidence of a stopped worker.
        try:
            import psutil
            return abs(psutil.Process(lease['pid']).create_time()-lease['process_started']) < 0.01
        except ImportError:
            return True
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True

    def _recover_local_run_lease(self, db, oid, lease):
        # This only releases private research-run ownership after PID+creation
        # time proves that worker stopped. Shared uncertain resources still block.
        if self._owner_may_be_alive(lease):
            raise ValueError('previous worker not confirmed stopped')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='assistant_sessions'").fetchone():
            db.execute("UPDATE assistant_sessions SET owner=NULL,lease_until=0 WHERE id IN (SELECT a.run_id FROM workflow_attempts a JOIN workflow_tasks t ON t.id=a.task_id WHERE t.objective_id=?)",(oid,))
        self._event(db,oid,'worker_recovered',{'previous_owner':lease['owner'],'previous_pid':lease['pid']})

    def release(self, oid, owner):
        with self.connect() as db:
            db.execute("DELETE FROM workflow_leases WHERE objective_id=? AND owner=? AND uncertain=0", (oid, owner))

    def control(self, oid, action):
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("unknown control")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._live(db, oid)
            if action == "resume":
                leases = db.execute("SELECT * FROM workflow_leases WHERE objective_id=?", (oid,)).fetchall()
                if any(self._owner_may_be_alive(lease) for lease in leases):
                    raise ValueError("worker is still active; do not resume concurrently")
                for lease in leases:
                    self._recover_local_run_lease(db,oid,lease)
                db.execute('DELETE FROM workflow_leases WHERE objective_id=?',(oid,))
            state = {"pause": "paused", "resume": "ready", "cancel": "cancelled"}[action]
            db.execute("UPDATE workflow_objectives SET state=?,next_wake=0,external_stop=? WHERE id=?",
                       (state, "unconfirmed_external_work_may_continue" if action == "cancel" else "not_requested", oid))
            self._event(db, oid, action, {"external_stop_confirmed": False})
        return self.status(oid)

    def skip(self, task_id, reason):
        if not isinstance(reason, str) or not 1 <= len(reason) <= 1000:
            raise ValueError("skip reason required")
        with self.connect() as db:
            task = db.execute("SELECT * FROM workflow_tasks WHERE id=?", (task_id,)).fetchone()
            if not task or not task["optional"] or task["state"] != "ready":
                raise ValueError("only unstarted optional tasks may be skipped")
            self._live(db, task["objective_id"])
            db.execute("UPDATE workflow_tasks SET state='skipped',skip_reason=? WHERE id=?", (reason, task_id))
            self._event(db, task["objective_id"], "task_skipped", {"reason": reason}, task_id)

    def next_attempt(self, oid):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            obj = self._live(db, oid)
            tasks = db.execute("SELECT * FROM workflow_tasks WHERE objective_id=? ORDER BY created,id", (oid,)).fetchall()
            done = {t["id"] for t in tasks if t["state"] in {"verified", "skipped"}}
            for task in tasks:
                if task["id"] in done or not set(json.loads(task["dependencies"])) <= done:
                    continue
                previous = db.execute("SELECT * FROM workflow_attempts WHERE task_id=? ORDER BY created DESC", (task["id"],)).fetchall()
                # A failed/needs_revision label is not evidence of non-delivery.
                # Preserve the same operation until its uncertain send is reconciled.
                if previous and (previous[0]["delivery"] == "uncertain" or (
                        previous[0]["submit_event_id"] and previous[0]["delivery"] not in
                        {"observed", "observed_via_reply"})):
                    return dict(previous[0])
                if previous and previous[0]["state"] not in {"needs_revision", "failed"}:
                    return dict(previous[0])
                if len(previous) >= 3:
                    db.execute("UPDATE workflow_tasks SET state='failed' WHERE id=?", (task["id"],))
                    db.execute("UPDATE workflow_objectives SET state='failed' WHERE id=?", (oid,))
                    self._event(db, oid, "attempt_budget_exhausted", {}, task["id"])
                    return None
                contract = json.loads(task["contract"])
                if digest(contract) != task["contract_hash"]:
                    raise ValueError("task contract changed")
                aid, marker = uuid.uuid4().hex, uuid.uuid4().hex
                correction = json.loads(previous[0]["correction"]) if previous else {}
                prompt = (f"Task marker: {marker}. {contract['requirement']} "
                          "Return only raw single-line JSON (no markdown code fences) with task_marker equal to that marker, complete:true, "
                          "and files:{\"solution.py\":\"complete Python source as a JSON string using \\\\t tabs instead of spaces for indentation\"}. "
                          "Do not run commands, edit files, use tools, or request approvals. "
                          "This is a proposed code artifact; an independent verifier will test it.")
                if correction:
                    prompt += " Previous attempt failed: " + json.dumps(correction)[:1100]
                db.execute("INSERT INTO workflow_attempts(id,task_id,parent_id,state,operation_id,marker,expected_prompt,prompt_hash,correction,harness_hash,created,deadline) VALUES(?,?,?,'assigned',?,?,?,?,?,?,?,?)",
                           (aid, task["id"], previous[0]["id"] if previous else None, aid, marker,
                            prompt, digest(prompt), json.dumps(correction), harness_hash(), self.clock(),
                            min(obj["deadline"], self.clock()+900)))
                db.execute("UPDATE workflow_tasks SET state='assigned',verification_state='unverified' WHERE id=?", (task["id"],))
                self._event(db, oid, "attempt_created", {"operation_id": aid, "prompt_hash": digest(prompt)}, task["id"], aid)
                return dict(db.execute("SELECT * FROM workflow_attempts WHERE id=?", (aid,)).fetchone())
            return None

    def block(self, aid, reason, kind="interface"):
        with self.connect() as db:
            row = db.execute("SELECT a.task_id,t.objective_id FROM workflow_attempts a JOIN workflow_tasks t ON t.id=a.task_id WHERE a.id=?", (aid,)).fetchone()
            db.execute("UPDATE workflow_attempts SET state='blocked',failure_kind=? WHERE id=?", (kind, aid))
            db.execute("UPDATE workflow_tasks SET state='blocked' WHERE id=?", (row["task_id"],))
            db.execute("UPDATE workflow_objectives SET state='blocked' WHERE id=? AND state NOT IN ('cancelled','paused','expired')", (row["objective_id"],))
            self._event(db, row["objective_id"], "blocked", {"reason": reason[:1000], "failure_kind": kind}, row["task_id"], aid)
            self._metrics(db, aid)

    def status(self, oid, after_id=0, limit=100):
        self.expire()
        with self.connect() as db:
            obj = db.execute("SELECT * FROM workflow_objectives WHERE id=?", (oid,)).fetchone()
            if obj is None:
                raise ValueError("unknown objective")
            tasks = [dict(r) for r in db.execute("SELECT * FROM workflow_tasks WHERE objective_id=? ORDER BY created,id", (oid,))]
            attempts = [dict(r) for r in db.execute("SELECT a.* FROM workflow_attempts a JOIN workflow_tasks t ON t.id=a.task_id WHERE t.objective_id=? ORDER BY a.created", (oid,))]
            events = [dict(r) for r in db.execute("SELECT * FROM workflow_events WHERE objective_id=? AND id>? ORDER BY id LIMIT ?", (oid, max(0,int(after_id)), max(1,min(100,int(limit)))))]
            for attempt in attempts:
                attempt.pop("expected_prompt", None)
                attempt["verifications"] = [dict(r) for r in db.execute("SELECT * FROM workflow_verifications WHERE attempt_id=? ORDER BY created", (attempt["id"],))]
            return {"objective": dict(obj), "tasks": tasks, "attempts": attempts, "events": events,
                    "next_event_id": events[-1]["id"] if events else after_id}

    def list_objectives(self, offset=0, limit=50):
        self.expire()
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,goal,state,created,expires,external_stop FROM workflow_objectives ORDER BY created DESC LIMIT ? OFFSET ?",
                                                (max(1,min(50,int(limit))), max(0,int(offset))))]

    def expire(self):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM workflow_objectives WHERE expires<=? AND state!='expired'", (self.clock(),)).fetchall()
            for row in rows:
                oid = row[0]
                # Raw artifacts are a bounded manifest, never recursively delete a computed root.
                for a in db.execute("SELECT a.* FROM workflow_attempts a JOIN workflow_tasks t ON t.id=a.task_id WHERE t.objective_id=?", (oid,)).fetchall():
                    if a["artifact_path"]:
                        path = Path(a["artifact_path"]).resolve()
                        if path.is_relative_to(self.artifacts) and path.name == "solution.py":
                            path.unlink(missing_ok=True)
                    db.execute("UPDATE workflow_attempts SET expected_prompt='',correction='{}',artifact_path=NULL,state='expired' WHERE id=?", (a["id"],))
                    # Keep numerical verdicts and hashes; strip potential error/source previews.
                    for v in db.execute("SELECT id,verdict FROM workflow_verifications WHERE attempt_id=?", (a["id"],)).fetchall():
                        result = json.loads(v["verdict"])
                        for check in result.get("checks", []):
                            check.pop("detail", None)
                        db.execute("UPDATE workflow_verifications SET verdict=? WHERE id=?", (json.dumps(result), v["id"]))
                db.execute("UPDATE workflow_objectives SET state='expired',goal='',contract='{}' WHERE id=?", (oid,))
                db.execute("UPDATE workflow_tasks SET contract='{}',skip_reason='' WHERE objective_id=?", (oid,))
                db.execute("UPDATE workflow_events SET detail='{}' WHERE objective_id=?", (oid,))
                log = self.artifacts / oid / 'worker.log'
                if log.resolve().is_relative_to(self.artifacts):
                    log.unlink(missing_ok=True)


def workflow_attempt(db, run_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_attempts'").fetchone():
        return None
    return db.execute("SELECT a.*,t.contract,t.objective_id,o.state AS objective_state,o.expires,o.experimental FROM workflow_attempts a JOIN workflow_tasks t ON t.id=a.task_id JOIN workflow_objectives o ON o.id=t.objective_id WHERE a.run_id=?", (run_id,)).fetchone()


def guard_desktop(db, run_id, tool, args, now):
    """Called before native execution. Historical/non-workflow runs are unaffected."""
    a = workflow_attempt(db, run_id)
    if a is None:
        return
    if a["objective_state"] != "running" or a["expires"] <= now or a["deadline"] <= now:
        raise ValueError("workflow paused, expired, or out of budget")
    lease = db.execute("SELECT 1 FROM workflow_leases WHERE resource='desktop:primary' AND objective_id=? AND until>?", (a["objective_id"], now)).fetchone()
    if not lease or a["harness_hash"] != harness_hash():
        raise ValueError("workflow lease unavailable or harness changed")
    if a["tool_calls"] >= 40:
        raise ValueError("workflow desktop call budget exhausted")
    if tool in {'desktop_snapshot', 'desktop_find', 'desktop_list_windows'}:
        recent = db.execute("SELECT result FROM assistant_desktop_audit_events WHERE run_id=? AND tool=? ORDER BY id DESC LIMIT 3", (run_id, tool)).fetchall()
        if len(recent) == 3 and all(r[0] and json.loads(r[0]).get('ok') is False for r in recent):
            raise ValueError("observation failed after two retries; diagnose before another run")
    h = db.execute("SELECT prompt_event_id,conversation_key FROM assistant_cross_app_handoffs WHERE handoff_id=?", (a['handoff_id'],)).fetchone()
    typed = bool(h and h[0])
    # Native submission evidence wins over a stale or reset delivery label.
    # There is no model-callable reset/retry authorization for an attempted send.
    if a['submit_event_id'] and tool in {'desktop_click', 'desktop_type', 'desktop_set_value', 'desktop_press'}:
        raise ValueError('submission already attempted; only observe and reconcile before submission retry')
    if tool in {"desktop_type", "desktop_set_value"} and (args.get("value") != a["expected_prompt"] or a["delivery"] != "not_submitted"):
        raise ValueError("only the frozen task prompt may be typed before submission")
    if typed and tool in {'desktop_type', 'desktop_click', 'desktop_set_value'}:
        raise ValueError("prompt already entered; submit exactly once with Enter")
    if tool == 'desktop_press' and typed and str(args.get('keys','')).lower() != 'enter':
        raise ValueError("entered prompt must be submitted once using Enter")
    if tool == "desktop_press" and str(args.get("keys", "")).lower() == "enter":
        if not typed:
            raise ValueError('cannot submit before the frozen prompt is entered')
        if a["delivery"] != "not_submitted":
            raise ValueError("submission already attempted; reconcile instead of resending")
        if not h['conversation_key']:
            raise ValueError('submission requires an observed conversation identity')
        db.execute("UPDATE workflow_attempts SET state='submitting',delivery='uncertain',conversation_key=? WHERE id=?", (h['conversation_key'], a["id"]))
    if a["delivery"] != "not_submitted" and tool in {"desktop_click", "desktop_type", "desktop_set_value", "desktop_press"}:
        raise ValueError("submission uncertain or observed; only observe the current conversation")
    db.execute("UPDATE workflow_attempts SET tool_calls=tool_calls+1 WHERE id=?", (a["id"],))


def _normalize_smart_quotes_json(text: str, marker: str) -> str | None:
    if "\u201c" not in text and "\u201d" not in text:
        return None
    if marker not in text:
        return None
    # Normalize outer structural smart quotes and inner Python string smart quotes
    # produced when Electron Markdown renders un-fenced JSON prose.
    import re
    s = text.strip()
    m = re.search(
        r"\{[\s\u201c\u201d\"]*task_marker[\s\u201c\u201d\"]*:\s*[\u201c\u201d\"]"
        + re.escape(marker)
        + r"[\u201c\u201d\"]\s*,\s*[\u201c\u201d\"]complete[\u201c\u201d\"]\s*:\s*true\s*,\s*[\u201c\u201d\"]files[\u201c\u201d\"]\s*:\s*\{\s*[\u201c\u201d\"]solution\.py[\u201c\u201d\"]\s*:\s*[\u201c\u201d\"](.*)[\u201c\u201d\"]\s*\}\s*\}",
        s,
        re.DOTALL,
    )
    if not m:
        return None
    raw_code = m.group(1)
    # Unescape literal \n, \t, \" and replace smart quotes inside Python code with ASCII quotes
    code = (
        raw_code.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    return json.dumps({"task_marker": marker, "complete": True, "files": {"solution.py": code}})


def visible_artifact(result, marker):
    """Only complete, marker-matched JSON from recorded accessible UI or API evidence is accepted."""
    strings = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"name", "value", "text", "content", "raw_reply"} and isinstance(item, str):
                    strings.append(item)
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            strings.append(value)
    if isinstance(result, str):
        strings.append(result)
    elif isinstance(result, dict):
        visit(result.get("data", result))
    decoder = json.JSONDecoder()
    candidates = {}
    search_pool = list(strings) + ["\n".join(strings)]
    for s_item in list(strings):
        norm = _normalize_smart_quotes_json(s_item, marker)
        if norm:
            search_pool.append(norm)
    for text in search_pool:
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            if (isinstance(obj, dict) and obj.get("task_marker") == marker
                    and obj.get("complete") is True and isinstance(obj.get("files"), dict)):
                files = obj["files"]
                if set(files) == {"solution.py"} and isinstance(files["solution.py"], str) and len(files["solution.py"].encode()) <= 65536:
                    candidates[digest(files)] = files
    if len(candidates) != 1:
        raise ValueError("no unique complete marker-matched artifact in recorded reply")
    return next(iter(candidates.values()))


def reconcile_submission(db, event_id, result):
    """An explicit marker-matched reply can resolve uncertain delivery, not a retry."""
    event = db.execute('SELECT * FROM assistant_desktop_audit_events WHERE id=?',(event_id,)).fetchone()
    if not event or event['tool'] != 'desktop_snapshot' or result.get('ok') is not True:
        return
    a = workflow_attempt(db,event['run_id'])
    if a is None or a['delivery'] != 'uncertain' or not a['submit_event_id'] or a['app'] != event['app']:
        return
    try:
        visible_artifact(result,a['marker'])
    except ValueError:
        return
    from tools.agent_desktop_broker import extract_window_conversation_identity
    conversation = extract_window_conversation_identity(result).get('conversation_key')
    if (not conversation or conversation != a['conversation_key']
            or event['handoff_id'] != a['handoff_id']):
        return
    submission = db.execute('SELECT created FROM assistant_desktop_audit_events WHERE id=? AND run_id=? AND handoff_id=?',
                            (a['submit_event_id'],event['run_id'],a['handoff_id'])).fetchone()
    if not submission or submission[0] > event['created']:
        return
    db.execute("UPDATE workflow_attempts SET delivery='observed_via_reply',state='waiting',reconciliation_event_id=? WHERE id=?",(event_id,a['id']))
    db.execute('UPDATE assistant_cross_app_handoffs SET submit_event_id=?,submitted=? WHERE handoff_id=?',
               (a['submit_event_id'],submission[0],a['handoff_id']))


class WorkflowWorker:
    def __init__(self, store, adapter, verifier):
        self.store, self.adapter, self.verifier = store, adapter, verifier
        self.owner = uuid.uuid4().hex

    def step(self, oid):
        s = self.store
        s.claim(oid, self.owner)
        try:
            attempt = s.next_attempt(oid)
            if attempt is None:
                return s.status(oid)
            aid = attempt["id"]
            if attempt["state"] == "blocked":
                if attempt['failure_kind'] != 'environment' or not attempt['revision_hash']:
                    s.block(aid, "Unresolved attempt requires diagnosis; create a fresh objective after fixing the blocker")
                    return s.status(oid)
            if attempt["harness_hash"] != harness_hash():
                s.block(aid, "harness_fingerprint_changed", "environment")
                return s.status(oid)
            try:
                if not attempt['revision_hash']:
                    self.adapter.advance(s, attempt)
                self.collect_and_verify(aid)
            except Exception as exc:
                s.block(aid, f"{type(exc).__name__}: {exc}",
                        'environment' if isinstance(exc,(OSError,TimeoutError)) else 'interface')
            with s.connect() as db:
                db.execute("UPDATE workflow_objectives SET next_wake=? WHERE id=? AND state='running'", (s.clock()+10, oid))
            return s.status(oid)
        finally:
            s.release(oid, self.owner)

    def collect_and_verify(self, aid):
        s = self.store
        with s.connect() as db:
            a = db.execute("SELECT a.*,t.objective_id,t.contract FROM workflow_attempts a JOIN workflow_tasks t ON t.id=a.task_id WHERE a.id=?", (aid,)).fetchone()
            obj = s._live(db, a["objective_id"])
            if obj["state"] != "running":
                return
            # 1. Transport-neutral evidence (API, CLI, or explicit Desktop evidence)
            ev = db.execute("SELECT * FROM workflow_evidence WHERE attempt_id=? ORDER BY created DESC LIMIT 1", (aid,)).fetchone()
            if ev is not None:
                if ev["delivery_status"] in {"failed", "blocked"}:
                    raise ValueError(f"external {ev['transport']} branch {ev['delivery_status']}: {ev['error']}")
                if ev["delivery_status"] != "received":
                    if s.clock() >= a["deadline"]:
                        raise ValueError("attempt budget exhausted without a complete reply")
                    return
                files = visible_artifact(ev["raw_response"], a["marker"])
                artifact_hash = digest(files)
                contract = json.loads(a["contract"])
                db.execute("UPDATE workflow_attempts SET state='collecting',revision_hash=?,delivery=? WHERE id=?",
                           (artifact_hash, f"observed_via_{ev['transport']}", aid))
                s._event(db, a["objective_id"], "output_collected",
                         {"transport": ev["transport"], "provider": ev["provider"], "model": ev["model"],
                          "evidence_id": ev["id"], "artifact_hash": artifact_hash},
                         a["task_id"], aid)
            else:
                # 2. Native desktop handoff check (legacy fallback)
                h = db.execute("SELECT * FROM assistant_cross_app_handoffs WHERE run_id=? AND handoff_id=?", (a["run_id"], a["handoff_id"])).fetchone()
                if not h or h["status"] != "completed":
                    if h and h["status"] in {"failed", "unavailable", "stopped"}:
                        raise ValueError("external branch " + h["status"])
                    if s.clock() >= a["deadline"] or a["tool_calls"] >= 40:
                        raise ValueError("attempt budget exhausted without a complete reply")
                    return
                event = db.execute("SELECT * FROM assistant_desktop_audit_events WHERE id=? AND run_id=? AND handoff_id=?", (h["reply_event_id"], a["run_id"], h["handoff_id"])).fetchone()
                if (not event or not event["result"] or not event["completed"]
                        or event["completed"] < h["submitted"] or event["app"] != h["app"]):
                    raise ValueError("missing linked post-submission reply")
                from tools.agent_desktop_broker import extract_window_conversation_identity
                result = json.loads(event["result"])
                identity = extract_window_conversation_identity(result).get("conversation_key")
                if not identity or identity != h["conversation_key"] or result.get("ok") is not True:
                    raise ValueError("reply conversation identity mismatch")
                files = visible_artifact(result, a["marker"])
                artifact_hash = digest(files)
                contract = json.loads(a["contract"])
                db.execute("UPDATE workflow_attempts SET state='collecting',revision_hash=?,reply_event_id=?,conversation_key=? WHERE id=?",
                           (artifact_hash, event["id"], identity, aid))
                s._event(db, a["objective_id"], "output_collected", {"handoff_id": h["handoff_id"], "event_id": event["id"], "artifact_hash": artifact_hash}, a["task_id"], aid)
        target = s.artifacts / a["objective_id"] / aid / artifact_hash / "solution.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") != files["solution.py"]:
            raise ValueError("immutable artifact revision changed")
        target.write_text(files["solution.py"], encoding="utf-8", newline="")
        with s.connect() as db:
            db.execute("UPDATE workflow_attempts SET state='verifying',artifact_path=?,manifest=? WHERE id=?", (str(target), json.dumps({"solution.py": artifact_hash}), aid))
            db.execute("UPDATE workflow_tasks SET state='verifying',verification_state='running' WHERE id=?", (a["task_id"],))
        # No model tool can supply this verdict. Code is executed only by the trusted verifier.
        verdict = self.verifier.check({"suite_id": contract["suite_id"], "suite_sha256": contract["suite_sha256"], "files": files})
        if verdict.get("status") == "blocked":
            s.block(aid, verdict.get("reason", "verifier unavailable"), "environment")
            return
        if (verdict.get("artifact_sha256") != artifact_hash or verdict.get("suite_sha256") != contract["suite_sha256"]
                or verdict.get("execution_mode") != "live"):
            raise ValueError("verification provenance mismatch")
        checks = verdict.get("checks", [])
        passed = (verdict.get("status") == "passed" and {c.get("id") for c in checks} == set(contract["criteria"])
                  and all(c.get("passed") is True for c in checks))
        if digest({"solution.py": target.read_text(encoding="utf-8")}) != artifact_hash:
            raise ValueError("artifact changed after verification")
        with s.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            obj = s._live(db, a["objective_id"])
            if obj["state"] != "running":
                return  # Cancellation or pause wins over a late verifier result.
            vid = uuid.uuid4().hex
            db.execute("INSERT INTO workflow_verifications VALUES(?,?,?,?,?,?)", (vid, aid, artifact_hash, contract["suite_sha256"], json.dumps(verdict), s.clock()))
            state = "verified" if passed else "needs_revision"
            correction = {"failed_criteria": [c["id"] for c in checks if not c.get("passed")],
                          "passed_criteria": [c["id"] for c in checks if c.get("passed")],
                          "check_results": checks,
                          "artifact_hash": artifact_hash, "verification_id": vid}
            old = db.execute("SELECT revision_hash FROM workflow_attempts WHERE task_id=? AND id!=? AND state='needs_revision'", (a["task_id"], aid)).fetchall()
            if not passed and any(r[0] == artifact_hash for r in old):
                state = "failed"
            db.execute("UPDATE workflow_attempts SET state=?,correction=?,failure_kind=? WHERE id=?", (state, json.dumps(correction), None if passed else "app_quality", aid))
            db.execute("UPDATE workflow_tasks SET state=?,verification_state=? WHERE id=?", (state, "passed" if passed else "failed", a["task_id"]))
            s._event(db, a["objective_id"], "verification_" + ("passed" if passed else "failed"), {"verification_id": vid, **correction}, a["task_id"], aid)
            s._metrics(db, aid)
            remaining = db.execute("SELECT state,optional FROM workflow_tasks WHERE objective_id=?", (a["objective_id"],)).fetchall()
            if state == "failed":
                db.execute("UPDATE workflow_objectives SET state='failed' WHERE id=?", (a["objective_id"],))
            elif all(t["state"] == "verified" or (t["optional"] and t["state"] == "skipped") for t in remaining):
                revisions = db.execute("SELECT a.artifact_path,a.revision_hash FROM workflow_attempts a JOIN workflow_tasks t ON t.id=a.task_id WHERE t.objective_id=? AND a.state='verified'", (a['objective_id'],)).fetchall()
                if any(not r['artifact_path'] or not Path(r['artifact_path']).is_file() or
                       digest({'solution.py':Path(r['artifact_path']).read_text(encoding='utf-8')}) != r['revision_hash'] for r in revisions):
                    db.execute("UPDATE workflow_objectives SET state='blocked' WHERE id=?",(a['objective_id'],))
                    s._event(db,a['objective_id'],'artifact_changed',{})
                    return
                db.execute("UPDATE workflow_objectives SET state='verified' WHERE id=?", (a["objective_id"],))
                s._event(db, a["objective_id"], "objective_verified", {"artifact_hash": artifact_hash, "verification_id": vid})


class DesktopAssignmentAdapter:
    """The existing 14B research loop chooses app and observations; no scripted clicks."""
    def advance(self, store, attempt):
        from tools.control_client import configured
        client = configured()
        if client is None:
            raise ValueError('workflow inference requires the lab coordinator')
        # Share the established resource name with other desktop clients. Never
        # bypass or automatically reconcile an uncertain reservation.
        with client.lease('workspace:windows-desktop-primary', wait_seconds=3):
            self._advance_owned(store, attempt)

    def _advance_owned(self, store, attempt):
        from tools import sabrina_lab_assistant_mcp as bridge
        board = bridge._get_coordination()
        if Path(board.store.path).resolve() != store.path:
            raise ValueError("workflow and desktop audit must use the same database")
        run_id = attempt["run_id"]
        if not run_id:
            context = ("Choose one app using performance_lookup if available. This is an experimental "
                       "proposed-artifact task. No host tools, app commands, file edits or approvals. "
                       "To delegate the task to an installed application, call delegate_task(app='Claude.exe'). "
                       "The controller executes the tested desktop playbook, supplies the frozen prompt, "
                       "and returns the recorded response and independent checks. If checks require correction, "
                       "call delegate_task again with follow_up_guidance. The exact app prompt is:\n"
                       + attempt["expected_prompt"])
            session = board.begin("Delegate the frozen task and collect its complete visible JSON reply. Do not implement it yourself.", context,
                                  desktop_audit_retention_days=7)
            run_id = session["id"]
            broker = bridge._get_desktop_broker()
            broker.configure(run_id, "autonomous", list(APPS), new_run=True)
            broker.configure_cross_app_review(run_id, True, new_run=True)
            with store.connect() as db:
                db.execute("UPDATE workflow_attempts SET run_id=? WHERE id=? AND run_id IS NULL", (run_id, attempt["id"]))
            board.release(session)
        os.environ["SABRINA_LAB_ASSISTANT_THINK"] = "true"
        os.environ["SABRINA_LAB_ASSISTANT_DEVICE"] = "gpu"
        try:
            bridge.ask_sabrina_lab_assistant(run_id=run_id, model=bridge.DEFAULT_DESKTOP_MODEL,
                                            desktop_access="autonomous", desktop_apps=list(APPS),
                                            host_access="none", cross_app_review=True, max_tokens=2048)
            with store.connect() as db:
                done = db.execute("SELECT 1 FROM assistant_cross_app_handoffs WHERE run_id=? AND status='completed' LIMIT 1", (run_id,)).fetchone()
            if not done:
                bridge.ask_sabrina_lab_assistant(run_id=run_id,
                                                guidance="Call delegate_task(app='Claude.exe') now to submit the frozen task prompt and collect the visible JSON reply.",
                                                model=bridge.DEFAULT_DESKTOP_MODEL,
                                                desktop_access="autonomous", desktop_apps=list(APPS),
                                                host_access="none", cross_app_review=True, max_tokens=2048)
        finally:
            bridge._get_desktop_broker().release_desktop_lease(run_id)


def main():
    import argparse
    import tomllib
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["create", "step", "work", "status", "list", "pause", "resume", "cancel"])
    parser.add_argument("--id", default="")
    parser.add_argument("--goal", default="Implement and independently verify a duration parser.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    if args.action in {"step", "work"}:
        config = tomllib.loads((Path.home()/".codex/config.toml").read_text(encoding="utf-8"))
        env = config.get("mcp_servers", {}).get("sabrina-lab-assistant", {}).get("env", {})
        os.environ.update({k: str(v) for k,v in env.items() if k.startswith("SABRINA_")})
        os.environ["SABRINA_ASSISTANT_DB"] = args.db
        os.environ["SABRINA_LAB_ASSISTANT_THINK"] = "true"
        os.environ["SABRINA_LAB_ASSISTANT_DEVICE"] = "gpu"
    store = WorkflowStore(args.db)
    if args.action == "create":
        result = store.create(args.goal)
    elif args.action == "list":
        result = store.list_objectives()
    elif args.action == "status":
        result = store.status(args.id)
    elif args.action in {"pause", "resume", "cancel"}:
        result = store.control(args.id, args.action)
    else:
        from tools.sabrina_workflow_verifier import LabVerifier
        worker = WorkflowWorker(store, DesktopAssignmentAdapter(), LabVerifier())
        while True:
            result = worker.step(args.id)
            if args.action == "step" or result["objective"]["state"] != "running":
                break
            time.sleep(max(0, min(10, result["objective"]["next_wake"]-time.time())))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
