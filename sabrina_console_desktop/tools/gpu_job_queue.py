import os
import sqlite3
import json
import time
import uuid
import logging
import threading
from typing import Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [gpu_job_queue] %(message)s")
logger = logging.getLogger("gpu_job_queue")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "gpu_jobs.db"

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)


def init_db():
    """Initialize the SQLite database schema for the GPU Job Queue."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gpu_jobs (
                job_id TEXT PRIMARY KEY,
                topic TEXT,
                task_type TEXT,
                base_model TEXT,
                dataset TEXT,
                target_host TEXT,
                assigned_host TEXT,
                status TEXT,
                progress REAL,
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                metrics TEXT,
                artifact_paths TEXT,
                logs TEXT,
                error TEXT,
                job_spec TEXT
            )
        ''')
        if "research_key" not in {row[1] for row in cursor.execute("PRAGMA table_info(gpu_jobs)")}:
            cursor.execute("ALTER TABLE gpu_jobs ADD COLUMN research_key TEXT")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS gpu_jobs_research_key ON gpu_jobs(research_key)")
        conn.commit()


# Call init_db on module import
init_db()


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def queue_job(job_spec: Dict[str, Any]) -> Dict[str, Any]:
    """Queue a new model training/tuning job."""
    research_key = None
    if job_spec.get("workflow"):
        if job_spec['workflow'] == 'calibrated_wsp':
            from tools.coder_agent.calibrated_wsp import normalize_spec
        elif job_spec['workflow'] == 'jury_qlora_pilot':
            from tools.coder_agent.jury_qlora import normalize_spec
        else:
            from tools.coder_agent.research_pilot import normalize_spec
        job_spec = normalize_spec(job_spec)
        research_key = job_spec["experiment_id"]
        if job_spec.get("episode_id"):
            from tools.dream_rsi.research_blackboard import ResearchBlackboard
            if ResearchBlackboard().get_episode(job_spec["episode_id"]) is None:
                raise ValueError("Unknown research episode_id")
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    
    topic = job_spec.get("topic", "threat_triage")
    task_type = job_spec.get("task_type", "")
    base_model = job_spec.get("base_model", "")
    dataset = job_spec.get("dataset", "")
    target_host = job_spec.get("target_host", "auto")

    with _get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        if research_key:
            old = cursor.execute("SELECT job_id,status,job_spec FROM gpu_jobs WHERE research_key=?", (research_key,)).fetchone()
            if old:
                original = json.loads(old["job_spec"])
                original.pop("_retry_at", None)
                if original != job_spec:
                    raise ValueError("Experiment ID already has a different job specification")
                return {"job_id": old["job_id"], "status": old["status"], "deduplicated": True}
        cursor.execute('''
            INSERT INTO gpu_jobs (
                job_id, topic, task_type, base_model, dataset, target_host, 
                status, progress, created_at, job_spec, research_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            job_id, topic, task_type, base_model, dataset, target_host,
            'queued', 0.0, now, json.dumps(job_spec), research_key
        ))
        conn.commit()
    
    logger.info(f"Job {job_id} queued for {topic} (target_host: {target_host})")
    _research_event(job_spec, job_id, "queued")
    
    # Trigger scheduler to wake up if possible (handled in daemon loop)
    return {"job_id": job_id, "status": "queued", "message": "Job successfully queued"}


def _research_event(spec, job_id, state, evidence=None):
    if spec.get("workflow") and spec.get("episode_id"):
        from tools.dream_rsi.research_blackboard import ResearchBlackboard
        ResearchBlackboard().record_job_event(spec["episode_id"], job_id=job_id, state=state, evidence=evidence)


def get_job(job_id: str, *, include_live_progress: bool = False) -> Optional[Dict[str, Any]]:
    """Retrieve details for a specific job."""
    with _get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM gpu_jobs WHERE job_id = ?', (job_id,))
        row = cursor.fetchone()
        
    if row:
        job_dict = dict(row)
        for json_field in ['metrics', 'artifact_paths', 'job_spec']:
            if job_dict.get(json_field):
                try:
                    job_dict[json_field] = json.loads(job_dict[json_field])
                except json.JSONDecodeError:
                    pass
        if include_live_progress and job_dict["status"] == "running" and job_dict.get("job_spec", {}).get("workflow") in {"native_tool_pilot", "calibrated_wsp", "jury_qlora_pilot"}:
            try:
                from tools.coder_agent.research_status import remote_status
                job_dict["live_progress"] = remote_status(job_dict["job_spec"]["experiment_id"])
            except Exception as error:
                job_dict["live_progress_error"] = str(error)
        return job_dict
    return None


def list_jobs(status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all jobs, optionally filtering by status."""
    with _get_conn() as conn:
        cursor = conn.cursor()
        if status_filter:
            cursor.execute('SELECT * FROM gpu_jobs WHERE status = ? ORDER BY created_at DESC', (status_filter,))
        else:
            cursor.execute('SELECT * FROM gpu_jobs ORDER BY created_at DESC')
        rows = cursor.fetchall()
        
    jobs = []
    for row in rows:
        job_dict = dict(row)
        for json_field in ['metrics', 'artifact_paths', 'job_spec']:
            if job_dict.get(json_field):
                try:
                    job_dict[json_field] = json.loads(job_dict[json_field])
                except json.JSONDecodeError:
                    pass
        jobs.append(job_dict)
    return jobs


def cancel_job(job_id: str) -> Dict[str, Any]:
    """Cancel a pending or running job."""
    with _get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT status FROM gpu_jobs WHERE job_id = ?', (job_id,))
        row = cursor.fetchone()
        
        if not row:
            return {"job_id": job_id, "status": "error", "message": "Job not found"}
            
        current_status = row['status']
        if current_status in ['completed', 'failed', 'cancelled']:
            return {"job_id": job_id, "status": "error", "message": f"Cannot cancel job in state: {current_status}"}
            
        now = datetime.utcnow().isoformat()
        cursor.execute('UPDATE gpu_jobs SET status = ?, completed_at = ? WHERE job_id = ?', 
                       ('cancelled', now, job_id))
        conn.commit()
        
    logger.info(f"Job {job_id} cancelled.")
    return {"job_id": job_id, "status": "cancelled", "message": "Job successfully cancelled"}


def get_hosts_telemetry() -> Dict[str, Any]:
    """Return telemetry for available GPU hosts."""
    # Count active jobs per host
    active_local = 0
    active_remote = 0
    with _get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT assigned_host, COUNT(*) as count FROM gpu_jobs WHERE status = 'running' GROUP BY assigned_host")
        for row in cursor.fetchall():
            if row['assigned_host'] == 'local':
                active_local = row['count']
            elif row['assigned_host'] == 'remote_gpu':
                active_remote = row['count']
                
    from distributed_node_orchestrator import DistributedNodeOrchestrator
    try:
        caps = DistributedNodeOrchestrator.detect_capabilities()
        local_name = f"Local Node ({caps.os_type} - {caps.vram_gb}GB VRAM)"
        local_max = 1 if caps.has_gpu else 0
    except Exception:
        local_name = "Local Workstation (Unknown)"
        local_max = 1
        
    return {
        "remote_gpu": {
            "name": "Lab Host (RTX 5060 Ti)",
            "active_jobs": active_remote,
            "max_jobs": 1,
            "status": "busy" if active_remote >= 1 else "idle"
        },
        "local_gpu": {
            "name": local_name,
            "active_jobs": active_local,
            "max_jobs": local_max,
            "status": "busy" if active_local >= 1 else "idle"
        }
    }


class GPUQueueScheduler:
    """Background daemon to dispatch queued jobs."""
    def __init__(self):
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
    
    def start(self):
        with self._lock:
            if not self.running:
                self.running = True
                self.thread = threading.Thread(target=self._loop, daemon=True, name="GPUQueueScheduler")
                self.thread.start()
                logger.info("GPUQueueScheduler started.")
                
    def stop(self):
        with self._lock:
            self.running = False
            
    def _loop(self):
        while self.running:
            try:
                self._process_next_job()
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
            time.sleep(5)
            
    def _process_next_job(self):
        telemetry = get_hosts_telemetry()
        local_free = telemetry["local_gpu"]["active_jobs"] < telemetry["local_gpu"]["max_jobs"]
        remote_free = telemetry["remote_gpu"]["active_jobs"] < telemetry["remote_gpu"]["max_jobs"]
        
        if not local_free and not remote_free:
            return # No hosts available
            
        with _get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT job_id, job_spec, target_host FROM gpu_jobs WHERE status = 'queued' ORDER BY created_at ASC")
            jobs = cursor.fetchall()
            
        if not jobs:
            return # No queued jobs
            
        has_local_cuda = None
            
        for job_row in jobs:
            job_id = job_row['job_id']
            job_spec = json.loads(job_row['job_spec'])
            if float(job_spec.get("_retry_at", 0)) > time.time():
                continue
            requested_target = job_row['target_host']
            task_type = job_spec.get('task_type', '')
            
            assigned_host = None
            if requested_target == 'auto':
                if has_local_cuda is None:
                    from tools.model_tuning_engine import is_local_cuda_available
                    has_local_cuda = is_local_cuda_available()
                if task_type == 'causal_lm' or not has_local_cuda:
                    if remote_free:
                        assigned_host = 'remote_gpu'
                else:
                    if local_free:
                        assigned_host = 'local'
                    elif remote_free:
                        assigned_host = 'remote_gpu'
            elif requested_target == 'remote_gpu' and remote_free:
                assigned_host = 'remote_gpu'
            elif requested_target == 'local' and local_free:
                assigned_host = 'local'
                
            if assigned_host:
                self._dispatch_job(job_id, job_spec, assigned_host)
                break # Process one job per cycle
                
    def _dispatch_job(self, job_id: str, job_spec: Dict[str, Any], assigned_host: str, *, background=True):
        logger.info(f"Dispatching job {job_id} to {assigned_host}")
        now = datetime.utcnow().isoformat()
        with _get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE gpu_jobs 
                SET status = 'running', assigned_host = ?, started_at = ? 
                WHERE job_id = ? AND status = 'queued'
            ''', (assigned_host, now, job_id))
            if cursor.rowcount == 0:
                logger.warning(f"Job {job_id} could not be marked running (maybe cancelled?)")
                return
            conn.commit()
            
        # Run in a separate thread so scheduler can continue monitoring/dispatching
        if background:
            threading.Thread(target=self._run_job_worker, args=(job_id, job_spec, assigned_host), daemon=True).start()
        else:
            self._run_job_worker(job_id, job_spec, assigned_host)
        
    def _run_job_worker(self, job_id: str, job_spec: Dict[str, Any], assigned_host: str):
        if job_spec.get("workflow"):
            self._run_research_worker(job_id, job_spec)
            return
        from tools.model_tuning_engine import tune_model
        
        # Override target host in spec based on assignment
        job_spec['target_host'] = assigned_host
        
        try:
            # We call the existing synchronous tune_model which handles SSH/local dispatch
            result_str = tune_model(job_spec)
            if 'Status: DEFERRED' in result_str:
                job_spec['_retry_at'] = time.time() + 60
                with _get_conn() as conn:
                    conn.execute("UPDATE gpu_jobs SET status='queued',assigned_host=NULL,job_spec=?,logs=? WHERE job_id=? AND status='running'",
                                 (json.dumps(job_spec), result_str, job_id))
                return
            
            # Parse the string result to extract structured data (or ideally tune_model should return dict, 
            # but it returns a formatted string as per its current implementation. 
            # Let's extract some basic info, or just store the full string as log.)
            status = 'completed'
            error_msg = None
            if "[Model Tuning Result]" in result_str:
                if "Status: ERROR" in result_str:
                    status = 'failed'
                    # Extract error line if possible
                    for line in result_str.splitlines():
                        if line.startswith("- Error Details:"):
                            error_msg = line.replace("- Error Details:", "").strip()
                            break
            
            now = datetime.utcnow().isoformat()
            with _get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE gpu_jobs 
                    SET status = ?, completed_at = ?, progress = 100.0, logs = ?, error = ?
                    WHERE job_id = ? AND status = 'running'
                ''', (status, now, result_str, error_msg, job_id))
                conn.commit()

            logger.info(f"Job {job_id} finished with status: {status}")
        except Exception as e:
            logger.error(f"Job {job_id} worker crashed: {e}", exc_info=True)
            now = datetime.utcnow().isoformat()
            with _get_conn() as conn:
                conn.execute("UPDATE gpu_jobs SET status='failed',completed_at=?,error=? WHERE job_id=? AND status='running'",
                             (now, str(e), job_id))

    def _run_research_worker(self, job_id, spec):
        """Use the existing queue's lifecycle; a busy GPU is deferred, not success."""
        if spec.get('workflow') == 'calibrated_wsp':
            from tools.coder_agent.calibrated_wsp import run_remote
        elif spec.get('workflow') == 'jury_qlora_pilot':
            from tools.coder_agent.jury_qlora import run_remote
        else:
            from tools.coder_agent.research_pilot import run_remote
        try:
            _research_event(spec, job_id, "running")
            result = run_remote(spec)
            if result.get("status") not in {"completed", "deferred"}:
                raise RuntimeError("Research worker returned no terminal/deferred status")
            deferred = result["status"] == "deferred"
            state = "queued" if deferred else "completed"
            next_spec = dict(spec)
            if deferred:
                next_spec["_retry_at"] = time.time() + max(30, min(3600, int(result.get("retry_after_seconds", 300))))
            else:
                next_spec.pop("_retry_at", None)
            with _get_conn() as conn:
                changed = conn.execute("""UPDATE gpu_jobs SET status=?,assigned_host=?,completed_at=?,progress=?,
                    metrics=?,artifact_paths=?,logs=?,error=NULL,job_spec=? WHERE job_id=? AND status='running'""",
                    (state, None if deferred else "remote_gpu", None if deferred else datetime.utcnow().isoformat(),
                     0 if deferred else 100, json.dumps(result.get("metrics", {})),
                     json.dumps(result.get("artifact_paths", {})), json.dumps(result), json.dumps(next_spec), job_id)).rowcount
            if changed:
                _research_event(spec, job_id, result["status"], result)
        except Exception as error:
            with _get_conn() as conn:
                changed = conn.execute("UPDATE gpu_jobs SET status='failed',completed_at=?,error=? WHERE job_id=? AND status='running'",
                    (datetime.utcnow().isoformat(), str(error), job_id)).rowcount
            if changed:
                _research_event(spec, job_id, "failed", {"error": str(error)})
                
# Global instance
scheduler = GPUQueueScheduler()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", help="Run exactly one queued job through the existing queue lifecycle")
    arguments = parser.parse_args()
    if arguments.job_id:
        job = get_job(arguments.job_id)
        if not job or job["status"] != "queued" or float(job["job_spec"].get("_retry_at", 0)) > time.time():
            raise SystemExit("Job is not queued/eligible")
        target = job["target_host"]
        if target not in {"local", "remote_gpu"}:
            raise SystemExit("One-job dispatch requires an explicit target_host")
        scheduler._dispatch_job(job["job_id"], job["job_spec"], target, background=False)
        raise SystemExit(0 if get_job(job["job_id"])["status"] in {"completed", "queued"} else 1)
    # Test script if run directly
    print("Starting GPU Queue Scheduler...")
    scheduler.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.stop()
        print("Stopped.")
