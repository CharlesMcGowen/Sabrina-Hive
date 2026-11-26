"""Thread-safe SQLite Telemetry Blackboard indexed by agent_id.

Stores live telemetry for each swarm node in dnd_rsi_blackboard.sqlite3,
strictly isolated from operator chat history.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DND_DB_PATH = ROOT_DIR / "data" / "dnd_rsi_blackboard.sqlite3"


def get_blackboard_db_path(explicit_path: Path | str | None = None) -> Path:
    if explicit_path is not None:
        return Path(explicit_path)
    env_p = os.environ.get("SABRINA_DND_BLACKBOARD_DB")
    if env_p:
        return Path(env_p)
    return DEFAULT_DND_DB_PATH


class TelemetryBlackboard:
    """Manages SQLite persistence of live agent telemetry in dnd_rsi_blackboard.sqlite3."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = get_blackboard_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_telemetry (
                    agent_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    model TEXT,
                    logprob REAL,
                    gate_status TEXT,
                    task TEXT,
                    command TEXT,
                    parent_id TEXT,
                    depth INTEGER,
                    action_trail_json TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_telemetry_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    task TEXT,
                    command TEXT,
                    logprob REAL,
                    gate_status TEXT,
                    timestamp REAL NOT NULL,
                    time_str TEXT NOT NULL,
                    detail TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_agent_telemetry_events_agent_time
                    ON agent_telemetry_events(agent_id, timestamp DESC);

                CREATE TABLE IF NOT EXISTS swarm_transition_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    rule_id TEXT,
                    from_role TEXT NOT NULL,
                    to_role TEXT NOT NULL,
                    condition_trigger TEXT,
                    evaluated_result TEXT,
                    token_spend_est INTEGER DEFAULT 0,
                    latency_ms REAL DEFAULT 0.0,
                    critic_rubric_json TEXT,
                    timestamp REAL NOT NULL,
                    created_at_iso TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_swarm_transition_traces_mission
                    ON swarm_transition_traces(mission_id, timestamp ASC);
            """)

    def record_transition_trace(
        self,
        mission_id: str,
        from_role: str,
        to_role: str,
        rule_id: str = "",
        condition_trigger: str = "",
        evaluated_result: str = "TRUE",
        token_spend_est: int = 0,
        latency_ms: float = 0.0,
        critic_rubric: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        """Record an explicit A2A transition trace or spawning evaluation."""
        ts = float(timestamp if timestamp is not None else time.time())
        iso_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        rubric_json = json.dumps(critic_rubric, ensure_ascii=False) if critic_rubric else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO swarm_transition_traces (
                    mission_id, rule_id, from_role, to_role,
                    condition_trigger, evaluated_result, token_spend_est,
                    latency_ms, critic_rubric_json, timestamp, created_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(mission_id),
                    str(rule_id),
                    str(from_role),
                    str(to_role),
                    str(condition_trigger),
                    str(evaluated_result),
                    int(token_spend_est),
                    float(latency_ms),
                    rubric_json,
                    ts,
                    iso_str,
                ),
            )

        return {
            "mission_id": mission_id,
            "rule_id": rule_id,
            "from_role": from_role,
            "to_role": to_role,
            "condition_trigger": condition_trigger,
            "evaluated_result": evaluated_result,
            "token_spend_est": token_spend_est,
            "latency_ms": latency_ms,
            "critic_rubric": critic_rubric,
            "timestamp": ts,
        }

    def get_mission_transition_traces(self, mission_id: str) -> list[dict[str, Any]]:
        """Retrieve chronological transition traces for a mission."""
        clean_mid = str(mission_id or "").strip()
        if not clean_mid:
            return []
        traces = []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM swarm_transition_traces WHERE mission_id = ? ORDER BY timestamp ASC, id ASC",
                    (clean_mid,),
                ).fetchall()
                for r in rows:
                    rubric = None
                    if r["critic_rubric_json"]:
                        try:
                            rubric = json.loads(r["critic_rubric_json"])
                        except Exception:
                            rubric = None
                    traces.append({
                        "id": r["id"],
                        "mission_id": r["mission_id"],
                        "rule_id": r["rule_id"],
                        "from_role": r["from_role"],
                        "to_role": r["to_role"],
                        "condition_trigger": r["condition_trigger"],
                        "evaluated_result": r["evaluated_result"],
                        "token_spend_est": r["token_spend_est"],
                        "latency_ms": r["latency_ms"],
                        "critic_rubric": rubric,
                        "timestamp": r["timestamp"],
                        "created_at_iso": r["created_at_iso"],
                    })
        except Exception:
            pass
        return traces

    def record_telemetry(
        self,
        agent_id: str,
        state: str,
        mode: str,
        model: str = "",
        logprob: float | None = None,
        gate_status: str = "PENDING",
        task: str = "",
        command: str = "",
        parent_id: str | None = None,
        depth: int = 1,
        action: str = "",
        detail: str = "",
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        """Record/update telemetry for an agent, updating its latest snapshot and appending to its action trail."""
        clean_id = str(agent_id or "").strip()
        if not clean_id:
            raise ValueError("agent_id must be a non-empty string.")

        clean_state = str(state or "ACTIVE").upper().strip()
        clean_mode = str(mode or "DYNAMIC").upper().strip()
        clean_task = str(task or "").strip()
        clean_command = str(command or "").strip()
        clean_gate = str(gate_status or "PASS").upper().strip()
        clean_model = str(model or "").strip()
        ts = float(timestamp if timestamp is not None else time.time())
        time_str = time.strftime("%H:%M:%S", time.localtime(ts))

        action_summary = action or f"Transitioned to {clean_state}"
        if clean_command:
            action_summary = f"{action_summary} | cmd: {clean_command}"

        with self._connect() as conn:
            # Fetch existing action trail
            row = conn.execute(
                "SELECT action_trail_json FROM agent_telemetry WHERE agent_id = ?",
                (clean_id,),
            ).fetchone()

            trail: list[dict[str, Any]] = []
            if row and row["action_trail_json"]:
                try:
                    trail = json.loads(row["action_trail_json"])
                    if not isinstance(trail, list):
                        trail = []
                except Exception:
                    trail = []

            # Append new action trail item
            trail_entry = {
                "timestamp": ts,
                "time_str": time_str,
                "state": clean_state,
                "action": action_summary,
                "command": clean_command,
                "task": clean_task,
                "gate_status": clean_gate,
                "logprob": logprob,
            }
            trail.append(trail_entry)
            if len(trail) > 50:
                trail = trail[-50:]

            trail_json = json.dumps(trail, ensure_ascii=False)

            conn.execute(
                """
                INSERT INTO agent_telemetry (
                    agent_id, state, mode, model, logprob, gate_status,
                    task, command, parent_id, depth, action_trail_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    state = excluded.state,
                    mode = excluded.mode,
                    model = CASE WHEN excluded.model != '' THEN excluded.model ELSE agent_telemetry.model END,
                    logprob = CASE WHEN excluded.logprob IS NOT NULL THEN excluded.logprob ELSE agent_telemetry.logprob END,
                    gate_status = excluded.gate_status,
                    task = excluded.task,
                    command = excluded.command,
                    parent_id = excluded.parent_id,
                    depth = excluded.depth,
                    action_trail_json = excluded.action_trail_json,
                    updated_at = excluded.updated_at
                """,
                (
                    clean_id,
                    clean_state,
                    clean_mode,
                    clean_model,
                    logprob,
                    clean_gate,
                    clean_task,
                    clean_command,
                    parent_id,
                    depth,
                    trail_json,
                    ts,
                ),
            )

            conn.execute(
                """
                INSERT INTO agent_telemetry_events (
                    agent_id, state, mode, task, command, logprob, gate_status,
                    timestamp, time_str, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_id,
                    clean_state,
                    clean_mode,
                    clean_task,
                    clean_command,
                    logprob,
                    clean_gate,
                    ts,
                    time_str,
                    detail or action_summary,
                ),
            )

        return {
            "agent_id": clean_id,
            "state": clean_state,
            "mode": clean_mode,
            "model": clean_model,
            "logprob": logprob,
            "gate_status": clean_gate,
            "task": clean_task,
            "command": clean_command,
            "parent_id": parent_id,
            "depth": depth,
            "action_trail": trail,
            "updated_at": ts,
        }

    def get_latest_telemetry_by_agent(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of agent_id -> latest telemetry payload, isolated from operator chat history."""
        result: dict[str, dict[str, Any]] = {}
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM agent_telemetry ORDER BY updated_at DESC").fetchall()
                for r in rows:
                    trail = []
                    if r["action_trail_json"]:
                        try:
                            trail = json.loads(r["action_trail_json"])
                        except Exception:
                            trail = []
                    aid = str(r["agent_id"])
                    result[aid] = {
                        "agent_id": aid,
                        "state": str(r["state"]),
                        "mode": str(r["mode"]),
                        "model": str(r["model"] or ""),
                        "logprob": r["logprob"],
                        "gate_status": str(r["gate_status"] or "PASS"),
                        "task": str(r["task"] or ""),
                        "command": str(r["command"] or ""),
                        "parent_id": r["parent_id"],
                        "depth": int(r["depth"] or 0),
                        "action_trail": trail,
                        "updated_at": float(r["updated_at"] or 0.0),
                    }
        except Exception:
            pass
        return result

    def get_agent_record(self, agent_id: str) -> dict[str, Any] | None:
        """Fetch single agent telemetry record."""
        clean_id = str(agent_id or "").strip()
        if not clean_id:
            return None
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM agent_telemetry WHERE agent_id = ?", (clean_id,)).fetchone()
            if not r:
                return None
            trail = []
            if r["action_trail_json"]:
                try:
                    trail = json.loads(r["action_trail_json"])
                except Exception:
                    trail = []
            return {
                "agent_id": str(r["agent_id"]),
                "state": str(r["state"]),
                "mode": str(r["mode"]),
                "model": str(r["model"] or ""),
                "logprob": r["logprob"],
                "gate_status": str(r["gate_status"] or "PASS"),
                "task": str(r["task"] or ""),
                "command": str(r["command"] or ""),
                "parent_id": r["parent_id"],
                "depth": int(r["depth"] or 0),
                "action_trail": trail,
                "updated_at": float(r["updated_at"] or 0.0),
            }


_GLOBAL_TELEMETRY_BLACKBOARD: TelemetryBlackboard | None = None


def get_telemetry_blackboard(db_path: Path | str | None = None) -> TelemetryBlackboard:
    global _GLOBAL_TELEMETRY_BLACKBOARD
    if _GLOBAL_TELEMETRY_BLACKBOARD is None or db_path is not None:
        _GLOBAL_TELEMETRY_BLACKBOARD = TelemetryBlackboard(db_path)
    return _GLOBAL_TELEMETRY_BLACKBOARD
