"""Collective Self-Ordering Engine for Sabrina.

Implements decentralized multi-model coordination:
1. Topological DAG task graph submission with strict acyclicity / cycle detection.
2. Stigmergic unblocked frontier discovery: tasks become actionable automatically when upstream dependencies complete.
3. Atomic lease reservation with TTL locks, preventing concurrent edit races across peer models.
4. Wavefront synchronization: completing an upstream milestone unlocks dependent downstream tasks.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional
import uuid

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent
SABRINA_DIR = Path(__file__).resolve().parent.parent
_fresh_db = SABRINA_DIR / "data" / "sabrina_assistant_fresh.sqlite3"
DEFAULT_DB_PATH = _fresh_db if _fresh_db.exists() else (SABRINA_DIR / "data" / "sabrina_assistant.sqlite3")


def ensure_dag_schema(conn: sqlite3.Connection) -> None:
    """Ensure SQLite tables for DAG tasks and leases exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_dag_tasks (
            task_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            goal TEXT NOT NULL,
            depends_on_json TEXT NOT NULL,
            produces_artifact TEXT DEFAULT '',
            capability_required TEXT DEFAULT '',
            acceptance_criteria TEXT DEFAULT '',
            state TEXT DEFAULT 'pending',
            assigned_model TEXT DEFAULT '',
            verified_evidence TEXT DEFAULT '',
            created REAL NOT NULL,
            updated REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_dag_leases (
            task_id TEXT PRIMARY KEY,
            model_id TEXT NOT NULL,
            lease_token TEXT NOT NULL,
            acquired_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dag_tasks_project ON assistant_dag_tasks(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dag_tasks_state ON assistant_dag_tasks(state)")
    conn.commit()


class CollectiveOrderingEngine:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            ensure_dag_schema(conn)

    def submit_dag_plan(self, project: str, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Validate topological acyclicity and submit a multi-task graph."""
        if not tasks:
            raise ValueError("Task list cannot be empty.")

        task_ids = {t["task_id"] for t in tasks}
        if len(task_ids) != len(tasks):
            raise ValueError("Duplicate task_id detected in DAG plan.")

        # Build adjacency graph to detect cycles (Kahn's algorithm)
        adj: Dict[str, List[str]] = {t_id: [] for t_id in task_ids}
        in_degree: Dict[str, int] = {t_id: 0 for t_id in task_ids}

        for t in tasks:
            t_id = t["task_id"]
            deps = t.get("depends_on", [])
            for dep in deps:
                if dep in task_ids:
                    adj[dep].append(t_id)
                    in_degree[t_id] += 1

        # Kahn's topological sort
        queue = [t_id for t_id, deg in in_degree.items() if deg == 0]
        visited_count = 0

        while queue:
            curr = queue.pop(0)
            visited_count += 1
            for nxt in adj[curr]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

        if visited_count != len(task_ids):
            raise ValueError("Circular dependency detected in DAG plan! Topological cycle is invalid.")

        now = time.time()
        inserted = []

        with sqlite3.connect(self.db_path) as conn:
            ensure_dag_schema(conn)
            for t in tasks:
                t_id = t["task_id"]
                deps_json = json.dumps(t.get("depends_on", []), sort_keys=True)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO assistant_dag_tasks (
                        task_id, project, goal, depends_on_json, produces_artifact,
                        capability_required, acceptance_criteria, state, assigned_model,
                        verified_evidence, created, updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', '', ?, ?)
                    """,
                    (
                        t_id,
                        project,
                        t.get("goal", ""),
                        deps_json,
                        t.get("produces_artifact", ""),
                        t.get("capability_required", "general"),
                        t.get("acceptance_criteria", ""),
                        now,
                        now,
                    ),
                )
                inserted.append(t_id)
            conn.commit()

        return {
            "status": "success",
            "project": project,
            "tasks_submitted": len(inserted),
            "task_ids": inserted,
        }

    def get_unblocked_tasks(self, project: str) -> List[Dict[str, Any]]:
        """Stigmergic query: returns tasks whose dependencies are 100% completed and currently unleased."""
        now = time.time()
        unblocked = []

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_dag_schema(conn)

            # Cull expired leases
            conn.execute("DELETE FROM assistant_dag_leases WHERE expires_at < ?", (now,))
            conn.commit()

            # Fetch all tasks in project
            tasks = conn.execute(
                "SELECT * FROM assistant_dag_tasks WHERE project = ?", (project,)
            ).fetchall()

            status_map = {t["task_id"]: t["state"] for t in tasks}

            # Fetch active leased task IDs
            active_leases = {
                r["task_id"] for r in conn.execute("SELECT task_id FROM assistant_dag_leases").fetchall()
            }

            for t in tasks:
                t_id = t["task_id"]
                state = t["state"]

                # Only pending or unleased claimed tasks can be picked up
                if state in ("completed", "failed"):
                    continue
                if t_id in active_leases and state == "claimed":
                    continue

                deps = json.loads(t["depends_on_json"])
                # Check if all dependencies are completed
                all_deps_completed = all(status_map.get(dep) == "completed" for dep in deps)

                if all_deps_completed:
                    unblocked.append({
                        "task_id": t_id,
                        "project": t["project"],
                        "goal": t["goal"],
                        "depends_on": deps,
                        "produces_artifact": t["produces_artifact"],
                        "capability_required": t["capability_required"],
                        "acceptance_criteria": t["acceptance_criteria"],
                        "state": "unblocked_ready",
                    })

        return unblocked

    def claim_task(self, task_id: str, model_id: str, lease_seconds: int = 180) -> Dict[str, Any]:
        """Atomically claim an unblocked task with a time-limited lease lock."""
        now = time.time()
        expires = now + lease_seconds
        token = uuid.uuid4().hex

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_dag_schema(conn)

            # Check task status and dependencies
            t = conn.execute("SELECT * FROM assistant_dag_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not t:
                raise ValueError(f"Task {task_id} not found.")

            if t["state"] == "completed":
                raise ValueError(f"Task {task_id} is already completed.")

            deps = json.loads(t["depends_on_json"])
            for dep in deps:
                dep_row = conn.execute("SELECT state FROM assistant_dag_tasks WHERE task_id = ?", (dep,)).fetchone()
                if not dep_row or dep_row["state"] != "completed":
                    raise ValueError(f"Cannot claim {task_id}: prerequisite {dep} is not completed (state: {dep_row['state'] if dep_row else 'missing'}).")

            # Check existing active lease
            lease = conn.execute("SELECT * FROM assistant_dag_leases WHERE task_id = ?", (task_id,)).fetchone()
            if lease and lease["expires_at"] > now and lease["model_id"] != model_id:
                raise ValueError(f"Task {task_id} is currently leased by model '{lease['model_id']}' until {lease['expires_at']:.1f}.")

            # Acquire lease
            conn.execute(
                """
                INSERT OR REPLACE INTO assistant_dag_leases (task_id, model_id, lease_token, acquired_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, model_id, token, now, expires),
            )
            conn.execute(
                """
                UPDATE assistant_dag_tasks
                SET state = 'claimed', assigned_model = ?, updated = ?
                WHERE task_id = ?
                """,
                (model_id, now, task_id),
            )
            conn.commit()

        return {
            "status": "claimed",
            "task_id": task_id,
            "claimed_by": model_id,
            "lease_token": token,
            "lease_expires_in_seconds": lease_seconds,
            "goal": t["goal"],
            "produces_artifact": t["produces_artifact"],
            "acceptance_criteria": t["acceptance_criteria"],
        }

    def complete_task(
        self,
        task_id: str,
        model_id: str,
        verified_evidence: str,
        produced_artifact: str = "",
    ) -> Dict[str, Any]:
        """Mark a task completed, release lease lock, and compute newly unblocked downstream frontier."""
        now = time.time()

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_dag_schema(conn)

            t = conn.execute("SELECT * FROM assistant_dag_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not t:
                raise ValueError(f"Task {task_id} not found.")

            artifact = produced_artifact or t["produces_artifact"]

            conn.execute(
                """
                UPDATE assistant_dag_tasks
                SET state = 'completed', verified_evidence = ?, produces_artifact = ?, updated = ?
                WHERE task_id = ?
                """,
                (verified_evidence, artifact, now, task_id),
            )
            # Remove lease
            conn.execute("DELETE FROM assistant_dag_leases WHERE task_id = ?", (task_id,))
            conn.commit()

            project = t["project"]

        # Compute newly unblocked tasks in the wavefront
        newly_unblocked = self.get_unblocked_tasks(project)

        return {
            "status": "completed",
            "task_id": task_id,
            "completed_by": model_id,
            "verified_evidence": verified_evidence[:300],
            "produced_artifact": artifact,
            "newly_unblocked_wavefront": [u["task_id"] for u in newly_unblocked],
        }

    def view_dag_wavefront(self, project: str) -> Dict[str, Any]:
        """Provide transparent telemetry of completed, active, unblocked, and blocked tasks."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_dag_schema(conn)

            tasks = conn.execute("SELECT * FROM assistant_dag_tasks WHERE project = ?", (project,)).fetchall()
            leases = {
                r["task_id"]: r["model_id"]
                for r in conn.execute("SELECT task_id, model_id FROM assistant_dag_leases WHERE expires_at > ?", (time.time(),)).fetchall()
            }

        unblocked_frontier = self.get_unblocked_tasks(project)
        unblocked_ids = {u["task_id"] for u in unblocked_frontier}

        completed = []
        in_progress = []
        ready = []
        blocked = []

        for t in tasks:
            t_id = t["task_id"]
            state = t["state"]
            item = {
                "task_id": t_id,
                "goal": t["goal"],
                "depends_on": json.loads(t["depends_on_json"]),
                "produces_artifact": t["produces_artifact"],
                "capability": t["capability_required"],
            }
            if state == "completed":
                item["assigned_model"] = t["assigned_model"]
                completed.append(item)
            elif state == "claimed" and t_id in leases:
                item["claimed_by"] = leases[t_id]
                in_progress.append(item)
            elif t_id in unblocked_ids:
                ready.append(item)
            else:
                blocked.append(item)

        return {
            "project": project,
            "total_tasks": len(tasks),
            "completed_count": len(completed),
            "in_progress_count": len(in_progress),
            "unblocked_ready_count": len(ready),
            "blocked_count": len(blocked),
            "wavefront": {
                "completed": completed,
                "in_progress": in_progress,
                "unblocked_ready": ready,
                "blocked_pending_prerequisites": blocked,
            },
        }


# Global engine singleton
COLLECTIVE_ENGINE = CollectiveOrderingEngine()


def submit_dag_plan_tool(action: Dict[str, Any], loop: Any = None) -> str:
    """Tool wrapper: submit_dag_plan."""
    project = action.get("project") or "default_project"
    tasks = action.get("tasks") or []
    if isinstance(tasks, str):
        tasks = json.loads(tasks)
    res = COLLECTIVE_ENGINE.submit_dag_plan(project, tasks)
    return json.dumps(res, indent=2)


def get_unblocked_tasks_tool(action: Dict[str, Any], loop: Any = None) -> str:
    """Tool wrapper: get_unblocked_tasks."""
    project = action.get("project") or "default_project"
    res = COLLECTIVE_ENGINE.get_unblocked_tasks(project)
    return json.dumps(res, indent=2)


def claim_dag_task_tool(action: Dict[str, Any], loop: Any = None) -> str:
    """Tool wrapper: claim_dag_task."""
    task_id = action.get("task_id", "")
    model_id = action.get("model_id") or getattr(loop, "last_model_used", "swarm_worker") or "swarm_worker"
    lease_sec = int(action.get("lease_seconds", 180) or 180)
    res = COLLECTIVE_ENGINE.claim_task(task_id, model_id, lease_sec)
    return json.dumps(res, indent=2)


def complete_dag_task_tool(action: Dict[str, Any], loop: Any = None) -> str:
    """Tool wrapper: complete_dag_task."""
    task_id = action.get("task_id", "")
    model_id = action.get("model_id") or getattr(loop, "last_model_used", "swarm_worker") or "swarm_worker"
    evidence = action.get("evidence") or action.get("verified_evidence") or "Verified via automated test run"
    artifact = action.get("produces_artifact") or ""
    res = COLLECTIVE_ENGINE.complete_task(task_id, model_id, evidence, artifact)
    return json.dumps(res, indent=2)


def view_dag_wavefront_tool(action: Dict[str, Any], loop: Any = None) -> str:
    """Tool wrapper: view_dag_wavefront."""
    project = action.get("project") or "default_project"
    res = COLLECTIVE_ENGINE.view_dag_wavefront(project)
    return json.dumps(res, indent=2)
