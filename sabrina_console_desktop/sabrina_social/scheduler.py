"""Durable, capability-scoped scheduler for Sabrina operations."""
from __future__ import annotations

import importlib
import json
import logging
import random
import sqlite3
import time
import uuid
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import math
import threading
from pathlib import Path
from typing import Callable

import tools.capability_registry as _capability_registry_mod
from tools.capability_registry import CapabilityRegistry
from tools.worker_identity import DependencyUnavailable, operation_identity, executor_identity


logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}
VALID_EXECUTION_KINDS = {"code", "agent", "browser"}


class JobStore:
    def __init__(self, path: str | Path = "data/social/scheduler.sqlite3"):
        from .availability_support import backup_database
        backup_database(path, migration='availability-v1')
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
              id TEXT PRIMARY KEY, capability TEXT NOT NULL, payload TEXT NOT NULL,
              resources TEXT NOT NULL, status TEXT NOT NULL, next_run REAL NOT NULL,
              interval_min INTEGER, interval_max INTEGER, max_attempts INTEGER NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, result TEXT,
              created REAL NOT NULL, updated REAL NOT NULL, heartbeat REAL
            );
            CREATE TABLE IF NOT EXISTS scheduled_job_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
              phase TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_jobs(status,next_run);
            CREATE TABLE IF NOT EXISTS scheduler_heartbeat (name TEXT PRIMARY KEY, updated REAL NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scheduled_occurrences (job_id TEXT PRIMARY KEY, id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scheduled_executors (job_id TEXT PRIMARY KEY, identity TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        with self._lock:
            db = sqlite3.connect(str(self.path), timeout=30.0)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def _decode(self, row):
        if not row: return None
        v = dict(row)
        for k in ("payload", "resources", "result"):
            if v.get(k): v[k] = json.loads(v[k])
        return v

    def event(self, job_id, phase, detail=None):
        with self.db() as db:
            db.execute("INSERT INTO scheduled_job_events(job_id,phase,detail,created) VALUES(?,?,?,?)",
                       (job_id, phase, json.dumps(detail or {}), time.time()))

    def heartbeat(self, detail=None):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO scheduler_heartbeat VALUES(?,?,?)",
                       ("scheduler", time.time(), json.dumps(detail or {})))

    def heartbeat_state(self):
        with self.db() as db:
            row = db.execute("SELECT updated,detail FROM scheduler_heartbeat WHERE name='scheduler'").fetchone()
        if not row: return {"active": False, "status": "never_started"}
        age = max(0, time.time() - row["updated"])
        return {"active": age < 45, "status": "active" if age < 45 else "stale",
                "updated": row["updated"], "age_seconds": age, "detail": json.loads(row["detail"])}

    def create(self, capability, payload, resources, next_run, interval_min=None,
               interval_max=None, max_attempts=3, draft=False, execution_kind=None):
        ident, now = "job_" + uuid.uuid4().hex, time.time()
        with self.db() as db:
            db.execute("INSERT INTO scheduled_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (ident, capability, json.dumps(payload), json.dumps(resources), "draft" if draft else "queued",
                        next_run, interval_min, interval_max, max(1, int(max_attempts)), 0,
                        None, None, now, now, None))
        detail = {"next_run": next_run}
        if execution_kind:
            detail["execution_kind"] = execution_kind
        self.event(ident, "scheduled", detail); return self.get(ident)

    def get(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM scheduled_jobs WHERE id=?", (job_id,)).fetchone()
            events = db.execute("SELECT * FROM scheduled_job_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
            occurrence = db.execute('SELECT id FROM scheduled_occurrences WHERE job_id=?', (job_id,)).fetchone()
        value = self._decode(row)
        if value:
            value["events"] = [dict(e) for e in events]
            value['occurrence_id'] = occurrence['id'] if occurrence else None
            for e in value["events"]:
                if e.get("phase") == "scheduled" and e.get("detail"):
                    try:
                        det = json.loads(e["detail"])
                        if isinstance(det, dict) and det.get("execution_kind"):
                            value["execution_kind"] = det["execution_kind"]
                            break
                    except Exception:
                        pass
        return value

    def list(self, status=None, limit=100):
        with self.db() as db:
            q = "SELECT * FROM scheduled_jobs"; args=[]
            if status: q += " WHERE status=?"; args.append(status)
            q += " ORDER BY next_run LIMIT ?"; args.append(min(500, max(1, int(limit))))
            ids = [r["id"] for r in db.execute(q, args).fetchall()]
        return [self.get(i) for i in ids]

    def claim_due(self, now=None, *, draft=None, execution_seconds=1200,
                  exclude_resources=None, can_claim=None):
        """Claim a due queue entry or one named draft owned by a scoped worker."""
        now = now or time.time()
        if type(execution_seconds) is not int or not 1 <= execution_seconds <= 7200:
            raise ValueError('Invalid executor duration')
        excluded = set(exclude_resources or ())
        claimed_id = None
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if draft is None:
                rows = db.execute(
                    "SELECT * FROM scheduled_jobs WHERE status='queued' AND next_run<=? ORDER BY next_run LIMIT 100",
                    (now,),
                ).fetchall()
                row = None
                for candidate in rows:
                    if candidate["attempts"] >= candidate["max_attempts"]:
                        db.execute("UPDATE scheduled_jobs SET status='failed',last_error=?,updated=? WHERE id=?",
                                   ("retry limit exceeded", now, candidate["id"]))
                        continue
                    cand_resources = set(json.loads(candidate["resources"] or "[]"))
                    if excluded and (cand_resources & excluded):
                        continue
                    if can_claim is not None and not can_claim(self._decode(candidate)):
                        continue
                    row = candidate
                    break
            else:
                job_id, capability = draft
                row = db.execute(
                    "SELECT * FROM scheduled_jobs WHERE id=? AND capability=? "
                    "AND status='draft' AND next_run<=? AND interval_min IS NULL",
                    (job_id, capability, now),
                ).fetchone()
                if row and row["attempts"] >= row["max_attempts"]:
                    db.execute("UPDATE scheduled_jobs SET status='failed',last_error=?,updated=? WHERE id=?",
                               ("retry limit exceeded", now, row["id"]))
                    return None
            if not row: return None
            db.execute("UPDATE scheduled_jobs SET status='running',attempts=attempts+1,heartbeat=?,updated=? WHERE id=? AND status=?", (now, now, row["id"], row["status"]))
            db.execute('INSERT OR IGNORE INTO scheduled_occurrences VALUES(?,?)', (row['id'], uuid.uuid4().hex))
            occurrence = db.execute('SELECT id FROM scheduled_occurrences WHERE job_id=?', (row['id'],)).fetchone()
            db.execute('INSERT OR REPLACE INTO scheduled_executors VALUES(?,?)',
                       (row['id'], json.dumps({**executor_identity(occurrence['id']), 'deadline': now + execution_seconds})))
            claimed_id = row["id"]
        return self.get(claimed_id) if claimed_id else None

    def finish(self, job_id, status, result=None, error=None, next_run=None):
        now = time.time()
        with self.db() as db:
            db.execute("UPDATE scheduled_jobs SET status=?,result=?,last_error=?,next_run=?,updated=?,heartbeat=? WHERE id=?",
                       (status, json.dumps(result or {}), error, next_run or now, now, now, job_id))
        self.event(job_id, status, {"error": error, "result": result or {}})

    def set_status(self, job_id, status):
        with self.db() as db:
            db.execute("UPDATE scheduled_jobs SET status=?,updated=? WHERE id=?", (status, time.time(), job_id))
        self.event(job_id, status)

    def update_payload(self, job_id, payload):
        with self.db() as db:
            db.execute("UPDATE scheduled_jobs SET payload=?,updated=? WHERE id=?",
                       (json.dumps(payload, ensure_ascii=False), time.time(), job_id))
        self.event(job_id, "payload_updated", {"keys": sorted(payload)})


class Scheduler:
    def __init__(
        self,
        store=None,
        registry=None,
        adapters=None,
        clock=time.time,
        rng=None,
        execution_seconds=1200,
        max_code_workers=4,
        max_agent_workers=2,
        max_browser_workers=2,
    ):
        self.store = store or JobStore()
        self.registry = registry or CapabilityRegistry()
        self.adapters = adapters or {}
        self.clock = clock; self.rng = rng or random.Random()
        self.execution_seconds = execution_seconds
        self.max_code_workers = max(1, int(max_code_workers))
        self.max_agent_workers = max(1, int(max_agent_workers))
        self.max_browser_workers = max(1, int(max_browser_workers))
        self._active_lock = threading.Lock()
        reg_file = getattr(_capability_registry_mod, "__file__", None)
        self.registry_path = Path(reg_file).resolve() if reg_file else (Path(__file__).resolve().parent.parent / "tools" / "capability_registry.py")
        self.registry_mtime: float | None = self._current_registry_mtime()

    def _current_registry_mtime(self) -> float | None:
        try:
            return self.registry_path.stat().st_mtime if self.registry_path.exists() else None
        except OSError:
            return None

    def reload_registry_if_changed(self, force: bool = False) -> bool:
        """Reload tools.capability_registry if its file mtime changed (or force=True)."""
        current_mtime = self._current_registry_mtime()
        if not force and (current_mtime is None or current_mtime == self.registry_mtime):
            return False
        mod = importlib.import_module("tools.capability_registry")
        mod = importlib.reload(mod)
        globals()["_capability_registry_mod"] = mod
        globals()["CapabilityRegistry"] = mod.CapabilityRegistry
        reg_path = getattr(self.registry, "path", None)
        self.registry = mod.CapabilityRegistry(reg_path) if reg_path is not None else mod.CapabilityRegistry()
        self.registry_mtime = self._current_registry_mtime()
        logger.info("Reloaded capability_registry (mtime=%s, force=%s)", self.registry_mtime, force)
        return True

    def get_execution_kind(self, capability) -> str:
        """Return 'code', 'agent', or 'browser' for a capability name, dict, or object."""
        raw_kind = None
        if isinstance(capability, str):
            discover = getattr(self.registry, "discover", None)
            if callable(discover):
                try:
                    found = discover(capability=capability)
                except Exception:
                    found = []
                if found:
                    first = found[0]
                    raw_kind = first.get("execution_kind", "code") if isinstance(first, dict) else getattr(first, "execution_kind", "code")
        elif isinstance(capability, dict):
            raw_kind = capability.get("execution_kind", "code")
        elif capability is not None:
            raw_kind = getattr(capability, "execution_kind", "code")
        kind = str(raw_kind or "code").strip().lower()
        return kind if kind in VALID_EXECUTION_KINDS else "code"

    def _kind_cap(self, kind: str) -> int:
        if kind == "agent":
            return self.max_agent_workers
        if kind == "browser":
            return self.max_browser_workers
        return self.max_code_workers

    def _max_total_workers(self) -> int:
        return self.max_code_workers + self.max_agent_workers + self.max_browser_workers

    def schedule(self, capability, payload, next_run=None, interval_min=None, interval_max=None, max_attempts=3, draft=False):
        if next_run is not None and (not math.isfinite(next_run) or next_run < 0):
            raise ValueError('Invalid scheduled time')
        if interval_min is None and interval_max is not None:
            raise ValueError('Repeat minimum is required')
        if capability == 'kraken.inspect':
            from tools.kraken_objectives import validate_payload
            payload = validate_payload(payload)
        if capability == 'kraken.objective':
            from tools.kraken_skill_runner import validate_payload
            payload = validate_payload(payload)
            max_attempts = 1  # Interrupted objectives require an intentional new run.
        if interval_min is not None and (interval_min < 60 or (interval_max or interval_min) < interval_min):
            raise ValueError('repeat interval must be at least 60 seconds and max >= min')
        found = self.registry.discover(capability=capability)
        if not found: raise ValueError(f"Unknown capability: {capability}")
        first = found[0]
        resources = list(first.get("resources", []) if isinstance(first, dict) else getattr(first, "resources", ()))
        execution_kind = self.get_execution_kind(first)
        return self.store.create(
            capability,
            payload,
            resources,
            next_run if next_run is not None else self.clock(),
            interval_min,
            interval_max,
            max_attempts,
            draft,
            execution_kind=execution_kind,
        )

    def schedule_approved_social_action(self, runtime, capability, payload, next_run=None,
                                        interval_min=None, interval_max=None,
                                        max_attempts=1, approved_at_schedule=False):
        """Schedule one exact, explicitly approved post/repost; never repeat live shares."""
        actions = {
            "linkedin.publish_post": "publish_post",
            "linkedin.reshare": "reshare_post",
            "x.publish": "x.publish",
        }
        action = actions.get(capability)
        if not action:
            raise ValueError("Capability is not a scheduled social publishing action")
        if not approved_at_schedule:
            raise ValueError("Confirm approval for this exact one-time social action")
        if interval_min is not None or interval_max is not None:
            raise ValueError("Publishing and reposting jobs must be one-time; schedule reports to repeat")
        clean = runtime.validate_scheduled_action(action, payload)
        metadata = {key: payload[key] for key in ("duration_minutes", "scheduled_slot", "objective", "mode")
                    if key in payload}
        job_payload = {**clean, **metadata}
        job = self.schedule(capability, job_payload, next_run=next_run,
                            max_attempts=max_attempts, draft=True)
        approval = None
        try:
            approval = runtime.request_scheduled_action(action, clean, job["id"])
            self.store.update_payload(job["id"], {
                **job_payload, "scheduled_approval_id": approval["id"],
                "approval_required": True, "approved_at_schedule": True,
            })
            self.store.set_status(job["id"], "queued")
            return self.store.get(job["id"])
        except Exception:
            if approval:
                runtime.store.cancel_scheduled_approval(approval["id"])
            self.store.set_status(job["id"], "cancelled")
            raise

    def _cleanup_stale_objectives(self):
        # A stopped worker must never leave an objective looking active forever.
        with self.store.db() as db:
            db.execute(
                "UPDATE scheduled_jobs SET status='blocked',last_error='Objective worker heartbeat lost; inspect saved evidence before resuming',updated=? WHERE capability='kraken.objective' AND status='running' AND heartbeat<?",
                (self.clock(), self.clock() - 120),
            )

    def dispatch_once(self, *, draft=None):
        if draft is None:
            self._cleanup_stale_objectives()
        elif draft[1] not in self.adapters:
            raise ValueError('Scoped worker requires an adapter for the named draft')
        job = self.store.claim_due(self.clock()) if draft is None else self.store.claim_due(self.clock(), draft=draft, execution_seconds=self.execution_seconds)
        if not job: return None
        return self._execute_claimed_job(job, draft=draft)

    def _execute_claimed_job(self, job, *, draft=None):
        execution_kind = self.get_execution_kind(job["capability"])
        self.store.event(job["id"], "dispatching", {"capability": job["capability"], "execution_kind": execution_kind})
        adapter = self.adapters.get(job["capability"])
        if not adapter:
            self.store.finish(job["id"], "blocked", error="no adapter registered")
            return self.store.get(job["id"])
        try:
            deadline = self.clock() + self.execution_seconds
            hb_stop = threading.Event()
            def _job_heartbeat():
                while not hb_stop.wait(10):
                    try:
                        now_ts = self.clock()
                        self.store.heartbeat({'running_job': job['id'], 'worker_deadline': deadline})
                        with self.store.db() as db:
                            db.execute("UPDATE scheduled_jobs SET heartbeat=?, updated=? WHERE id=? AND status='running'", (now_ts, now_ts, job['id']))
                    except Exception:
                        pass
            # Scoped workers share the JobStore but leave the main loop heartbeat alone.
            hb_thread = None
            if draft is None:
                self.store.heartbeat({'running_job': job['id'], 'worker_deadline': deadline})
                hb_thread = threading.Thread(target=_job_heartbeat, daemon=True)
                hb_thread.start()
            try:
                with operation_identity(job['occurrence_id']):
                    result = self._run_kraken_objective(job, adapter) if job['capability']=='kraken.objective' else adapter(job["payload"])
            finally:
                if hb_thread is not None:
                    hb_stop.set()
                    hb_thread.join(1)
            if job["capability"] in {"linkedin.publish_post", "linkedin.reshare", "x.publish"}:
                action_result = result.get("result", result)
                if action_result.get("verified") is not True:
                    reason = action_result.get("reason") or action_result.get("message") or action_result.get("status", "Social action was not verified")
                    state = "blocked" if action_result.get("status") == "blocked" else "failed"
                    self.store.finish(job["id"], state, result, error=str(reason))
                    return self.store.get(job["id"])
            if job['capability'] in {'kraken.inspect','kraken.objective'} and result.get('status') != 'completed':
                self.store.finish(job['id'], 'blocked', result, error='; '.join(result.get('blockers', [])) or result.get('status','Objective did not complete'))
                return self.store.get(job['id'])
            if job['capability'] in {'kraken.paper.director', 'kraken.paper.experiment'} and result.get('status') in {'failed', 'blocked', 'cancelled'}:
                self.store.finish(job['id'], result['status'], result, error=result.get('blocker'))
                return self.store.get(job['id'])
            recurring = job["interval_min"] is not None
            if recurring:
                low, high = job["interval_min"], job["interval_max"] or job["interval_min"]
                nxt = self.clock() + self.rng.randint(int(low), int(high))
                self.store.finish(job["id"], "completed", result, next_run=nxt)
                # Retries belong to one occurrence, not the lifetime of the job.
                with self.store.db() as db:
                    db.execute("UPDATE scheduled_jobs SET status='queued',attempts=0 WHERE id=? AND status='completed'", (job['id'],))
                    db.execute('DELETE FROM scheduled_occurrences WHERE job_id=?', (job['id'],))
                self.store.event(job['id'], 'queued', {'next_run':nxt, 'retry_budget_reset':True})
            else:
                self.store.finish(job["id"], "completed", result)
        except DependencyUnavailable as exc:
            if draft is not None:
                # The shared loop may not own this capability.
                self.store.finish(job['id'], 'blocked', {'status': 'waiting_dependency'}, error=str(exc))
                return self.store.get(job['id'])
            # No adapter-side external operation has started. Preserve its execution budget.
            self.store.finish(job['id'], 'queued', {'status': 'waiting_dependency', 'reason': str(exc)},
                              error=str(exc), next_run=self.clock() + 30)
            with self.store.db() as db:
                db.execute('UPDATE scheduled_jobs SET attempts=MAX(0,attempts-1) WHERE id=?', (job['id'],))
            self.store.event(job['id'], 'dependency_deferred', {'reason': str(exc)})
        except Exception as exc:
            if job["capability"] in {"linkedin.publish_post", "linkedin.reshare", "x.publish"}:
                # The approval is single-use. An uncertain browser outcome must be inspected,
                # never automatically replayed as a second external action.
                self.store.finish(job["id"], "failed", error=str(exc))
                return self.store.get(job["id"])
            if draft is not None or job["attempts"] >= job["max_attempts"]:
                self.store.finish(job["id"], "failed", error=str(exc))
            else:
                self.store.finish(job["id"], "queued", error=str(exc), next_run=self.clock() + 30)
            self._maybe_trigger_reactive_self_repair(job, str(exc))
        return self.store.get(job["id"])

    def _maybe_trigger_reactive_self_repair(self, failed_job: dict, err_text: str) -> None:
        """Event-driven zero-HITL self-repair trigger on unexpected job failure (debounced 15m)."""
        cap = str(failed_job.get("capability") or "")
        if cap == "system.self_repair_workshop" or "system.self_repair_workshop" not in self.adapters:
            return
        if cap.startswith("kraken."):
            return
        try:
            now_ts = self.clock()
            with self.store.db() as db:
                recent = db.execute(
                    """
                    SELECT id FROM scheduled_jobs
                    WHERE capability = 'system.self_repair_workshop'
                      AND (status IN ('queued', 'running') OR updated > ?)
                    LIMIT 1
                    """,
                    (now_ts - 900,),
                ).fetchone()
            if recent is None:
                self.schedule(
                    "system.self_repair_workshop",
                    {"max_tickets": 1, "auto_apply": True, "trigger": f"reactive:{cap}"},
                    next_run=now_ts + 5,
                    max_attempts=1,
                )
        except Exception:
            pass

    def cancel(self, job_id):
        job = self.store.get(job_id)
        if not job: raise KeyError(job_id)
        if job['capability'] in {'kraken.paper.director', 'kraken.paper.experiment'}:
            raise ValueError('Use research program controls; individual jobs have shared budgets and lineage')
        if job["status"] == "running": raise ValueError("running job is not interrupted by cancellation")
        approval_id = (job.get("payload") or {}).get("scheduled_approval_id")
        if approval_id:
            from .runtime import SocialRuntime
            SocialRuntime(data_dir=self.store.path.parent).store.cancel_scheduled_approval(approval_id)
        self.store.set_status(job_id, "cancelled"); return self.store.get(job_id)

    def _run_kraken_objective(self, job, adapter):
        stop = threading.Event()
        def progress(state):
            with self.store.db() as db:
                db.execute("UPDATE scheduled_jobs SET result=?,heartbeat=?,updated=? WHERE id=? AND status='running'",
                           (json.dumps(state),self.clock(),self.clock(),job['id']))
        def heartbeat():
            while not stop.wait(10):
                self.store.heartbeat({'running_objective':job['id']})
                with self.store.db() as db:
                    db.execute("UPDATE scheduled_jobs SET heartbeat=? WHERE id=? AND status='running'",(self.clock(),job['id']))
        worker=threading.Thread(target=heartbeat,daemon=True); worker.start()
        try:
            return adapter(job['payload'],progress=progress)
        finally:
            stop.set(); worker.join(2)

    def pause(self, job_id):
        return self._transition(job_id, 'paused')

    def resume(self, job_id):
        return self._transition(job_id, 'queued')

    def _transition(self, job_id, status, run_now=False):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,capability FROM scheduled_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None: raise KeyError(job_id)
            if row['capability'] in {'kraken.paper.director', 'kraken.paper.experiment'}:
                raise ValueError('Use research program controls; individual jobs have shared budgets and lineage')
            if row['status'] == 'running': raise ValueError('Running job must finish before changing its schedule')
            db.execute('UPDATE scheduled_jobs SET status=?, attempts=0, updated=? WHERE id=?', (status, self.clock(), job_id))
            if run_now:
                db.execute('UPDATE scheduled_jobs SET next_run=? WHERE id=?', (self.clock(), job_id))
        self.store.event(job_id, status)
        return self.store.get(job_id)

    def run_now(self, job_id):
        return self._transition(job_id, 'queued', run_now=True)

    def _claim_eligible_job(self, active_counts: dict[str, int], active_resources: set[str]):
        def _can_claim(candidate: dict) -> bool:
            kind = self.get_execution_kind(candidate["capability"])
            return active_counts.get(kind, 0) < self._kind_cap(kind)

        return self.store.claim_due(
            self.clock(),
            execution_seconds=self.execution_seconds,
            exclude_resources=active_resources,
            can_claim=_can_claim,
        )

    def dispatch_parallel_step(self, max_jobs: int | None = None) -> list[dict]:
        """Claim and execute up to bounded per-kind concurrency caps in parallel, respecting resource locks."""
        self._cleanup_stale_objectives()
        limit = self._max_total_workers() if max_jobs is None else max(1, int(max_jobs))
        claimed: list[dict] = []
        active_counts: dict[str, int] = {"code": 0, "agent": 0, "browser": 0}
        active_resources: set[str] = set()
        while len(claimed) < limit:
            job = self._claim_eligible_job(active_counts, active_resources)
            if not job:
                break
            kind = self.get_execution_kind(job["capability"])
            active_counts[kind] = active_counts.get(kind, 0) + 1
            active_resources.update(job.get("resources") or [])
            claimed.append(job)
        if not claimed:
            return []
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=len(claimed), thread_name_prefix="sabrina-sched") as executor:
            futures = [executor.submit(self._execute_claimed_job, job) for job in claimed]
            for fut in futures:
                results.append(fut.result())
        return results

    def run_forever(self, interval_seconds=15, stop=None):
        poll_interval = max(0.1, float(interval_seconds))
        in_flight: dict = {}
        with ThreadPoolExecutor(max_workers=self._max_total_workers(), thread_name_prefix="sabrina-sched") as executor:
            try:
                while not (stop and stop()):
                    try:
                        self.reload_registry_if_changed()
                    except Exception as exc:
                        logger.warning("Registry hot-reload failed: %s", exc)
                    done_futs = [f for f in in_flight if f.done()]
                    for f in done_futs:
                        in_flight.pop(f, None)
                        try:
                            f.result()
                        except Exception:
                            pass

                    active_counts: dict[str, int] = {"code": 0, "agent": 0, "browser": 0}
                    active_resources: set[str] = set()
                    for meta in in_flight.values():
                        k = meta["kind"]
                        active_counts[k] = active_counts.get(k, 0) + 1
                        active_resources.update(meta["resources"])

                    self.store.heartbeat({
                        "interval_seconds": interval_seconds,
                        "active_jobs": [m["id"] for m in in_flight.values()],
                        "active_counts": active_counts,
                    })
                    self._cleanup_stale_objectives()

                    while len(in_flight) < self._max_total_workers():
                        if stop and stop():
                            break
                        job = self._claim_eligible_job(active_counts, active_resources)
                        if not job:
                            break
                        kind = self.get_execution_kind(job["capability"])
                        resources = set(job.get("resources") or [])
                        active_counts[kind] = active_counts.get(kind, 0) + 1
                        active_resources.update(resources)
                        fut = executor.submit(self._execute_claimed_job, job)
                        in_flight[fut] = {"id": job["id"], "kind": kind, "resources": resources}

                    if stop and stop():
                        break
                    if in_flight:
                        wait(list(in_flight.keys()), timeout=min(1.0, poll_interval), return_when=FIRST_COMPLETED)
                    else:
                        deadline = time.monotonic() + poll_interval
                        while time.monotonic() < deadline:
                            if stop and stop():
                                break
                            time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))
            finally:
                for f in list(in_flight.keys()):
                    try:
                        f.result()
                    except Exception:
                        pass


def build_social_scheduler(runtime=None):
    from .runtime import SocialRuntime
    from tools.kraken_objectives import run_objective
    def run_scheduled(payload, progress=None):
        # A missing optional research dependency must not prevent social services starting.
        from tools.kraken_skill_runner import run_scheduled as execute
        return execute(payload, progress=progress)
    runtime = runtime or SocialRuntime()
    from .performance_report import linkedin_performance_report
    def approved_action(payload):
        return runtime.execute_scheduled_action(payload["scheduled_approval_id"])
    registry = CapabilityRegistry()
    def run_dual_orchestration(payload):
        payload = payload or {}
        plan = registry.plan_thought_and_implement(
            objective=str(payload.get("objective") or "Scheduled dual orchestration block"),
            mode=str(payload.get("mode") or "codex_thought_antigravity_implement"),
            payload=payload,
        )
        return {"status": "completed", **plan}
    def run_capability_handoff(capability_name: str):
        def _execute(payload):
            payload = payload or {}
            hid = registry.handoff(
                capability_name,
                str(payload.get("objective") or f"Scheduled {capability_name} block"),
                payload,
            )
            rsi_cycle = None
            try:
                rsi_cycle = runtime.run_read("rsi_reflect", {"rounds": int(payload.get("rounds", 1))})
            except Exception as exc:
                rsi_cycle = {"status": "skipped", "reason": str(exc)}
            return {"status": "completed", "handoff_id": hid, "capability": capability_name, "rsi_cycle": rsi_cycle}
        return _execute
    def run_frontier_research(capability_name: str):
        def _execute(payload):
            from tools.frontier_research_guild import run_scheduled_frontier_capability
            res = run_scheduled_frontier_capability(capability_name, payload or {})
            if capability_name == "frontier.research.rsi_augment":
                try:
                    res["rsi_cycle"] = runtime.run_read("rsi_reflect", {"rounds": 1})
                except Exception as exc:
                    res["rsi_cycle"] = {"status": "skipped", "reason": str(exc)}
            return res
        return _execute
    def with_browser_cleanup(fn):
        def _wrapped(payload):
            try:
                return fn(payload)
            finally:
                try:
                    runtime.disconnect_browser()
                except Exception:
                    pass
        return _wrapped
    scheduler = Scheduler(registry=registry, adapters={
        "kraken.inspect": run_objective,
        "kraken.objective": run_scheduled,
        "linkedin.browser": with_browser_cleanup(lambda payload: runtime.execute_live_action("engage_feed", payload)),
        "linkedin.replies": with_browser_cleanup(lambda payload: runtime.run_read("check_replies", payload)),
        "social.semantic_review": with_browser_cleanup(lambda payload: runtime.run_read("feed_suggestions", payload)),
        "linkedin.performance_report": lambda payload: linkedin_performance_report(runtime, payload),
        "social.cybersecurity_scout": with_browser_cleanup(lambda payload: runtime.run_read("cybersecurity_scout", payload)),
        "jobs.ai_eval_scout": with_browser_cleanup(lambda payload: runtime.run_read("ai_eval_scout", payload)),
        "linkedin.jobs_discover": with_browser_cleanup(lambda payload: runtime.run_read("jobs_discover", payload)),
        "linkedin.jobs_inspect": with_browser_cleanup(lambda payload: runtime.run_read("jobs_inspect", payload)),
        "linkedin.jobs_apply": with_browser_cleanup(lambda payload: runtime.run_read("jobs_apply", payload)),
        "linkedin.inbox_triage": with_browser_cleanup(lambda payload: runtime.run_read("inbox", payload)),
        "ai_services.health_and_recover": lambda payload: runtime.run_read("ai_services_health", payload),
        "rsi.universal_reflect": lambda payload: runtime.run_read("rsi_reflect", payload),
        "resources.internet_index_sweep": lambda payload: runtime.run_read("internet_resource_sweep", payload),
        "linkedin.publish_post": with_browser_cleanup(approved_action),
        "linkedin.reshare": with_browser_cleanup(approved_action),
        "x.feed": with_browser_cleanup(lambda payload: runtime.run_read("x.feed", payload)),
        "x.status": with_browser_cleanup(lambda payload: runtime.run_read("x.status", payload)),
        "x.analytics": with_browser_cleanup(lambda payload: runtime.run_read("x.analytics", payload)),
        "x.creator_pulse": with_browser_cleanup(lambda payload: runtime.run_read("x_creator_pulse", payload)),
        "x.publish": with_browser_cleanup(approved_action),
        "capabilities.dual_orchestration": run_dual_orchestration,
        "codex.astra.thought": run_capability_handoff("codex.astra.thought"),
        "codex.astra.implement": run_capability_handoff("codex.astra.implement"),
        "antigravity.thought": run_capability_handoff("antigravity.thought"),
        "antigravity.implement": run_capability_handoff("antigravity.implement"),
        "claude.thought": run_capability_handoff("claude.thought"),
        "frontier.research.rsi_augment": run_frontier_research("frontier.research.rsi_augment"),
        "frontier.research.wsp_thinking": run_frontier_research("frontier.research.wsp_thinking"),
        "frontier.research.hard_challenge": run_frontier_research("frontier.research.hard_challenge"),
        "frontier.research.synthesis": run_frontier_research("frontier.research.synthesis"),
        "frontier.research.blt_augmentation": run_frontier_research("frontier.research.blt_augmentation"),
        "compression.autonomous_experiment": lambda payload: runtime.run_read("compression_experiment", payload),
        "exploitgym.rsi_cycle": lambda payload: runtime.run_read("exploitgym_rsi", payload),
        "system.self_repair_workshop": lambda payload: runtime.run_read("self_repair_workshop", payload),
        "system.compute_federation": lambda payload: runtime.run_read("compute_federation", payload),
        "github.autonomous_repo_forge": lambda payload: runtime.run_read("github_repo_forge", payload),
        "forge.daily_paper_to_code": lambda payload: runtime.run_read("paper_tool_forge", payload),
        "bounty.cve_variant_hunter": lambda payload: runtime.run_read("cve_variant_hunter", payload),
    })
    from .health_report import health_report
    scheduler.adapters['system.health_report'] = lambda payload: health_report(scheduler.store)
    return scheduler


def main():
    parser = argparse.ArgumentParser(description="Sabrina internal capability scheduler")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=15)
    args = parser.parse_args()
    scheduler = build_social_scheduler()
    if args.loop:
        scheduler.run_forever(args.interval_seconds)
    else:
        print(json.dumps(scheduler.dispatch_once() or {"status": "idle"}, default=str))


if __name__ == "__main__":
    main()
