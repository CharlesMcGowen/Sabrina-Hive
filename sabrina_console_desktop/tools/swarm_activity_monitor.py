"""
Real-time Swarm Activity & Review Monitor Bridge for Sabrina Operator Console.
Ensures model thoughts, code reviews, diagnostics, and test executions reflect
accurately across all Console monitors (Swarm Control, Decision Trace, Activity Feed).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(DESKTOP_ROOT) not in sys.path:
    sys.path.insert(0, str(DESKTOP_ROOT))

try:
    from tools.sabrina_console_swarm import _assistant_db_path, _capabilities_db_path
except ImportError:
    def _assistant_db_path() -> Path:
        fresh = DESKTOP_ROOT / "data" / "sabrina_assistant_fresh.sqlite3"
        return fresh if fresh.exists() else DESKTOP_ROOT / "data" / "sabrina_assistant.sqlite3"
    def _capabilities_db_path() -> Path:
        return DESKTOP_ROOT / "data" / "control" / "capabilities.sqlite3"

try:
    from tools.telemetry_blackboard import get_telemetry_blackboard
except ImportError:
    try:
        from sabrina_console_desktop.tools.telemetry_blackboard import get_telemetry_blackboard
    except ImportError:
        get_telemetry_blackboard = None


def ensure_assistant_sessions_table(db_path: Optional[Path] = None) -> Path:
    """Ensures `assistant_sessions` exists with all columns expected by Sabrina Operator Console."""
    target_path = Path(db_path) if db_path else _assistant_db_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=5)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS assistant_sessions (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'ready',
                revision INTEGER NOT NULL DEFAULT 0,
                messages TEXT NOT NULL DEFAULT '[]',
                refs TEXT NOT NULL DEFAULT '[]',
                progress TEXT NOT NULL DEFAULT '',
                answer TEXT NOT NULL DEFAULT '',
                owner TEXT,
                lease_until REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                requested_device TEXT NOT NULL DEFAULT '',
                activity TEXT NOT NULL DEFAULT '',
                verified_result TEXT NOT NULL DEFAULT '',
                verification_evidence TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL DEFAULT 0,
                desktop_audit_retention_days INTEGER NOT NULL DEFAULT 0,
                desktop_audit_expires REAL NOT NULL DEFAULT 0
            )
        """)
        # Ensure assistant_cross_app_handoffs exists for node cards
        conn.execute("""
            CREATE TABLE IF NOT EXISTS assistant_cross_app_handoffs (
                handoff_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                app TEXT NOT NULL,
                status TEXT NOT NULL,
                conversation_key TEXT NOT NULL DEFAULT 'main',
                prompt TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                error_detail TEXT NOT NULL DEFAULT '',
                started REAL NOT NULL,
                completed REAL,
                desktop_call_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    finally:
        conn.close()
    return target_path


class SwarmActivityMonitor:
    """Singleton/service that records live model turns, reviews, and test activities."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = ensure_assistant_sessions_table(db_path)
        self.telemetry_bb = get_telemetry_blackboard() if get_telemetry_blackboard else None

    def record_model_activity(
        self,
        model: str,
        activity: str,
        role: str = "specialist",
        state: str = "running",
        progress: str = "",
        task: str = "Swarm Mission Execution",
        run_id: Optional[str] = None,
        app: Optional[str] = None,
        objective_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Record a fine-grained activity heartbeat for an active model.
        Updates `assistant_sessions`, `workflow_events`, and `telemetry_blackboard`.
        """
        now = time.time()
        eff_run_id = str(run_id or f"run_{role}_{int(now)}")
        eff_model = str(model or "unknown-model").strip()
        eff_activity = str(activity or "").strip()
        eff_state = str(state or "running").lower().strip()
        eff_progress = str(progress or "").strip()
        eff_app = str(app or "").strip()

        conn = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            # 1. Upsert into assistant_sessions
            conn.execute("""
                INSERT INTO assistant_sessions (
                    id, task, context, state, revision, messages, refs, progress,
                    answer, owner, lease_until, updated, model, requested_device,
                    activity, verified_result, verification_evidence, created,
                    desktop_audit_retention_days, desktop_audit_expires
                ) VALUES (?, ?, '', ?, 1, '[]', '[]', ?, '', ?, ?, ?, ?, 'auto', ?, '', '', ?, 7, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task=excluded.task,
                    state=excluded.state,
                    progress=excluded.progress,
                    activity=excluded.activity,
                    model=excluded.model,
                    updated=excluded.updated
            """, (
                eff_run_id,
                task,
                eff_state,
                eff_progress,
                role,
                now + 300,
                now,
                eff_model,
                eff_activity,
                now,
                now + (7 * 86400)
            ))

            # 2. Record cross-app handoff if an app node is specified (Codex, Antigravity, Claude)
            if eff_app:
                handoff_id = f"handoff_{eff_app.lower()}_{eff_run_id[:8]}"
                conn.execute("""
                    INSERT INTO assistant_cross_app_handoffs (
                        handoff_id, run_id, app, status, conversation_key, prompt,
                        summary, error_detail, started, completed, desktop_call_count
                    ) VALUES (?, ?, ?, ?, 'main', ?, ?, '', ?, NULL, 1)
                    ON CONFLICT(handoff_id) DO UPDATE SET
                        status=excluded.status,
                        summary=excluded.summary,
                        completed=(CASE WHEN excluded.status='completed' THEN ? ELSE completed END)
                """, (
                    handoff_id,
                    eff_run_id,
                    eff_app,
                    eff_state,
                    eff_activity,
                    eff_activity,
                    now,
                    now
                ))

            # 3. If objective_id is provided, record a workflow event
            if objective_id:
                try:
                    conn.execute("""
                        INSERT INTO workflow_events (objective_id, kind, detail, created)
                        VALUES (?, 'model_review', ?, ?)
                    """, (
                        objective_id,
                        json.dumps({
                            "model": eff_model,
                            "role": role,
                            "activity": eff_activity,
                            "state": eff_state,
                            "progress": eff_progress,
                            **(detail or {})
                        }),
                        now
                    ))
                except Exception:
                    pass

            conn.commit()
        finally:
            conn.close()

        # 4. Sync into Telemetry Blackboard
        if self.telemetry_bb:
            try:
                self.telemetry_bb.record_telemetry(
                    agent_id=role,
                    state=eff_state.upper(),
                    mode="SUPERVISED",
                    model=eff_model,
                    task=task,
                    command=eff_progress,
                    action=eff_activity,
                    detail=json.dumps(detail or {}),
                    timestamp=now
                )
            except Exception:
                pass

        return {
            "ok": True,
            "run_id": eff_run_id,
            "model": eff_model,
            "role": role,
            "activity": eff_activity,
            "updated": now
        }

    @contextlib.contextmanager
    def model_review_step(
        self,
        model: str,
        role: str,
        step_title: str,
        task: str = "Swarm Mission Execution",
        run_id: Optional[str] = None,
        app: Optional[str] = None,
        objective_id: Optional[str] = None,
    ):
        """Context manager for automatically logging step start, duration, and completion."""
        t0 = time.monotonic()
        self.record_model_activity(
            model=model,
            role=role,
            state="running",
            activity=f"Starting: {step_title}",
            task=task,
            run_id=run_id,
            app=app,
            objective_id=objective_id,
        )
        try:
            yield
            elapsed = round(time.monotonic() - t0, 2)
            self.record_model_activity(
                model=model,
                role=role,
                state="completed",
                activity=f"Completed in {elapsed}s: {step_title}",
                task=task,
                run_id=run_id,
                app=app,
                objective_id=objective_id,
                detail={"elapsed_sec": elapsed, "status": "PASS"}
            )
        except Exception as exc:
            elapsed = round(time.monotonic() - t0, 2)
            self.record_model_activity(
                model=model,
                role=role,
                state="failed",
                activity=f"Failed in {elapsed}s: {step_title} ({exc})",
                task=task,
                run_id=run_id,
                app=app,
                objective_id=objective_id,
                detail={"elapsed_sec": elapsed, "error": str(exc), "status": "FAIL"}
            )
            raise


# Global singleton helper
_GLOBAL_MONITOR: Optional[SwarmActivityMonitor] = None

def get_activity_monitor() -> SwarmActivityMonitor:
    global _GLOBAL_MONITOR
    if _GLOBAL_MONITOR is None:
        _GLOBAL_MONITOR = SwarmActivityMonitor()
    return _GLOBAL_MONITOR

def record_activity(
    model: str,
    activity: str,
    role: str = "specialist",
    state: str = "running",
    progress: str = "",
    task: str = "Swarm Mission Execution",
    run_id: Optional[str] = None,
    app: Optional[str] = None,
    objective_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convenience helper to record live model activity from any module or script."""
    return get_activity_monitor().record_model_activity(
        model=model,
        activity=activity,
        role=role,
        state=state,
        progress=progress,
        task=task,
        run_id=run_id,
        app=app,
        objective_id=objective_id,
        detail=detail
    )
